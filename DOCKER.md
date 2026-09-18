# Docker setup

Backend only (this repo). The frontend ("New Lead gen model") is a separate project,
deployed independently, same as today. Not live-verified against an actual Docker
build (Docker wasn't installed in the environment this was written in) -- read the
"What to check before trusting this" section below before relying on it.

## Services

- **redis** -- Celery's broker.
- **web** -- gunicorn serving the Django app on :8000.
- **celery_worker** -- runs `reconcile_outcomes_task`/`run_scheduled_tuff_task`/
  `run_all_states_tuff_task` (see `planning/tasks.py`).
- **celery_beat** -- fires the two scheduled tasks at the same times the old crontab
  did (6:00 AM reconcile, 6:15 AM tuff -- see `config/settings.py`'s
  `CELERY_BEAT_SCHEDULE`).

## One-time setup before the first `docker compose up`

1. `cp .env.docker.example .env` and fill in real values -- at minimum `SECRET_KEY`,
   `DEBUG=False`, `REDSHIFT_*`, and whichever LLM provider(s) you're using. See that
   file's own comments for what's required vs. optional.

2. **Create the SQLite files on the host before first run.** Docker bind-mounts a
   path that doesn't exist yet as a *directory*, not a file -- if `db.sqlite3` etc.
   don't already exist on the host, the containers will find a directory where they
   expect a file and fail to start Django at all.

   ```
   touch db.sqlite3 db.sqlite3-wal db.sqlite3-shm
   chmod 666 db.sqlite3 db.sqlite3-wal db.sqlite3-shm
   ```

   The `chmod` matters too, not just the `touch`: bind-mounted files keep whatever
   ownership/permissions they have on the host -- the container runs as a non-root
   user (`appuser`, uid 1000) for security, which very likely won't match your host
   user's uid, so without this the app can read the database but fail to write to it.
   Same reasoning applies to `output/` and `logs/` if you hit permission errors there
   too (`chmod -R 777 output logs`, or match ownership to uid 1000 directly if you'd
   rather not open permissions that wide).

3. Make sure `DC_RAnk.csv`, `config and parameter /`, `Niyojan Q2-FY_26_27 Dashboard -
   Planning.csv`, and `pitch_config/` already exist at the project root (they should,
   if you're running this from a normal checkout that's already been used locally) --
   these are bind-mounted in as-is, not created automatically.

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

## What to check before trusting this in production

This was built by careful review of the app's actual settings/URLs/task code, but
**Docker itself was not installed in the environment this was written in, so none of
it has been built or run.** Before relying on it:

1. `docker compose build` -- confirm the image actually builds (dependency
   resolution, no missing system libs).
2. `docker compose up` -- confirm all 4 containers start and `web`'s healthcheck goes
   green; watch `celery_worker`/`celery_beat` logs for both tasks registering
   correctly (matching what's documented in `planning/tasks.py`).
3. Hit a real endpoint (`curl http://localhost:8000/api/planning/se/v1/<a real SE
   email>/?date=<today>`) and confirm it reaches the real Redshift/database
   correctly, the same way every change in this project was live-verified locally.
4. Confirm a Celery task actually runs end-to-end inside the container (e.g. trigger
   `admin_generate_all_states` and watch `celery_worker`'s logs) -- the `--pool=solo`
   choice in particular is carried over from a macOS-specific bug and deserves a real
   check on whatever platform this actually gets deployed to.
