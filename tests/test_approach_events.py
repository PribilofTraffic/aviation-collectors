#!/usr/bin/env python3
"""Tests for approach_events on the real 2026-08-20 tracks (see
test_adsb_ground_truth for the day's story)."""

import json
import os
import unittest
from datetime import datetime

import approach_events as ae

FIXTURE = os.path.join(os.path.dirname(__file__), 'fa_fixtures',
                       'adsb_aer66_sry1801_20260820.json')
SVX_FIXTURE = os.path.join(os.path.dirname(__file__), 'fa_fixtures',
                           'adsb_svx46_hold_pasn_20260903.json')
LATER = datetime(2026, 8, 21, 9, 0)
SVX_LATER = datetime(2026, 9, 4, 9, 0)


def track(ident, fixture=FIXTURE):
    with open(fixture) as fh:
        rows = json.load(fh)
    pts = [(datetime.fromisoformat(r[0]), r[3], r[4], r[5])
           for r in rows if r[2] == ident]
    hex_id = next(r[1] for r in rows if r[2] == ident)
    return pts, hex_id


class Aer66(unittest.TestCase):
    def setUp(self):
        pts, hx = track('AER66')
        self.events = ae.events_for_track(pts, 'AER66', hx, now=LATER)

    def kinds(self):
        return [(e['pattern_type'], e['airport']) for e in self.events]

    def test_story_in_order(self):
        self.assertEqual(self.kinds(), [
            ('go_around', 'PASN'),   # 6:19 PM, 700 ft
            ('hold', 'PASN'),        # 12 min at ~5,000 ft
            ('go_around', 'PAPB'),   # 6:43 PM, 700 ft
            ('hold', 'PAPB'),        # came back around for the second try
        ])

    def test_go_around_facts(self):
        ga = self.events[0]
        self.assertTrue(ga['detected_at'].startswith('2026-08-20T18:1'))
        self.assertIn('-08:00', ga['detected_at'])
        self.assertEqual(ga['min_altitude'], 700)
        self.assertEqual(ga['nearest_island'], 'St. Paul')
        self.assertEqual(ga['callsign'], 'AER66')
        self.assertTrue(ga['radar_url'].startswith('/papb1090/?icao=a03000'))

    def test_track_for_replay(self):
        ga = self.events[0]
        trk = ga['track']
        self.assertGreater(len(trk), 15)
        # Starts before the pass, ends after the climb-out, every point
        # near St. Paul, altitudes present, time strings carry the zone.
        self.assertLess(trk[0][0], ga['detected_at'])
        self.assertGreater(trk[-1][0], ga['last_seen'])
        self.assertTrue(all(abs(p[1] - 57.17) < 0.7 for p in trk))
        self.assertIn(700, [p[3] for p in trk])
        self.assertTrue(trk[0][0].endswith('-08:00'))

    def test_hold_facts(self):
        hold = self.events[1]
        self.assertGreaterEqual(hold['duration_min'], 10)
        self.assertGreaterEqual(hold['turn_deg'], 300)
        self.assertLessEqual(hold['max_altitude'], 5200)
        self.assertEqual(hold['source'], 'adsb_history')


class Sry1801(unittest.TestCase):
    def test_one_go_around_no_hold(self):
        pts, hx = track('SRY1801')
        events = ae.events_for_track(pts, 'SRY1801', hx, now=LATER)
        self.assertEqual([(e['pattern_type'], e['airport']) for e in events],
                         [('go_around', 'PASN')])
        self.assertTrue(events[0]['detected_at'].startswith('2026-08-20T15:0'))


class Svx46WideOrbit(unittest.TestCase):
    """2026-09-03: Security Aviation SVX46 came out from Anchorage, flew
    laps at 4,300 ft off St. Paul for 15 minutes (900+ deg of turn) and went
    home without landing. The orbit was centred ~13 nm from the field with a
    ~4 nm radius, so its far side crossed 15 nm on every lap -- the owner
    watched it circle and found nothing on /flightpatterns/ next morning."""

    def setUp(self):
        pts, hx = track('SVX46', SVX_FIXTURE)
        self.events = ae.events_for_track(pts, 'SVX46', hx, now=SVX_LATER)

    def test_orbit_straddling_the_radius_is_one_hold(self):
        self.assertEqual([(e['pattern_type'], e['airport']) for e in self.events],
                         [('hold', 'PASN')])

    def test_hold_facts(self):
        hold = self.events[0]
        self.assertTrue(hold['detected_at'].startswith('2026-09-03T20:4'))
        self.assertGreaterEqual(hold['duration_min'], 12)
        # The card reports the whole orbit, outer laps included: the replay
        # shows two and a half loops, and "1.1 turns" would belie it.
        self.assertGreaterEqual(hold['turn_deg'], 720)
        self.assertLess(hold['distance_nm'], ae.HOLD_NM)
        self.assertEqual(hold['min_altitude'], 4300)
        self.assertGreater(len(hold['track']), 20)


class Svx52StraightIn(unittest.TestCase):
    def test_landing_and_departure_are_no_event(self):
        # Same evening, SVX52 flew straight in to PASN, sat 44 minutes and
        # climbed straight out: no hold, and a landing is not a go-around.
        pts, hx = track('SVX52', SVX_FIXTURE)
        self.assertEqual(ae.events_for_track(pts, 'SVX52', hx, now=SVX_LATER), [])


class Quiet(unittest.TestCase):
    def test_straight_in_arrival_is_no_event(self):
        # Ten minutes straight south into PASN and gone: nothing to report.
        base = datetime(2026, 8, 20, 12, 0)
        pts = [(base.replace(minute=m), 8000 - m * 700, 57.33 - m * 0.016,
                -170.2205) for m in range(10)]
        self.assertEqual(ae.events_for_track(pts, 'X', 'abc123', now=LATER), [])


if __name__ == '__main__':
    unittest.main()
