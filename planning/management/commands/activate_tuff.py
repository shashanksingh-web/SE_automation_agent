import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planning.models import PlanRun
from planning.notify import send_alert
from planning.reporting import summary_lines, table_lines
from planning.services import PlanningError, generate_plan_for_scope, make_farmer_meeting_asker

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library


def _todays_run_summary(output_dir: Path):
    """Returns the parsed Run_Summary.json if it exists and its Run_Timestamp falls on
    today's UTC calendar date (Run_Timestamp is written via agent.utc_now_iso(), and
    Django's TIME_ZONE is UTC -- same clock, no conversion needed). None otherwise, so
    the caller always has an unambiguous run-or-skip decision instead of guessing from
    file mtimes (mtime survives a copy/checkout and can lie; the timestamp inside the
    file is the actual pipeline run time)."""
    summary_path = output_dir / "Run_Summary.json"
    if not summary_path.exists():
        return None
    try:
        summary = json.loads(summary_path.read_text())
        run_date = datetime.fromisoformat(summary["Run_Timestamp"]).date()
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    return summary if run_date == datetime.now(timezone.utc).date() else None


class Command(BaseCommand):
    """Agent TUFF -- the full two-agent flow in one command, in-process (no subprocess,
    no poll loop): Step 1 runs the Data Normalization Agent, Step 2 runs the SE Daily
    Task Agent for the requested scope (which itself auto-triggers the Pitching Agent
    right after DailyTask rows are created -- see planning.services.generate_plan_for_scope
    and planning/pitching.py), Step 3 prints the outcome table. Approved design
    2026-08-06, replacing the previous manual 2-command + background-process + wait-loop
    sequencing. See AGENT_OPERATING_PROMPTS.md and the reference_tuff_agent_name /
    ask-custom-config-before-planning memories for the full history -- the custom-config
    gate that used to sit between steps 1 and 2 is retired (all 4 values are fixed in
    Source 5 now), so this is a straight 2-step pipeline, not 3.

    Step 1 default behavior (2026-08-09): Data Normalization only actually re-runs once
    per UTC calendar day -- if output/Run_Summary.json already has a Run_Timestamp from
    today, it's reused automatically and Step 1 is skipped, since re-pulling the same
    live sources repeatedly in one day wastes Redshift/Metabase load for no new data.
    Use --force-normalization to override this and pull fresh live data anyway (e.g. if
    you know upstream data changed intraday); --skip-normalization still unconditionally
    skips Step 1 even on stale/missing data, for callers who've already checked."""

    help = "Activate Agent TUFF: run the Data Normalization Agent (once per day, auto-reused after) then the SE Daily Task + Pitching Agents for one scope."

    def add_arguments(self, parser):
        parser.add_argument("scope_type", choices=[c.value for c in PlanRun.ScopeType], help="SE, ABM, NODE, BLOCK, DISTRICT, or STATE")
        parser.add_argument("scope_value", help="e.g. an SE email, a node name, a state name, an ABM employee code, ...")
        parser.add_argument("--date", default=None, help="Plan date YYYY-MM-DD, used for BOTH steps (default: today)")
        parser.add_argument(
            "--skip-normalization", action="store_true",
            help="Unconditionally skip Step 1 and reuse whatever is in output/ now, even if stale -- for callers who've already verified freshness themselves",
        )
        parser.add_argument(
            "--force-normalization", action="store_true",
            help="Force Step 1 to re-run live even if today's normalization already ran earlier -- use when you know upstream data changed intraday",
        )
        parser.add_argument(
            "--confirm-farmer-meeting", action="append", default=[], metavar="EMAIL",
            help="Explicitly confirm a Farmer Meeting for this SE email today, no interactive terminal needed "
                 "(repeatable for multiple SEs). Applies even if the SE isn't FM_Urgency-flagged this run.",
        )

    def _abort_if_empty(self, scope_type, scope_value, row_counts):
        """Shared guard: Step 2's scope resolution hard-depends on DC_Master_Normalized
        having real rows (planning.services.load_dc_master()). Must run on every path
        that lets Step 2 proceed against output/ -- a fresh Step 1 run, an auto-reused
        same-day run, AND an explicit --skip-normalization -- not just the fresh-run
        path, or a corrupted/empty output/ from earlier in the day gets silently reused
        by every later activation instead of failing loudly once."""
        dc_master_rows = row_counts.get("DC_Master_Normalized", 0)
        if dc_master_rows == 0:
            send_alert(
                f"activate_tuff {scope_type}={scope_value}: DC_Master_Normalized has 0 rows in "
                "output/ -- aborted before Step 2.", severity="critical",
            )
            raise CommandError(
                "TUFF aborted: DC_Master_Normalized has 0 rows -- the SE Daily Task Agent "
                "has nothing to resolve scope against. Not proceeding to Step 2 on "
                "empty/failed normalization output (re-run with --force-normalization)."
            )

    def handle(self, *args, **options):
        if options["skip_normalization"] and options["force_normalization"]:
            raise CommandError("--skip-normalization and --force-normalization are mutually exclusive.")

        output_dir = Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output"
        reusable_summary = None if options["force_normalization"] else _todays_run_summary(output_dir)

        if options["skip_normalization"]:
            self.stdout.write(self.style.WARNING("=== TUFF Step 1 skipped (--skip-normalization) -- reusing existing output/ as-is ==="))
            summary_path = output_dir / "Run_Summary.json"
            if summary_path.exists():
                try:
                    self._abort_if_empty(options["scope_type"], options["scope_value"], json.loads(summary_path.read_text())["Row_Counts"])
                except (json.JSONDecodeError, KeyError):
                    pass  # unreadable summary -- let Step 2 itself fail loudly rather than guessing here
            self.stdout.write("")
        elif reusable_summary is not None:
            self.stdout.write(self.style.WARNING(
                f"=== TUFF Step 1 skipped -- today's normalization already ran at "
                f"{reusable_summary['Run_Timestamp']}, reusing output/ (pass --force-normalization to override) ==="
            ))
            self.stdout.write(f"  Row_Counts: {reusable_summary['Row_Counts']}")
            self._abort_if_empty(options["scope_type"], options["scope_value"], reusable_summary["Row_Counts"])
            self.stdout.write("")
        else:
            self.stdout.write(self.style.SUCCESS("=== TUFF Step 1: Data Normalization Agent ==="))
            run_summary = agent.run_pipeline(output_dir, options["date"])

            self.stdout.write(f"  {run_summary['Note']}")
            self.stdout.write(f"  Row_Counts: {run_summary['Row_Counts']}")
            fails = {k: v for k, v in run_summary["Check_Summary"].items() if v.get("fail")}
            if fails:
                self.stdout.write(f"  Exceptions (fail counts): {fails}")

            self._abort_if_empty(options["scope_type"], options["scope_value"], run_summary["Row_Counts"])
            self.stdout.write("")

        self.stdout.write(self.style.SUCCESS("=== TUFF Step 2: SE Daily Task Agent ==="))
        try:
            plan_run = generate_plan_for_scope(
                options["scope_type"], options["scope_value"], options["date"],
                farmer_meeting_asker=make_farmer_meeting_asker(self.stdout, self.style),
                farmer_meeting_confirmed_emails=set(options["confirm_farmer_meeting"]),
            )
        except PlanningError as e:
            raise CommandError(str(e))

        lines = summary_lines(plan_run)
        self.stdout.write(self.style.SUCCESS(lines[0]))
        for line in lines[1:]:
            self.stdout.write(line)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== TUFF Outcome ==="))
        for line in table_lines(plan_run):
            self.stdout.write(line)
