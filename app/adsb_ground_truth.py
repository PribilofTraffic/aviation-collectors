#!/usr/bin/env python3
"""Did the plane actually land? Ask our own antenna, not FlightAware.

FlightAware's arrival boards get Pribilof days wrong in both directions
(2026-08-20, AER66): a phantom position-derived "-sw-" record scored a
missed approach at St. Paul as LANDED, and the same plane's real landing at
St. George -- reached by diverting straight from the St. Paul miss -- never
appeared on the St. George board at all, so that row scored CANCELLED.

data/adsb_history.db (adsb_collector.py, one row per aircraft per minute
from the St. George receiver, which sees both islands) holds the truth.
The rule, from watching years of these arrivals: every plane that lands
at PASN or PAPB is on the ground for a good while; a missed approach is
back on the antenna within a couple of minutes. Measured over ten months
of tracks (Oct 2025 - Aug 2026) the gap after the last low position is
cleanly bimodal: 1-2 minutes (a climb-out) or 11 minutes and up (the
quickest turnaround seen), with NOTHING between 3 and 10. LANDED_GAP_MIN
sits in that void. So: a run of "low" positions (within LOW_NM of the
field, below LOW_FT) followed by NO position for LANDED_GAP_MIN or more
is a landing, and a low run followed by a climb-out within that window is
a missed approach. A low run that starts after a gap is the departure,
not an arrival. A climb-out often dips back into the low box for a minute
(terrain, a turn); low runs under PASS_MERGE_MIN apart are one pass.

At PAPB terrain blocks the signal below a couple of thousand feet, so the
last low position is well before touchdown -- the gap carries the verdict,
not the altitude, which is why LOW_FT is generous.

verdict() returns a dict the record script stores verbatim:
  verdict    'landed' | 'missed' | 'none'  (none = no opinion: never low,
                                            no rows, or gap still open)
  landed_at  ISO Alaska-local time of the last position before the gap
  departed_at  first low position of a post-landing climb-out, if seen
  missed     count of missed approaches
  missed_at  their times (lowest point of each pass)
"""

import os
import time

import settings
settings.apply_timezone()

import math
import json
import sqlite3
from datetime import datetime, timedelta

DB_PATH = settings.DB_PATH

# {icao: (lat, lon)} for every airport in the site file.
FIELDS = {a['icao']: (a['lat'], a['lon']) for a in settings.AIRPORTS}

LOW_NM = 6.0
LOW_FT = 2500
LANDED_GAP_MIN = 8
PASS_MERGE_MIN = 5


def _nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 3440.065 * 2 * math.asin(math.sqrt(a))


def is_low(alt, lat, lon, airport):
    """Within LOW_NM of the field and below LOW_FT (or on the ground)."""
    if lat is None or lon is None:
        return False
    if alt == 'ground':
        alt = 0
    if alt is None:
        return False
    try:
        alt = float(alt)
    except (TypeError, ValueError):
        return False
    flat, flon = FIELDS[airport]
    return alt <= LOW_FT and _nm(lat, lon, flat, flon) <= LOW_NM


def _ts(s):
    return datetime.fromisoformat(s)


def verdict(rows, airport, now=None):
    """rows: iterable of (timestamp_iso_local, alt_baro, lat, lon) for ONE
    ident on one day, any order. Rows without a position are ignored --
    the collector writes them when tar1090 momentarily lacks a fix, and
    they must not read as either a gap or a contact."""
    now = now or datetime.now()
    gap = timedelta(minutes=LANDED_GAP_MIN)
    pts = sorted((_ts(t), alt, lat, lon) for t, alt, lat, lon in rows
                 if lat is not None and lon is not None)
    out = {'verdict': 'none', 'landed_at': None, 'departed_at': None,
           'missed': 0, 'missed_at': []}
    if not pts:
        return out

    # Split the positions into passes: low points, possibly with brief
    # non-low excursions, where each low point follows the previous low
    # point within PASS_MERGE_MIN. A pass ends at its last low point.
    low = [is_low(p[1], p[2], p[3], airport) for p in pts]
    merge = timedelta(minutes=PASS_MERGE_MIN)
    runs = []  # (first_idx, last_idx) -- both low points
    i = 0
    while i < len(pts):
        if not low[i]:
            i += 1
            continue
        j = i
        k = j + 1
        while k < len(pts) and pts[k][0] - pts[j][0] < merge:
            if low[k]:
                j = k
            k += 1
        runs.append((i, j))
        i = j + 1

    for first, last in runs:
        before_gap = first == 0 or pts[first][0] - pts[first - 1][0] >= gap
        if last + 1 < len(pts):
            after_gap = pts[last + 1][0] - pts[last][0] >= gap
        else:
            # Nothing after it yet: only a gap that has already elapsed
            # counts; otherwise the plane may still be on approach.
            if now - pts[last][0] < gap:
                continue
            after_gap = True
        if after_gap:
            if before_gap and out['landed_at']:
                # Appeared out of a gap and vanished into another: a
                # ground movement/second leg, not a fresh arrival.
                continue
            out['verdict'] = 'landed'
            out['landed_at'] = pts[last][0].isoformat()
        elif before_gap:
            # Came out of nowhere low and climbed away: a departure.
            if out['landed_at'] and not out['departed_at']:
                out['departed_at'] = pts[first][0].isoformat()
        else:
            # Came down, climbed back out within the window: missed.
            low = min(pts[first:last + 1],
                      key=lambda p: (float(p[1]) if p[1] != 'ground' else 0))
            out['missed'] += 1
            out['missed_at'].append(low[0].isoformat())
    if out['verdict'] == 'none' and out['missed']:
        out['verdict'] = 'missed'
    return out


KNOWN_AIRCRAFT = settings.KNOWN_AIRCRAFT


def antenna_idents(ident, known_path=KNOWN_AIRCRAFT):
    """The callsigns the antenna may hear this ident as, the ident first.

    Grant Aviation files (rarely) as its ICAO code plus the last three of
    the tail -- GUN5SR -- while the King Air broadcasts its TAIL, N55SR.
    Matched by exact ident, the 2026-09-03 St. George landing (antenna:
    on the ground 3:09 PM, off again 3:27) found no rows and the ledger
    sat at "pending". A tail in known_aircraft.json whose last three
    characters match the ident's suffix is an alias, provided the code's
    operator (when known) is the tail's operator.
    """
    ident = (ident or '').strip().upper()
    if not ident:
        return []
    out = [ident]
    code, suffix = ident[:3], ident[3:]
    if not (code.isalpha() and suffix):
        return out
    try:
        with open(known_path) as fh:
            known = json.load(fh).get('known_aircraft') or {}
    except (OSError, ValueError):
        return out
    code_op = (known.get(code) or {}).get('operator')
    for key, val in known.items():
        tail = key.upper()
        if (tail.startswith('N') and len(tail) > 3 and tail[1].isdigit()
                and tail.endswith(suffix) and tail != ident
                and isinstance(val, dict)
                and (not code_op or val.get('operator') == code_op)):
            out.append(tail)
    return out


def rows_from_db(ident, day, db_path=DB_PATH):
    """All sightings for an ident (callsign) on an Alaska-local day.
    Timestamps in the DB are naive Alaska local (the collector sets TZ)."""
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        cur = conn.execute(
            'SELECT timestamp, alt_baro, lat, lon FROM aircraft_sightings '
            'WHERE timestamp >= ? AND timestamp < ? AND flight = ?',
            (day, day + 'T99', ident))
        return cur.fetchall()
    finally:
        conn.close()


def antenna_alive(day, db_path=DB_PATH):
    """Any sightings at all that day -- if the receiver was dead, the
    absence of a plane proves nothing."""
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        cur = conn.execute(
            'SELECT 1 FROM aircraft_sightings WHERE timestamp >= ? '
            'AND timestamp < ? LIMIT 1', (day, day + 'T99'))
        return cur.fetchone() is not None
    finally:
        conn.close()


def verdict_from_db(ident, airport, day, now=None, db_path=DB_PATH,
                    known_path=KNOWN_AIRCRAFT):
    """The antenna's verdict for an ident on a day, or None if the antenna
    was dead. Tries the ident, then its aliases (antenna_idents); when an
    alias carried the rows the result says so in `seen_as`."""
    if not antenna_alive(day, db_path):
        return None
    for i, name in enumerate(antenna_idents(ident, known_path)):
        rows = rows_from_db(name, day, db_path)
        if rows:
            out = verdict(rows, airport, now=now)
            if i:
                out['seen_as'] = name
            return out
    return verdict([], airport, now=now)


if __name__ == '__main__':
    import json
    import sys
    ident, airport, day = sys.argv[1:4]
    print(json.dumps(verdict_from_db(ident, airport, day), indent=1))
