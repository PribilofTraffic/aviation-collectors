#!/usr/bin/env python3
"""Archive each day's flight-plans feed into a running arrival track record.

flight_plans_collect.py only keeps TODAY -- at the first run after Alaska
midnight, yesterday's made-it/scrubbed outcomes are overwritten. This script
runs 10 minutes after it (cron 57 */2, UTC hours) and upserts the feed's
flights into data/flight_record_{pasn,papb}.json, keyed by day and
fa_flight_id, so the season's "how many flights actually made it in" tally
survives the rollover. Mostly no API calls: it reads the local feeds.

The one exception is divert resolution (seen live 2026-08-20, SRY1801
turning back to Anchorage): a divert re-points the flight's destination, so
the PASN/PAPB arrival boards STOP returning it and the boards-only feed can
never flip its `diverted` flag -- the day would end as a bogus no_show.
When a flight vanishes from the boards unresolved (or a day rolls over with
one), a single /flights/{fa_flight_id} lookup ($0.005, max 3 attempts per
flight) fetches the truth. AeroAPI answers with TWO records per
fa_flight_id after a divert: the leg actually flown (PANC->PANC, landed,
diverted=false) and the abandoned plan (PANC->PASN, diverted AND
cancelled). Only the record whose destination matches this airport tells
our story -- the other would score the no-show as "landed". Regression
fixture: tests/fa_fixtures/sry1801_diverted_20260820.json.

History starts 2026-08-19 -- the Personal AeroAPI tier has no historical
access, so past days cannot be backfilled (same philosophy as fog_verify).

Once a day is over, our own ADS-B antenna outranks FlightAware
(adsb_ground_truth.py): a plane that got low at the field and then stayed
off the antenna 15+ minutes LANDED, whatever the boards say; one that got
low and climbed back out flew a missed approach and is DIVERTED -- to the
other island if the antenna saw it land there, else home. Born of
2026-08-20, when FA scored AER66 backwards on both islands (a phantom
"-sw-" record "landed" at St. Paul where it actually went around; the
real landing at St. George never reached the PAPB board at all).

Outcomes per flight: landed (actual_on set), diverted, cancelled, no_show
(the day ended with none of those -- FlightAware never resolved it), or
pending (still today, not yet resolved). Diverted outranks cancelled
because a divert is filed as both. Percentages count only resolved
flights, so a plane still in the air is never scored as a miss.
"""

import os
import time

import settings
settings.apply_timezone()

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

import adsb_ground_truth
import flight_legs
import grant_record

# Shared with the collector so key handling and operator naming cannot
# diverge.
from flight_plans_collect import (get_api_key, load_operator_names,
                                  API_BASE, REQUEST_TIMEOUT)

ALASKA = settings.TZ      # the site's local zone (the name is historical)

AIRPORTS = {
    a['icao']: {'name': a['name'],
                'feed': settings.plans_file(a['icao']),
                'output': settings.record_file(a['icao'])}
    for a in settings.AIRPORTS
}

# FLC* is the FAA's flight-check bird ("FLIGHT CHECK" telephony; FLC91 is
# the one that works the Pribilof ILS approaches, per the owner) -- an
# inspection run, not a scheduled arrival, so it never enters the ledger.
# N55SR is NOT on this list: it looked like a check flight on 2026-08-25
# (BE20, low approaches at both islands) but it is a Grant Aviation bird.
EXCLUDED_IDENT_PREFIXES = settings.EXCLUDED_IDENT_PREFIXES

# Grant Aviation rarely files a plan, so the boards-built ledger scores
# only the Grant flights that were filed. grant_record scores their POSTED
# timetable against our antenna instead; the page shows both numbers.
GRANT_SCHEDULE = settings.TIMETABLE_FILE     # None when no timetable operator
KNOWN_AIRCRAFT = adsb_ground_truth.KNOWN_AIRCRAFT


def is_excluded(flight):
    ident = str(flight.get('ident') or '').upper()
    return bool(EXCLUDED_IDENT_PREFIXES) and ident.startswith(EXCLUDED_IDENT_PREFIXES)

# The per-flight fields worth keeping for the record. Times and booleans only
# -- route/altitude detail stays in the daily feed.
KEEP_FIELDS = (
    'ident', 'operator', 'operator_name', 'aircraft_type',
    'origin', 'origin_city', 'when', 'actual_on', 'cancelled', 'diverted',
)


def load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_atomic(path, payload):
    """Write via .tmp + replace -- nginx serves this path directly."""
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, path)


def outcome_of(flight, day, today):
    seen = (flight.get('adsb') or {}).get('verdict')
    if seen == 'landed':
        return 'landed'
    if seen == 'missed':
        return 'diverted'
    if flight.get('actual_on') and seen != 'overruled':
        return 'landed'
    if flight.get('diverted'):
        return 'diverted'
    if flight.get('cancelled'):
        return 'cancelled'
    return 'pending' if day >= today else 'no_show'


MAX_LOOKUP_ATTEMPTS = 3


def board_ids(flights):
    """Ids of today's arrivals the boards actually list. The collector
    carries a dropped flight forward stamped `off_board` (with its own
    lookup) -- that flight is NOT on the board, and this ledger keeps
    resolving it independently rather than trusting the carry-over."""
    return {f.get('fa_flight_id') for f in flights
            if f.get('direction') == 'arrival' and not f.get('off_board')}


def resolve_vanished(days, airport, today, present_ids, fetch_states):
    """Resolve flights the boards abandoned, via /flights/{fa_flight_id}.

    Looks up an entry only when it is unresolved AND off the boards: its day
    is past, or it is today's and missing from the current feed
    (present_ids; None means the feed is unusable, so today's absences are
    not trusted). fetch_states returns the API's flight-record list, or
    None on a transient failure -- which costs no attempt, while an answer
    that still does not resolve the flight burns one of
    MAX_LOOKUP_ATTEMPTS so a genuinely unresolved flight is not re-queried
    forever.
    """
    for day in sorted(days):
        for entry in days[day]:
            if (entry.get('actual_on') or entry.get('cancelled')
                    or entry.get('diverted')):
                continue
            fa_id = entry.get('id') or ''
            if '-' not in fa_id:
                continue  # ident-keyed fallback row; a bare-ident lookup
                          # returns weeks of other legs, skip it
            if day >= today and (present_ids is None or fa_id in present_ids):
                continue  # still on today's board; the feed will resolve it
            if entry.get('lookup_attempts', 0) >= MAX_LOOKUP_ATTEMPTS:
                continue
            states = fetch_states(fa_id)
            if states is None:
                continue
            entry['lookup_attempts'] = entry.get('lookup_attempts', 0) + 1
            match = next((s for s in states
                          if (s.get('destination') or {}).get('code') == airport),
                         None)
            if not match:
                print(f'  {entry.get("ident")}: lookup had no {airport}-bound'
                      f' record ({len(states)} records)')
                continue
            entry['diverted'] = bool(match.get('diverted'))
            entry['cancelled'] = bool(match.get('cancelled'))
            entry['actual_on'] = match.get('actual_on')
            print(f'  {entry.get("ident")} ({day}): resolved off-board as '
                  f'{outcome_of(entry, day, today)}')


# Where a flight that missed here may have landed instead: every other
# airport the antenna watches, in site-file order.
OTHER_AIRPORTS = {a: [b for b in AIRPORTS if b != a] for a in AIRPORTS}


def _primary_entry(entries, verdict):
    """The one instance of an ident that carries the antenna's verdict:
    the entry whose FA state agrees with it. Landed -> the entry FA also
    called landed (its synthetic "-sw-" record is then real, as SRY1803's
    was on 2026-08-19). Missed -> a real filed plan, since a "-sw-" record
    claiming a landing the antenna never saw is a phantom (AER66,
    2026-08-20). Ties go to the latest filing. The others keep their FA
    state and fall to day_outcomes' superseding."""
    if verdict == 'landed':
        key = lambda e: (bool(e.get('actual_on')), e.get('when') or '')
    else:
        key = lambda e: ('-sw-' not in (e.get('id') or ''),
                         not e.get('cancelled'), e.get('when') or '')
    return max(entries, key=key)


def verify_with_adsb(days, airport, today, verdict_fn):
    """Stamp each closed day's flights with what the antenna saw.

    verdict_fn(ident, airport, day) returns adsb_ground_truth's dict, or
    None when the receiver logged nothing that day -- then absence proves
    nothing and the day is left for a later run (the DB may just be
    catching up). A verdict is stored once per ident per day, on the
    primary entry, and never recomputed: the day's sightings are final.
    """
    for day in sorted(days):
        if day >= today:
            continue
        by_ident = {}
        for e in days[day]:
            by_ident.setdefault(e.get('ident'), []).append(e)
        for ident, entries in by_ident.items():
            if not ident or any('adsb' in e for e in entries):
                continue
            seen = verdict_fn(ident, airport, day)
            if seen is None:
                break  # antenna dead that day; try again next run
            seen = dict(seen)
            if seen['verdict'] == 'missed':
                seen['diverted_to'] = next(
                    (other for other in OTHER_AIRPORTS.get(airport, ())
                     if (verdict_fn(ident, other, day) or {}).get('verdict') == 'landed'),
                    None)
            entry = _primary_entry(entries, seen['verdict'])
            entry['adsb'] = seen
            if seen['verdict'] != 'none':
                # The antenna has the landing story for this ident; a
                # sibling's FA "landed" (a phantom -sw- record, or the
                # abandoned half of a re-filing) must not count as a
                # second one. Stripped of that claim it falls to no_show,
                # which day_outcomes supersedes beside a flown instance.
                for e in entries:
                    if e is not entry and e.get('actual_on'):
                        e['adsb'] = {'verdict': 'overruled'}
            if seen['verdict'] != 'none':
                fa = outcome_of({k: v for k, v in entry.items() if k != 'adsb'},
                                day, today)
                print(f'  {ident} ({day}): antenna says {seen["verdict"]}'
                      f' (FA had {fa})')


def strip_excluded(days):
    """Drop excluded idents from the whole stored history, every run --
    entries recorded before an ident was excluded drop out too."""
    for day in list(days):
        days[day] = [f for f in days[day]
                     if not (isinstance(f, dict) and is_excluded(f))]


def fill_operator_names(days, names):
    """Name the operator on flights the feed left blank. Tail-number idents
    (N55SR) carry no operator code, so the collector's prefix mapping missed
    them and they scored as 'Unknown operator'; known_aircraft.json knows
    the tail. Sweeps the whole history so old entries get named too."""
    for flights in days.values():
        for f in flights:
            if not isinstance(f, dict) or f.get('operator_name'):
                continue
            ident = str(f.get('ident') or '').upper()
            name = (names.get(str(f.get('operator') or '').upper())
                    or names.get(ident[:3]) or names.get(ident))
            if name:
                f['operator_name'] = name


def fold_days(days):
    """Fold each day's multi-leg trips (flight_legs) in place. Runs over
    the whole history every update so days recorded before the rule
    existed -- and a leg the feed re-presents today -- fold the same way."""
    for day, flights in days.items():
        days[day] = flight_legs.fold_legs(
            [f for f in flights if isinstance(f, dict)])


def tally(bucket, outcome):
    bucket['scheduled'] += 1
    bucket[outcome] += 1


def day_outcomes(flights, day, today):
    """Outcome per flight, with re-filed duplicates collapsed.

    A delayed flight often gets a NEW fa_flight_id (seen live: SRY1803 filed
    17:25Z, re-filed 21:14Z the same day). The abandoned instance would end
    the day as a bogus no_show -- or, once the off-board lookup resolves it,
    as an equally bogus 'cancelled' (FA marks abandoned filings cancelled)
    -- and drag the percentage down. So, within one day and one ident:
    a no_show that coexists with any other instance is 'superseded'; a
    cancelled that coexists with an instance that flew or may still fly
    (landed/diverted/pending) is 'superseded' too; and duplicates with
    nothing better collapse to a single counted one. A diverted instance
    always counts -- it really flew and turned back, even if a second
    attempt made it in. Superseded flights stay listed but never enter the
    percentages."""
    outcomes = [outcome_of(f, day, today) for f in flights]
    by_ident = {}
    for i, f in enumerate(flights):
        by_ident.setdefault(f.get('ident'), []).append(i)
    for indexes in by_ident.values():
        flown = [i for i in indexes
                 if outcomes[i] in ('landed', 'diverted', 'pending')]
        no_shows = [i for i in indexes if outcomes[i] == 'no_show']
        cancels = [i for i in indexes if outcomes[i] == 'cancelled']
        # Keep one no_show only when the ident has nothing else that day
        # (a genuine unresolved scrub); otherwise another instance tells
        # the real story and every no_show is a re-filing artifact.
        keep = 0 if len(no_shows) < len(indexes) else 1
        for i in no_shows[keep:]:
            outcomes[i] = 'superseded'
        # Same for cancellations: with a flown instance they are re-filing
        # paperwork; with nothing flown, one cancellation is the story.
        keep = 0 if flown else 1
        for i in cancels[keep:]:
            outcomes[i] = 'superseded'
    return outcomes


def summarize(days, today):
    """Per-month, per-operator and total outcome counts, and an 'outcome'
    annotation on every stored flight (so the pages never re-derive the
    classification). pct_landed uses resolved flights only (scheduled minus
    pending/superseded) and is null until anything has resolved."""
    def new_bucket():
        return {'scheduled': 0, 'landed': 0, 'cancelled': 0, 'diverted': 0,
                'no_show': 0, 'pending': 0, 'superseded': 0}

    months = {}
    operators = {}
    totals = new_bucket()
    for day in sorted(days):
        month = day[:7]
        months.setdefault(month, new_bucket())
        outcomes = day_outcomes(days[day], day, today)
        for flight, outcome in zip(days[day], outcomes):
            flight['outcome'] = outcome
            tally(months[month], outcome)
            tally(totals, outcome)
            op = (flight.get('operator_name') or flight.get('operator')
                  or 'Unknown operator')
            operators.setdefault(op, new_bucket())
            tally(operators[op], outcome)

    def finish(bucket):
        resolved = (bucket['scheduled'] - bucket['pending']
                    - bucket['superseded'])
        bucket['resolved'] = resolved
        bucket['pct_landed'] = (
            round(100 * bucket['landed'] / resolved) if resolved else None)
        return bucket

    return {
        'months': {m: finish(b) for m, b in months.items()},
        'operators': {o: finish(b) for o, b in operators.items()},
        'totals': finish(totals),
    }


def make_fetcher(api_key):
    """A fetch_states for resolve_vanished: the API's record list, or None
    on any failure (no retry here -- the next cron run is the retry)."""
    def fetch(fa_id):
        try:
            r = requests.get(f'{API_BASE}/flights/{fa_id}',
                             headers={'x-apikey': api_key},
                             timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json().get('flights') or []
        except Exception as e:
            print(f'  lookup {fa_id} failed: {e}')
            return None
    return fetch


def adsb_verdict(ident, airport, day):
    try:
        return adsb_ground_truth.verdict_from_db(ident, airport, day)
    except Exception as e:
        print(f'  {ident} ({day}): ADS-B lookup failed: {e}')
        return None


def grant_block(record, days, airport, today, verdict_fn,
                schedule=None, known=None):
    """(grant_days, summary['grant']) for the record: Grant's schedule
    days scored by the antenna, verdicts stored once and carried forward.
    Any failure keeps the stored verdicts and yields no summary -- the
    filed-flight ledger must still be written."""
    stored = record.get('grant_days') or {}
    start = record.get('start_date') or today
    if schedule is None and not (GRANT_SCHEDULE and grant_record.OPERATOR):
        return stored, None            # no timetable operator at this site
    try:
        schedule = schedule if schedule is not None else load_json(GRANT_SCHEDULE)
        known = known if known is not None else load_json(KNOWN_AIRCRAFT)
        tails = grant_record.grant_tails(known)
        stored = grant_record.update(stored, airport, start, today, tails,
                                     verdict_fn)
        return stored, grant_record.summarize(stored, airport, start, today,
                                              schedule, days)
    except Exception as e:
        print(f'{airport}: Grant schedule scoring failed: {e}')
        return stored, None


def update_airport(airport, cfg, today, now, fetch_states=None):
    feed = load_json(cfg['feed'])
    day = feed.get('day_key')
    record = load_json(cfg['output'])
    days = record.get('days', {})
    feed_usable = not (feed.get('stale') or not day
                       or not isinstance(feed.get('flights'), list))

    if not feed_usable:
        # Nothing trustworthy to merge; still refresh the summary so
        # yesterday's pending flights resolve to no_show after midnight.
        print(f'{airport}: feed unusable (stale or missing), summary refresh only')
    else:
        def key_of(f):
            return f.get('id') or f.get('fa_flight_id') or f.get('ident')

        by_id = {key_of(f): f for f in days.get(day, [])
                 if isinstance(f, dict) and key_of(f)}
        for flight in feed['flights']:
            if flight.get('direction') != 'arrival':
                continue
            entry = {k: flight.get(k) for k in KEEP_FIELDS}
            entry['id'] = flight.get('fa_flight_id')
            # Upsert by fa_flight_id; the newest feed state wins, so a
            # scheduled flight flips to landed/cancelled on a later run.
            if key_of(entry):
                by_id[key_of(entry)] = entry
        days[day] = sorted(by_id.values(), key=lambda f: f.get('when') or '')

    strip_excluded(days)
    fill_operator_names(days, load_operator_names())
    fold_days(days)

    if fetch_states:
        present_ids = None
        if feed_usable and day == today:
            present_ids = board_ids(feed['flights'])
        resolve_vanished(days, airport, today, present_ids, fetch_states)

    verify_with_adsb(days, airport, today, adsb_verdict)
    grant_days, grant_summary = grant_block(record, days, airport, today,
                                            adsb_verdict)

    summary = summarize(days, today)
    summary['grant'] = grant_summary
    payload = {
        'airport': airport,
        'airport_name': cfg['name'],
        'start_date': record.get('start_date') or today,
        'updated': now.isoformat(),
        'updated_display': now.strftime('%B %d, %Y at %I:%M %p %Z'),
        'days': days,
        'grant_days': grant_days,
        'summary': summary,
    }
    write_atomic(cfg['output'], payload)
    if grant_summary:
        g = grant_summary
        print(f"{airport}: Grant per antenna: landed {g['landed']} of "
              f"{g['scheduled']} scheduled days ({g['unknown']} unknown, "
              f"{g['filed']} filed)")
    t = payload['summary']['totals']
    print(f"{airport}: {sum(len(v) for v in days.values())} flights over "
          f"{len(days)} day(s); landed {t['landed']}/{t['resolved']} resolved"
          f" ({t['pct_landed']}%)" if t['resolved'] else
          f"{airport}: {sum(len(v) for v in days.values())} flight(s) recorded,"
          f" none resolved yet")


def main():
    now = datetime.now(ALASKA)
    today = now.strftime('%Y-%m-%d')
    print(f'[{now.isoformat()}] flight arrival record update')
    api_key = get_api_key()
    fetch_states = make_fetcher(api_key) if api_key else None
    if not fetch_states:
        print('no AeroAPI key; off-board flights will not be resolved this run')
    for airport, cfg in AIRPORTS.items():
        try:
            update_airport(airport, cfg, today, now, fetch_states)
        except Exception as e:
            # Keep the previous record file untouched on any failure.
            print(f'{airport}: FAILED to update record: {e}')


if __name__ == '__main__':
    main()
