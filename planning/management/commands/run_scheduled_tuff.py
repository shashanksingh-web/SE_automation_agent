import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from planning.models import ScheduledScope
from planning.notify import send_alert
from planning.services import PlanningError, generate_plan_for_scope

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library


class Command(BaseCommand):
    """Cron entrypoint for Agent TUFF -- runs Step 1 (Data Normalization Agent) once,
    then Step 2 (SE Daily Task Agent) for every active ScheduledScope, isolating
    failures per scope so one bad scope doesn't block the rest. This replaces manually
    typing `activate_tuff <SCOPE> <VALUE>` every day for a fixed set of scopes.

    Add scopes via the Django shell/admin, e.g.:
        python manage.py shell -c \
            "from planning.models import ScheduledScope; ScheduledScope.objects.create(scope_type='NODE', scope_value='Kota')"

    Intended crontab lines (adjust paths/venv -- NOT installed automatically):
        0 6 * * *  cd /path/to/project && venv/bin/python manage.py reconcile_outcomes --date $(date -d yesterday +\\%Y-\\%m-\\%d)
        15 6 * * * cd /path/to/project && venv/bin/python manage.py run_scheduled_tuff
    reconcile_outcomes runs first so today's scoring sees fresh completion stats, not stale
    (see planning.models.ObjectiveCompletionStats / se_daily_plan_agent.completion_multiplier).

    Farmer Meeting (8.11 FM_Urgency): unattended, so no SE ever gets auto-scheduled --
    DC Visit stays the default for every scope. Any SE flagged FM_Urgency this run is
    still printed here (with the full pacing detail) so whoever checks the log knows who
    needs a manual decision; run activate_tuff/generate_se_plan from a real terminal for
    that SE to actually get asked and confirm one.
    """

    help = "Run Agent TUFF for every active ScheduledScope (cron entrypoint)."

    def add_arguments(self, parser):
        parser.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today)")

    def handle(self, *args, **options):
        output_dir = Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output"
        self.stdout.write(self.style.SUCCESS("=== run_scheduled_tuff Step 1: Data Normalization Agent ==="))
        try:
            run_summary = agent.run_pipeline(output_dir, options["date"])
        except Exception as e:
            send_alert(f"run_scheduled_tuff: Step 1 (Data Normalization Agent) crashed: {type(e).__name__}: {e}", severity="critical")
            raise

        self.stdout.write(f"  Row_Counts: {run_summary['Row_Counts']}")
        dc_master_rows = run_summary["Row_Counts"].get("DC_Master_Normalized", 0)
        if dc_master_rows == 0:
            send_alert("run_scheduled_tuff: Step 1 produced 0 DC_Master_Normalized rows -- aborting all scheduled scopes.", severity="critical")
            self.stderr.write(self.style.ERROR("Aborted: DC_Master_Normalized has 0 rows -- nothing to plan against."))
            return

        scopes = list(ScheduledScope.objects.filter(active=True))
        if not scopes:
            self.stdout.write(self.style.WARNING("No active ScheduledScope rows -- nothing to run. Add one first (see this command's help text)."))
            return

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"=== run_scheduled_tuff Step 2: SE Daily Task Agent ({len(scopes)} scope(s)) ==="))
        succeeded, failed, fm_urgency_total = 0, 0, 0
        for scope in scopes:
            try:
                # No farmer_meeting_asker passed -- cron has no human to ask, so every SE
                # stays DC Visit by default (see generate_plan_for_scope()'s docstring).
                # FM_Urgency is still computed and persisted as an ExceptionRecord
                # (reason_code=FM_Urgency_Provisional) either way -- surfaced here so
                # whoever reads logs/cron_tuff.log each morning knows which SEs need a
                # manual Farmer Meeting decision, run interactively via activate_tuff/
                # generate_se_plan from a real terminal (that's where the actual y/N
                # prompt lives, not here).
                plan_run = generate_plan_for_scope(scope.scope_type, scope.scope_value, options["date"])
                scope.last_run_at = timezone.now()
                scope.save(update_fields=["last_run_at"])
                succeeded += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  {scope.scope_type}={scope.scope_value}: PlanRun #{plan_run.id}, {plan_run.task_count} tasks"
                ))
                fm_flags = list(plan_run.exceptions.filter(reason_code="FM_Urgency_Provisional"))
                if fm_flags:
                    fm_urgency_total += len(fm_flags)
                    self.stdout.write(self.style.WARNING(
                        f"    {len(fm_flags)} SE(s) flagged FM_Urgency -- needs a manual Farmer Meeting decision:"
                    ))
                    for exc in fm_flags:
                        self.stdout.write(f"      - {exc.detail}")
            except PlanningError as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {scope.scope_type}={scope.scope_value}: {e}"))
                send_alert(f"run_scheduled_tuff: scope {scope.scope_type}={scope.scope_value} failed: {e}", severity="error")
            except Exception as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {scope.scope_type}={scope.scope_value}: unexpected {type(e).__name__}: {e}"))
                send_alert(f"run_scheduled_tuff: scope {scope.scope_type}={scope.scope_value} crashed: {type(e).__name__}: {e}", severity="error")

        self.stdout.write("")
        fm_note = f" {fm_urgency_total} SE(s) across all scopes flagged FM_Urgency -- review the log above for who needs a manual Farmer Meeting decision." if fm_urgency_total else ""
        self.stdout.write(f"Done: {succeeded} succeeded, {failed} failed out of {len(scopes)} scope(s).{fm_note}")
