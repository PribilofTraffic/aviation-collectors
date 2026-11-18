#!/usr/bin/env python3
"""
Flight Patterns Collector

Polls the aircraft-patterns container on the St. George ADS-B Pi and merges its
detections (circles, racetracks, survey grids) into an append-only archive at
data/flight_patterns.json.

The container stores its own history in CSVs inside the container; recreating it
without the volume erases them. This archive is the durable copy and is what the
public page reads, so the page still works when the receiver is unreachable.

Since 2026-08-21 the archive also carries go-arounds and holds found by
approach_events.py in our own data/adsb_history.db -- the container never
detected a single missed approach or holding pattern in its life, and those
are the events an islander at the terminal actually cares about. Each run
re-scans today and yesterday; a hold still in progress grows on each pass
(same key, newer record wins). These events are archived even when the
container is unreachable.

The container's clock is Alaska local and its timestamps carry no offset;
alaska_iso() stamps one so the page never has to guess the zone.
"""

import os
import time

import settings
settings.apply_timezone()

import json
import math
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import approach_events

SOURCE_URL = settings.PATTERNS_URL    # aircraft-patterns container; optional
REQUEST_TIMEOUT = 10
DATA_DIR = settings.DATA_DIR
OUTPUT_FILE = f'{DATA_DIR}/flight_patterns.json'
ALASKA = settings.TZ      # the site's local zone (the name is historical)

# Airport reference points, used for "13 NM S of St. Paul" style bearings.
ISLANDS = tuple(
    ((a.get('place') or {}).get('name') or settings.short_name(a['icao']),
     (a.get('place') or {}).get('lat', a['lat']),
     (a.get('place') or {}).get('lon', a['lon']))
    for a in settings.AIRPORTS)

EARTH_RADIUS_NM = 3440.065


def great_circle(lat1, lon1, lat2, lon2):
    """Return (distance_nm, bearing_deg) from point 1 to point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    distance = 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))

    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360

    return distance, bearing


def nearest_island(lat, lon):
    """Return (island_name, distance_nm, bearing_deg) for the closer of PASN/PAPB."""
    best = None
    for name, ilat, ilon in ISLANDS:
        distance, bearing = great_circle(ilat, ilon, lat, lon)
        if best is None or distance < best[1]:
            best = (name, distance, bearing)
    return best[0], round(best[1], 1), int(round(best[2]))


def rewrite_radar_url(hex_id, lat, lon):
    """Container URLs point at the docker-internal host 'ultrafeeder', which no
    visitor can resolve. Rewrite to the site's own tar1090 proxy.

    hex_id lands unescaped in an href on the public page, so it must be
    validated here rather than trusted from the container's JSON: an ICAO
    address is exactly 6 hex characters."""
    if not re.fullmatch(r'[0-9a-fA-F]{6}', str(hex_id or '')):
        raise ValueError(f'refusing to build a radar URL from a malformed hex_id: {hex_id!r}')
    return f'/papb1090/?icao={hex_id}&lat={lat:.4f}&lon={lon:.4f}&zoom=13'


def alaska_iso(value):
    """Stamp a naive ISO timestamp as Alaska local; leave aware ones alone."""
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ALASKA)
    return parsed.isoformat()


def _duration_minutes(detected_at, last_seen):
    if not detected_at or not last_seen:
        return 0
    try:
        start = datetime.fromisoformat(detected_at)
        end = datetime.fromisoformat(last_seen)
    except ValueError:
        return 0
    return max(0, int((end - start).total_seconds() // 60))


def _as_number(value, kind=float):
    """Coerce to `kind` (int or float), or None if not numeric.

    coverage_area, num_legs, max_altitude and min_altitude arrive from the
    container with no type validation upstream -- only hex_id is checked, in
    rewrite_radar_url(). A stray string in one of these must not survive into
    the archive: the page calls .toFixed()/.toLocaleString() on them, and one
    bad record throwing there blanks the entire detections list, not just its
    own card."""
    if value is None:
        return None
    try:
        return kind(float(value))
    except (TypeError, ValueError):
        return None


def normalize_detection(raw, fallback_type):
    """Flatten one container record into our schema. Returns None if unusable."""
    lat = raw.get('center_lat')
    lon = raw.get('center_lon')
    if lat is None or lon is None:
        return None

    island, distance, bearing = nearest_island(lat, lon)
    detected_at = alaska_iso(raw.get('detected_at'))
    last_seen = alaska_iso(raw.get('last_seen'))

    try:
        radar_url = rewrite_radar_url(raw.get('hex_id'), lat, lon)
    except ValueError:
        # A malformed hex_id must not kill the whole run -- skip just this
        # record, the same as we already do for one missing coordinates.
        return None

    return {
        'pattern_type': raw.get('pattern_type') or fallback_type,
        'callsign': (raw.get('callsign') or '').strip() or 'Unknown',
        'hex_id': raw.get('hex_id'),
        'detected_at': detected_at,
        'last_seen': last_seen,
        'duration_min': _duration_minutes(detected_at, last_seen),
        'center_lat': lat,
        'center_lon': lon,
        'coverage_area': _as_number(raw.get('coverage_area')),
        'num_legs': _as_number(raw.get('num_legs'), kind=int),
        'max_altitude': _as_number(raw.get('max_altitude'), kind=int),
        'min_altitude': _as_number(raw.get('min_altitude'), kind=int),
        'nearest_island': island,
        'bearing_deg': bearing,
        'distance_nm': _as_number(distance),
        'radar_url': radar_url,
    }


def detection_key(det):
    """Stable identity for a detection across repeat polls."""
    return f"{det.get('hex_id')}|{det.get('detected_at')}|{det.get('pattern_type')}"


def merge_detections(existing, incoming):
    """Append-only merge, newest first. Existing records are never dropped."""
    merged = {detection_key(d): d for d in existing}
    for det in incoming:
        merged[detection_key(det)] = det  # newer snapshot of a growing hold wins
    return sorted(
        merged.values(), key=lambda d: d.get('detected_at') or '', reverse=True
    )


def build_output(detections, now, source_online=True):
    if now.tzinfo is None:
        raise ValueError('build_output requires a timezone-aware datetime; '
                         'a naive one silently produces an empty %Z suffix')
    kinds = [d.get('pattern_type') for d in detections]
    circles = kinds.count('circle')
    go_arounds = kinds.count('go_around')
    holds = kinds.count('hold')
    grids = len(detections) - circles - go_arounds - holds
    return {
        'generated': now.isoformat(),
        'generated_display': now.strftime('%B %d, %Y at %I:%M %p %Z'),
        'source_online': source_online,
        'counts': {'circles': circles, 'grids': grids, 'go_arounds': go_arounds,
                   'holds': holds, 'total': len(detections)},
        'detections': detections,
    }


def extract_detections(payload):
    """Flatten the container's {circles:[], grids:[]} into one normalized list."""
    detections = []
    for key, fallback in (('circles', 'circle'), ('grids', 'grid')):
        for raw in payload.get(key) or []:
            det = normalize_detection(raw, fallback)
            if det is not None:
                detections.append(det)
    return detections


def load_existing(path):
    """Return archived detections, or [] if the file does not exist yet.

    A MISSING file is safe to treat as empty (first run, fresh deploy). A
    file that EXISTS but cannot be parsed is NOT safe to treat as empty:
    silently rebuilding from the container would let one corrupt write erase
    every detection ever archived, which defeats the entire reason this
    archive exists (the upstream container's own history can be wiped).
    Callers must treat a present-but-corrupt file as a hard failure -- this
    function deliberately lets json.JSONDecodeError/ValueError/AttributeError
    propagate uncaught so `main()` can refuse and fail loudly instead.
    """
    try:
        with open(path) as fh:
            detections = json.load(fh).get('detections', [])
    except FileNotFoundError:
        return []
    # Records archived before 2026-08-21 carry naive container times.
    for det in detections:
        det['detected_at'] = alaska_iso(det.get('detected_at'))
        det['last_seen'] = alaska_iso(det.get('last_seen'))
    return detections


def write_atomic(path, payload):
    """Write via .tmp + replace. Nginx serves this path directly, so a partial
    file must never be visible to a visitor. If the write itself fails partway
    (disk full, permissions), remove the stray .tmp before re-raising so a
    failed run never leaves debris next to the real file."""
    tmp = f'{path}.tmp'
    try:
        with open(tmp, 'w') as fh:
            json.dump(payload, fh, indent=2)
            # This site runs on unreliable remote power. os.replace() is
            # atomic against concurrent readers, but without flushing and
            # fsyncing first, an unclean shutdown can make the rename durable
            # while the data blocks behind it are not -- producing a
            # truncated or zero-length archive. Combined with the (correct)
            # hard-fail-on-corrupt behavior in load_existing(), that would
            # make the collector refuse to run forever.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def fetch_history():
    response = requests.get(SOURCE_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def adsb_events(now):
    """Go-arounds and holds from our own history, today and yesterday."""
    events = []
    for offset in (1, 0):
        day = (now - timedelta(days=offset)).strftime('%Y-%m-%d')
        try:
            events.extend(approach_events.scan_day(day))
        except Exception as exc:
            print(f'WARNING: ADS-B event scan failed for {day}: {exc}')
    return events


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(ALASKA)

    source_online = True
    incoming = []
    try:
        if SOURCE_URL:
            payload = fetch_history()
            incoming = extract_detections(payload)
        else:
            source_online = False      # no second source configured
    except Exception as exc:
        # The container is one of two sources now; losing it must not stop
        # our own go-around/hold events from being archived.
        print(f'WARNING: container unavailable ({SOURCE_URL}): {exc}')
        source_online = False

    try:
        existing = load_existing(OUTPUT_FILE)
    except Exception as exc:
        print(f'FAILED: existing archive at {OUTPUT_FILE} is corrupt and unreadable: {exc}')
        print('Previous archive left in place.')
        return 1

    own = adsb_events(now.replace(tzinfo=None))
    merged = merge_detections(existing, incoming + own)
    added = len(merged) - len(existing)

    try:
        write_atomic(OUTPUT_FILE, build_output(merged, now, source_online))
    except Exception as exc:
        print(f'FAILED: could not write {OUTPUT_FILE}: {exc}')
        print('Previous archive left in place.')
        return 1

    print(
        f'OK: {len(incoming)} detection(s) reported by container, '
        f'{len(own)} go-around/hold event(s) from our antenna, '
        f'{added} new, {len(merged)} total archived -> {OUTPUT_FILE}'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
