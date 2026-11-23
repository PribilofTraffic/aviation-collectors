#!/usr/bin/env python3
"""Plane-day helpers shared by the two approach commentary scripts.

One module on purpose: pasn_approach_commentary.py and
papb_approach_commentary.py carry byte-identical plane-day blocks, and the
PAPB wind parser sat broken for nine months because a fix landed in only
one twin. Anything with a rule in it lives here instead.

The overdue rule is the owner's (2026-09-03): "if 1801 isn't here by 1 PM
we can assume it's on weather hold or it cancelled, if you don't see it
circling or in the ADS-B data." Generalised as: the filed ETA has passed
by OVERDUE_GRACE_MIN with no evidence the plane ever left. Radar is the
caller's check — a flight on the scope is never overdue, it is here.
"""

from datetime import datetime, timedelta, timezone

import adsb_ground_truth

# Aleutian's ETA runs ~11:20 AM under the September 2026 schedule; the
# owner's 1 PM cutoff is ~90 minutes past that.
OVERDUE_GRACE_MIN = 90


def _parse(stamp):
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def is_overdue(flight, heard=None, now=None, grace_min=OVERDUE_GRACE_MIN):
    """True when a filed arrival is past its ETA by grace_min and nothing
    says it ever left the mainland.

    Never overdue: landed (actual_on), airborne (actual_off, or our PANC
    antenna heard it — `heard` is truthy), cancelled or diverted (those are
    their own story), or a record with no usable ETA. A missing ETA yields
    False, never a guess.
    """
    if not flight:
        return False
    if flight.get('actual_on') or flight.get('actual_off') or heard:
        return False
    if flight.get('cancelled') or flight.get('diverted'):
        return False
    eta = _parse(flight.get('when') or flight.get('estimated_on')
                 or flight.get('scheduled_on'))
    if eta is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - eta >= timedelta(minutes=grace_min)


# The departure clock (owner, 2026-11-22): "the best one is seeing if it
# departed at its scheduled time, but it could be on weather hold too." The
# overdue rule above waits for the ARRIVAL time plus 90 minutes (1:10 PM for
# SRY1801); the plane's failure to leave Anchorage is known hours sooner.
# Our Anchorage antenna (panc_departure_watch.py, every 2 minutes) hears a
# watched flight climb out, so an hour past its departure time with the watch
# running and nothing heard means it is still on the ground there. That says
# nothing about WHY (weather hold, delay or cancellation) and must not.
DEPARTURE_GRACE_MIN = 60
WATCH_FRESH_MIN = 10            # the watch writes every 2 minutes
WATCHED_ORIGINS = ('PANC',)     # the only airport our antenna can hear leaving


def watch_is_fresh(panc_feed, now=None, fresh_min=WATCH_FRESH_MIN):
    """True when the Anchorage departure watch wrote its file in the last
    fresh_min minutes. A stopped watch proves nothing about a departure, so
    a stale or missing file must never read as "not heard"."""
    written = _parse((panc_feed or {}).get('generated'))
    if written is None:
        return False
    now = now or datetime.now(timezone.utc)
    return timedelta(0) <= now - written <= timedelta(minutes=fresh_min)


def not_departed(flight, heard=None, watching=False, now=None,
                 grace_min=DEPARTURE_GRACE_MIN):
    """True when a flight from a watched airport is grace_min past its
    departure time, the departure watch is running, and nothing says it left.

    The departure time is FlightAware's estimate when there is one (it moves
    when the airline posts a delay) and the schedule otherwise. Never true
    for: an origin our antenna cannot hear (Bethel), a stopped watch, a
    flight that took off (actual_off, or heard), landed, was cancelled or
    diverted, or has no departure time at all.
    """
    if not flight or not watching:
        return False
    if (flight.get('origin') or '').upper() not in WATCHED_ORIGINS:
        return False
    if flight.get('actual_on') or flight.get('actual_off') or heard:
        return False
    if flight.get('cancelled') or flight.get('diverted'):
        return False
    leaves = _parse(flight.get('estimated_off') or flight.get('scheduled_off'))
    if leaves is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - leaves >= timedelta(minutes=grace_min)


def flight_label(flight):
    """'Aleutian Airways 1801' -- operator name plus the bare flight number,
    the way the commentary names a plane."""
    operator = (flight.get('operator') or '').upper()
    ident = (flight.get('ident') or '').upper()
    number = ident[len(operator):] if operator and ident.startswith(operator) else ident
    name = flight.get('operator_name') or operator or 'An unlisted operator'
    return f"{name} {number}".strip()


def cancelled_note(planned, live):
    """A prompt line naming today's cancelled flights when some OTHER flight
    is still the story -- otherwise the scrub silently vanished from the
    commentary the moment the collector marked it (only the all-cancelled
    branch ever mentioned a scrub). '' when there is nothing to say."""
    if not live:
        return ''
    gone = [f for f in (planned or [])
            if f.get('cancelled') and not f.get('actual_on')]
    if not gone:
        return ''
    names = ', '.join(flight_label(f) for f in gone)
    return (f"- Cancelled today: {names} - already scrubbed, not coming. "
            "Mention it in passing with dry sympathy if it fits; do NOT "
            "say it is inbound.\n")


# A landing the antenna saw more than this long before the filed ETA
# belongs to an earlier trip under the same ident (AER66 lands at PASN in
# the morning and again in the afternoon on a two-run day).
LANDING_WINDOW_H = 3


def clock_of(naive_local_iso):
    """'2026-09-03T15:09:54.035613' (naive Alaska local, as adsb_history.db
    stores it) -> '3:09 PM'. None for anything unparseable."""
    if not naive_local_iso:
        return None
    try:
        t = datetime.fromisoformat(str(naive_local_iso)[:19])
    except ValueError:
        return None
    return t.strftime('%I:%M %p').lstrip('0')


def antenna_landing(flight, airport, now=None, verdict_fn=None):
    """What our own antenna says about THIS flight today: a dict with
    `landed_at` / `departed_at` (naive Alaska-local ISO) and `seen_as`
    (the callsign actually heard, when it differs), or None.

    Grant rarely files a plan and FlightAware never sees our antenna, so
    a Grant landing can sit "airborne, due 3:18 PM" in FA for hours after
    the wheels were down -- the antenna is the primary source here, not
    a tie-breaker. Any failure returns None: the caller must then say
    nothing, never "landed".
    """
    if not flight or not flight.get('ident'):
        return None
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone()
    day = local_now.strftime('%Y-%m-%d')
    fn = verdict_fn or (lambda ident, ap, d: adsb_ground_truth.verdict_from_db(
        ident, ap, d, now=local_now.replace(tzinfo=None)))
    try:
        seen = fn(flight['ident'], airport, day)
    except Exception:
        return None
    if not seen or seen.get('verdict') != 'landed' or not seen.get('landed_at'):
        return None
    eta = _parse(flight.get('when') or flight.get('estimated_on')
                 or flight.get('scheduled_on'))
    try:
        landed = datetime.fromisoformat(str(seen['landed_at'])[:19])
    except ValueError:
        return None
    if eta is not None:
        landed_aware = landed.replace(tzinfo=local_now.tzinfo)
        if landed_aware < eta - timedelta(hours=LANDING_WINDOW_H):
            return None   # an earlier trip's landing, not this one
    return {'landed_at': seen['landed_at'],
            'departed_at': seen.get('departed_at'),
            'seen_as': seen.get('seen_as')}


def radar_match(callsign, flight):
    """Is this callsign on the scope the filed flight? By operator prefix
    (SRY1801 for any SRY), or by the antenna alias of the ident (GUN5SR is
    heard as N55SR)."""
    cs = (callsign or '').strip().upper()
    if not cs:
        return False
    operator = (flight.get('operator') or '').upper()
    if operator and cs.startswith(operator):
        return True
    return cs in adsb_ground_truth.antenna_idents(flight.get('ident'))
