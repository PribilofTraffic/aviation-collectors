#!/usr/bin/env python3
"""
ADS-B Reception Coverage Collector

Polls the ADSb-Vue container on the St. George ADS-B Pi and writes a reception
coverage summary to data/adsb_coverage.json for /skystats/.

WHY ONLY THE HEADER
-------------------
/cone returns a custom packed payload: a 4-byte little-endian length, then a
JSON metadata block, then packed reception cells. The upstream server's own
comments say the viewer and the packed cell format are version-locked, so
decoding the cells would break on any upstream update.

We never touch the cells. Everything this collector needs -- max range per
bearing (cone_all / cone_low), receiver position, cell count, and the time
window -- lives in the readable JSON header. Parsing stops at the end of that
block, so a change to the packed format cannot affect us.

WHY AN ARCHIVE
--------------
cone_all is cumulative over whatever the container's SQLite currently holds,
which is wiped if the container is recreated without its volume (the same trap
flight_patterns_collector.py exists to work around). We keep a running
best-ever value per bearing so a reset on the Pi cannot erase the site's
record of how far the receiver has actually heard.
"""

import os
import time

import settings
settings.apply_timezone()

import json
import struct
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

SOURCE_URL = settings.COVERAGE_URL    # ADSb-Vue /cone; empty switches this collector off
REQUEST_TIMEOUT = 15
RETRIES = 3
RETRY_BACKOFF_SECS = 3
DATA_DIR = settings.DATA_DIR
OUTPUT_FILE = f'{DATA_DIR}/adsb_coverage.json'
ALASKA = settings.TZ      # the site's local zone (the name is historical)

# The receiver this feed belongs to. Named in the payload so a mis-wired
# endpoint shows the wrong island's name, not just the wrong ranges --
# the same guard js/hrrr.js uses for the two HRRR feeds.
SITE = dict(settings.COVERAGE_SITE)

# The JSON header is ~4.5 KB in practice. Anything wildly larger means we are
# not looking at the format we think we are, so refuse rather than try to
# json.loads() a few hundred megabytes of packed cells.
MAX_HEADER_BYTES = 2 * 1024 * 1024


def fetch_cone(url=SOURCE_URL):
    """Fetch /cone, retrying a couple of times on transport errors.

    The container's HTTP server resets connections while it is flushing its
    SQLite batch (every 60 s per /health), so an occasional
    ConnectionResetError is normal rather than a sign the receiver is down.
    Observed live: one reset, then six clean fetches in a row. Without a retry
    a single reset costs a whole polling interval.
    """
    last = None
    for attempt in range(RETRIES):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last = exc
            if attempt + 1 < RETRIES:
                time.sleep(RETRY_BACKOFF_SECS * (attempt + 1))
    raise last


def parse_header(blob):
    """Return the decoded JSON metadata block from a /cone response.

    Deliberately reads the header and nothing else -- see module docstring.
    """
    if len(blob) < 4:
        raise ValueError(f'response too short to hold a header length ({len(blob)} bytes)')

    (header_len,) = struct.unpack('<I', blob[:4])
    if header_len <= 0 or header_len > MAX_HEADER_BYTES:
        raise ValueError(f'implausible header length {header_len} -- payload format changed?')
    if len(blob) < 4 + header_len:
        raise ValueError(
            f'truncated response: header claims {header_len} bytes, got {len(blob) - 4}'
        )

    return json.loads(blob[4:4 + header_len])


def ring_stats(ring):
    """Summarize one max-range-per-bearing array.

    Bearings the receiver has never heard anything on sit at 0.0, and they are
    real information (a terrain or antenna blind spot), so they are counted but
    kept out of the mean -- averaging them in would quietly understate range.
    """
    if not ring:
        return None

    step = 360.0 / (len(ring) - 1) if len(ring) > 1 else 360.0
    heard = [v for v in ring if v > 0]
    peak_index = max(range(len(ring)), key=lambda i: ring[i])

    return {
        'max_nm': round(ring[peak_index], 1),
        'max_bearing': round(peak_index * step) % 360,
        'mean_nm': round(sum(heard) / len(heard), 1) if heard else 0.0,
        'covered': len(heard),
        'bearings': len(ring),
    }


def load_existing(path):
    """Return the previous payload, or None if there isn't a usable one.

    A missing file is the normal first run. A corrupt one is not fatal here --
    unlike an append-only archive, every field can be rebuilt from the next
    poll, so we start the best-ever ring over rather than wedging the job.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (ValueError, OSError) as exc:
        print(f'WARNING: existing {path} unreadable ({exc}); rebuilding best-ever ring')
        return None


def merge_best(previous, ring):
    """Element-wise max of the stored best-ever ring and this poll's ring."""
    if not previous:
        return list(ring)

    stored = previous.get('rings', {}).get('best', [])
    if len(stored) != len(ring):
        # Upstream changed its bearing resolution. Old values are no longer
        # comparable bearing-for-bearing, so start clean rather than
        # silently pairing 1-degree data against, say, 2-degree data.
        return list(ring)

    return [max(a, b) for a, b in zip(stored, ring)]


def build_output(meta, best_ring, now):
    ring_all = [round(float(v), 1) for v in meta.get('cone_all', [])]
    ring_low = [round(float(v), 1) for v in meta.get('cone_low', [])]

    t_min, t_max = meta.get('t_min'), meta.get('t_max')
    window = None
    if t_min and t_max:
        window = {
            'start': datetime.fromtimestamp(t_min, ALASKA).isoformat(),
            'end': datetime.fromtimestamp(t_max, ALASKA).isoformat(),
            'hours': round((t_max - t_min) / 3600.0, 1),
        }

    return {
        'generated': now.isoformat(),
        'generated_display': now.strftime('%B %d, %Y at %I:%M %p %Z'),
        # A label, never the address: this file is served to the public.
        'source': 'ADSb-Vue /cone',
        'site': dict(
            SITE,
            lat=meta.get('recv_lat'),
            lon=meta.get('recv_lon'),
            antenna_agl_ft=meta.get('antenna_agl_ft'),
        ),
        'cells': meta.get('count'),
        'window': window,
        'stats': {
            'all': ring_stats(ring_all),
            'low': ring_stats(ring_low),
            'best': ring_stats(best_ring),
        },
        'rings': {
            'all': ring_all,
            'low': ring_low,
            'best': [round(float(v), 1) for v in best_ring],
        },
    }


def write_atomic(path, payload):
    """Write via .tmp + replace. Nginx serves this path directly, so a partial
    file must never be visible to a visitor."""
    tmp = f'{path}.tmp'
    try:
        with open(tmp, 'w') as fh:
            json.dump(payload, fh, indent=2)
            # Remote site, unreliable power: os.replace() is atomic against
            # concurrent readers, but without flushing and fsyncing first an
            # unclean shutdown can make the rename durable while the data
            # blocks behind it are not.
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def main():
    if not SOURCE_URL:
        print('no coverage source set (AVIATION_COVERAGE_URL); coverage collector is off')
        return 0
    os.makedirs(DATA_DIR, exist_ok=True)

    try:
        blob = fetch_cone()
    except Exception as exc:
        print(f'FAILED: could not reach {SOURCE_URL}: {exc}')
        print('Previous coverage summary left in place.')
        return 1

    try:
        meta = parse_header(blob)
    except Exception as exc:
        print(f'FAILED: malformed /cone payload: {exc}')
        print('Previous coverage summary left in place.')
        return 1

    ring_all = [float(v) for v in meta.get('cone_all', [])]
    if not ring_all:
        print('FAILED: payload carried no cone_all ring.')
        print('Previous coverage summary left in place.')
        return 1

    previous = load_existing(OUTPUT_FILE)
    best_ring = merge_best(previous, ring_all)
    payload = build_output(meta, best_ring, datetime.now(ALASKA))

    try:
        write_atomic(OUTPUT_FILE, payload)
    except Exception as exc:
        print(f'FAILED: could not write {OUTPUT_FILE}: {exc}')
        print('Previous coverage summary left in place.')
        return 1

    now_stats = payload['stats']['all']
    best_stats = payload['stats']['best']
    print(
        f"OK: {payload['cells']} cells, "
        f"max {now_stats['max_nm']} nm at {now_stats['max_bearing']:03d} deg, "
        f"mean {now_stats['mean_nm']} nm over {now_stats['covered']}/{now_stats['bearings']} bearings "
        f"(best ever {best_stats['max_nm']} nm) -> {OUTPUT_FILE}"
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
