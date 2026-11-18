#!/usr/bin/env python3
"""Tests for grant_record: Grant Aviation's schedule scored by the antenna.

Grant rarely files a flight plan, so the ledger (FlightAware boards) only
ever sees the handful of Grant flights that were filed -- and scores those
100% while the antenna knows the King Air stayed home on most scheduled
days. This module asks adsb_history for the known Grant tails on every
closed day and scores the POSTED timetable instead.

Numbers below are the real 2026-08-19..09-06 picture: 10 closed schedule
days at PASN, landings on Aug 20, 25 and 28, one filed plan (Aug 25), an
off-schedule Saturday landing on Sep 5.
"""

import unittest

import grant_record

SCHEDULE = {
    'as_of': '2026-08-31',
    'airports': {
        'PASN': {'days': ['Sunday', 'Tuesday', 'Thursday', 'Friday']},
        'PAPB': {'days': ['Sunday', 'Tuesday', 'Thursday', 'Friday']},
    },
}

KNOWN = {
    'known_aircraft': {
        'N24GN': {'operator': 'Grant Aviation', 'type': 'Beech King Air 200'},
        'N55SR': {'operator': 'Grant Aviation', 'type': 'Beech King Air 200'},
        'GUN': {'operator': 'Grant Aviation', 'match_type': 'prefix'},
        'N441BD': {'operator': 'Security Aviation'},
        'AER': {'operator': 'Ace Air Cargo', 'match_type': 'prefix'},
    }
}

START = '2026-08-19'
TODAY = '2026-09-06'

# What the antenna said, keyed by (tail, day). Days absent for every tail
# answer 'none'; days in DEAD answer None (receiver logged nothing).
LANDINGS = {
    ('N55SR', '2026-08-20'): '2026-08-20T15:30:00',
    ('N55SR', '2026-08-25'): '2026-08-25T15:07:00',
    ('N55SR', '2026-08-28'): '2026-08-28T18:34:00',
    ('N55SR', '2026-09-05'): '2026-09-05T18:44:00',
}
MISSED = {('N24GN', '2026-09-03')}
DEAD = {'2026-08-23'}


def fake_antenna(calls=None):
    def verdict_fn(ident, airport, day):
        if calls is not None:
            calls.append((ident, airport, day))
        if day in DEAD:
            return None
        if (ident, day) in LANDINGS:
            return {'verdict': 'landed', 'landed_at': LANDINGS[(ident, day)],
                    'departed_at': None, 'missed': 0, 'missed_at': []}
        if (ident, day) in MISSED:
            return {'verdict': 'missed', 'landed_at': None,
                    'departed_at': None, 'missed': 1,
                    'missed_at': [day + 'T14:50:00']}
        return {'verdict': 'none', 'landed_at': None, 'departed_at': None,
                'missed': 0, 'missed_at': []}
    return verdict_fn


LEDGER_DAYS = {
    '2026-08-25': [
        {'ident': 'N55SR', 'operator_name': 'Grant Aviation'},
        {'ident': 'SRY1801', 'operator_name': 'Aleutian Airways'},
    ],
    '2026-09-05': [{'ident': 'GUN5SR', 'operator_name': 'Grant Aviation'}],
    '2026-09-06': [{'ident': 'SRY1801', 'operator_name': 'Aleutian Airways'}],
}


class TestScheduleAndTails(unittest.TestCase):
    def test_scheduled_dates_are_the_closed_schedule_days(self):
        days = grant_record.scheduled_dates(START, TODAY,
                                            SCHEDULE['airports']['PASN']['days'])
        self.assertEqual(days, [
            '2026-08-20', '2026-08-21', '2026-08-23', '2026-08-25',
            '2026-08-27', '2026-08-28', '2026-08-30', '2026-09-01',
            '2026-09-03', '2026-09-04'])

    def test_scheduled_dates_excludes_today(self):
        days = grant_record.scheduled_dates(START, TODAY, ['Sunday'])
        self.assertNotIn('2026-09-06', days)

    def test_grant_tails_are_the_registered_n_numbers_only(self):
        self.assertEqual(grant_record.grant_tails(KNOWN), ['N24GN', 'N55SR'])


class TestUpdate(unittest.TestCase):
    def test_stores_a_landing_with_tail_and_time(self):
        stored = grant_record.update({}, 'PASN', START, TODAY,
                                     ['N24GN', 'N55SR'], fake_antenna())
        self.assertEqual(stored['2026-08-20'], {
            'antenna': 'landed', 'tail': 'N55SR',
            'landed_at': '2026-08-20T15:30:00', 'departed_at': None})

    def test_stores_a_quiet_day_as_none(self):
        stored = grant_record.update({}, 'PASN', START, TODAY,
                                     ['N24GN', 'N55SR'], fake_antenna())
        self.assertEqual(stored['2026-08-21']['antenna'], 'none')
        self.assertIsNone(stored['2026-08-21']['tail'])

    def test_a_go_around_is_stored_as_missed(self):
        stored = grant_record.update({}, 'PASN', START, TODAY,
                                     ['N24GN', 'N55SR'], fake_antenna())
        self.assertEqual(stored['2026-09-03']['antenna'], 'missed')
        self.assertEqual(stored['2026-09-03']['tail'], 'N24GN')

    def test_dead_antenna_day_is_not_stored(self):
        stored = grant_record.update({}, 'PASN', START, TODAY,
                                     ['N24GN', 'N55SR'], fake_antenna())
        self.assertNotIn('2026-08-23', stored)

    def test_today_is_not_scored(self):
        stored = grant_record.update({}, 'PASN', START, TODAY,
                                     ['N24GN', 'N55SR'], fake_antenna())
        self.assertNotIn(TODAY, stored)

    def test_off_schedule_days_are_checked_too(self):
        stored = grant_record.update({}, 'PASN', START, TODAY,
                                     ['N24GN', 'N55SR'], fake_antenna())
        self.assertEqual(stored['2026-09-05']['antenna'], 'landed')

    def test_stored_days_are_never_recomputed(self):
        calls = []
        prior = {'2026-08-20': {'antenna': 'none', 'tail': None,
                                'landed_at': None, 'departed_at': None}}
        stored = grant_record.update(prior, 'PASN', START, TODAY,
                                     ['N24GN', 'N55SR'], fake_antenna(calls))
        self.assertEqual(stored['2026-08-20']['antenna'], 'none')
        self.assertNotIn(('N55SR', 'PASN', '2026-08-20'), calls)
        self.assertNotIn(('N24GN', 'PASN', '2026-08-20'), calls)

    def test_does_not_mutate_the_input(self):
        prior = {}
        grant_record.update(prior, 'PASN', START, TODAY, ['N55SR'],
                            fake_antenna())
        self.assertEqual(prior, {})


class TestSummarize(unittest.TestCase):
    def setUp(self):
        stored = grant_record.update({}, 'PASN', START, TODAY,
                                     ['N24GN', 'N55SR'], fake_antenna())
        self.summary = grant_record.summarize(
            stored, 'PASN', START, TODAY, SCHEDULE, LEDGER_DAYS)

    def test_counts_scheduled_days_by_outcome(self):
        s = self.summary
        self.assertEqual(s['scheduled'], 10)
        self.assertEqual(s['landed'], 3)
        self.assertEqual(s['no_show'], 6)
        self.assertEqual(s['unknown'], 1)

    def test_pct_is_of_known_days_only(self):
        # 3 landed of 9 known (the dead-antenna day is not a miss)
        self.assertEqual(self.summary['pct_landed'], 33)

    def test_filed_counts_schedule_days_with_a_grant_plan(self):
        self.assertEqual(self.summary['filed'], 1)

    def test_day_rows_carry_verdict_time_tail_and_filed(self):
        by_day = {d['day']: d for d in self.summary['days']}
        self.assertEqual(by_day['2026-08-25'], {
            'day': '2026-08-25', 'verdict': 'landed', 'tail': 'N55SR',
            'landed_at': '2026-08-25T15:07:00', 'departed_at': None,
            'filed': True})
        self.assertEqual(by_day['2026-08-21']['verdict'], 'no_show')
        self.assertFalse(by_day['2026-08-21']['filed'])
        self.assertEqual(by_day['2026-08-23']['verdict'], 'unknown')

    def test_a_go_around_is_a_no_show_that_says_so(self):
        by_day = {d['day']: d for d in self.summary['days']}
        self.assertEqual(by_day['2026-09-03']['verdict'], 'no_show')
        self.assertTrue(by_day['2026-09-03']['missed'])

    def test_day_rows_are_schedule_days_in_order(self):
        self.assertEqual([d['day'] for d in self.summary['days']],
                         grant_record.scheduled_dates(START, TODAY,
                                                      SCHEDULE['airports']['PASN']['days']))

    def test_off_schedule_landings_listed_separately(self):
        self.assertEqual(self.summary['extra'], [
            {'day': '2026-09-05', 'tail': 'N55SR',
             'landed_at': '2026-09-05T18:44:00', 'departed_at': None,
             'filed': True}])

    def test_carries_the_schedule_it_scored(self):
        self.assertEqual(self.summary['weekdays'],
                         ['Sunday', 'Tuesday', 'Thursday', 'Friday'])
        self.assertEqual(self.summary['as_of'], '2026-08-31')

    def test_pct_is_null_before_any_known_day(self):
        s = grant_record.summarize({}, 'PASN', '2026-09-06', TODAY,
                                   SCHEDULE, {})
        self.assertEqual(s['scheduled'], 0)
        self.assertIsNone(s['pct_landed'])

    def test_missing_schedule_for_airport_yields_none(self):
        self.assertIsNone(grant_record.summarize(
            {}, 'PAKN', START, TODAY, SCHEDULE, {}))


if __name__ == '__main__':
    unittest.main()
