#!/usr/bin/env python3
"""Go-arounds and holds, found in our own ADS-B history.

The aircraft-patterns container on the St. George Pi is tuned for survey
grids and long circling; in its whole life it flagged two one-minute
"racetracks" and missed every event that matters to an islander waiting
at the terminal. On 2026-08-20 AER66 flew a missed approach at St. Paul,
held 12 minutes at 5,000 ft, went around once more at St. George and
landed on the second try -- none of it detected. data/adsb_history.db
has every minute of it, so this module reads that instead.

Two event types, both emitted in the flight_patterns.json detection schema
so the /flightpatterns/ page renders them beside the container's:

  go_around  a low pass at a field (adsb_ground_truth's rules) that
             climbed back out instead of landing. One event per pass,
             timed at its lowest point.
  hold       >= HOLD_MIN minutes within HOLD_NM of a field, not on the
             approach itself, with >= HOLD_TURN_DEG of accumulated heading
             change -- a racetrack or circling, not a transit. Tuned on a
             month of real tracks: straight-in arrivals accumulate under
             ~150 deg, a single hold lap 360. A fix out to HOLD_NM +
             HOLD_ORBIT_NM keeps the segment ALIVE but is not judged: an
             orbit has a radius, and on 2026-09-03 SVX46 flew 15 minutes of
             laps centred 13 nm off St. Paul whose far side crossed 15 nm
             every lap -- judged fix by fix, the hold shattered into 2-4
             minute fragments and was never reported. The ring must not
             COUNT, though, not even as a chord between the fixes either
             side of it: the standard St. Paul arrival overflies the field,
             reverses course 17-19 nm out and comes back down final, and
             scored as a 300-350 deg "hold" whenever that reversal was let
             in (29 phantom holds over ten months). Nor can the SIGN of the
             turn separate a reversal from a lap: at one fix a minute a
             standard-rate 180 falls between two samples and its direction
             is a coin flip, which dropped AER66's real 12-minute hold of
             2026-08-20.

Each event carries its own `track` -- the aircraft's positions from
TRACK_PAD_MIN before the event to TRACK_PAD_MIN after it, within
TRACK_NM of the field -- so the page can replay it. The Pi's tar1090 keeps
no per-aircraft traces (only a heatmap), so a radar link can show a past
event nothing; our DB is the only replay source.

Timestamps in the DB are naive Alaska local; events are emitted tz-aware.
"""

import os
import time

import settings
settings.apply_timezone()

import math
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import adsb_ground_truth as gt

ALASKA = settings.TZ      # the site's local zone (the name is historical)
DB_PATH = gt.DB_PATH

HOLD_NM = 15.0
HOLD_ORBIT_NM = 5.0      # a lap's far side may swing this far past HOLD_NM
                         # without ending the hold; it earns no time or turn
HOLD_MIN = 8
HOLD_TURN_DEG = 300
SEGMENT_GAP = timedelta(minutes=3)
TRACK_PAD_MIN = 12
TRACK_NM = 40.0
TRACK_MAX_POINTS = 240

ISLAND_NAME = {a['icao']: settings.short_name(a['icao']) for a in settings.AIRPORTS}


def _heading(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1))
    return math.degrees(math.atan2(y, x)) % 360


def _turn(run):
    """Accumulated |heading change| along consecutive fixes of one run."""
    turn = 0.0
    for i in range(2, len(run)):
        h1 = _heading(run[i - 2][2:4], run[i - 1][2:4])
        h2 = _heading(run[i - 1][2:4], run[i][2:4])
        turn += abs((h2 - h1 + 180) % 360 - 180)
    return turn


def _alt(a):
    if a == 'ground':
        return 0
    try:
        return float(a)
    except (TypeError, ValueError):
        return None


def _aware(naive):
    return naive.replace(tzinfo=ALASKA).isoformat()


def _track(all_pts, airport, start, end):
    """Replay points around an event: [iso_local_aware, lat, lon, alt, gs]."""
    flat, flon = gt.FIELDS[airport]
    pad = timedelta(minutes=TRACK_PAD_MIN)
    out = []
    for p in all_pts:
        if not (start - pad <= p[0] <= end + pad):
            continue
        if gt._nm(p[2], p[3], flat, flon) > TRACK_NM:
            continue
        alt = _alt(p[1])
        gs = p[4] if len(p) > 4 else None
        out.append([_aware(p[0]), round(p[2], 5), round(p[3], 5),
                    int(alt) if alt is not None else None,
                    int(gs) if gs is not None else None])
    if len(out) > TRACK_MAX_POINTS:
        step = len(out) / TRACK_MAX_POINTS
        out = [out[int(i * step)] for i in range(TRACK_MAX_POINTS)]
    return out


def _event(kind, callsign, hex_id, airport, start, end, pts, all_pts=()):
    alts = [_alt(p[1]) for p in pts]
    alts = [a for a in alts if a is not None]
    clat = sum(p[2] for p in pts) / len(pts)
    clon = sum(p[3] for p in pts) / len(pts)
    flat, flon = gt.FIELDS[airport]
    dist = gt._nm(clat, clon, flat, flon)
    brg = _heading((flat, flon), (clat, clon))
    return {
        'pattern_type': kind,
        'callsign': callsign or 'Unknown',
        'hex_id': hex_id,
        'detected_at': _aware(start),
        'last_seen': _aware(end),
        'duration_min': max(0, int((end - start).total_seconds() // 60)),
        'center_lat': round(clat, 6),
        'center_lon': round(clon, 6),
        'coverage_area': None,
        'num_legs': None,
        'max_altitude': int(max(alts)) if alts else None,
        'min_altitude': int(min(alts)) if alts else None,
        'nearest_island': ISLAND_NAME[airport],
        'airport': airport,
        'bearing_deg': int(round(brg)),
        'distance_nm': round(dist, 1),
        'radar_url': settings.radar_url(hex_id, clat, clon, 12),
        'source': 'adsb_history',
        'track': _track(all_pts, airport, start, end),
    }


def events_for_track(pts, callsign, hex_id, now=None):
    """pts: [(timestamp_naive_local, alt_baro, lat, lon[, gs])] for one
    aircraft on one day, any order. Returns go_around and hold events."""
    now = now or datetime.now()
    pts = sorted(p for p in pts if p[2] is not None and p[3] is not None)
    out = []
    for airport in gt.FIELDS:
        low = [gt.is_low(p[1], p[2], p[3], airport) for p in pts]
        rows = [(p[0].isoformat(), p[1], p[2], p[3]) for p in pts]
        v = gt.verdict(rows, airport, now=now)
        for when in v['missed_at']:
            t = datetime.fromisoformat(when)
            i = next(i for i, p in enumerate(pts) if p[0] == t)
            # The pass around the low point: expand while still low.
            a = b = i
            while a > 0 and low[a - 1]:
                a -= 1
            while b + 1 < len(pts) and low[b + 1]:
                b += 1
            out.append(_event('go_around', callsign, hex_id, airport,
                              pts[a][0], pts[b][0], pts[a:b + 1], pts))
        # Holds: near the field, not on the approach, continuous in time.
        # A fix in the outer ring keeps the segment alive (an orbit's far
        # side); only runs of fixes within HOLD_NM are judged.
        flat, flon = gt.FIELDS[airport]
        gather_nm = HOLD_NM + HOLD_ORBIT_NM
        seg = []
        segs = []
        for p, is_low in zip(pts, low):
            near = (not is_low
                    and gt._nm(p[2], p[3], flat, flon) <= gather_nm)
            if near and (not seg or p[0] - seg[-1][0] < SEGMENT_GAP):
                seg.append(p)
            else:
                if seg:
                    segs.append(seg)
                seg = [p] if near else []
        if seg:
            segs.append(seg)
        for s in segs:
            runs, run = [], []
            for p in s:
                if gt._nm(p[2], p[3], flat, flon) <= HOLD_NM:
                    run.append(p)
                elif run:
                    runs.append(run)
                    run = []
            if run:
                runs.append(run)
            inner = [p for r in runs for p in r]
            if len(inner) < 4:
                continue
            if (inner[-1][0] - inner[0][0]) < timedelta(minutes=HOLD_MIN):
                continue
            if sum(_turn(r) for r in runs) >= HOLD_TURN_DEG:
                ev = _event('hold', callsign, hex_id, airport,
                            inner[0][0], inner[-1][0], inner, pts)
                # Gate on the inner runs; report the whole orbit, so the
                # card's "N turns" matches the laps the replay draws.
                ev['turn_deg'] = int(_turn(s))
                out.append(ev)
    return sorted(out, key=lambda e: e['detected_at'])


def scan_day(day, db_path=DB_PATH, now=None):
    """All events for one Alaska-local day (YYYY-MM-DD)."""
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        rows = conn.execute(
            'SELECT hex, flight, timestamp, alt_baro, lat, lon, gs '
            'FROM aircraft_sightings WHERE timestamp >= ? AND timestamp < ? '
            'AND lat IS NOT NULL ORDER BY hex, timestamp',
            (day, day + 'T99')).fetchall()
    finally:
        conn.close()
    by_hex = {}
    for hex_id, flight, ts, alt, lat, lon, gs in rows:
        entry = by_hex.setdefault(hex_id, {'callsign': None, 'pts': []})
        if flight and not entry['callsign']:
            entry['callsign'] = flight.strip()
        entry['pts'].append((datetime.fromisoformat(ts), alt, lat, lon, gs))
    events = []
    for hex_id, entry in by_hex.items():
        events.extend(events_for_track(entry['pts'], entry['callsign'],
                                       hex_id, now=now))
    return events


if __name__ == '__main__':
    import json
    import sys
    for day in sys.argv[1:] or [datetime.now().strftime('%Y-%m-%d')]:
        for e in scan_day(day):
            print(json.dumps(e))
