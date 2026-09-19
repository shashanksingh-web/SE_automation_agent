from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from planning import agent  # moved from a sys.path-inserted top-level script to planning/agent.py 2026-09-18
from planning.directory import list_states
from planning.notify import send_alert
from planning.services import PlanningError, generate_plan_for_scope
from planning.services import _output_dir as _planning_output_dir

# Bounded, not unlimited (2026-09-19, explicit user request to parallelize the
# previously-sequential Step 2 loop -- an 11-state run took ~1h51min end to end).
# Memory is the real constraint here, not CPU: a single large state (Bihar-scale,
# ~5,000 DCs) observed ~1GB peak for the web/worker process the same day a 2GB Colima
# VM OOM-killed a gunicorn worker mid-generation (fixed by bumping to 6GB, but that
# was sized for ONE state running at a time). 3 concurrent states stays comfortably
# under 6GB with headroom for gunicorn/redis/celery_beat, without the OOM risk full
# 11-way parallelism would reintroduce. Tune down if `docker stats` shows this cap
# still gets close to the VM's memory limit.
MAX_CONCURRENT_STATES = 3


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
    running exactly as before; this is a separate, on-demand, manually-triggered path.

    Step 2 runs up to MAX_CONCURRENT_STATES states concurrently (added 2026-09-19,
    explicit user request to parallelize -- states are fully independent of each
    other, nothing about the previous strict sequencing was required by the data).
    Uses real OS threads (concurrent.futures.ThreadPoolExecutor), not Celery's own
    worker pool -- this task already runs under Celery's --pool=solo (kept for an
    unrelated macOS/Python 3.14 prefork bug, deliberately not revisited here), and
    generate_plan_for_scope's live-data-pull-then-write shape is I/O bound enough
    that plain threads are sufficient without needing process-level parallelism.
    Confirmed safe against this app's SQLite setup the same day this was written:
    concurrent generate_plan_for_scope calls across different states, run via
    separate docker exec processes, produced no corruption once db.sqlite3 moved off
    the bind mount onto a named volume (see config/settings.py's WAL + IMMEDIATE +
    300s-busy-timeout DATABASES config, built for exactly this). Each worker thread
    gets its own Django DB connection automatically (thread-local by default);
    close_old_connections() at the end of each thread's work prevents those from
    accumulating across repeated runs in this long-lived Celery worker process,
    since nothing outside a request cycle closes them otherwise."""

    help = "Run Agent TUFF for every STATE in DC_Master_Normalized (on-demand 'generate for everyone')."

    def add_arguments(self, parser):
        parser.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today)")

    def _generate_one_state(self, state, plan_date):
        """One thread's unit of work -- mirrors the try/except shape the old sequential
        loop had per state, just returning a (state, outcome) tuple instead of writing
        stdout/stderr directly (two threads writing to self.stdout concurrently would
        interleave individual write() calls unpredictably; the caller prints once per
        completed future instead, off the main thread, so output stays one clean line
        per state)."""
        try:
            plan_run = generate_plan_for_scope("STATE", state, plan_date)
            return state, ("ok", plan_run)
        except PlanningError as e:
            return state, ("planning_error", e)
        except Exception as e:
            return state, ("crashed", e)
        finally:
            close_old_connections()

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

        state_rows = list_states(_planning_output_dir())
        if not state_rows:
            self.stdout.write(self.style.WARNING("No states found in DC_Master_Normalized -- nothing to run."))
            return
        # Largest DC count first (longest-job-first scheduling): under a fixed
        # MAX_CONCURRENT_STATES worker cap, starting the slowest states immediately
        # rather than having them queue up behind small ones minimizes total makespan.
        states = [row["state"] for row in sorted(state_rows, key=lambda r: r["dc_count"], reverse=True)]

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"=== run_all_states_tuff Step 2: SE Daily Task Agent "
            f"({len(states)} state(s), up to {MAX_CONCURRENT_STATES} concurrent) ==="
        ))
        succeeded, failed = 0, 0
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_STATES) as pool:
            futures = {pool.submit(self._generate_one_state, state, options["date"]): state for state in states}
            for future in as_completed(futures):
                state, (outcome, payload) = future.result()
                if outcome == "ok":
                    succeeded += 1
                    plan_run = payload
                    self.stdout.write(self.style.SUCCESS(
                        f"  STATE={state}: PlanRun #{plan_run.id}, {plan_run.se_count} SEs, {plan_run.task_count} tasks"
                    ))
                elif outcome == "planning_error":
                    failed += 1
                    self.stderr.write(self.style.ERROR(f"  STATE={state}: {payload}"))
                    send_alert(f"run_all_states_tuff: state {state} failed: {payload}", severity="error")
                else:
                    failed += 1
                    self.stderr.write(self.style.ERROR(f"  STATE={state}: unexpected {type(payload).__name__}: {payload}"))
                    send_alert(f"run_all_states_tuff: state {state} crashed: {type(payload).__name__}: {payload}", severity="error")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"=== Done: {succeeded} succeeded, {failed} failed, {len(states)} total states ==="))
