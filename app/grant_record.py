#!/usr/bin/env python3
"""Grant Aviation's posted schedule, scored by our own antenna.

The arrival ledger (flight_arrival_record.py) is built from FlightAware's
arrival boards, which only ever see FILED plans. Grant Aviation rarely
files one (owner, 2026-09-03: "they have a habit of not filing a flight
plan"), so the ledger scores Grant on the few flights it did file -- and
those all landed, so the airline table read "Grant Aviation 100%" while
the King Air stayed home on most of its scheduled days. A traveler at the
terminal on a Thursday does not care whether the no-show was filed.

This module scores the POSTED timetable (data/grant_schedule.json,
hand-maintained) instead: for every closed day since the ledger began it
asks adsb_history.db whether a known Grant tail (known_aircraft.json,
operator "Grant Aviation") landed at the island, using the same
adsb_ground_truth verdict the ledger already trusts over FlightAware.

Rules, same as the rest of the ledger:
- Only closed days are scored; today is still in play.
- A day the antenna logged nothing at all is UNKNOWN, never a no-show
  (a checker that could not check says nothing about the plane), and is
  retried next run because it is not stored.
- A stored verdict is final -- the day's sightings do not change.
- Every closed day is checked, not just schedule days, so an off-schedule
  landing (makeup run or charter) is listed separately, never scored.
"""

from datetime import date, timedelta

import settings

OPERATOR = settings.TIMETABLE_OPERATOR
STORED_KEYS = ('antenna', 'tail', 'landed_at', 'departed_at')


def grant_tails(known):
    """Registered N-numbers whose operator is Grant, in stable order.
    Prefix entries (GUN) are filing codes, not aircraft, and are skipped:
    the antenna hears the tail."""
    out = []
    for key, val in sorted((known.get('known_aircraft') or {}).items()):
        tail = key.upper()
        if (isinstance(val, dict) and val.get('operator') == OPERATOR
                and tail.startswith('N') and len(tail) > 3
                and tail[1].isdigit()):
            out.append(tail)
    return out


def _dates(start, end_exclusive):
    d = date.fromisoformat(start)
    end = date.fromisoformat(end_exclusive)
    while d < end:
        yield d
        d += timedelta(days=1)


def scheduled_dates(start, today, weekdays):
    """ISO dates from start up to (not including) today whose weekday name
    is in the posted schedule."""
    wanted = set(weekdays or [])
    return [d.isoformat() for d in _dates(start, today)
            if d.strftime('%A') in wanted]


def _day_verdict(tails, airport, day, verdict_fn):
    """The best word the antenna has for any Grant tail that day: a landing
    beats a missed approach beats silence. None if the antenna was dead."""
    best = None
    rank = {'landed': 2, 'missed': 1, 'none': 0}
    for tail in tails:
        seen = verdict_fn(tail, airport, day)
        if seen is None:
            return None
        cand = {'antenna': seen.get('verdict', 'none'), 'tail': tail,
                'landed_at': seen.get('landed_at'),
                'departed_at': seen.get('departed_at')}
        if cand['antenna'] == 'none':
            cand['tail'] = None
        if best is None or rank.get(cand['antenna'], 0) > rank.get(best['antenna'], 0):
            best = cand
        if best['antenna'] == 'landed':
            break
    return best


def update(stored, airport, start, today, tails, verdict_fn):
    """Return stored plus a verdict for every closed day not yet stored.
    verdict_fn(ident, airport, day) is adsb_ground_truth.verdict_from_db or
    a stand-in; None means the antenna logged nothing that day."""
    out = {k: dict(v) for k, v in (stored or {}).items()}
    for d in _dates(start, today):
        day = d.isoformat()
        if day in out:
            continue
        seen = _day_verdict(tails, airport, day, verdict_fn)
        if seen is None:
            continue
        out[day] = seen
    return out


def _filed(ledger_days, day):
    return any((f.get('operator_name') or '') == OPERATOR
               for f in (ledger_days or {}).get(day, []) if isinstance(f, dict))


def summarize(stored, airport, start, today, schedule, ledger_days):
    """The page's block: each schedule day's verdict, the counts, and any
    off-schedule landings. None when the schedule does not cover this
    airport (the page then shows nothing rather than a zero)."""
    site = ((schedule or {}).get('airports') or {}).get(airport)
    if not site:
        return None
    weekdays = list(site.get('days') or [])
    sched = scheduled_dates(start, today, weekdays)
    sched_set = set(sched)
    stored = stored or {}

    days = []
    counts = {'landed': 0, 'no_show': 0, 'unknown': 0, 'filed': 0}
    for day in sched:
        rec = stored.get(day)
        filed = _filed(ledger_days, day)
        if rec is None:
            verdict = 'unknown'
        elif rec.get('antenna') == 'landed':
            verdict = 'landed'
        else:
            verdict = 'no_show'
        row = {'day': day, 'verdict': verdict,
               'tail': (rec or {}).get('tail'),
               'landed_at': (rec or {}).get('landed_at'),
               'departed_at': (rec or {}).get('departed_at'),
               'filed': filed}
        if rec is not None and rec.get('antenna') == 'missed':
            row['missed'] = True
        days.append(row)
        counts[verdict] += 1
        if filed:
            counts['filed'] += 1

    extra = []
    for day in sorted(stored):
        rec = stored[day]
        if day in sched_set or day < start or day >= today:
            continue
        if rec.get('antenna') == 'landed':
            extra.append({'day': day, 'tail': rec.get('tail'),
                          'landed_at': rec.get('landed_at'),
                          'departed_at': rec.get('departed_at'),
                          'filed': _filed(ledger_days, day)})

    known = counts['landed'] + counts['no_show']
    return {
        'operator': OPERATOR,
        'weekdays': weekdays,
        'as_of': (schedule or {}).get('as_of'),
        'scheduled': len(sched),
        'landed': counts['landed'],
        'no_show': counts['no_show'],
        'unknown': counts['unknown'],
        'filed': counts['filed'],
        'pct_landed': round(100 * counts['landed'] / known) if known else None,
        'days': days,
        'extra': extra,
    }
