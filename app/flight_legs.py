#!/usr/bin/env python3
"""Fold a multi-leg trip's FlightAware legs into one flight.

AER66 (Ace Air Cargo) flies PANC -> fuel stop (Bethel, Cold Bay, or
wherever the day's weather allows) -> PASN -> PAPB as ONE trip, but
FlightAware files the hop after the fuel stop as a separate leg -- a
synthetic `-sw-` id with the fuel stop as its origin. Left alone, the
Anchorage filing ends the day "superseded" and every consumer (ledger,
approach card, commentary) says a flight "from Bethel" came in.

Shared by flight_plans_collect.py (today's feed) and
flight_arrival_record.py (the stored history), so the two never disagree.

Rule, within one ident (and direction, where records carry one), sorted
by `when`: the earliest filing is the trunk. A later filing from a
DIFFERENT origin is the next leg of the same trip, provided the trunk has
not itself landed (a landed trunk plus another arrival is a second
flight). The trunk keeps its identity and its departure-from-origin
times; the leg's arrival status and times become the trip's. The stops
are recorded in `via` / `via_city` and the folded ids in `legs`, so the
fold is idempotent when the collector re-presents the leg next run.
A later filing from the SAME origin is a re-file, not a leg -- that is
flight_arrival_record.day_outcomes' 'superseded' case and is left alone.
"""

# Keys that describe the trunk's origin or its departure from it; the
# leg's values for these belong to the fuel stop, never to the trip.
TRUNK_KEYS = frozenset((
    'id', 'fa_flight_id', 'ident', 'operator', 'operator_name',
    'registration', 'direction', 'origin', 'origin_city', 'aircraft_type',
    'route', 'filed_altitude', 'filed_ete', 'scheduled_off',
    'estimated_off', 'actual_off', 'via', 'via_city', 'legs',
    'lookup_attempts',
))


def _id_of(f):
    return f.get('id') or f.get('fa_flight_id')


def _group_key(f):
    return (f.get('ident'), f.get('direction'))


def _fold_into(trunk, leg):
    trunk.setdefault('via', [])
    trunk.setdefault('via_city', [])
    trunk.setdefault('legs', [])
    leg_id = _id_of(leg)
    if leg.get('origin') and leg['origin'] not in trunk['via']:
        trunk['via'].append(leg['origin'])
        trunk['via_city'].append(leg.get('origin_city') or leg['origin'])
    if leg_id and leg_id not in trunk['legs']:
        trunk['legs'].append(leg_id)
    # A leg the collector re-presents on a later run carries newer state
    # (airborne -> landed), so its fields are always taken afresh.
    for k, v in leg.items():
        if k in TRUNK_KEYS:
            continue
        if k == 'adsb' and trunk.get('adsb'):
            continue  # the antenna's verdict is stamped once; keep it
        trunk[k] = v


def fold_legs(flights):
    """Return the flights with each trip's later legs folded into its
    trunk filing. Records are modified in place (the trunk gains via/
    legs and the leg's status); order follows `when`."""
    ordered = sorted(flights, key=lambda f: f.get('when') or '')
    trunks = {}
    out = []
    for f in ordered:
        key = _group_key(f)
        trunk = trunks.get(key)
        if (trunk is not None and f.get('origin')
                and f.get('origin') != trunk.get('origin')
                and _id_of(f) != _id_of(trunk)
                and (not trunk.get('actual_on')
                     or _id_of(f) in (trunk.get('legs') or []))):
            _fold_into(trunk, f)
            continue
        if key[0] and key not in trunks:
            trunks[key] = f
        out.append(f)
    return out
