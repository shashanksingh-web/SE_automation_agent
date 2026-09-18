#!/bin/bash
# Celery Beat -- added 2026-09-18, replacing the crontab entries for
# reconcile_outcomes (0 6 * * *) and run_scheduled_tuff (15 6 * * *). See
# config/settings.py's CELERY_BEAT_SCHEDULE for the schedule itself and
# planning/tasks.py for what each task actually runs. Requires
# scripts/run_celery_worker.sh (or an equivalent worker) running separately --
# Beat only fires tasks into the queue, a worker is what executes them.
set -euo pipefail
cd "$(dirname "$0")/.."
exec venv/bin/celery -A config beat --loglevel=info
