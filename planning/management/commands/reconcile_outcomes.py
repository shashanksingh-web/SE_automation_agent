import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planning.reconciliation import ReconciliationError, format_summary, reconcile_plan_date

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library


class Command(BaseCommand):
    """Feedback-loop Tier 1: closes the loop from a generated DailyTask back to what
    actually happened -- see planning.reconciliation for the full picture. Since
    2026-09-16 this is one of three entry points to the same logic (the others: every
    generate_plan_for_scope run reconciles its own SEs' past tasks first, and the
    Tracking dashboard's "Reconcile now"), so a scheduler is no longer load-bearing.
    Feeds `compute_completion_stats`, which in turn feeds the BO1/BO3 adaptive-
    weighting multiplier -- see se_daily_plan_agent.completion_multiplier()."""

    help = "Reconcile DailyTask outcomes against live data for a past plan_date."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="Plan date YYYY-MM-DD to reconcile (must be in the past)")
        parser.add_argument("--scope-type", default=None, help="Restrict to one PlanRun scope type (SE/ABM/RBM/NODE/BLOCK/DISTRICT/STATE)")
        parser.add_argument("--scope-value", default=None, help="Restrict to one PlanRun scope value (requires --scope-type)")
        parser.add_argument("--rebuild", action="store_true", help="Re-score every DC-visit task for the date, already-reconciled ones included (after an outcome-rule change); run rebuild_streaks afterwards for a multi-date rebuild")

    def handle(self, *args, **options):
        if options["scope_value"] and not options["scope_type"]:
            raise CommandError("--scope-value requires --scope-type.")
        client = agent.get_client()
        try:
            summary = reconcile_plan_date(
                options["date"], client=client, scope_type=options["scope_type"], scope_value=options["scope_value"],
                rebuild=options["rebuild"],
            )
        except ReconciliationError as e:
            raise CommandError(str(e))
        finally:
            client.close()
        if summary["tasks"] == 0 and not summary["farmer_meeting_skipped"]:
            self.stdout.write(self.style.WARNING(f"No un-reconciled DailyTask rows found for {options['date']} (already reconciled, or no plan was generated that day)."))
            return
        style = self.style.WARNING if summary["pull_failures"] else self.style.SUCCESS
        self.stdout.write(style(format_summary(summary)))
