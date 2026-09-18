"""Celery Beat's two scheduled jobs -- added 2026-09-18, replacing the crontab entries
that used to fire `manage.py reconcile_outcomes`/`manage.py run_scheduled_tuff`
directly (see config/settings.py's CELERY_BEAT_SCHEDULE for the exact times, matched
to what the old crontab ran). Each task calls the exact same management command the
cron job used to -- via call_command(), not a reimplementation -- so this only changes
the trigger mechanism, not the logic; both commands remain directly runnable by hand
(`manage.py reconcile_outcomes --date ...` / `manage.py run_scheduled_tuff`) exactly as
before, for manual/debugging use."""

from datetime import date, timedelta

from celery import shared_task
from django.core.management import call_command


@shared_task
def reconcile_outcomes_task() -> None:
    """Matches the old `0 6 * * * ... reconcile_outcomes --date $(date -v-1d ...)`
    crontab entry -- always reconciles yesterday's plan date."""
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    call_command("reconcile_outcomes", date=yesterday)


@shared_task
def run_scheduled_tuff_task() -> None:
    """Matches the old `15 6 * * * ... run_scheduled_tuff` crontab entry."""
    call_command("run_scheduled_tuff")
