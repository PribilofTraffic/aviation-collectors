#!/usr/bin/env python3
"""Tests for panc_departure_watch -- did the plane actually leave Anchorage?

Fixture: tar1090_fixtures/panc_aircraft_20260825.json is a real, unedited
/data/aircraft.json capture from the PANC receiver on 2026-08-25 at
22:25 UTC. It happens to hold one textbook departure (GTI742, an Atlas
747-8 climbing through 16,600 ft at 2,944 fpm, 24 nm out), one arrival
(ASA1434 descending at -960 fpm), and two records with no position at all
(ASA61, N541LM) -- the three cases the rule has to tell apart. The
scenario payloads below are built from those same real field shapes.

Why the rule is what it is: this collector replaced a PlaneFence feed
whose 2 nm / 3000 ft fence only caught a departure when its climb-out
happened to cross the antenna. tar1090 hears past 40 nm, so the whole
climb-out is in view -- but it is a live snapshot with no history, so a
sighting missed is a sighting gone.
"""

import json
import os
import unittest

import panc_departure_watch as w

FIXTURE = os.path.join(os.path.dirname(__file__), 'tar1090_fixtures',
                       'panc_aircraft_20260825.json')

with open(FIXTURE) as fh:
    LIVE = json.load(fh)

NOW = LIVE['now']                 # 1787696719.0
DAY_START = NOW - 8 * 3600        # some hours earlier the same day


def payload(*aircraft, now=NOW):
    return {'now': now, 'aircraft': list(aircraft)}


def plane(flight, **kw):
    """A tar1090 record with the fields the rule reads, real shapes."""
    rec = {'flight': flight, 'r': 'N811BB', 'hex': 'a1b2c3',
           'alt_baro': 4000, 'baro_rate': 1800, 'r_dst': 6.0,
           'track': 200.0, 'seen_pos': 1.0}
    rec.update(kw)
    return rec


class RealCapture(unittest.TestCase):
    def test_real_departure_is_matched(self):
        d = w.pick_departures(LIVE, {'GTI742'}, DAY_START)
        self.assertIn('GTI742', d)
        self.assertEqual(d['GTI742']['tail'], 'N859GT')
        self.assertEqual(d['GTI742']['altitude_ft'], 16600)

    def test_unwatched_idents_ignored(self):
        self.assertEqual(w.pick_departures(LIVE, {'SRY1801'}, DAY_START), {})

    def test_arrival_is_not_a_departure(self):
        """ASA1434 is descending at -960 fpm. It is landing, not leaving."""
        self.assertEqual(w.pick_departures(LIVE, {'ASA1434'}, DAY_START), {})

    def test_positionless_record_ignored(self):
        """ASA61 has no alt_baro, no r_dst, no seen_pos -- not evidence."""
        self.assertEqual(w.pick_departures(LIVE, {'ASA61'}, DAY_START), {})

    def test_callsign_whitespace_stripped(self):
        """tar1090 pads `flight` to 8 chars; idents from the feeds are bare.

        todays_idents() upper-cases what it collects, so the watch set is
        upper-case by contract -- only the padding has to be handled here.
        """
        raw = [a['flight'] for a in LIVE['aircraft']
               if (a.get('flight') or '').strip() == 'GTI742']
        self.assertEqual(raw, ['GTI742  '])
        self.assertIn('GTI742', w.pick_departures(LIVE, {'GTI742'}, DAY_START))


class DepartureRule(unittest.TestCase):
    def test_climbing_close_in_matches(self):
        d = w.pick_departures(payload(plane('SRY1801')), {'SRY1801'}, DAY_START)
        self.assertIn('SRY1801', d)

    def test_on_the_ground_is_not_yet_a_departure(self):
        p = payload(plane('SRY1801', alt_baro='ground', baro_rate=None))
        self.assertEqual(w.pick_departures(p, {'SRY1801'}, DAY_START), {})

    def test_beyond_range_ignored(self):
        p = payload(plane('SRY1801', r_dst=55.0))
        self.assertEqual(w.pick_departures(p, {'SRY1801'}, DAY_START), {})

    def test_level_flight_ignored(self):
        p = payload(plane('SRY1801', baro_rate=0, alt_baro=35000))
        self.assertEqual(w.pick_departures(p, {'SRY1801'}, DAY_START), {})

    def test_above_ceiling_rejected(self):
        """A cruising overflight step-climbing over Anchorage is not ours."""
        p = payload(plane('SRY1801', alt_baro=33000, baro_rate=500))
        self.assertEqual(w.pick_departures(p, {'SRY1801'}, DAY_START), {})


class NoVerticalRate(unittest.TestCase):
    """28% of records within 40 nm of PANC send no baro_rate at all, and they
    are exactly the fleet we watch -- BE20 King Airs, C441s, the Beech/Saab
    types. Dropping them is not an option, so the climb is read off the
    previous poll instead. Regression: on 2026-08-25 an `alt_baro < 10000`
    fallback logged TNV54 (a BE20) as "departing PANC 5:01 PM" while it was
    on short final into Anchorage -- it landed at 5:03.
    """

    def obs(self, alt, dist=6.0):
        return {'a1b2c3': {'alt': alt, 'dist': dist, 'seen': int(NOW - 120)}}

    def test_no_rate_and_no_prior_poll_defers(self):
        """One snapshot cannot tell a climb from a descent. Wait a poll."""
        p = payload(plane('SRY1801', baro_rate=None, alt_baro=4000))
        self.assertEqual(w.pick_departures(p, {'SRY1801'}, DAY_START), {})

    def test_no_rate_climbing_between_polls_matches(self):
        p = payload(plane('SRY1801', baro_rate=None, alt_baro=6000))
        d = w.pick_departures(p, {'SRY1801'}, DAY_START,
                              previous_obs=self.obs(3000))
        self.assertIn('SRY1801', d)

    def test_no_rate_descending_between_polls_rejected(self):
        """The TNV54 case: low, rate-less, and on the way DOWN."""
        p = payload(plane('TNV54', baro_rate=None, alt_baro=2000))
        d = w.pick_departures(p, {'TNV54'}, DAY_START,
                              previous_obs=self.obs(5000))
        self.assertEqual(d, {})

    def test_no_rate_level_between_polls_rejected(self):
        p = payload(plane('SRY1801', baro_rate=None, alt_baro=5100))
        d = w.pick_departures(p, {'SRY1801'}, DAY_START,
                              previous_obs=self.obs(5000))
        self.assertEqual(d, {})

    def test_prior_poll_is_matched_by_hex_not_callsign(self):
        """Callsigns blank out and change; the airframe id does not."""
        p = payload(plane('SRY1801', baro_rate=None, alt_baro=6000,
                          hex='deadbe'))
        d = w.pick_departures(p, {'SRY1801'}, DAY_START,
                              previous_obs=self.obs(3000))
        self.assertEqual(d, {})


class Observe(unittest.TestCase):
    """observe() produces the snapshot the next poll compares against."""

    def test_records_watched_aircraft_by_hex(self):
        o = w.observe(payload(plane('SRY1801', alt_baro=4000)), {'SRY1801'})
        self.assertEqual(o['a1b2c3']['alt'], 4000)

    def test_skips_unwatched(self):
        self.assertEqual(w.observe(payload(plane('UAL123')), {'SRY1801'}), {})

    def test_skips_positionless(self):
        p = payload(plane('SRY1801', seen_pos=None, r_dst=None, alt_baro=None))
        self.assertEqual(w.observe(p, {'SRY1801'}), {})

    def test_real_capture_tracks_the_climber(self):
        o = w.observe(LIVE, {'GTI742'})
        self.assertEqual(o['abc96f']['alt'], 16600)


class SightingRecord(unittest.TestCase):
    def test_seen_time_backs_out_seen_pos(self):
        """The observation time is now - seen_pos, not the poll time."""
        p = payload(plane('SRY1801', seen_pos=42.0), now=NOW)
        d = w.pick_departures(p, {'SRY1801'}, DAY_START)
        self.assertEqual(d['SRY1801']['seen'], int(NOW - 42.0))

    def test_track_rendered_as_compass_name(self):
        p = payload(plane('SRY1801', track=200.0))
        d = w.pick_departures(p, {'SRY1801'}, DAY_START)
        self.assertEqual(d['SRY1801']['track'], 'SSW')

    def test_contract_keys_unchanged(self):
        """js/flight_plans.js and both commentary scripts read these."""
        d = w.pick_departures(payload(plane('SRY1801')), {'SRY1801'}, DAY_START)
        self.assertEqual(set(d['SRY1801']),
                         {'seen', 'seen_display', 'tail', 'hex',
                          'altitude_ft', 'track'})


class EarliestWins(unittest.TestCase):
    def test_existing_earlier_sighting_kept(self):
        """A later pass is a divert coming home, not the departure."""
        existing = {'SRY1801': {'seen': int(NOW - 3600), 'seen_display': '1:08 PM',
                                'tail': 'N811BB', 'hex': 'a1b2c3',
                                'altitude_ft': 3000, 'track': 'S'}}
        d = w.pick_departures(payload(plane('SRY1801')), {'SRY1801'},
                              DAY_START, existing)
        self.assertEqual(d['SRY1801']['seen'], int(NOW - 3600))

    def test_earlier_sighting_replaces_later_one(self):
        existing = {'SRY1801': {'seen': int(NOW), 'seen_display': '2:25 PM',
                                'tail': 'N811BB', 'hex': 'a1b2c3',
                                'altitude_ft': 3000, 'track': 'S'}}
        d = w.pick_departures(payload(plane('SRY1801', seen_pos=600.0)),
                              {'SRY1801'}, DAY_START, existing)
        self.assertEqual(d['SRY1801']['seen'], int(NOW - 600))

    def test_sighting_before_today_ignored(self):
        p = payload(plane('SRY1801', seen_pos=9 * 3600.0))
        self.assertEqual(w.pick_departures(p, {'SRY1801'}, DAY_START), {})


class ConsecutivePollsRealCapture(unittest.TestCase):
    """tar1090_fixtures/panc_consecutive_polls_20260826.json: three real
    /data/aircraft.json snapshots 75 s apart, holding three rate-less
    turboprops -- none of them reporting baro_rate, so all three are
    decided purely on the poll-to-poll trend.

        N541LM  BE20  10375 -> 12750 -> 15200   climbing out    -> departure
        RVV35   BE20  15900 -> 16900 -> 16900   climb, then level
                                                (PANC->PAVD, off 5:48 PM,
                                                 confirmed by AeroAPI)
                                                                -> departure
        SVX41   C441   5100 ->  3875 ->  3225   descending      -> not one

    Yesterday's `alt_baro < 10000` fallback would have logged the descending
    SVX41 as a PANC departure on all three snapshots. RVV35 is the case that
    keeps the ceiling honest in the other direction: a real departure caught
    mid-climb that levels at 16,900 ft, well under MAX_ALT_FT.
    """

    def setUp(self):
        with open(os.path.join(os.path.dirname(__file__), 'tar1090_fixtures',
                               'panc_consecutive_polls_20260826.json')) as fh:
            self.polls = json.load(fh)
        self.start = self.polls[0]['now'] - 8 * 3600

    def run_polls(self, ident):
        """Replay the three snapshots the way the cron does."""
        departures, obs = {}, {}
        for p in self.polls:
            departures = w.pick_departures(p, {ident}, self.start,
                                           departures, obs)
            obs = w.observe(p, {ident})
        return departures

    def test_climbing_turboprop_is_a_departure(self):
        self.assertIn('N541LM', self.run_polls('N541LM'))

    def test_descending_turboprop_is_not(self):
        self.assertEqual(self.run_polls('SVX41'), {})

    def test_departure_that_levels_at_cruise_still_counts(self):
        """RVV35 gains 1,000 ft between the first two polls, then levels.

        Caught on the climb, so levelling afterwards changes nothing --
        earliest sighting wins and it is already recorded.
        """
        d = self.run_polls('RVV35')
        self.assertIn('RVV35', d)
        self.assertEqual(d['RVV35']['altitude_ft'], 16900)

    def test_climber_is_caught_on_the_second_poll_not_the_first(self):
        """The deferral costs one poll and no more."""
        first = w.pick_departures(self.polls[0], {'N541LM'}, self.start)
        self.assertEqual(first, {})
        obs = w.observe(self.polls[0], {'N541LM'})
        second = w.pick_departures(self.polls[1], {'N541LM'}, self.start,
                                   {}, obs)
        self.assertIn('N541LM', second)


class Compass(unittest.TestCase):
    def test_named_points(self):
        for deg, name in [(0, 'N'), (45, 'NE'), (90, 'E'), (180, 'S'),
                          (270, 'W'), (359, 'N'), (247.5, 'WSW')]:
            self.assertEqual(w.compass(deg), name, deg)

    def test_missing_track_is_none(self):
        self.assertIsNone(w.compass(None))


if __name__ == '__main__':
    unittest.main()
