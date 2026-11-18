"""Every site-specific value the collectors use, in one place.

Nothing else in this package names an address, a file path, an airport or a
time zone. Two sources feed this module:

1. Environment variables (AVIATION_*): addresses, paths, the FlightAware key
   and the schedule. In the container they come from the compose file. A
   script that imports these modules OUTSIDE the container (for example a cron
   job on the same host) has no such environment, so any variable that is not
   set is looked up in an env file: AVIATION_ENV_FILE if given, otherwise a
   `.env` in the folder above this repository. That keeps one `.env` as the
   only place a deployment writes its values.

2. A site file (JSON): the airports, the hub airport, display names. See
   config/site.example.json, which is a real working setup (two island
   airports in the Bering Sea fed from Anchorage) and doubles as the default
   when no site file is given, so the tests run with no setup.

Optional features switch themselves off when their setting is empty: no
FlightAware key means no flight-plan polling, no hub receiver means no
departure watch, and so on. The ADS-B history collector needs only
AVIATION_TAR1090_URL.
"""

import json
import os
import time
from zoneinfo import ZoneInfo

APP_DIR = os.path.dirname(os.path.realpath(__file__))
REPO_DIR = os.path.dirname(APP_DIR)


def _read_env_file():
    path = os.environ.get('AVIATION_ENV_FILE') or os.path.join(
        os.path.dirname(REPO_DIR), '.env')
    values = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, val = line.partition('=')
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
                    val = val[1:-1]
                values[key.strip()] = val
    except OSError:
        pass
    return values


_ENV_FILE = _read_env_file()


def get(name, default=None):
    """A setting: the environment first, then the env file, then the default.
    An empty value counts as not set."""
    val = os.environ.get(name)
    if val is None or val == '':
        val = _ENV_FILE.get(name)
    return default if val is None or val == '' else val


def get_int(name, default):
    try:
        return int(get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- paths

DATA_DIR = get('AVIATION_DATA_DIR', '/data')
CONFIG_DIR = get('AVIATION_CONFIG_DIR', '/config')


def data_path(name):
    return os.path.join(DATA_DIR, name)


def _first_existing(*paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return None


SITE_FILE = _first_existing(
    get('AVIATION_SITE_FILE'),
    os.path.join(CONFIG_DIR, 'site.json'),
    os.path.join(REPO_DIR, 'config', 'site.json'),
    os.path.join(REPO_DIR, 'config', 'site.example.json'),
)

# Who flies here: tail numbers and three-letter operator codes. Optional; with
# no file, operators are shown by their raw code.
KNOWN_AIRCRAFT = get('AVIATION_KNOWN_AIRCRAFT') or _first_existing(
    os.path.join(CONFIG_DIR, 'known_aircraft.json'),
    os.path.join(REPO_DIR, 'config', 'known_aircraft.json'),
    os.path.join(REPO_DIR, 'config', 'known_aircraft.example.json'),
) or os.path.join(CONFIG_DIR, 'known_aircraft.json')

DB_PATH = get('AVIATION_DB_PATH') or data_path('adsb_history.db')

# ---------------------------------------------------------------- sources

TAR1090_URL = get('AVIATION_TAR1090_URL')              # required: the receiver
HUB_TAR1090_URL = get('AVIATION_HUB_TAR1090_URL')      # optional: receiver at the hub airport
PATTERNS_URL = get('AVIATION_PATTERNS_URL')            # optional: aircraft-patterns container
COVERAGE_URL = get('AVIATION_COVERAGE_URL')            # optional: ADSb-Vue /cone
FA_KEY_FILE = get('AVIATION_FA_KEY_FILE')


def fa_api_key():
    """The FlightAware AeroAPI key, or None. Never logged, never written."""
    key = get('AVIATION_FA_AEROAPI_KEY') or os.environ.get('FA_AEROAPI_KEY')
    if key:
        return key.strip()
    if FA_KEY_FILE:
        try:
            with open(FA_KEY_FILE) as fh:
                return fh.read().strip() or None
        except OSError:
            return None
    return None


# ---------------------------------------------------------------- the site

def _load_site():
    if not SITE_FILE:
        return {}
    with open(SITE_FILE) as fh:
        return json.load(fh)


SITE = _load_site()

# ---------------------------------------------------------------- time zone
# The site's local zone: AVIATION_TZ, else "timezone" in the site file, else
# the container's TZ, else UTC. It decides what "today" means and the naive
# timestamps written to the history database, so set it once and leave it.

TZ_NAME = (get('AVIATION_TZ') or SITE.get('timezone')
           or os.environ.get('TZ') or 'UTC')
TZ = ZoneInfo(TZ_NAME)


def apply_timezone():
    """Make naive datetime.now() local to the site. The history database
    stores naive local timestamps, so every module calls this at import."""
    os.environ['TZ'] = TZ_NAME
    time.tzset()


# [{'icao', 'name', 'short_name', 'lat', 'lon', 'place': {'name','lat','lon'}}]
AIRPORTS = SITE.get('airports') or []
AIRPORT_CODES = [a['icao'] for a in AIRPORTS]


def airport(icao):
    return next((a for a in AIRPORTS if a['icao'] == icao), None)


def short_name(icao):
    a = airport(icao) or {}
    return a.get('short_name') or a.get('name') or icao


def plans_file(icao):
    return data_path(f'flight_plans_{icao.lower()}.json')


def record_file(icao):
    return data_path(f'flight_record_{icao.lower()}.json')


# The airport the scheduled flights leave from, heard by a second receiver.
HUB = SITE.get('hub') or {}
HUB_ICAO = HUB.get('icao') or 'HUB'
HUB_NAME = HUB.get('name') or HUB_ICAO
HUB_DEPARTURES_FILE = data_path(f'{HUB_ICAO.lower()}_departures.json')

# Which receiver the reception-coverage feed describes.
COVERAGE_SITE = SITE.get('coverage_site') or {}

# Callsign prefixes that are never a scheduled arrival (flight inspection).
EXCLUDED_IDENT_PREFIXES = tuple(SITE.get('excluded_ident_prefixes') or ())

# Operator names that win over known_aircraft.json, keyed by code.
OPERATOR_DISPLAY_NAMES = SITE.get('operator_display_names') or {}

# An operator that rarely files a flight plan but posts a weekly timetable:
# its landings are scored from the antenna against that timetable.
TIMETABLE = SITE.get('timetable') or {}
TIMETABLE_OPERATOR = TIMETABLE.get('operator')
TIMETABLE_FILE = data_path(TIMETABLE['file']) if TIMETABLE.get('file') else None
