"""Celery app for this project -- added 2026-09-18 to replace the macOS-specific
scheduling this project used to depend on (launchd plists, then plain crontab with a
hardcoded absolute venv path) with a portable, in-app scheduler. Celery Beat reads
CELERY_BEAT_SCHEDULE in config/settings.py and fires the two tasks in planning/tasks.py
on the same real-world times the old crontab entries used (see that schedule's own
comment for the crontab -> Celery Beat correspondence).

Two separate processes run this app, both from the project root:
    celery -A config worker -l info   -- executes tasks
    celery -A config beat   -l info   -- fires scheduled tasks into the worker's queue
Neither depends on launchd/cron; run them the same way any other background process in
this project is run (see scripts/ for examples), on Linux exactly as on macOS."""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("se_automation_server")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
