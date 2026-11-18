#!/usr/bin/env python3
"""Tests for flight_plans_collect.carry_over -- flights the boards dropped.

FlightAware's arrival boards silently DROP a cancelled flight instead of
flagging it (the collector log has never once printed "(cancelled)"), so
the feed just lost the flight and the card/commentary never learned of the
scrub. On 2026-09-03 the owner had confirmation SRY1801 was cancelled while
FlightAware still said "Scheduled" at 2:35 PM; past 1801 scrubs flipped
between ~5 and ~7 PM. carry_over keeps today's dropped arrivals in the
feed and asks FlightAware about each one, so the feed shows CANCELLED
within a poll of FlightAware knowing -- no hand input.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
import flight_plans_collect as fpc  # noqa: E402
import flight_legs  # noqa: E402

TODAY = '2026-09-03'

SRY1801 = {
    "fa_flight_id": "SRY1801-1788238978-airline-2006p", "ident": "SRY1801",
    "operator": "SRY", "operator_name": "Aleutian Airways", "registration": None,
    "direction": "arrival", "origin": "PANC", "origin_city": "Anchorage",
    "destination": "PASN", "destination_city": "St Paul Island",
    "aircraft_type": "SB20", "route": "ANC SNP", "filed_altitude": 340,
    "filed_ete": 6180, "when": "2026-09-03T19:23:15+00:00",
    "scheduled_off": "2026-09-03T17:40:00Z", "estimated_off": "2026-09-03T17:40:00Z",
    "actual_off": None, "estimated_on": "2026-09-03T19:23:15Z", "actual_on": None,
    "phase": "scheduled", "cancelled": False, "diverted": False,
}
AER66 = {
    "fa_flight_id": "AER66-1788249717-airline-606p", "ident": "AER66",
    "operator": "AER", "operator_name": "Ace Air Cargo", "registration": None,
    "direction": "arrival", "origin": "PANC", "origin_city": "Anchorage",
    "destination": "PASN", "destination_city": "St Paul Island",
    "aircraft_type": "B190", "route": "ANC SNP", "filed_altitude": 200,
    "filed_ete": 11040, "when": "2026-09-04T02:44:23+00:00",
    "scheduled_off": "2026-09-03T23:40:00Z", "estimated_off": "2026-09-03T23:40:00Z",
    "actual_off": None, "estimated_on": "2026-09-04T02:44:23Z", "actual_on": None,
    "phase": "scheduled", "cancelled": False, "diverted": False,
}


def fa_state(cancelled=False, diverted=False, actual_off=None, actual_on=None,
             dest='PASN'):
    return {'ident': 'SRY1801', 'destination': {'code': dest},
            'origin': {'code': 'PANC'}, 'cancelled': cancelled,
            'diverted': diverted, 'actual_off': actual_off, 'actual_on': actual_on}


def previous(*flights, day=TODAY, stale=False):
    return {'day_key': day, 'stale': stale,
            'flights': [copy.deepcopy(f) for f in flights]}


class Lookup:
    """A scripted /flights/{id} answer that counts its calls."""
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, fa_id):
        self.calls.append(fa_id)
        return self.answer


class TestCarryOver(unittest.TestCase):
    def test_dropped_flight_flightaware_calls_cancelled_is_kept_as_cancelled(self):
        lookup = Lookup([fa_state(cancelled=True)])
        out = fpc.carry_over(previous(SRY1801, AER66), [copy.deepcopy(AER66)],
                             TODAY, lookup)
        self.assertEqual([f['ident'] for f in out], ['SRY1801', 'AER66'])
        sry = out[0]
        self.assertTrue(sry['cancelled'])
        self.assertEqual(sry['phase'], 'cancelled')
        self.assertTrue(sry['off_board'])
        self.assertEqual(sry['lookup_attempts'], 1)
        self.assertEqual(lookup.calls, [SRY1801['fa_flight_id']])

    def test_dropped_flight_flightaware_still_scheduled_is_kept_unresolved(self):
        # The 2:35 PM case: off the board, FA not yet admitting anything.
        lookup = Lookup([fa_state()])
        out = fpc.carry_over(previous(SRY1801), [], TODAY, lookup)
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]['cancelled'])
        self.assertEqual(out[0]['phase'], 'scheduled')
        self.assertTrue(out[0]['off_board'])
        self.assertEqual(out[0]['lookup_attempts'], 1)

    def test_lookup_answers_accumulate_attempts_and_stop_at_the_cap(self):
        f = copy.deepcopy(SRY1801)
        f['lookup_attempts'] = fpc.MAX_LOOKUP_ATTEMPTS
        lookup = Lookup([fa_state(cancelled=True)])
        out = fpc.carry_over(previous(f), [], TODAY, lookup)
        self.assertEqual(lookup.calls, [])          # no more spending
        self.assertFalse(out[0]['cancelled'])       # and no guessing
        self.assertTrue(out[0]['off_board'])

    def test_transient_lookup_failure_costs_no_attempt(self):
        lookup = Lookup(None)
        out = fpc.carry_over(previous(SRY1801), [], TODAY, lookup)
        self.assertEqual(out[0].get('lookup_attempts', 0), 0)
        self.assertTrue(out[0]['off_board'])

    def test_lookup_takes_the_destination_matched_record_only(self):
        # After a divert FA returns two records; the PANC->PANC one says
        # landed and must not resolve a PASN arrival as landed.
        states = [fa_state(actual_on='2026-09-03T21:00:00Z', dest='PANC'),
                  fa_state(cancelled=True, diverted=True)]
        out = fpc.carry_over(previous(SRY1801), [], TODAY, Lookup(states))
        self.assertIsNone(out[0]['actual_on'])
        self.assertTrue(out[0]['diverted'])
        self.assertTrue(out[0]['cancelled'])

    def test_airborne_per_lookup_becomes_airborne(self):
        states = [fa_state(actual_off='2026-09-03T22:10:00Z')]
        out = fpc.carry_over(previous(SRY1801), [], TODAY, Lookup(states))
        self.assertEqual(out[0]['phase'], 'airborne')
        self.assertEqual(out[0]['actual_off'], '2026-09-03T22:10:00Z')

    def test_refile_under_a_new_id_is_not_carried(self):
        # Same ident, same origin, new fa_flight_id: superseded, the new
        # filing is the story (SRY1803, 2026-08-19).
        refiled = copy.deepcopy(SRY1801)
        refiled['fa_flight_id'] = 'SRY1801-1788250000-airline-2007p'
        refiled['when'] = '2026-09-03T23:30:00+00:00'
        lookup = Lookup([fa_state(cancelled=True)])
        out = fpc.carry_over(previous(SRY1801), [refiled], TODAY, lookup)
        self.assertEqual([f['fa_flight_id'] for f in out], [refiled['fa_flight_id']])
        self.assertEqual(lookup.calls, [])

    def test_trunk_is_carried_so_the_fuel_stop_leg_can_fold(self):
        # AER66 PANC filing vanishes once FA files the post-Bethel leg;
        # carrying the trunk lets fold_legs keep "Anchorage via Bethel".
        trunk = copy.deepcopy(AER66)
        leg = copy.deepcopy(AER66)
        leg['fa_flight_id'] = 'AER66-1788249717-sw-1'
        leg['origin'], leg['origin_city'] = 'PABE', 'Bethel'
        leg['when'] = '2026-09-04T03:10:00+00:00'
        leg['actual_off'] = '2026-09-04T01:30:00Z'
        leg['phase'] = 'airborne'
        lookup = Lookup([fa_state()])
        out = flight_legs.fold_legs(fpc.carry_over(previous(trunk), [leg], TODAY, lookup))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['fa_flight_id'], AER66['fa_flight_id'])
        self.assertEqual(out[0]['origin'], 'PANC')
        self.assertEqual(out[0]['via'], ['PABE'])
        self.assertEqual(out[0]['phase'], 'airborne')
        self.assertFalse(fpc.mark_folded_on_board(out, {leg['fa_flight_id']})[0]['off_board'])

    def test_landed_flight_dropped_off_the_board_is_kept_without_a_lookup(self):
        landed = copy.deepcopy(SRY1801)
        landed['actual_on'] = '2026-09-03T19:30:00Z'
        landed['phase'] = 'landed'
        lookup = Lookup([fa_state(cancelled=True)])
        out = fpc.carry_over(previous(landed), [], TODAY, lookup)
        self.assertEqual(out[0]['phase'], 'landed')
        self.assertEqual(lookup.calls, [])

    def test_already_cancelled_carry_needs_no_lookup(self):
        gone = copy.deepcopy(SRY1801)
        gone['cancelled'], gone['phase'] = True, 'cancelled'
        lookup = Lookup([fa_state(cancelled=True)])
        out = fpc.carry_over(previous(gone), [], TODAY, lookup)
        self.assertTrue(out[0]['cancelled'])
        self.assertEqual(lookup.calls, [])

    def test_yesterdays_or_stale_feed_carries_nothing(self):
        lookup = Lookup([fa_state(cancelled=True)])
        self.assertEqual(fpc.carry_over(previous(SRY1801, day='2026-09-02'), [], TODAY, lookup), [])
        self.assertEqual(fpc.carry_over(previous(SRY1801, stale=True), [], TODAY, lookup), [])
        self.assertEqual(fpc.carry_over(None, [], TODAY, lookup), [])
        self.assertEqual(lookup.calls, [])

    def test_no_lookup_function_still_carries_the_flight(self):
        out = fpc.carry_over(previous(SRY1801), [], TODAY, None)
        self.assertEqual(out[0]['ident'], 'SRY1801')
        self.assertTrue(out[0]['off_board'])

    def test_output_is_sorted_by_when(self):
        out = fpc.carry_over(previous(AER66, SRY1801), [], TODAY, Lookup([fa_state()]))
        self.assertEqual([f['ident'] for f in out], ['SRY1801', 'AER66'])


if __name__ == '__main__':
    unittest.main()
