# Docker setup

Backend only (this repo). The frontend ("New Lead gen model") is a separate project,
deployed independently, same as today. Live-verified end to end, including a real
corruption incident and fix -- see "The database (named volume, not a bind mount)"
below before assuming db.sqlite3 works like a normal file on disk.

## Services

- **redis** -- Celery's broker.
- **web** -- gunicorn serving the Django app on :8000.
- **celery_worker** -- runs `reconcile_outcomes_task`/`run_scheduled_tuff_task`/
  `run_all_states_tuff_task` (see `planning/tasks.py`).
- **celery_beat** -- fires the two scheduled tasks at the same times the old crontab
  did (6:00 AM reconcile, 6:15 AM tuff -- see `config/settings.py`'s
  `CELERY_BEAT_SCHEDULE`).

## The database (named volume, not a bind mount)

`db.sqlite3` lives in the `sqlite_data` **named Docker volume**, mounted at
`/app/db_data` inside the containers -- it has **no path on the host filesystem at
all**. This is deliberate, not an oversight: the database corrupted twice in one
session (2026-09-19) when it was a bind mount (`./db.sqlite3:/app/db.sqlite3`).
SQLite's WAL mode needs reliable shared-memory (mmap) semantics between every process
touching the file; Docker Desktop/Colima's bind-mount bridge (virtiofs/gRPC-FUSE)
between macOS and the Linux VM does not reliably provide that -- the second corruption
happened from a plain host-side `sqlite3 -readonly db.sqlite3 ...` **read**, merely
concurrent with the container's own write, no host-side write involved at all. A named
volume avoids the bridge entirely for this one file.

**Consequence: never run `sqlite3`, a local Python process, or any other host-native
tool directly against a `db.sqlite3` path for this project again -- there isn't one.**
To inspect or query the live database:

```
# one-off query (the image has no sqlite3 CLI -- use Python's stdlib module)
docker exec <web container> python -c "
import sqlite3
conn = sqlite3.connect('file:/app/db_data/db.sqlite3?mode=ro', uri=True)
print(conn.execute('PRAGMA integrity_check;').fetchone())
"

# interactive
docker exec -it <web container> python manage.py dbshell
```

To seed a fresh volume (first-ever run, or restoring from a recovered/backup copy),
copy the file in via a throwaway container -- the named volume must already exist
(`docker volume create <project>_sqlite_data`) and the directory + file both need
`chown 1000:1000` (the `appuser` the app containers run as) or WAL's `-wal`/`-shm`
companion files can't be created:

```
docker run --rm -v <project>_sqlite_data:/data -v /path/to/seed.sqlite3:/seed.sqlite3:ro \
  alpine sh -c "cp /seed.sqlite3 /data/db.sqlite3 && chown -R 1000:1000 /data && chmod 775 /data && chmod 664 /data/db.sqlite3"
```

A first-ever `docker compose up` with no volume seeded at all also works fine --
`manage.py migrate` creates a schema-only database automatically, same as any fresh
Django install; seeding only matters when you're carrying forward real data.

## One-time setup before the first `docker compose up`

1. `cp .env.docker.example .env` and fill in real values -- at minimum `SECRET_KEY`,
   `DEBUG=False`, `REDSHIFT_*`, and whichever LLM provider(s) you're using. See that
   file's own comments for what's required vs. optional.

2. Make sure `DC_RAnk.csv`, `config and parameter /`, `Niyojan Q2-FY_26_27 Dashboard -
   Planning.csv`, and `pitch_config/` already exist at the project root (they should,
   if you're running this from a normal checkout that's already been used locally) --
   these are still bind-mounted in as-is (unlike db.sqlite3, they're plain files/CSVs
   with no WAL-mode concurrency concerns), not created automatically. If you hit
   permission errors on `output/`/`logs/` (also still bind-mounted), `chmod -R 777
   output logs`, or match ownership to uid 1000 directly.

## Running it

```
docker compose up --build
```

`web`'s entrypoint runs `manage.py migrate` once before starting gunicorn;
`celery_worker`/`celery_beat` wait for `web` to report healthy before starting (see
`depends_on` in docker-compose.yml), so migrations are never raced.

The app is then reachable at `http://localhost:8000/`, same URLs as local dev
(`/api/planning/se/v1/<se_email>/` etc.).

## Known, accepted limitations (not fixed by this Docker setup itself)

- **The single-scope plan endpoints are still fully synchronous.** Only
  `admin_generate_all_states` runs through Celery -- `se_plan`/`state_plan`/`tuff`/
  `normalize` block the HTTP request for the whole generation (observed up to ~750s
  for a large state). gunicorn's `--timeout` is set to 900s to accommodate this
  rather than killing the worker mid-request, but a request that long still ties up
  one of gunicorn's 3 workers for that whole time -- with only 3 workers, 3
  concurrent long-running generations would exhaust the pool. Scale `--workers` up in
  the Dockerfile's CMD if this matters for your traffic, or revisit making more of
  these endpoints Celery-based (a bigger, deliberately out-of-scope change discussed
  and declined earlier in this project's history).
- **`celery_worker` has no process-level healthcheck**, by design -- see the comment
  in docker-compose.yml. `--pool=solo` means the same process handles both tasks and
  `celery inspect ping`, so a real long-running task would make a naive healthcheck
  misreport a busy-but-healthy worker as dead and trigger a restart mid-task.
- **`--pool=solo` is kept as the default**, carried over from the real, confirmed bug
  hit locally (Celery's prefork pool + billiard + Python 3.14 + macOS's spawn-based
  multiprocessing). This Docker image uses Python 3.12 on Linux, which might not hit
  the same issue -- solo just hasn't been tested against removing it here, and this
  app's task volume is low enough that solo costs nothing real either way.

## Live-verification history

All 4 of the checks below have actually been done, not just planned -- keeping the
list as a record of what "live-verified" means here, and as the checklist to re-run
after any future infra change to this stack:

1. `docker compose build` -- image builds clean, no missing system libs.
2. `docker compose up` -- all 4 containers start, `web`'s healthcheck goes green,
   `celery_worker`/`celery_beat` register `reconcile_outcomes_task`/
   `run_scheduled_tuff_task`/`run_all_states_tuff_task` correctly.
3. Real endpoints hit successfully against real Redshift/database data (state/node/SE
   directory listings, plan-run listings), confirmed serving the actual frontend
   (`localhost:5173`) traffic, not just curl.
4. A Celery task (`admin_generate_all_states`) run end-to-end inside the container,
   including a full network-wide generation -- `--pool=solo` confirmed working on this
   image's Python 3.12/Linux base, not just carried over untested from the macOS bug
   that originally required it.

Additionally, as of 2026-09-19: a real database corruption incident (see "The
database" section above) was recovered from twice (`sqlite3 .recover`) and the
underlying bind-mount root cause fixed with the named-volume migration -- confirmed
`PRAGMA integrity_check` stays `ok` even while a real write (a live plan generation)
is actively in progress against the volume-backed database.
