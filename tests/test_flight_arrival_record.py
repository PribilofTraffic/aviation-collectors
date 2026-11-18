#!/usr/bin/env python3
"""Tests for flight_arrival_record divert resolution.

Fixture: fa_fixtures/sry1801_diverted_20260820.json is the real AeroAPI
/flights/{fa_flight_id} response captured 2026-08-20, the day SRY1801
turned back to Anchorage. It shows the two-records-per-fa_flight_id shape
a diversion produces: the leg actually flown (PANC->PANC, landed, NOT
diverted) and the abandoned plan (PANC->PASN, diverted AND cancelled).
The destination-matched record is the one that tells the truth; the other
would score a St. Paul no-show as "landed".
"""

import json
import os
import unittest

import flight_arrival_record as far

FIXTURE = os.path.join(os.path.dirname(__file__), 'fa_fixtures',
                       'sry1801_diverted_20260820.json')

FA_ID = 'SRY1801-1787039478-airline-397p'


def fixture_states():
    with open(FIXTURE) as fh:
        return json.load(fh)['flights']


def unresolved_entry():
    """SRY1801 as the record actually held it after the 4:47 PM poll --
    airborne, then it vanished from the boards."""
    return {
        'id': FA_ID,
        'ident': 'SRY1801',
        'operator': 'SRY',
        'operator_name': 'Aleutian Airways',
        'origin': 'PANC',
        'when': '2026-08-21T01:35:27+00:00',
        'actual_on': None,
        'cancelled': False,
        'diverted': False,
    }


class TestOutcomeOrdering(unittest.TestCase):
    def test_diverted_beats_cancelled(self):
        # A divert is filed as diverted AND cancelled (see fixture); the
        # story is the divert, not a scrub that never launched.
        flight = {'actual_on': None, 'cancelled': True, 'diverted': True}
        self.assertEqual(far.outcome_of(flight, '2026-08-20', '2026-08-21'),
                         'diverted')


class TestSupersededRefilings(unittest.TestCase):
    """Divert lookups resolve abandoned re-filings as 'cancelled' (FA marks
    them so), which must stay a re-filing artifact -- not a real scrub that
    drags the day's percentage down when the plane actually landed."""

    def test_cancelled_refile_artifact_superseded_by_landing(self):
        # The real 2026-08-19 shape: SRY1803 filed, abandoned (FA:
        # cancelled), re-filed under a new fa_flight_id, landed.
        flights = [
            {'ident': 'SRY1803', 'cancelled': True},
            {'ident': 'SRY1803', 'actual_on': '2026-08-19T21:07:00Z'},
        ]
        self.assertEqual(
            far.day_outcomes(flights, '2026-08-19', '2026-08-21'),
            ['superseded', 'landed'])

    def test_lone_cancellation_still_counts(self):
        flights = [{'ident': 'SRY1801', 'cancelled': True}]
        self.assertEqual(
            far.day_outcomes(flights, '2026-08-19', '2026-08-21'),
            ['cancelled'])

    def test_double_cancellation_collapses_to_one(self):
        flights = [
            {'ident': 'SRY1803', 'cancelled': True},
            {'ident': 'SRY1803', 'cancelled': True},
        ]
        self.assertEqual(
            far.day_outcomes(flights, '2026-08-19', '2026-08-21'),
            ['cancelled', 'superseded'])

    def test_divert_counts_even_beside_a_later_landing(self):
        # A divert really flew and turned back -- a genuine miss, even if
        # a second attempt made it in the same day.
        flights = [
            {'ident': 'SRY1801', 'diverted': True, 'cancelled': True},
            {'ident': 'SRY1801', 'actual_on': '2026-08-21T03:00:00Z'},
        ]
        self.assertEqual(
            far.day_outcomes(flights, '2026-08-20', '2026-08-21'),
            ['diverted', 'landed'])


class TestResolveVanished(unittest.TestCase):
    def test_vanished_today_resolves_to_diverted(self):
        entry = unresolved_entry()
        days = {'2026-08-20': [entry]}
        calls = []

        def fetch(fa_id):
            calls.append(fa_id)
            return fixture_states()

        far.resolve_vanished(days, 'PASN', today='2026-08-20',
                             present_ids=set(), fetch_states=fetch)
        self.assertEqual(calls, [FA_ID])
        self.assertTrue(entry['diverted'])
        self.assertIsNone(entry['actual_on'])
        self.assertEqual(
            far.outcome_of(entry, '2026-08-20', '2026-08-20'), 'diverted')

    def test_past_day_unresolved_gets_lookup(self):
        entry = unresolved_entry()
        days = {'2026-08-20': [entry]}
        far.resolve_vanished(days, 'PASN', today='2026-08-21',
                             present_ids=None,
                             fetch_states=lambda _id: fixture_states())
        self.assertTrue(entry['diverted'])

    def test_still_listed_today_not_looked_up(self):
        entry = unresolved_entry()
        days = {'2026-08-20': [entry]}

        def fetch(_id):
            raise AssertionError('should not look up a flight still on the board')

        far.resolve_vanished(days, 'PASN', today='2026-08-20',
                             present_ids={FA_ID}, fetch_states=fetch)
        self.assertFalse(entry['diverted'])

    def test_resolved_flight_not_looked_up(self):
        entry = unresolved_entry()
        entry['actual_on'] = '2026-08-21T01:35:27Z'
        days = {'2026-08-20': [entry]}

        def fetch(_id):
            raise AssertionError('should not look up a landed flight')

        far.resolve_vanished(days, 'PASN', today='2026-08-21',
                             present_ids=None, fetch_states=fetch)

    def test_attempts_capped_at_three(self):
        # A lookup that answers but never resolves (only the flown-leg
        # record, wrong destination) must not be retried forever.
        entry = unresolved_entry()
        days = {'2026-08-20': [entry]}
        flown_leg_only = [s for s in fixture_states()
                          if s['destination']['code'] == 'PANC']
        calls = []

        def fetch(fa_id):
            calls.append(fa_id)
            return flown_leg_only

        for _ in range(5):
            far.resolve_vanished(days, 'PASN', today='2026-08-21',
                                 present_ids=None, fetch_states=fetch)
        self.assertEqual(len(calls), 3)
        self.assertFalse(entry['diverted'])

    def test_fetch_failure_does_not_burn_an_attempt(self):
        entry = unresolved_entry()
        days = {'2026-08-20': [entry]}

        def failing(_id):
            return None  # network/API error

        for _ in range(5):
            far.resolve_vanished(days, 'PASN', today='2026-08-21',
                                 present_ids=None, fetch_states=failing)
        self.assertEqual(entry.get('lookup_attempts', 0), 0)
        # ...and a later good answer still resolves it.
        far.resolve_vanished(days, 'PASN', today='2026-08-21',
                             present_ids=None,
                             fetch_states=lambda _id: fixture_states())
        self.assertTrue(entry['diverted'])



class TestBoardIds(unittest.TestCase):
    def test_carried_over_flight_is_not_on_the_board(self):
        flights = [
            {'fa_flight_id': 'A-1', 'direction': 'arrival'},
            {'fa_flight_id': 'B-2', 'direction': 'arrival', 'off_board': True},
            {'fa_flight_id': 'C-3', 'direction': 'departure'},
        ]
        self.assertEqual(far.board_ids(flights), {'A-1'})


if __name__ == '__main__':
    unittest.main()


class TestAdsbVerification(unittest.TestCase):
    """The antenna outranks FlightAware once a day is over. Shape of
    2026-08-20 (AER66): FA said PASN landed (phantom -sw- record) and PAPB
    cancelled; the antenna saw a missed approach at PASN and a landing at
    PAPB."""

    def verdicts(self, ident, airport, day):
        table = {
            ('AER66', 'PASN'): {'verdict': 'missed', 'missed': 1,
                                'missed_at': ['2026-08-20T18:19:45'],
                                'landed_at': None, 'departed_at': None},
            ('AER66', 'PAPB'): {'verdict': 'landed', 'missed': 1,
                                'missed_at': ['2026-08-20T18:43:51'],
                                'landed_at': '2026-08-20T18:56:55',
                                'departed_at': '2026-08-20T19:35:04'},
        }
        return table.get((ident, airport),
                         {'verdict': 'none', 'missed': 0, 'missed_at': [],
                          'landed_at': None, 'departed_at': None})

    def pasn_day(self):
        return [
            {'id': 'AER66-1787262867-sw-632p', 'ident': 'AER66',
             'origin': 'PABE', 'actual_on': '2026-08-21T00:43:00Z',
             'cancelled': False, 'diverted': False},
            {'id': 'AER66-1787038561-airline-2865p', 'ident': 'AER66',
             'origin': 'PANC', 'actual_on': None,
             'cancelled': False, 'diverted': False},
        ]

    def test_missed_approach_overrules_fa_landed(self):
        days = {'2026-08-20': self.pasn_day()}
        far.verify_with_adsb(days, 'PASN', '2026-08-21', self.verdicts)
        outcomes = far.day_outcomes(days['2026-08-20'], '2026-08-20',
                                    '2026-08-21')
        # The real filed plan carries the story; the phantom is superseded.
        self.assertEqual(outcomes, ['superseded', 'diverted'])
        real = days['2026-08-20'][1]
        self.assertEqual(real['adsb']['verdict'], 'missed')
        self.assertEqual(real['adsb']['diverted_to'], 'PAPB')

    def test_landing_overrules_fa_cancelled(self):
        days = {'2026-08-20': [
            {'id': 'AER66-1787038561-airline-2866p', 'ident': 'AER66',
             'origin': 'PASN', 'actual_on': None,
             'cancelled': True, 'diverted': False}]}
        far.verify_with_adsb(days, 'PAPB', '2026-08-21', self.verdicts)
        self.assertEqual(
            far.day_outcomes(days['2026-08-20'], '2026-08-20', '2026-08-21'),
            ['landed'])
        self.assertEqual(days['2026-08-20'][0]['adsb']['landed_at'],
                         '2026-08-20T18:56:55')

    def test_today_is_left_alone(self):
        days = {'2026-08-21': self.pasn_day()}
        far.verify_with_adsb(days, 'PASN', '2026-08-21', self.verdicts)
        self.assertNotIn('adsb', days['2026-08-21'][0])
        self.assertNotIn('adsb', days['2026-08-21'][1])

    def test_no_opinion_keeps_fa_verdict(self):
        days = {'2026-08-20': [
            {'id': 'SRY1801-x-airline-1p', 'ident': 'SRY1801',
             'actual_on': '2026-08-21T01:30:00Z',
             'cancelled': False, 'diverted': False}]}
        far.verify_with_adsb(days, 'PASN', '2026-08-21', self.verdicts)
        self.assertEqual(
            far.day_outcomes(days['2026-08-20'], '2026-08-20', '2026-08-21'),
            ['landed'])

    def test_antenna_dead_is_not_checked_again(self):
        calls = []
        def dead(ident, airport, day):
            calls.append(ident)
            return None
        days = {'2026-08-20': self.pasn_day()}
        far.verify_with_adsb(days, 'PASN', '2026-08-21', dead)
        far.verify_with_adsb(days, 'PASN', '2026-08-21', dead)
        self.assertNotIn('adsb', days['2026-08-20'][1])
        self.assertEqual(len(calls), 2)  # one probe per run, never per flight

    def test_checked_once(self):
        calls = []
        def counting(ident, airport, day):
            calls.append((ident, airport))
            return self.verdicts(ident, airport, day)
        days = {'2026-08-20': self.pasn_day()}
        far.verify_with_adsb(days, 'PASN', '2026-08-21', counting)
        far.verify_with_adsb(days, 'PASN', '2026-08-21', counting)
        # One ident: this airport plus the other island, once.
        self.assertEqual(sorted(calls), [('AER66', 'PAPB'), ('AER66', 'PASN')])


class TestExcludedIdents(unittest.TestCase):
    """FLC* is the FAA flight-check bird (FLC91 here) working the ILS, not
    an airline -- it must never score in the ledger. The sweep runs over
    the whole stored history so entries recorded before the rule existed
    drop out too. N55SR is Grant Aviation and must NOT be stripped (the
    owner briefly took it for the check flight on 2026-08-25)."""

    def test_flight_check_bird_stripped_from_history(self):
        days = {'2026-08-25': [
            {'id': 'a', 'ident': 'FLC91', 'origin': 'PANC',
             'actual_on': None, 'cancelled': False, 'diverted': False},
            {'id': 'b', 'ident': 'AER66', 'origin': 'PANC',
             'actual_on': '2026-08-26T02:53:00Z',
             'cancelled': False, 'diverted': False},
            {'id': 'c', 'ident': 'N55SR', 'origin': 'PADU',
             'actual_on': '2026-08-25T18:53:00+00:00',
             'cancelled': False, 'diverted': False},
        ]}
        far.strip_excluded(days)
        self.assertEqual([f['ident'] for f in days['2026-08-25']],
                         ['AER66', 'N55SR'])


class TestOperatorNaming(unittest.TestCase):
    """Tail-number idents file with no operator code, so the collector's
    prefix mapping missed them and they scored as 'Unknown operator'.
    known_aircraft.json knows the tail (N55SR = Grant Aviation)."""

    def test_tail_number_named_from_known_aircraft(self):
        days = {'2026-08-25': [
            {'ident': 'N55SR', 'operator': '', 'operator_name': None},
            {'ident': 'AER66', 'operator': 'AER',
             'operator_name': 'Ace Air Cargo'},
        ]}
        far.fill_operator_names(days, {'AER': 'Ace Air Cargo',
                                       'N55SR': 'Grant Aviation'})
        self.assertEqual(days['2026-08-25'][0]['operator_name'],
                         'Grant Aviation')
        self.assertEqual(days['2026-08-25'][1]['operator_name'],
                         'Ace Air Cargo')

    def test_live_known_aircraft_carries_the_tail(self):
        # The real known_aircraft.json must resolve N55SR, or the sweep
        # is a no-op in production.
        from flight_plans_collect import load_operator_names
        self.assertEqual(load_operator_names().get('N55SR'),
                         'Grant Aviation')


class TestGrantBlock(unittest.TestCase):
    """The ledger carries Grant's schedule-vs-antenna score alongside the
    filed-flight score (grant_record); a failure there must not sink the
    record write."""

    SCHEDULE = {'as_of': '2026-08-31', 'airports': {
        'PASN': {'days': ['Thursday']}}}
    KNOWN = {'known_aircraft': {
        'N55SR': {'operator': 'Grant Aviation'}}}

    def antenna(self, ident, airport, day):
        if day == '2026-08-20':
            return {'verdict': 'landed', 'landed_at': day + 'T15:30:00',
                    'departed_at': None}
        return {'verdict': 'none', 'landed_at': None, 'departed_at': None}

    def test_scores_schedule_days_and_keeps_stored_verdicts(self):
        record = {'start_date': '2026-08-19', 'grant_days': {}}
        stored, summary = far.grant_block(
            record, {}, 'PASN', '2026-08-22', self.antenna,
            schedule=self.SCHEDULE, known=self.KNOWN)
        self.assertEqual(stored['2026-08-20']['antenna'], 'landed')
        self.assertEqual(summary['scheduled'], 1)
        self.assertEqual(summary['landed'], 1)
        self.assertEqual(summary['pct_landed'], 100)

    def test_a_failure_keeps_the_previous_verdicts_and_no_summary(self):
        def broken(ident, airport, day):
            raise RuntimeError('db locked')
        prior = {'2026-08-20': {'antenna': 'landed', 'tail': 'N55SR',
                                'landed_at': '2026-08-20T15:30:00',
                                'departed_at': None}}
        record = {'start_date': '2026-08-19', 'grant_days': prior}
        stored, summary = far.grant_block(
            record, {}, 'PASN', '2026-08-22', broken,
            schedule=self.SCHEDULE, known=self.KNOWN)
        self.assertEqual(stored, prior)
        self.assertIsNone(summary)
