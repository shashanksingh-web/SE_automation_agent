"""Long-running agent tasks run via Celery -- reconcile_outcomes_task/
run_scheduled_tuff_task added 2026-09-18 as Celery Beat's two scheduled jobs,
replacing the crontab entries that used to fire `manage.py reconcile_outcomes`/
`manage.py run_scheduled_tuff` directly (see config/settings.py's CELERY_BEAT_SCHEDULE
for the exact times, matched to what the old crontab ran). run_all_states_tuff_task
added the same day, replacing admin_generate_all_states' own subprocess.Popen launch
of the same command (planning/views.py) -- same fire-and-forget contract (no dedicated
progress view, System Plan Runs/GET /runs/ is how a caller watches it land, unchanged
by this), just a more robust launching mechanism than hand-rolling `sys.executable
manage.py ...` as a detached subprocess.

Every task here calls the exact same management command its predecessor (cron or
subprocess) used to -- via call_command(), not a reimplementation -- so this only ever
changes the trigger mechanism, not the underlying logic; every one of these commands
remains directly runnable by hand (`manage.py reconcile_outcomes --date ...` /
`manage.py run_scheduled_tuff` / `manage.py run_all_states_tuff`) exactly as before,
for manual/debugging use."""

from datetime import date, timedelta
from typing import Optional

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


@shared_task
def run_all_states_tuff_task(plan_date: Optional[str] = None, log_path: Optional[str] = None) -> None:
    """Replaces admin_generate_all_states' own subprocess.Popen(["manage.py",
    "run_all_states_tuff", ...]) -- same command, same optional --date, just launched
    as a Celery task instead of a hand-built detached subprocess. log_path (if given)
    gets the command's own stdout/stderr (its "=== Step N ===" / "STATE=X: PlanRun #Y"
    progress lines) via call_command's own stdout/stderr kwargs, preserving the
    per-invocation timestamped log file the old subprocess-redirect gave callers to
    inspect after the fact -- the deeper per-source Redshift/LLM progress logger calls
    still go to logs/tuff.log as they always have, regardless of entry point, not
    duplicated here."""
    kwargs = {"date": plan_date} if plan_date else {}
    if log_path:
        with open(log_path, "a") as f:
            call_command("run_all_states_tuff", stdout=f, stderr=f, **kwargs)
    else:
        call_command("run_all_states_tuff", **kwargs)
