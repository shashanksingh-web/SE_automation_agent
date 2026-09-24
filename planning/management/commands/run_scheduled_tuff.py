from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from planning import agent  # moved from a sys.path-inserted top-level script to planning/agent.py 2026-09-18
from planning.concurrency import run_scopes_concurrently
from planning.locking import LockContendedError, orchestration_lock
from planning.models import ScheduledScope
from planning.notify import send_alert
from planning.services import PlanningError, generate_plan_for_scope

# Same bounded-concurrency reasoning as run_all_states_tuff.py's own MAX_CONCURRENT_STATES
# (added there 2026-09-19; brought to this command 2026-09-24, explicit user request --
# this command's Step 2 was still a plain sequential loop, ~93 scopes at ~1.5min each =
# ~80min for a full pass). Same value to start -- memory-bound, not CPU-bound, same
# reasoning as the sibling command's own comment; tune down if `docker stats` shows this
# getting close to the VM's memory limit.
MAX_CONCURRENT_SCOPES = 3


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

    Step 2 runs up to MAX_CONCURRENT_SCOPES scopes concurrently (added 2026-09-24,
    explicit user request to parallelize -- scopes are fully independent of each other,
    matching run_all_states_tuff.py's own already-parallel Step 2), via the shared
    planning.concurrency.run_scopes_concurrently helper. Also holds
    planning.locking.orchestration_lock() for its entire run (Step 1 + all of Step 2) --
    real incident, 2026-09-24: a manual run_scheduled_tuff invocation and celery_worker's
    own scheduled copy of this exact command ran concurrently against the same
    ScheduledScope rows, causing real data loss (DNS resolution failures from the
    resulting resource contention, and DailyTask rows that silently ended up with zero
    PitchScript, no exception ever logged). See planning.locking's own module docstring
    for the full incident and the two-tier lock design (this lock plus a narrower
    per-scope lock generate_plan_for_scope now acquires on its own, covering every
    entrypoint, not just this command)."""

    help = "Run Agent TUFF for every active ScheduledScope (cron entrypoint)."

    def add_arguments(self, parser):
        parser.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today)")

    def _generate_one_scope(self, scope, plan_date):
        """One thread's unit of work -- mirrors run_all_states_tuff.py's own
        _generate_one_state shape (returns a (outcome, payload) tuple instead of writing
        stdout/stderr directly, since two threads writing to self.stdout concurrently
        would interleave individual write() calls unpredictably -- the caller prints
        once per completed future instead, off the main thread). Also updates
        scope.last_run_at on success, same as the old sequential loop did inline --
        moved in here since it's per-scope work, same reasoning as
        generate_plan_for_scope's own call living in here rather than the caller."""
        try:
            plan_run = generate_plan_for_scope(scope.scope_type, scope.scope_value, plan_date)
            scope.last_run_at = timezone.now()
            scope.save(update_fields=["last_run_at"])
            return "ok", plan_run
        except PlanningError as e:
            return "planning_error", e
        except Exception as e:
            return "crashed", e
        finally:
            close_old_connections()

    def handle(self, *args, **options):
        try:
            with orchestration_lock():
                self._run(options)
        except LockContendedError as e:
            self.stderr.write(self.style.WARNING(f"run_scheduled_tuff: {e}"))
            send_alert(f"run_scheduled_tuff: {e}", severity="warning")

    def _run(self, options):
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
        self.stdout.write(self.style.SUCCESS(
            f"=== run_scheduled_tuff Step 2: SE Daily Task Agent "
            f"({len(scopes)} scope(s), up to {MAX_CONCURRENT_SCOPES} concurrent) ==="
        ))
        succeeded, failed, fm_urgency_total = 0, 0, 0
        results = run_scopes_concurrently(
            scopes, lambda scope: self._generate_one_scope(scope, options["date"]), MAX_CONCURRENT_SCOPES,
        )
        for scope, outcome, payload in results:
            if outcome == "ok":
                succeeded += 1
                plan_run = payload
                self.stdout.write(self.style.SUCCESS(
                    f"  {scope.scope_type}={scope.scope_value}: PlanRun #{plan_run.id}, {plan_run.task_count} tasks"
                ))
                # No farmer_meeting_asker was passed -- cron has no human to ask, so
                # every SE stays DC Visit by default (see generate_plan_for_scope's own
                # docstring). FM_Urgency is still computed and persisted as an
                # ExceptionRecord (reason_code=FM_Urgency_Provisional) either way --
                # surfaced here so whoever reads logs/cron_tuff.log each morning knows
                # which SEs need a manual Farmer Meeting decision, run interactively via
                # activate_tuff/generate_se_plan from a real terminal (that's where the
                # actual y/N prompt lives, not here).
                fm_flags = list(plan_run.exceptions.filter(reason_code="FM_Urgency_Provisional"))
                if fm_flags:
                    fm_urgency_total += len(fm_flags)
                    self.stdout.write(self.style.WARNING(
                        f"    {len(fm_flags)} SE(s) flagged FM_Urgency -- needs a manual Farmer Meeting decision:"
                    ))
                    for exc in fm_flags:
                        self.stdout.write(f"      - {exc.detail}")
            elif outcome == "planning_error":
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {scope.scope_type}={scope.scope_value}: {payload}"))
                send_alert(f"run_scheduled_tuff: scope {scope.scope_type}={scope.scope_value} failed: {payload}", severity="error")
            else:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {scope.scope_type}={scope.scope_value}: unexpected {type(payload).__name__}: {payload}"))
                send_alert(f"run_scheduled_tuff: scope {scope.scope_type}={scope.scope_value} crashed: {type(payload).__name__}: {payload}", severity="error")

        self.stdout.write("")
        fm_note = f" {fm_urgency_total} SE(s) across all scopes flagged FM_Urgency -- review the log above for who needs a manual Farmer Meeting decision." if fm_urgency_total else ""
        self.stdout.write(f"Done: {succeeded} succeeded, {failed} failed out of {len(scopes)} scope(s).{fm_note}")
