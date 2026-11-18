#!/usr/bin/env python3
"""Tests for flight_legs.fold_legs -- one trip, several FlightAware legs.

Fixture: fa_fixtures/aer66_fuel_stop_20260829.json is the real record of
2026-08-29 at PASN. AER66 flies PANC -> fuel stop (Bethel or Cold Bay) ->
PASN -> PAPB as ONE trip, but FlightAware filed the last hop as a separate
`-sw-` leg from PABE. The PANC filing then scored "superseded" and the
ledger said a flight from Bethel came in, when everyone on the island
knows it came from Anchorage.
"""

import copy
import json
import os
import unittest

import flight_legs
import flight_arrival_record as far

FIXTURE = os.path.join(os.path.dirname(__file__), 'fa_fixtures',
                       'aer66_fuel_stop_20260829.json')


def day_entries():
    with open(FIXTURE) as fh:
        return copy.deepcopy(json.load(fh)['pasn_2026-08-29'])


class TestFoldLegs(unittest.TestCase):
    def test_fuel_stop_leg_folds_into_the_anchorage_filing(self):
        folded = flight_legs.fold_legs(day_entries())
        self.assertEqual(len(folded), 1)
        f = folded[0]
        self.assertEqual(f['origin'], 'PANC')
        self.assertEqual(f['origin_city'], 'Anchorage')
        self.assertEqual(f['via'], ['PABE'])
        self.assertEqual(f['via_city'], ['Bethel'])
        # The trunk keeps its identity; the leg's landing becomes the trip's.
        self.assertEqual(f['id'], 'AER66-1787817749-airline-1059p')
        self.assertEqual(f['legs'], ['AER66-1788055599-sw-338p'])
        self.assertEqual(f['actual_on'], '2026-08-30T06:14:00Z')
        self.assertEqual(f['when'], '2026-08-30T06:14:00+00:00')
        self.assertEqual(f['adsb']['verdict'], 'landed')

    def test_fold_is_idempotent(self):
        once = flight_legs.fold_legs(day_entries())
        twice = flight_legs.fold_legs(copy.deepcopy(once))
        self.assertEqual(twice, once)

    def test_leg_reappearing_from_feed_folds_again(self):
        # The collector re-presents the PABE leg under its own id every
        # run; folding it into the already-folded trunk must not
        # duplicate the via or the legs audit list.
        entries = day_entries()
        leg = copy.deepcopy(entries[1])
        folded = flight_legs.fold_legs(entries)
        again = flight_legs.fold_legs(folded + [leg])
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]['via'], ['PABE'])
        self.assertEqual(again[0]['legs'], ['AER66-1788055599-sw-338p'])

    def test_leg_reappearing_with_newer_state_updates_the_trip(self):
        # Folded while the leg was still airborne; next run the collector
        # re-presents the same leg id, now landed. The trip must land.
        entries = day_entries()
        landed_leg = copy.deepcopy(entries[1])
        entries[1]['actual_on'] = None
        entries[1].pop('adsb')
        folded = flight_legs.fold_legs(entries)
        self.assertIsNone(folded[0]['actual_on'])
        again = flight_legs.fold_legs(folded + [landed_leg])
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]['actual_on'], '2026-08-30T06:14:00Z')
        self.assertEqual(again[0]['legs'], ['AER66-1788055599-sw-338p'])

    def test_same_origin_refile_is_left_for_supersede_logic(self):
        # SRY1803 2026-08-19: delayed, re-filed from PANC under a new id.
        # Not a leg of a trip -- that is day_outcomes' 'superseded' case.
        a = {'id': 'SRY1803-1-airline', 'ident': 'SRY1803', 'origin': 'PANC',
             'origin_city': 'Anchorage', 'when': '2026-08-19T17:25:00+00:00',
             'actual_on': None, 'cancelled': True, 'diverted': False}
        b = {'id': 'SRY1803-2-sw', 'ident': 'SRY1803', 'origin': 'PANC',
             'origin_city': 'Anchorage', 'when': '2026-08-19T21:14:00+00:00',
             'actual_on': '2026-08-20T01:00:00Z', 'cancelled': False,
             'diverted': False}
        self.assertEqual(flight_legs.fold_legs([a, b]), [a, b])

    def test_trunk_that_already_landed_is_a_different_flight(self):
        a = {'id': 'AER66-1', 'ident': 'AER66', 'origin': 'PANC',
             'origin_city': 'Anchorage', 'when': '2026-08-29T20:00:00+00:00',
             'actual_on': '2026-08-29T20:05:00Z', 'cancelled': False,
             'diverted': False}
        b = {'id': 'AER66-2', 'ident': 'AER66', 'origin': 'PACD',
             'origin_city': 'Cold Bay', 'when': '2026-08-30T04:00:00+00:00',
             'actual_on': None, 'cancelled': False, 'diverted': False}
        self.assertEqual(flight_legs.fold_legs([a, b]), [a, b])

    def test_different_idents_never_fold(self):
        a = {'id': 'AER66-1', 'ident': 'AER66', 'origin': 'PANC',
             'when': '2026-08-29T20:00:00+00:00', 'actual_on': None}
        b = {'id': 'SVX52-1', 'ident': 'SVX52', 'origin': 'PACD',
             'when': '2026-08-30T04:00:00+00:00', 'actual_on': None}
        self.assertEqual(flight_legs.fold_legs([a, b]), [a, b])

    def test_collector_shape_leg_wins_status_and_times(self):
        # flight_plans_collect records: the leg's phase/ETA tell the story
        # but the PANC departure times stay with the trunk.
        trunk = {'fa_flight_id': 'AER66-1', 'ident': 'AER66',
                 'direction': 'arrival', 'origin': 'PANC',
                 'origin_city': 'Anchorage', 'destination': 'PASN',
                 'when': '2026-08-30T02:44:42+00:00', 'phase': 'scheduled',
                 'scheduled_off': '2026-08-30T00:30:00Z',
                 'actual_off': '2026-08-30T02:37:00Z',
                 'estimated_on': '2026-08-30T02:44:42+00:00',
                 'actual_on': None, 'cancelled': False, 'diverted': False}
        leg = {'fa_flight_id': 'AER66-2-sw', 'ident': 'AER66',
               'direction': 'arrival', 'origin': 'PABE',
               'origin_city': 'Bethel', 'destination': 'PASN',
               'when': '2026-08-30T06:00:00+00:00', 'phase': 'airborne',
               'scheduled_off': None, 'actual_off': '2026-08-30T04:50:00Z',
               'estimated_on': '2026-08-30T06:00:00+00:00',
               'actual_on': None, 'cancelled': False, 'diverted': False}
        folded = flight_legs.fold_legs([trunk, leg])
        self.assertEqual(len(folded), 1)
        f = folded[0]
        self.assertEqual(f['fa_flight_id'], 'AER66-1')
        self.assertEqual(f['legs'], ['AER66-2-sw'])
        self.assertEqual(f['phase'], 'airborne')
        self.assertEqual(f['estimated_on'], '2026-08-30T06:00:00+00:00')
        self.assertEqual(f['actual_off'], '2026-08-30T02:37:00Z')
        self.assertEqual(f['via_city'], ['Bethel'])

    def test_departures_are_not_folded_with_arrivals(self):
        arr = {'fa_flight_id': 'AER66-1', 'ident': 'AER66',
               'direction': 'arrival', 'origin': 'PANC',
               'when': '2026-08-30T02:44:42+00:00', 'actual_on': None}
        dep = {'fa_flight_id': 'AER66-3', 'ident': 'AER66',
               'direction': 'departure', 'origin': 'PASN',
               'when': '2026-08-30T06:40:00+00:00', 'actual_on': None}
        self.assertEqual(flight_legs.fold_legs([arr, dep]), [arr, dep])


class TestLedgerDay(unittest.TestCase):
    def test_last_night_scores_one_landed_flight_from_anchorage(self):
        days = {'2026-08-29': day_entries()}
        far.fold_days(days)
        outcomes = far.day_outcomes(days['2026-08-29'], '2026-08-29',
                                    '2026-08-30')
        self.assertEqual(outcomes, ['landed'])
        self.assertEqual(days['2026-08-29'][0]['origin_city'], 'Anchorage')
        self.assertEqual(days['2026-08-29'][0]['via_city'], ['Bethel'])




class TestStatusMessages(unittest.TestCase):
    """aviation_status.json is public; no address may reach it."""

    def test_addresses_are_removed(self):
        import run
        text = run.public('adsb_coverage_collect.py exited 1: FAILED: could not reach '
                          'http://192.0.2.7:24556/cone: timed out')
        self.assertNotIn('192.0.2.7', text)
        self.assertIn('could not reach (address hidden) timed out', text)


if __name__ == '__main__':
    unittest.main()
