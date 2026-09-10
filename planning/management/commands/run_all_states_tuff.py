from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from planning.directory import list_states
from planning.notify import send_alert
from planning.services import PlanningError, generate_plan_for_scope
from planning.services import _output_dir as _planning_output_dir

import sys

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library


class Command(BaseCommand):
    """Admin-triggered "generate for everyone" entrypoint (added 2026-09-10, explicit
    user request via the System Plan Runs page -- "system run plan means it will
    generate the plan for all se with eligible dc"). Same two-step shape as
    run_scheduled_tuff (Step 1 Data Normalization once, then Step 2 SE Daily Task Agent
    per scope, isolating failures per scope), but iterates every STATE currently present
    in DC_Master_Normalized rather than the ScheduledScope table -- a STATE-scoped
    PlanRun already covers every SE under it (see generate_plan_for_scope), so this
    reaches the same "all SEs, eligible DCs only" outcome in ~11-12 broader runs instead
    of run_scheduled_tuff's 93 NODE-level ones. Meant to be launched as a detached
    background subprocess from admin_generate_all_states (planning/views.py), not run
    inline in a request -- a full pass takes real minutes and hits live data sources for
    every state.

    Does NOT touch or replace ScheduledScope/run_scheduled_tuff -- that cron job keeps
    running exactly as before; this is a separate, on-demand, manually-triggered path."""

    help = "Run Agent TUFF for every STATE in DC_Master_Normalized (on-demand 'generate for everyone')."

    def add_arguments(self, parser):
        parser.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today)")

    def handle(self, *args, **options):
        output_dir = Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output"
        self.stdout.write(self.style.SUCCESS("=== run_all_states_tuff Step 1: Data Normalization Agent ==="))
        try:
            run_summary = agent.run_pipeline(output_dir, options["date"])
        except Exception as e:
            send_alert(f"run_all_states_tuff: Step 1 (Data Normalization Agent) crashed: {type(e).__name__}: {e}", severity="critical")
            raise

        self.stdout.write(f"  Row_Counts: {run_summary['Row_Counts']}")
        dc_master_rows = run_summary["Row_Counts"].get("DC_Master_Normalized", 0)
        if dc_master_rows == 0:
            send_alert("run_all_states_tuff: Step 1 produced 0 DC_Master_Normalized rows -- aborting all states.", severity="critical")
            self.stderr.write(self.style.ERROR("Aborted: DC_Master_Normalized has 0 rows -- nothing to plan against."))
            return

        states = [row["state"] for row in list_states(_planning_output_dir())]
        if not states:
            self.stdout.write(self.style.WARNING("No states found in DC_Master_Normalized -- nothing to run."))
            return

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"=== run_all_states_tuff Step 2: SE Daily Task Agent ({len(states)} state(s)) ==="))
        succeeded, failed = 0, 0
        for state in states:
            try:
                plan_run = generate_plan_for_scope("STATE", state, options["date"])
                succeeded += 1
                self.stdout.write(self.style.SUCCESS(
                    f"  STATE={state}: PlanRun #{plan_run.id}, {plan_run.se_count} SEs, {plan_run.task_count} tasks"
                ))
            except PlanningError as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  STATE={state}: {e}"))
                send_alert(f"run_all_states_tuff: state {state} failed: {e}", severity="error")
            except Exception as e:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  STATE={state}: unexpected {type(e).__name__}: {e}"))
                send_alert(f"run_all_states_tuff: state {state} crashed: {type(e).__name__}: {e}", severity="error")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"=== Done: {succeeded} succeeded, {failed} failed, {len(states)} total states ==="))
