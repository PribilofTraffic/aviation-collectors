#!/usr/bin/env python3
"""The container's main loop: runs every collector on its own interval.

There is no cron inside the container. This loop wakes once a second, runs
whatever is due and goes back to sleep. Each job is a separate Python process
so one crashing or hanging cannot take the others down. The one exception is
the ADS-B history poll, which runs in this process: it is the job that must
never miss a minute, and it keeps its counter between polls.

Rules the loop keeps:
- A job that is switched off (its setting is empty) is never started and is
  listed as "off" in the status file.
- Last-run times are saved to disk. A restart does not re-run a job that is
  not due, which matters for the FlightAware job: every run costs money.
- A failed job is tried again at its next interval, never in a tight loop.
- The status file and the heartbeat are written whether or not the sources
  answer. A dead receiver is reported there; it does not stop the loop.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

import settings

settings.apply_timezone()

import adsb_collector  # noqa: E402  (after the time zone is applied)

APP_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = settings.data_path('.aviation_scheduler.json')
STATUS_FILE = settings.data_path('aviation_status.json')
HEARTBEAT_FILE = settings.data_path('.aviation_heartbeat')
JOB_TIMEOUT = settings.get_int('AVIATION_JOB_TIMEOUT', 600)
BACKUP_DIR = settings.get('AVIATION_BACKUP_DIR')


def _flights_on():
    return bool(settings.fa_api_key()) and bool(settings.AIRPORTS)


# name, seconds between runs, scripts to run in order, is it switched on
JOBS = (
    ('adsb_history', settings.get_int('AVIATION_ADSB_INTERVAL', 60),
     None, lambda: bool(settings.TAR1090_URL)),
    ('hub_departures', settings.get_int('AVIATION_HUB_INTERVAL', 120),
     ('panc_departure_watch.py',), lambda: bool(settings.HUB_TAR1090_URL)),
    ('flight_patterns', settings.get_int('AVIATION_PATTERNS_INTERVAL', 300),
     ('flight_patterns_collector.py',), lambda: bool(settings.AIRPORTS)),
    ('coverage', settings.get_int('AVIATION_COVERAGE_INTERVAL', 1800),
     ('adsb_coverage_collect.py',), lambda: bool(settings.COVERAGE_URL)),
    ('flights', settings.get_int('AVIATION_FLIGHTS_INTERVAL', 7200),
     ('flight_plans_collect.py', 'flight_arrival_record.py'), _flights_on),
    ('aircraft_stats', settings.get_int('AVIATION_STATS_INTERVAL', 21600),
     ('generate_aircraft_stats.py',), lambda: os.path.isfile(settings.DB_PATH)),
    ('backup', settings.get_int('AVIATION_BACKUP_INTERVAL', 86400),
     ('backup.py',), lambda: bool(BACKUP_DIR)),
)

_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True


def log(message):
    print(f'[{datetime.now(settings.TZ):%Y-%m-%d %H:%M:%S %Z}] {message}', flush=True)


def public(message):
    """A job message that is safe to publish: the status file sits in the data
    folder, which is usually served to the web, so addresses are removed."""
    return re.sub(r'\b\w+://\S+', '(address hidden)', str(message))[:300]


def load_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def run_scripts(scripts):
    """Run each script in turn, all of them even if one fails (the arrival
    record must still be updated when a FlightAware poll fails). Returns
    (ok, one-line message)."""
    problems = []
    for script in scripts:
        try:
            result = subprocess.run(
                [sys.executable, os.path.join(APP_DIR, script)],
                cwd=APP_DIR, timeout=JOB_TIMEOUT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except subprocess.TimeoutExpired:
            problems.append(f'{script} ran past {JOB_TIMEOUT} s and was stopped')
            continue
        output = (result.stdout or '').strip()
        if output:
            print(output, flush=True)
        if result.returncode not in (0, None):
            last = output.splitlines()[-1] if output else ''
            problems.append(f'{script} exited {result.returncode}: {last}')
    return (not problems), ('; '.join(problems)[:300] if problems else 'ok')


def main():
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    os.makedirs(settings.DATA_DIR, exist_ok=True)

    log(f'aviation collectors starting; site file {settings.SITE_FILE}, '
        f'time zone {settings.TZ_NAME}, airports {settings.AIRPORT_CODES or "none"}')
    state = load_json(STATE_FILE)
    status = {}
    for name, interval, _, enabled in JOBS:
        on = enabled()
        status[name] = {'state': 'waiting' if on else 'off', 'interval_s': interval}
        log(f'  {name}: ' + (f'every {interval} s' if on else 'off (not configured)'))

    if settings.TAR1090_URL:
        os.makedirs(os.path.dirname(settings.DB_PATH) or '.', exist_ok=True)
        adsb_collector.initialize_database()
    adsb_count = 0

    while not _stop:
        now = time.time()
        for name, interval, scripts, enabled in JOBS:
            if _stop or not enabled():
                continue
            if now - float(state.get(name) or 0) < interval:
                continue
            started = time.time()
            try:
                if scripts is None:
                    adsb_count += 1
                    stored = adsb_collector.collect_once(adsb_count)
                    ok = True
                    message = (f'{stored} aircraft records stored' if stored
                               else 'receiver gave no aircraft data')
                else:
                    ok, message = run_scripts(scripts)
            except Exception as exc:        # a job must never stop the loop
                ok, message = False, f'{type(exc).__name__}: {exc}'[:300]
            if not ok:
                log(f'{name} FAILED: {message}')
            state[name] = started
            status[name] = {
                'state': 'ok' if ok else 'failed',
                'interval_s': interval,
                'last_run': datetime.fromtimestamp(started, settings.TZ).isoformat(timespec='seconds'),
                'took_s': round(time.time() - started, 1),
                'message': public(message),
            }
            try:
                write_json(STATE_FILE, state)
            except OSError as exc:
                log(f'could not save scheduler state: {exc}')
        try:
            write_json(STATUS_FILE, {
                'updated': datetime.now(settings.TZ).isoformat(timespec='seconds'),
                'jobs': status})
            with open(HEARTBEAT_FILE, 'w') as fh:
                fh.write(str(int(time.time())))
        except OSError as exc:
            log(f'could not write status: {exc}')
        time.sleep(1)

    log('stopping')
    return 0


if __name__ == '__main__':
    sys.exit(main())
