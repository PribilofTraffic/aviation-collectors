#!/usr/bin/env python3
"""Collect today's filed/scheduled flights for PASN from FlightAware AeroAPI.

Queries the arrival boards (scheduled_arrivals + arrivals, so flights that
already landed today still show with actual times). One page per board =
2 result sets = ~$0.01/run on the Personal tier; the cron cadence (every 2 h)
keeps this near $3.60/month, inside the $10/month ADS-B feeder credit.

Output: data/flight_plans_pasn.json, rendered by js/flight_plans.js on
/pasnapproach/. To add St. George later, add PAPB to AIRPORTS -- the frontend
already selects its feed with data-endpoint.

The API key comes from FA_AEROAPI_KEY, falling back to
~/.config/fa_aeroapi_key (cron does not inherit shell exports).
"""

import os
import time

import settings
settings.apply_timezone()

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import flight_legs

API_BASE = 'https://aeroapi.flightaware.com/aeroapi'
REQUEST_TIMEOUT = 20
RETRIES = 3
RETRY_BACKOFF_SECS = 5

AIRPORTS = {
    a['icao']: {'name': a['name'], 'output': settings.plans_file(a['icao'])}
    for a in settings.AIRPORTS
}
# Inbound only, by request — add the departure boards back here if outbound
# legs are ever wanted again ('scheduled_departures', 'departures').
BOARDS = ('scheduled_arrivals', 'arrivals')
ALASKA = settings.TZ      # the site's local zone (the name is historical)

def load_operator_names():
    """Operator names for callsign prefixes, from the site's own
    known_aircraft.json so this cannot diverge from what the commentary
    scripts call each carrier (SRY = Aleutian Airways, AER = Alaska Central
    Express cargo — NOT interchangeable). Unmapped codes render raw."""
    names = {}
    try:
        with open(settings.KNOWN_AIRCRAFT) as fh:
            data = json.load(fh)

        def walk(node):
            if not isinstance(node, dict):
                return
            for key, val in node.items():
                if isinstance(val, dict) and val.get('operator'):
                    if len(key) == 3 and key.isalpha() and key.isupper():
                        names[key] = val['operator']
                    # Tail-number keys (N55SR = Grant Aviation): a tail
                    # files with no operator code and no useful prefix, so
                    # the full ident is the only handle on the carrier.
                    elif (key.startswith('N') and len(key) > 3
                          and key[1:2].isdigit() and key.isalnum()
                          and key.isupper()):
                        names[key] = val['operator']
                walk(val)

        walk(data)
    except Exception as e:
        print(f'known_aircraft.json unavailable ({e}); operator codes render raw')
    # Display preferences from the site file win over known_aircraft.json's
    # company names (locals may know an operation by another name).
    names.update(settings.OPERATOR_DISPLAY_NAMES)
    return names


def get_api_key():
    return settings.fa_api_key()


def fetch_board(airport, board, api_key):
    """One board's flight list, or raise after RETRIES attempts."""
    url = f'{API_BASE}/airports/{airport}/flights/{board}'
    headers = {'x-apikey': api_key}
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=headers,
                             params={'max_pages': 1}, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json().get(board) or []
        except Exception as e:
            last = e
            print(f'{airport}/{board} attempt {attempt}/{RETRIES} failed: {e}')
            if attempt < RETRIES:
                time.sleep(RETRY_BACKOFF_SECS * attempt)
    raise last


def parse_utc(stamp):
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        return None


def phase_of(f):
    """One word the card can color: cancelled / landed / airborne / scheduled."""
    if f.get('cancelled'):
        return 'cancelled'
    if f.get('actual_on') or f.get('actual_in'):
        return 'landed'
    if f.get('actual_off'):
        return 'airborne'
    return 'scheduled'


def normalize(f, direction, operator_names):
    """The subset of a board entry the card renders.

    `when` is the flight's best-known time at THIS airport (arrival time for
    arrivals, departure time for departures) -- it decides which Alaska day
    the flight belongs to and the sort order.
    """
    if direction == 'arrival':
        stamp = (f.get('actual_on') or f.get('estimated_on')
                 or f.get('scheduled_on') or f.get('scheduled_in'))
    else:
        stamp = (f.get('actual_off') or f.get('estimated_off')
                 or f.get('scheduled_off') or f.get('scheduled_out'))
    when = parse_utc(stamp)
    operator = (f.get('operator') or '').upper()
    # Charters can arrive with no operator field; the callsign prefix still
    # identifies the carrier ("charters under any SRY number"). A tail
    # number (N55SR) has neither code nor prefix -- the full ident is the
    # last resort, and deliberately the LAST one, so a display override
    # like AER -> "Ace Air Cargo" still wins for coded flights.
    ident = (f.get('ident') or '').upper()
    prefix = ident[:3]
    return {
        'fa_flight_id': f.get('fa_flight_id'),
        'ident': f.get('ident'),
        'operator': operator,
        'operator_name': (operator_names.get(operator)
                          or operator_names.get(prefix)
                          or operator_names.get(ident)),
        'registration': f.get('registration'),
        'direction': direction,
        'origin': (f.get('origin') or {}).get('code'),
        'origin_city': (f.get('origin') or {}).get('city'),
        'destination': (f.get('destination') or {}).get('code'),
        'destination_city': (f.get('destination') or {}).get('city'),
        'aircraft_type': f.get('aircraft_type'),
        'route': f.get('route'),
        'filed_altitude': f.get('filed_altitude'),   # flight level (hundreds of ft)
        'filed_ete': f.get('filed_ete'),             # seconds
        'when': when.isoformat() if when else None,
        'scheduled_off': f.get('scheduled_off'),
        'estimated_off': f.get('estimated_off'),
        'actual_off': f.get('actual_off'),
        'estimated_on': f.get('estimated_on'),
        'actual_on': f.get('actual_on'),
        'phase': phase_of(f),
        'cancelled': bool(f.get('cancelled')),
        'diverted': bool(f.get('diverted')),
    }


# ---- Flights the boards dropped --------------------------------------
# FlightAware's arrival boards silently DROP a cancelled flight rather
# than flag it -- this log has never once printed "(cancelled)". Left
# alone, the feed simply lost the flight, so the card and the approach
# commentary never learned of the scrub (SRY1801, 2026-09-03: cancelled
# per the owner at 2 PM, off the board later, never shown as cancelled).
# carry_over keeps today's dropped arrivals and asks FlightAware about
# each one ($0.005), so the feed shows CANCELLED within one poll of
# FlightAware knowing, and nobody has to type it in.

MAX_LOOKUP_ATTEMPTS = 3   # per flight per day; after that it stays as-is


def fetch_flight_states(fa_id, api_key):
    """/flights/{fa_flight_id}: the API's record list, or None on any
    failure (no retry -- the next cron run is the retry)."""
    try:
        r = requests.get(f'{API_BASE}/flights/{fa_id}',
                         headers={'x-apikey': api_key},
                         timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json().get('flights') or []
    except Exception as e:
        print(f'  lookup {fa_id} failed: {e}')
        return None


def _resolve_off_board(flight, lookup):
    """Apply FlightAware's own answer about one dropped flight, in place.
    A transient failure (None) costs no attempt; an answer that does not
    resolve it burns one, so a stubborn flight is not queried forever."""
    if (flight.get('cancelled') or flight.get('diverted')
            or flight.get('actual_on')):
        return
    fa_id = flight.get('fa_flight_id') or ''
    if lookup is None or '-' not in fa_id:
        return
    if flight.get('lookup_attempts', 0) >= MAX_LOOKUP_ATTEMPTS:
        return
    states = lookup(fa_id)
    if states is None:
        return
    flight['lookup_attempts'] = flight.get('lookup_attempts', 0) + 1
    # After a divert FA returns two records per id; only the one bound
    # for THIS airport tells the story (the other says "landed" at PANC).
    match = next((st for st in states
                  if (st.get('destination') or {}).get('code') == flight.get('destination')),
                 None)
    if not match:
        return
    flight['cancelled'] = bool(match.get('cancelled'))
    flight['diverted'] = bool(match.get('diverted'))
    flight['actual_off'] = match.get('actual_off') or flight.get('actual_off')
    flight['actual_on'] = match.get('actual_on')
    flight['phase'] = phase_of(flight)


def carry_over(previous, current, today, lookup):
    """Today's arrivals from the previous feed that the boards no longer
    list, appended to `current` (a new list, sorted by `when`), each
    stamped `off_board: True` and resolved via `lookup` where it can be.

    Not carried: a flight re-filed under a new id from the same origin
    (superseded -- the new filing is the story) and a leg already folded
    into a trunk on the board. A landed or already-cancelled flight is
    kept as-is with no lookup. A trunk whose fuel-stop leg is on the
    board IS carried, so flight_legs can fold the leg into it.
    """
    out = list(current)
    if not previous or previous.get('stale') or previous.get('day_key') != today:
        return sorted(out, key=lambda f: f.get('when') or '')
    present_ids = {f.get('fa_flight_id') for f in current}
    folded_ids = {leg for f in current for leg in (f.get('legs') or [])}
    refiled = {(f.get('ident'), f.get('origin')) for f in current}
    for old in previous.get('flights') or []:
        if old.get('direction') != 'arrival':
            continue
        fa_id = old.get('fa_flight_id')
        if not fa_id or fa_id in present_ids or fa_id in folded_ids:
            continue
        if (old.get('ident'), old.get('origin')) in refiled:
            continue
        flight = dict(old)
        flight['off_board'] = True
        _resolve_off_board(flight, lookup)
        out.append(flight)
    return sorted(out, key=lambda f: f.get('when') or '')


def mark_folded_on_board(flights, present_ids):
    """A carried trunk whose leg is on the board is on the board."""
    for f in flights:
        if f.get('off_board') and any(leg in present_ids for leg in (f.get('legs') or [])):
            f['off_board'] = False
    return flights


def collect_airport(airport, api_key, now, previous=None, lookup=None):
    """Today's flights (Alaska date) across the boards, deduped, plus
    today's flights the boards dropped (see carry_over)."""
    today = now.date().isoformat()
    operator_names = load_operator_names()
    # Completed boards come last so a flight that has landed overwrites its
    # scheduled_* entry for the same fa_flight_id.
    merged = {}
    for board in BOARDS:
        direction = 'arrival' if 'arrival' in board else 'departure'
        for raw in fetch_board(airport, board, api_key):
            rec = normalize(raw, direction, operator_names)
            if not rec['when']:
                continue
            local_day = parse_utc(rec['when']).astimezone(ALASKA).date().isoformat()
            if local_day != today:
                continue
            key = rec['fa_flight_id'] or f"{rec['ident']}|{rec['when']}"
            merged[key] = rec
    flights = sorted(merged.values(), key=lambda r: r['when'])
    present_ids = set(merged)
    flights = carry_over(previous, flights, today, lookup)
    # AER66's post-fuel-stop hop is filed as its own leg from Bethel or
    # Cold Bay; fold it back into the Anchorage filing (see flight_legs).
    return mark_folded_on_board(flight_legs.fold_legs(flights), present_ids)


def write_atomic(path, payload):
    """Write via .tmp + replace -- nginx serves this path directly."""
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, path)


def load_previous(path):
    """The feed as last written, for carry_over; {} if unreadable."""
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def mark_stale(airport, error):
    """Keep the last good feed, re-stamped stale, so a fetch failure does not
    blank the card or read as 'no flights today'."""
    path = AIRPORTS[airport]['output']
    try:
        with open(path) as fh:
            feed = json.load(fh)
    except (OSError, ValueError):
        print(f'{airport}: no previous feed to preserve')
        return
    feed['stale'] = True
    feed['fetch_error'] = str(error)[:200]
    try:
        write_atomic(path, feed)
        print(f'{airport}: kept previous feed, marked stale')
    except Exception as write_err:
        print(f'{airport}: failed to mark stale: {write_err}')


def main():
    api_key = get_api_key()
    if not api_key:
        print('No FlightAware key set (AVIATION_FA_AEROAPI_KEY); nothing collected')
        return 1

    now = datetime.now(ALASKA)
    rc = 0
    for airport, cfg in AIRPORTS.items():
        try:
            previous = load_previous(cfg['output'])
            flights = collect_airport(
                airport, api_key, now, previous=previous,
                lookup=lambda fa_id: fetch_flight_states(fa_id, api_key))
        except Exception as e:
            print(f'{airport}: collection failed: {e}')
            mark_stale(airport, e)
            rc = 1
            continue

        payload = {
            'airport': airport,
            'airport_name': cfg['name'],
            'generated': now.isoformat(),
            'generated_display': now.strftime('%B %d, %Y at %I:%M %p %Z'),
            'day_key': now.date().isoformat(),
            'flights': flights,
            'stale': False,
        }
        try:
            write_atomic(cfg['output'], payload)
            summary = ', '.join(
                f"{f['ident']} {f['origin']}->{f['destination']} ({f['phase']}"
                f"{', off board' if f.get('off_board') else ''})"
                for f in flights) or 'none'
            print(f'{airport}: {len(flights)} flights today [{summary}]')
        except Exception as e:
            print(f'{airport}: write failed: {e}')
            mark_stale(airport, e)
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
