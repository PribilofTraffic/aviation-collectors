#!/usr/bin/env python3
"""Tests for plane_day.is_overdue — the 'hold or cancelled' rule.

The fixture flight is SRY1801's real 2026-09-03 filing: estimated in at
19:23Z (11:23 AM AKDT), never departed. The 2:05 PM commentary that day
told readers it was "due in about an hour".
"""

import copy
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import plane_day  # noqa: E402

SRY1801 = {
    "fa_flight_id": "SRY1801-1788238978-airline-2006p", "ident": "SRY1801",
    "operator": "SRY", "operator_name": "Aleutian Airways",
    "origin": "PANC", "destination": "PASN", "aircraft_type": "SB20",
    "when": "2026-09-03T19:23:15+00:00",
    "scheduled_off": "2026-09-03T17:40:00Z", "estimated_off": "2026-09-03T17:40:00Z",
    "actual_off": None, "estimated_on": "2026-09-03T19:23:15Z", "actual_on": None,
    "phase": "scheduled", "cancelled": False, "diverted": False,
}


def at(hhmm_utc):
    h, m = hhmm_utc.split(':')
    return datetime(2026, 9, 3, int(h), int(m), tzinfo=timezone.utc)


class TestIsOverdue(unittest.TestCase):
    def test_the_real_2pm_case_is_overdue(self):
        # 22:05Z = 2:05 PM AKDT, ETA 2h42m ago, never left Anchorage.
        self.assertTrue(plane_day.is_overdue(SRY1801, now=at('22:05')))

    def test_before_the_eta_is_not_overdue(self):
        self.assertFalse(plane_day.is_overdue(SRY1801, now=at('19:00')))

    def test_grace_period_boundary(self):
        # ETA 19:23 + 90 min = 20:53Z (12:53 PM AKDT): the owner's "by 1 PM".
        self.assertFalse(plane_day.is_overdue(SRY1801, now=at('20:52')))
        self.assertTrue(plane_day.is_overdue(SRY1801, now=at('20:54')))

    def test_airborne_per_flightaware_is_not_overdue(self):
        f = copy.deepcopy(SRY1801)
        f['actual_off'] = '2026-09-03T21:30:00Z'
        self.assertFalse(plane_day.is_overdue(f, now=at('23:30')))

    def test_heard_by_the_panc_antenna_is_not_overdue(self):
        heard = {'seen_display': '1:31 PM'}
        self.assertFalse(plane_day.is_overdue(SRY1801, heard=heard, now=at('23:30')))

    def test_landed_cancelled_diverted_are_their_own_story(self):
        for key, val in (('actual_on', '2026-09-03T19:30:00Z'),
                         ('cancelled', True), ('diverted', True)):
            f = copy.deepcopy(SRY1801)
            f[key] = val
            self.assertFalse(plane_day.is_overdue(f, now=at('23:30')), key)

    def test_no_eta_is_never_a_guess(self):
        f = copy.deepcopy(SRY1801)
        f['when'] = None
        f['estimated_on'] = None
        f['scheduled_on'] = None
        self.assertFalse(plane_day.is_overdue(f, now=at('23:30')))
        self.assertFalse(plane_day.is_overdue({}, now=at('23:30')))
        self.assertFalse(plane_day.is_overdue(None, now=at('23:30')))

    def test_falls_back_to_estimated_on_when_when_is_missing(self):
        f = copy.deepcopy(SRY1801)
        del f['when']
        self.assertTrue(plane_day.is_overdue(f, now=at('22:05')))

    def test_garbage_eta_is_not_overdue(self):
        f = copy.deepcopy(SRY1801)
        f['when'] = 'soon'
        f['estimated_on'] = 'later'
        self.assertFalse(plane_day.is_overdue(f, now=at('23:30')))



AER66 = {"ident": "AER66", "operator": "AER", "operator_name": "Ace Air Cargo",
         "cancelled": False, "actual_on": None}


class TestLabelsAndNotes(unittest.TestCase):
    def test_flight_label(self):
        self.assertEqual(plane_day.flight_label(SRY1801), 'Aleutian Airways 1801')
        self.assertEqual(plane_day.flight_label({'ident': 'N55SR', 'operator': '',
                                                 'operator_name': 'Grant Aviation'}),
                         'Grant Aviation N55SR')
        self.assertEqual(plane_day.flight_label({'ident': 'XYZ1', 'operator': 'XYZ'}),
                         'XYZ 1')

    def test_cancelled_note_names_the_scrub_when_another_flight_is_live(self):
        gone = dict(SRY1801, cancelled=True)
        note = plane_day.cancelled_note([gone, AER66], [AER66])
        self.assertIn('Cancelled today: Aleutian Airways 1801', note)
        self.assertIn('do NOT say it is inbound', note)

    def test_cancelled_note_is_silent_when_nothing_is_live_or_cancelled(self):
        gone = dict(SRY1801, cancelled=True)
        self.assertEqual(plane_day.cancelled_note([gone], []), '')   # scrub branch owns it
        self.assertEqual(plane_day.cancelled_note([AER66], [AER66]), '')
        self.assertEqual(plane_day.cancelled_note([], []), '')

    def test_a_landed_flight_flagged_cancelled_is_not_a_scrub(self):
        odd = dict(SRY1801, cancelled=True, actual_on='2026-09-03T19:30:00Z')
        self.assertEqual(plane_day.cancelled_note([odd, AER66], [AER66]), '')



GUN5SR = {"ident": "GUN5SR", "operator": "GUN", "operator_name": "Grant Aviation",
          "origin": "PADU", "when": "2026-09-03T23:18:47+00:00",
          "actual_off": "2026-09-03T22:24:20Z", "actual_on": None,
          "cancelled": False, "diverted": False}
LANDED = {"verdict": "landed", "landed_at": "2026-09-03T15:09:54.035613",
          "departed_at": "2026-09-03T15:27:59.150963", "missed": 0,
          "missed_at": [], "seen_as": "N55SR"}


class TestAntenna(unittest.TestCase):
    def test_clock_of(self):
        self.assertEqual(plane_day.clock_of("2026-09-03T15:09:54.035613"), "3:09 PM")
        self.assertEqual(plane_day.clock_of("2026-09-03T00:05:00"), "12:05 AM")
        self.assertIsNone(plane_day.clock_of(None))
        self.assertIsNone(plane_day.clock_of("soon"))

    def test_landing_seen_by_the_antenna_is_reported(self):
        calls = []
        def fn(ident, airport, day):
            calls.append((ident, airport, day)); return LANDED
        seen = plane_day.antenna_landing(GUN5SR, 'PAPB', now=at('23:40'), verdict_fn=fn)
        self.assertEqual(seen['landed_at'], LANDED['landed_at'])
        self.assertEqual(seen['departed_at'], LANDED['departed_at'])
        self.assertEqual(seen['seen_as'], 'N55SR')
        self.assertEqual(calls, [('GUN5SR', 'PAPB', '2026-09-03')])

    def test_no_verdict_or_not_landed_is_none(self):
        self.assertIsNone(plane_day.antenna_landing(GUN5SR, 'PAPB', now=at('23:40'),
                                                    verdict_fn=lambda *a: None))
        missed = dict(LANDED, verdict='missed', landed_at=None)
        self.assertIsNone(plane_day.antenna_landing(GUN5SR, 'PAPB', now=at('23:40'),
                                                    verdict_fn=lambda *a: missed))

    def test_antenna_failure_is_silence_not_a_landing(self):
        def boom(*a):
            raise RuntimeError('db locked')
        self.assertIsNone(plane_day.antenna_landing(GUN5SR, 'PAPB', now=at('23:40'),
                                                    verdict_fn=boom))

    def test_an_earlier_trips_landing_is_not_this_flights(self):
        # AER66 lands at PASN in the morning; the afternoon makeup run is
        # a different trip -- a 9 AM landing must not close a 6 PM ETA.
        afternoon = dict(SRY1801, ident='AER66', operator='AER',
                         when='2026-09-04T02:44:23+00:00')
        morning = dict(LANDED, landed_at='2026-09-03T09:05:00', departed_at=None,
                       seen_as=None)
        self.assertIsNone(plane_day.antenna_landing(afternoon, 'PASN', now=at('23:40'),
                                                    verdict_fn=lambda *a: morning))
        near = dict(morning, landed_at='2026-09-03T17:30:00')
        self.assertIsNotNone(plane_day.antenna_landing(afternoon, 'PASN', now=at('23:40'),
                                                       verdict_fn=lambda *a: near))

    def test_radar_match_by_prefix_and_by_alias(self):
        self.assertTrue(plane_day.radar_match('SRY1801', SRY1801))
        self.assertTrue(plane_day.radar_match('SRY1803 ', SRY1801))
        self.assertFalse(plane_day.radar_match('AER66', SRY1801))
        self.assertTrue(plane_day.radar_match('GUN5SR', GUN5SR))
        self.assertTrue(plane_day.radar_match('N55SR', GUN5SR))
        self.assertFalse(plane_day.radar_match('N24GN', GUN5SR))
        self.assertFalse(plane_day.radar_match('', GUN5SR))


# The departure clock. Real filings of 2026-11-22, the day the owner raised it:
# SRY1801 due off Anchorage 18:40Z (9:40 AM AKST), never left (cancelled for
# weather, which FlightAware had not posted by 3 PM). AER66 left Bethel, which
# our antenna cannot hear, at 22:31Z.
SRY1801_1122 = {
    "ident": "SRY1801", "operator": "SRY", "operator_name": "Aleutian Airways",
    "origin": "PANC", "destination": "PASN", "aircraft_type": "SB20",
    "scheduled_off": "2026-11-22T18:40:00Z", "estimated_off": "2026-11-22T18:40:00Z",
    "actual_off": None, "when": "2026-11-22T20:40:00+00:00",
    "estimated_on": "2026-11-22T20:40:00Z", "actual_on": None,
    "cancelled": False, "diverted": False,
}
AER66_1122 = {
    "ident": "AER66", "operator": "AER", "origin": "PABE", "destination": "PASN",
    "scheduled_off": "2026-11-22T22:00:00Z", "estimated_off": "2026-11-22T22:00:00Z",
    "actual_off": None, "when": "2026-11-23T00:05:59+00:00",
    "cancelled": False, "diverted": False,
}


def nov22(hhmm_utc):
    h, m = hhmm_utc.split(':')
    return datetime(2026, 11, 22, int(h), int(m), tzinfo=timezone.utc)


def watch_file(written):
    return {'day_key': '2026-11-22', 'generated': written.isoformat(), 'departures': {}}


class TestDepartureClock(unittest.TestCase):
    def test_the_real_day_is_flagged_an_hour_after_departure(self):
        # 19:40Z = 10:40 AM AKST, two and a half hours before the ETA rule.
        self.assertTrue(plane_day.not_departed(SRY1801_1122, watching=True, now=nov22('19:40')))
        self.assertFalse(plane_day.is_overdue(SRY1801_1122, now=nov22('19:40')))

    def test_inside_the_grace_hour_is_not_flagged(self):
        self.assertFalse(plane_day.not_departed(SRY1801_1122, watching=True, now=nov22('19:39')))

    def test_a_posted_delay_moves_the_clock(self):
        late = dict(SRY1801_1122, estimated_off='2026-11-22T20:30:00Z')
        self.assertFalse(plane_day.not_departed(late, watching=True, now=nov22('20:00')))
        self.assertTrue(plane_day.not_departed(late, watching=True, now=nov22('21:30')))

    def test_falls_back_to_the_schedule(self):
        bare = dict(SRY1801_1122, estimated_off=None)
        self.assertTrue(plane_day.not_departed(bare, watching=True, now=nov22('19:40')))

    def test_heard_or_airborne_or_landed_is_not_held(self):
        now = nov22('20:00')
        self.assertFalse(plane_day.not_departed(SRY1801_1122, heard={'seen_display': '9:52 AM'},
                                                watching=True, now=now))
        for key, value in (('actual_off', '2026-11-22T18:55:00Z'),
                           ('actual_on', '2026-11-22T20:41:00Z'),
                           ('cancelled', True), ('diverted', True)):
            with self.subTest(key=key):
                self.assertFalse(plane_day.not_departed(dict(SRY1801_1122, **{key: value}),
                                                        watching=True, now=now))

    def test_an_origin_our_antenna_cannot_hear_is_never_held(self):
        # AER66 from Bethel: silence from Anchorage means nothing there.
        self.assertFalse(plane_day.not_departed(AER66_1122, watching=True, now=nov22('23:30')))

    def test_a_stopped_watch_proves_nothing(self):
        self.assertFalse(plane_day.not_departed(SRY1801_1122, watching=False, now=nov22('20:00')))

    def test_no_departure_time_is_never_a_guess(self):
        bare = dict(SRY1801_1122, estimated_off=None, scheduled_off=None)
        self.assertFalse(plane_day.not_departed(bare, watching=True, now=nov22('23:00')))
        garbage = dict(SRY1801_1122, estimated_off='soon')
        self.assertFalse(plane_day.not_departed(garbage, watching=True, now=nov22('23:00')))

    def test_watch_freshness(self):
        now = nov22('19:40')
        self.assertTrue(plane_day.watch_is_fresh(watch_file(now - timedelta(minutes=3)), now=now))
        self.assertFalse(plane_day.watch_is_fresh(watch_file(now - timedelta(minutes=11)), now=now))
        self.assertFalse(plane_day.watch_is_fresh({}, now=now))
        self.assertFalse(plane_day.watch_is_fresh(None, now=now))
        self.assertFalse(plane_day.watch_is_fresh({'generated': 'yesterday'}, now=now))
        # A local-time stamp with its offset, the way the watch writes it.
        self.assertTrue(plane_day.watch_is_fresh(
            {'generated': '2026-11-22T10:38:01.8-09:00'}, now=now))


if __name__ == '__main__':
    unittest.main()
