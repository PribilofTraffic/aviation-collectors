#!/usr/bin/env python3
"""Cross-check the flight-plans feeds against our own Anchorage antenna.

tar1090 on a receiver at the hub airport (PANC in the original deployment)
hears everything within ~40 nm of Anchorage, which covers a PANC
departure's entire climb-out. FlightAware's boards are only polled every
2 h (they cost money); this feed is free, local, and sees wheels-up
within a minute or two. Matching today's scheduled Pribilof arrivals
against it answers "did it actually take off?" long before the next
AeroAPI poll.

Writes data/panc_departures.json -- {ident: sighting} for today's flights
-- consumed by js/flight_plans.js (a "heard departing Anchorage" line on
the cards) and by both approach commentary scripts (the airborne
plane-day branch fires off it). Browsers cannot reach a LAN receiver, so
this is the collector -> data/*.json -> vanilla JS pattern, no nginx
proxy involved.

WHY NOT PLANEFENCE (this script's original source, until 2026-08-25):
PlaneFence on the same box is a 2 nm / 3000 ft fence around the house,
not area coverage. A departure was logged only when its climb-out
happened to cross that bubble, which is a coin flip on the runway in
use -- it caught SRY1803 and AER970 on 08-19 but heard nothing at all on
08-25, a day it logged just three aircraft total. tar1090 sees the whole
climb-out regardless of runway.

THE COST OF THE SWAP: tar1090 here is `readsb` with "history": 1 and
/globe_history/ closed (403), so it is a LIVE SNAPSHOT with no traces and
no backfill. PlaneFence kept a persistent log, so a missed poll cost
nothing; here a sighting missed is a sighting gone. That is why the cron
runs every 2 minutes (1-59/2) rather than every 10 -- a Saab 2000 climbing
at 250 kt clears 40 nm in about ten minutes, so the old cadence would
have been a coin flip of its own. Do not stretch the interval back out.
"""

import os
import time

import settings
settings.apply_timezone()

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

TAR1090_URL = settings.HUB_TAR1090_URL   # the receiver at the hub airport; optional
FEEDS = tuple(settings.plans_file(a['icao']) for a in settings.AIRPORTS)
OUTPUT = settings.HUB_DEPARTURES_FILE
REQUEST_TIMEOUT = 15
ALASKA = settings.TZ      # the site's local zone (the name is historical)

# PANC sits 3.4 nm from the antenna and the receiver hears past 40 nm, so
# the whole climb-out is in the window. Wide enough to be forgiving, tight
# enough that an overflight 100 nm out is never mistaken for a departure.
MAX_RANGE_NM = 40.0
# A PANC departure is always caught well below this within 40 nm at a 2-minute
# cadence, so anything higher is a cruising overflight -- possibly one in a
# step climb, which would otherwise satisfy the climb test.
MAX_ALT_FT = 25000
# Altitude a rate-less aircraft must gain between polls to count as climbing.
# Real climb-outs gain thousands of feet in two minutes; this only has to
# clear the jitter in successive barometric readings.
MIN_CLIMB_FT = 300

COMPASS = ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
           'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW')


def compass(deg):
    """Degrees -> 16-point name, matching what PlaneFence used to emit."""
    if not isinstance(deg, (int, float)):
        return None
    return COMPASS[int((float(deg) % 360) / 22.5 + 0.5) % 16]


def _number(value):
    """tar1090 sends alt_baro as the STRING 'ground' when a plane is down."""
    return value if isinstance(value, (int, float)) else None


def _is_departing(alt_ft, rate_fpm, prior_alt):
    """Climbing out of Anchorage, as opposed to arriving or overflying.

    A reported climb is the signal. Roughly a quarter of the records within
    40 nm of PANC carry no vertical rate at all, and they are precisely the
    fleet this watches -- BE20 King Airs, C441s, the Beech and Saab types --
    so they cannot simply be dropped. For those the climb is read off the
    PREVIOUS poll instead, which costs one polling interval and settles the
    question honestly.

    Altitude alone must never stand in for the rate: an arrival on short
    final is every bit as low as a departure. That mistake logged TNV54, a
    rate-less BE20, as "departing PANC 5:01 PM" on 2026-08-25 while it was
    two minutes from touching down at Anchorage.
    """
    if alt_ft is None or alt_ft > MAX_ALT_FT:
        return False
    if rate_fpm is not None:
        return rate_fpm > 0
    if prior_alt is None:
        return False        # One snapshot cannot tell a climb from a descent.
    return alt_ft - prior_alt >= MIN_CLIMB_FT


def _usable(a, idents):
    """(callsign, distance, altitude) for a watched aircraft with a position.

    tar1090 also lists aircraft it hears on Mode S alone, with no lat/lon,
    altitude or age. Those are contacts, not evidence.
    """
    callsign = (a.get('flight') or '').strip().upper()
    if callsign not in idents:
        return None
    seen_pos = _number(a.get('seen_pos'))
    dist = _number(a.get('r_dst'))
    if seen_pos is None or dist is None or dist > MAX_RANGE_NM:
        return None
    return callsign, seen_pos, dist, _number(a.get('alt_baro'))


def observe(payload, idents):
    """Per-airframe snapshot for the next poll to compare against.

    Keyed by ICAO hex, not callsign: callsigns blank out and change mid-leg,
    the airframe id does not.
    """
    out = {}
    for a in ((payload or {}).get('aircraft') or []):
        hit = _usable(a, idents)
        if hit is None or a.get('hex') is None:
            continue
        _, seen_pos, dist, alt = hit
        if alt is None:
            continue
        out[a['hex']] = {'alt': alt, 'dist': dist,
                         'seen': int(_number(payload.get('now')) - seen_pos)}
    return out


def pick_departures(payload, idents, day_start, existing=None,
                    previous_obs=None):
    """Departure sightings for `idents` in one tar1090 snapshot.

    `previous_obs` is the last poll's observe() output, used to settle the
    aircraft that report no vertical rate.

    Keeps the EARLIEST sighting per ident: that is the departure. A later
    pass under the same callsign would be a divert coming home, and
    FlightAware's diverted flag tells that story, not this file.
    """
    departures = dict(existing or {})
    previous_obs = previous_obs or {}
    now = _number((payload or {}).get('now'))
    if now is None:
        return departures

    for a in (payload.get('aircraft') or []):
        hit = _usable(a, idents)
        if hit is None:
            continue
        callsign, seen_pos, _dist, alt = hit

        prior = previous_obs.get(a.get('hex')) or {}
        if not _is_departing(alt, _number(a.get('baro_rate')),
                             _number(prior.get('alt'))):
            continue

        seen = int(now - seen_pos)
        if seen < day_start:
            continue
        if callsign in departures and departures[callsign]['seen'] <= seen:
            continue

        departures[callsign] = {
            'seen': seen,
            'seen_display': (datetime.fromtimestamp(seen, ALASKA)
                             .strftime('%I:%M %p').lstrip('0')),
            'tail': a.get('r'),
            'hex': a.get('hex'),
            'altitude_ft': alt,
            'track': compass(a.get('track')),
        }
    return departures


def load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_atomic(path, payload):
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, path)


def todays_idents(today):
    """Callsigns worth watching: today's filed arrivals from both feeds."""
    idents = set()
    for path in FEEDS:
        feed = load_json(path)
        if feed.get('day_key') != today:
            continue
        for f in feed.get('flights', []):
            if f.get('ident'):
                idents.add(f['ident'].upper())
    return idents


def main():
    if not TAR1090_URL:
        print('no hub receiver set (AVIATION_HUB_TAR1090_URL); departure watch is off')
        return
    now = datetime.now(ALASKA)
    today = now.strftime('%Y-%m-%d')
    day_start = datetime(now.year, now.month, now.day, tzinfo=ALASKA).timestamp()

    idents = todays_idents(today)
    if not idents:
        # Nothing scheduled = nothing to watch; write an empty-but-fresh feed
        # so the frontend can tell "no departures" from "watcher is dead".
        write_atomic(OUTPUT, {'day_key': today, 'generated': now.isoformat(),
                              'departures': {}})
        print(f'[{now.isoformat()}] no flights filed today, nothing to watch')
        return

    try:
        r = requests.get(TAR1090_URL, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        snapshot = r.json()
    except Exception as e:
        # Keep the previous file: a dead receiver must not erase a sighting
        # already recorded earlier today.
        print(f'[{now.isoformat()}] tar1090 unreachable, kept previous '
              f'file: {e}')
        return

    previous = load_json(OUTPUT)
    same_day = previous.get('day_key') == today
    existing = previous.get('departures', {}) if same_day else {}
    previous_obs = previous.get('tracking', {}) if same_day else {}
    departures = pick_departures(snapshot, idents, day_start, existing,
                                 previous_obs)

    # `tracking` is this poll's altitudes, for the next poll to compare
    # against. It rides in the public feed because that keeps the whole
    # update to one atomic write; js/flight_plans.js reads only `day_key`
    # and `departures` and ignores it.
    write_atomic(OUTPUT, {'day_key': today, 'generated': now.isoformat(),
                          'departures': departures,
                          'tracking': observe(snapshot, idents)})
    if departures:
        summary = ', '.join(f"{k} {v['seen_display']}"
                            for k, v in sorted(departures.items()))
        print(f'[{now.isoformat()}] heard departing {settings.HUB_ICAO}: {summary}')
    else:
        print(f'[{now.isoformat()}] watching {len(idents)} flight(s), '
              f'none heard at {settings.HUB_ICAO} yet')


if __name__ == '__main__':
    main()
