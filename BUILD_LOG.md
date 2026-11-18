# Build Log - aviation-collectors

## The Problem I Was Solving

- The aircraft side of pribiloftraffic.com ran as one systemd service
  (`adsb_collector.py`) and six cron jobs. Between them they are the only
  writers of data nobody else keeps: the aircraft history database, the
  "Made It In?" arrival ledger and the go-around archive.
- Cron gives no warning when a job stops. One job on this host once went nine
  months without running, and a nightly reboot once tore a history file in
  half. A missed minute of ADS-B or a missed day of FlightAware cannot be
  fetched again.
- The owner wanted the collectors in containers so other sites can use the
  data, and wanted this one published so anyone with a receiver can run it
  with their own `.env`.

## Critical Decisions & Why

### Decision 1: One "aviation" container, not separate ADS-B and Flights ones
**What I chose**: every aircraft collector in one image.
**Why**: the flight ledger and the go-around detector both import
`adsb_ground_truth.py` and `approach_events.py`, and both read the same
database. Two images would mean two copies of those modules, and this
codebase has already been burned by copies drifting apart (a wind parser fixed
in one twin script and broken in the other for nine months).
**Alternative considered**: two containers sharing a mounted code folder.
**Why not**: more moving parts for no gain; the jobs are small.

### Decision 2: All site-specific values in `app/settings.py`
**What I chose**: one module reads the environment and one site file
(`config/site.json`). No other module names an address, a path, an airport or
a time zone.
**Why**: that is what makes the repository usable by someone else.
**Gotcha**: module-level constants (`FIELDS`, `AIRPORTS`, `ISLANDS`, `ALASKA`)
kept their old names and are now built from settings at import. The names are
historical; `ALASKA` is simply the site's time zone.
**Could break if**: a new module hardcodes a value instead of asking
settings. Grep for `192.168`, `/var/www` and `America/` before publishing.

### Decision 3: The example site file is the real Pribilof setup
**What I chose**: `config/site.example.json` is the original deployment, and
settings falls back to it when no site file is given **in a source checkout**.
**Why**: the 185 tests are recordings of real Pribilof days and need those
airports; this way they run with no setup.
**Gotcha**: the image does NOT fall back to it. A container started with no
`site.json` has no airports and says so in its first log lines. A stranger
must never silently collect for somebody else's airports.

### Decision 4: A loop inside the container, not cron
**What I chose**: `app/run.py` wakes every second and runs what is due. Each
job is its own Python process, except the ADS-B poll, which runs in the loop.
**Why**: one process to watch, one log stream, and a crash in one collector
cannot stop the others. The ADS-B poll stays in-process because it is the job
that must not miss a minute.
**Gotcha**: last-run times are saved in `<data>/.aviation_scheduler.json`.
Without that, every restart would trigger a FlightAware poll, and this host
reboots daily. Do not delete that file casually.

### Decision 5: Healthy means "the loop is alive", not "data is fresh"
**What I chose**: the health check reads a heartbeat file the loop rewrites on
every pass.
**Why**: owner's rule: a checker being down is not the sensor being down. A
dead receiver would otherwise mark the container unhealthy and invite a
restart that cannot help. What each job last did is in
`<data>/aviation_status.json` instead.

### Decision 6: Settings fall back to an env file for host-side scripts
**What I chose**: when a variable is not in the environment, settings reads
the `.env` in the folder above the repository.
**Why**: on the original host two cron-run commentary scripts import
`plane_day` and `adsb_ground_truth` from outside the container. They must see
the same paths as the container without a second copy of the values.
**Gotcha**: the container's compose block overrides the three folder
variables with `/data`, `/config`, `/backups`; the `.env` holds the host
paths. Both are right for where they are read.

### Decision 7: Backups are part of the container
**What I chose**: a daily job copies the database (SQLite's own backup call)
and the ledgers into a dated folder and keeps the newest three.
**Why**: owner's rule after losing a month of history in September 2026:
atomic writes prevent a torn file, they do nothing for a file that is gone.

## Build Timeline & Gotchas

### 2026-11-18: built and trialled
- Copied twelve modules and nine test files out of
  `pribiloftraffic/scripts/`, replaced every hardcoded value with settings.
- **Gotcha #1**: the tests assume Alaska time. With no time zone set the
  default was UTC and six tests failed on `-08:00` offsets. **Solution**:
  `timezone` became a key in the site file, so the example site carries it.
- **Gotcha #2**: the two "traffic monitor" cron jobs looked like collectors
  and are not. They watch for approaching aircraft and launch the AI
  commentary, so they stayed on the host.
- **Gotcha #3**: `OTHER_AIRPORT` was a hardcoded pair (a flight that missed
  one island may have landed at the other). It is now "every other airport in
  the site file, in order", which is the same thing for two airports.
- **Gotcha #4**: with `env_file: .env` the container would receive every
  secret in a shared `.env`. The compose block lists its variables by name
  instead.
- **Gotcha #5** (caught by the post-push security review): the reception
  feed carried the collector's source address in a public file, and the
  go-around events built a radar link from an aircraft address without
  checking it. The feed now carries a label, and `settings.radar_url()` is
  the only place a radar link is built: six hex characters or no link.
- Trial: ran the image as `aviation-trial` against the real receivers,
  writing to `/home/zemchug/aviation_trial/`, beside the live systemd service
  and cron jobs. Compared database rows per minute and the JSON feeds.

## What Is NOT Done Yet

See the cutover checklist in `pribiloftraffic/UPGRADE_ROADMAP.md`. In short:
the live site still runs the old service and cron jobs until the owner stops
them; the container has only ever written to the trial folder.

## If It Breaks

| Symptom | Look at |
|---|---|
| Container `unhealthy` | `docker logs aviation`; the loop itself has stopped |
| Healthy but a feed is stale | `<data>/aviation_status.json`: the job's `state` and `message` |
| `receiver gave no aircraft data` | the receiver or the network to it, not this container |
| A job listed as `off (not configured)` | its variable is empty in `.env`, or it is missing from the compose block |
| No airports in the first log line | `site.json` is not in the mounted config folder |
| Extra FlightAware charges | `.aviation_scheduler.json` was deleted or the data folder is not persistent |
