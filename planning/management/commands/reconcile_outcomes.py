import sys
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from planning.models import DailyTask, DCVisitStreak
from planning.services import _sql_order_outcomes, _sql_visit_outcomes

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library

# 3+ consecutive misses on the same (SE, DC) pair triggers an escalation note + a
# priority_multiplier boost on the just-reconciled task -- a visible flag in the outcome
# table for chronic non-execution, not a silent internal-only adjustment.
ESCALATION_THRESHOLD = 3
ESCALATION_BOOST = 1.5
MAX_PRIORITY_MULTIPLIER = 3.0


class Command(BaseCommand):
    """Feedback-loop Tier 1: closes the loop from a generated DailyTask back to what
    actually happened. For a past plan_date, checks live Visits/Sales data in the
    [plan_date, plan_date+2] window to mark each task COMPLETED/PARTIAL/MISSED, updates
    the DCVisitStreak per (SE, DC), and escalates DCs missed 3+ times running. Feeds
    `compute_completion_stats`, which in turn feeds the BO1/BO3 adaptive-weighting
    multiplier -- see se_daily_plan_agent.completion_multiplier()."""

    help = "Reconcile DailyTask outcomes against live data for a past plan_date."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="Plan date YYYY-MM-DD to reconcile (must be in the past)")
        parser.add_argument("--scope-type", default=None, help="Restrict to one PlanRun scope type (SE/ABM/NODE/BLOCK/DISTRICT/STATE)")
        parser.add_argument("--scope-value", default=None, help="Restrict to one PlanRun scope value (requires --scope-type)")

    def handle(self, *args, **options):
        plan_date = options["date"]
        if datetime.fromisoformat(plan_date).date() >= timezone.now().date():
            raise CommandError("reconcile_outcomes requires a past plan_date -- outcomes can't exist yet for today or the future.")

        qs = DailyTask.objects.filter(plan_date=plan_date, outcome_status=DailyTask.OutcomeStatus.UNKNOWN)
        if options["scope_type"]:
            qs = qs.filter(plan_run__scope_type=options["scope_type"].upper())
            if options["scope_value"]:
                qs = qs.filter(plan_run__scope_value=options["scope_value"])
        elif options["scope_value"]:
            raise CommandError("--scope-value requires --scope-type.")

        all_tasks = list(qs)
        if not all_tasks:
            self.stdout.write(self.style.WARNING(f"No un-reconciled DailyTask rows found for {plan_date} (already reconciled, or no plan was generated that day)."))
            return

        # Farmer Meeting tasks (8.11, wired 2026-08-06) have no dc_id -- reconciling
        # whether a meeting actually happened needs farmer_in_meeting_vw, a genuinely
        # separate capability this command doesn't build. Skipped honestly (stay
        # outcome_status=UNKNOWN), not silently scored MISSED or crashed on.
        tasks = [t for t in all_tasks if t.dc_id]
        fm_tasks_skipped = len(all_tasks) - len(tasks)
        if not tasks:
            self.stdout.write(self.style.WARNING(f"Only Farmer Meeting task(s) ({fm_tasks_skipped}) found for {plan_date} -- no DC-visit reconciliation to do."))
            return

        se_ids = sorted({int(t.se_id) for t in tasks if t.se_id.isdigit()})
        dc_ids = sorted({t.dc_id for t in tasks})

        client = agent.get_client()
        if not client.configured:
            raise CommandError("Live data client not configured (REDSHIFT_HOST/USER/PASSWORD or METABASE_URL/METABASE_API_KEY) -- reconciliation needs live Visits/Sales data.")

        visited: dict = {}  # (se_id_str, dc_id) -> earliest visit date in window
        try:
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_visit_outcomes(dc_ids, se_ids, plan_date)):
                key = (str(row["se_user_id"]), row["sap_partner_id"])
                d = agent.standardize_date(row["plan_execution_date"])
                if key not in visited or (d and d < visited[key]):
                    visited[key] = d
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Visit-outcome pull failed ({type(e).__name__}: {e}) -- all tasks will be treated as MISSED/UNKNOWN for visits."))

        ordered_value: dict = {}  # dc_id -> largest order amount in window
        try:
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_order_outcomes(dc_ids, plan_date)):
                dc_id = agent.normalize_id(row.get("dc_id"))
                if dc_id:
                    ordered_value[dc_id] = agent.parse_number(row.get("amount_total"))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Order-outcome pull failed ({type(e).__name__}: {e})."))

        now = timezone.now()
        counts = {"COMPLETED": 0, "PARTIAL": 0, "MISSED": 0}
        escalated = 0

        for task in tasks:
            key = (task.se_id, task.dc_id)
            visit_date = visited.get(key)
            order_value = ordered_value.get(task.dc_id)

            if visit_date:
                status = DailyTask.OutcomeStatus.COMPLETED
                task.actual_visit_date = visit_date
            elif order_value:
                status = DailyTask.OutcomeStatus.PARTIAL
            else:
                status = DailyTask.OutcomeStatus.MISSED
            counts[status] += 1

            task.outcome_status = status
            task.actual_order_value = order_value
            task.reconciled_at = now

            streak, _ = DCVisitStreak.objects.get_or_create(se_id=task.se_id, dc_id=task.dc_id)
            if status == DailyTask.OutcomeStatus.MISSED:
                streak.consecutive_misses += 1
            else:
                streak.consecutive_misses = 0
            streak.last_outcome_date = task.plan_date
            streak.save(update_fields=["consecutive_misses", "last_outcome_date", "updated_at"])

            if status == DailyTask.OutcomeStatus.MISSED and streak.consecutive_misses >= ESCALATION_THRESHOLD:
                task.priority_multiplier = round(min(task.priority_multiplier * ESCALATION_BOOST, MAX_PRIORITY_MULTIPLIER), 2)
                note = f" [ESCALATED: missed {streak.consecutive_misses}x running]"
                if "[ESCALATED" not in task.reason_of_visit:
                    task.reason_of_visit = (task.reason_of_visit + note)[:255]
                escalated += 1

            task.save(update_fields=[
                "outcome_status", "actual_visit_date", "actual_order_value", "reconciled_at",
                "priority_multiplier", "reason_of_visit",
            ])

        skipped_note = f" ({fm_tasks_skipped} Farmer Meeting task(s) skipped, no DC to reconcile against)" if fm_tasks_skipped else ""
        self.stdout.write(self.style.SUCCESS(
            f"Reconciled {len(tasks)} task(s) for {plan_date}: "
            f"{counts['COMPLETED']} completed, {counts['PARTIAL']} partial, {counts['MISSED']} missed "
            f"({escalated} newly escalated at {ESCALATION_THRESHOLD}+ consecutive misses){skipped_note}."
        ))
