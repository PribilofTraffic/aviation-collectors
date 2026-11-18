#!/usr/bin/env python3
"""Tests for adsb_ground_truth -- did the plane actually land?

Fixture: fa_fixtures/adsb_aer66_sry1801_20260820.json holds the real
aircraft_sightings rows for 2026-08-20. AER66 flew a missed approach at
St. Paul (700 ft at 6:19 PM, climbing again two minutes later), then went
on to St. George and landed (~6:57 PM, off the antenna for 40 minutes,
reappeared climbing out at 7:36 PM). FlightAware scored that day exactly
backwards: PASN "landed" (a phantom -sw- record), PAPB "cancelled".
SRY1801 the same day flew one missed approach at St. Paul (750 ft at
3:06 PM) and turned back to Anchorage -- FlightAware called that one a
divert correctly; the antenna agrees. AER66 also missed once at St.
George (700 ft at 6:43 PM) before landing on the second try.
"""

import json
import os
import unittest
from datetime import datetime

import adsb_ground_truth as gt

FIXTURE = os.path.join(os.path.dirname(__file__), 'fa_fixtures',
                       'adsb_aer66_sry1801_20260820.json')
DAY = '2026-08-20'
# Well after the day ended, so the final drop-off counts as a landing.
LATER = datetime(2026, 8, 21, 9, 0)


def rows_for(ident):
    with open(FIXTURE) as fh:
        rows = json.load(fh)
    return [(r[0], r[3], r[4], r[5]) for r in rows if r[2] == ident]


class PassDetection(unittest.TestCase):
    def test_aer66_missed_at_pasn(self):
        v = gt.verdict(rows_for('AER66'), 'PASN', now=LATER)
        self.assertEqual(v['verdict'], 'missed')
        self.assertEqual(v['missed'], 1)
        self.assertIsNone(v['landed_at'])
        self.assertTrue(v['missed_at'][0].startswith('2026-08-20T18:19'))

    def test_aer66_landed_at_papb(self):
        v = gt.verdict(rows_for('AER66'), 'PAPB', now=LATER)
        self.assertEqual(v['verdict'], 'landed')
        self.assertEqual(v['missed'], 1)
        self.assertTrue(v['missed_at'][0].startswith('2026-08-20T18:43'))
        self.assertTrue(v['landed_at'].startswith('2026-08-20T18:56'))
        # The 7:36 PM climb-out is a departure, never a second arrival.
        self.assertTrue(v['departed_at'].startswith('2026-08-20T19:3'))

    def test_sry1801_missed_then_went_home(self):
        v = gt.verdict(rows_for('SRY1801'), 'PASN', now=LATER)
        self.assertEqual(v['verdict'], 'missed')
        self.assertEqual(v['missed'], 1)
        self.assertTrue(v['missed_at'][0].startswith('2026-08-20T15:06'))
        v = gt.verdict(rows_for('SRY1801'), 'PAPB', now=LATER)
        self.assertEqual(v['verdict'], 'none')

    def test_no_rows_is_no_opinion(self):
        self.assertEqual(gt.verdict([], 'PASN', now=LATER)['verdict'], 'none')

    def test_gap_shorter_than_cutoff_is_undecided(self):
        """Dropped off the antenna 4 minutes ago: too soon to call."""
        rows = [r for r in rows_for('AER66') if r[0] < '2026-08-20T18:57']
        v = gt.verdict(rows, 'PAPB', now=datetime(2026, 8, 20, 19, 1))
        self.assertEqual(v['verdict'], 'missed')  # the 6:43 PM pass only
        self.assertIsNone(v['landed_at'])

    def test_quick_turnaround_is_a_landing(self):
        """N55SR, 2026-08-11: 500 ft at PASN, gone 12 minutes, back out
        climbing. That is a drop-off, not a go-around."""
        rows = [
            ('2026-08-11T17:20:00', 875, 57.17, -170.22),
            ('2026-08-11T17:22:00', 500, 57.18, -170.21),
            ('2026-08-11T17:34:00', 3675, 57.20, -170.20),
            ('2026-08-11T17:35:00', 5575, 57.24, -170.18),
        ]
        v = gt.verdict(rows, 'PASN', now=LATER)
        self.assertEqual(v['verdict'], 'landed')
        self.assertEqual(v['missed'], 0)
        self.assertTrue(v['landed_at'].startswith('2026-08-11T17:22'))

    def test_climb_out_dipping_back_low_is_one_pass(self):
        """N55SR, 2026-08-08 PAPB: 875 ft, climbed to 2,900, back to
        2,175 ft four minutes later, then away. One go-around, not two."""
        rows = [
            ('2026-08-08T16:19:00', 875, 56.60, -169.68),
            ('2026-08-08T16:21:00', 2800, 56.59, -169.62),
            ('2026-08-08T16:22:00', 2925, 56.57, -169.58),
            ('2026-08-08T16:24:00', 1650, 56.60, -169.70),
            ('2026-08-08T16:28:00', 2175, 56.62, -169.72),
            ('2026-08-08T16:31:00', 7350, 56.70, -169.90),
        ]
        v = gt.verdict(rows, 'PAPB', now=LATER)
        self.assertEqual(v['verdict'], 'missed')
        self.assertEqual(v['missed'], 1)

    def test_ground_altitude_counts_as_low(self):
        self.assertTrue(gt.is_low('ground', 57.1673, -170.2205, 'PASN'))

    def test_null_position_rows_do_not_break_a_pass(self):
        rows = [
            ('2026-08-20T10:00:00', 1500, 57.15, -170.22),
            ('2026-08-20T10:01:00', None, None, None),
            ('2026-08-20T10:02:00', 900, 57.16, -170.22),
        ]
        v = gt.verdict(rows, 'PASN', now=LATER)
        self.assertEqual(v['verdict'], 'landed')
        self.assertEqual(v['missed'], 0)



N55SR_FIXTURE = os.path.join(os.path.dirname(__file__), 'fa_fixtures',
                             'adsb_n55sr_papb_20260903.json')


class IdentAliases(unittest.TestCase):
    """Grant files GUN + the last three of the tail; the plane broadcasts
    the tail. Real day: 2026-09-03, GUN5SR filed PADU->PAPB, antenna
    heard N55SR land 3:09 PM and leave 3:27 PM."""

    def test_gun_ident_aliases_to_the_grant_tail(self):
        self.assertEqual(gt.antenna_idents('GUN5SR'), ['GUN5SR', 'N55SR'])
        self.assertEqual(gt.antenna_idents('GUN4GN'), ['GUN4GN', 'N24GN'])

    def test_other_idents_have_no_alias(self):
        self.assertEqual(gt.antenna_idents('SRY1801'), ['SRY1801'])
        self.assertEqual(gt.antenna_idents('AER66'), ['AER66'])
        self.assertEqual(gt.antenna_idents('N55SR'), ['N55SR'])
        self.assertEqual(gt.antenna_idents(''), [])
        self.assertEqual(gt.antenna_idents(None), [])

    def test_unreadable_known_aircraft_means_no_alias(self):
        self.assertEqual(gt.antenna_idents('GUN5SR', known_path='/nonexistent.json'),
                         ['GUN5SR'])

    def _db(self):
        import sqlite3
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE aircraft_sightings (timestamp TEXT, hex TEXT,'
                     ' flight TEXT, alt_baro, lat REAL, lon REAL)')
        with open(N55SR_FIXTURE) as fh:
            conn.executemany('INSERT INTO aircraft_sightings VALUES (?,?,?,?,?,?)',
                             json.load(fh))
        conn.commit()
        conn.close()
        self.addCleanup(os.unlink, path)
        return path

    def test_verdict_from_db_finds_the_landing_under_the_alias(self):
        later = datetime(2026, 9, 3, 16, 30)
        out = gt.verdict_from_db('GUN5SR', 'PAPB', '2026-09-03', now=later,
                                 db_path=self._db())
        self.assertEqual(out['verdict'], 'landed')
        self.assertTrue(out['landed_at'].startswith('2026-09-03T15:09'))
        self.assertTrue(out['departed_at'].startswith('2026-09-03T15:27'))
        self.assertEqual(out['seen_as'], 'N55SR')

    def test_direct_hit_carries_no_seen_as(self):
        later = datetime(2026, 9, 3, 16, 30)
        out = gt.verdict_from_db('N55SR', 'PAPB', '2026-09-03', now=later,
                                 db_path=self._db())
        self.assertEqual(out['verdict'], 'landed')
        self.assertNotIn('seen_as', out)

    def test_unknown_ident_on_a_live_antenna_is_none_verdict_not_none(self):
        out = gt.verdict_from_db('SRY1801', 'PAPB', '2026-09-03',
                                 now=datetime(2026, 9, 3, 16, 30), db_path=self._db())
        self.assertEqual(out['verdict'], 'none')


if __name__ == '__main__':
    unittest.main()
