"""Outcome reconciliation -- closes the loop from a generated DailyTask back to what
actually happened (the Tracking dashboard's Tier 1, and the only writer of
DailyTask.outcome_status / actual_* / reconciled_at).

Lifted out of `manage.py reconcile_outcomes` on 2026-09-16 (explicit user request:
"fix reconcile also"). That command had NEVER successfully run: its launchd schedule
(com.dehaat.se-automation.reconcile, 06:00 daily) dies with "Operation not permitted"
because the project lives under ~/Desktop, a macOS-privacy-protected folder that
background launchd jobs can't read -- 20 attempts logged, 0 reconciliations, plist
since disabled. Meanwhile 5,900+ tasks accumulated with outcome_status UNKNOWN.

So reconciliation no longer depends on a scheduler at all. It runs:
  1. inside generate_plan_for_scope, for the SEs in scope, before their new plan is
     built -- an SE opening today's plan reconciles yesterday's outcomes for that SE,
     which is exactly when the result is needed: DCVisitStreak.consecutive_misses
     feeds the Critical flag on the plan about to be generated;
  2. on demand from the Tracking dashboard (POST /admin/reconcile/), network-wide;
  3. still from the CLI (`manage.py reconcile_outcomes --date`), now a thin wrapper.

What a reconciliation records, per task, from live data in [plan_date, plan_date+2]:
  COMPLETED  a task_management_task DC visit by that SE at that DC (actual_visit_date)
  PARTIAL    no visit, but the DC placed an order (actual_order_value)
  MISSED     neither
plus actual_payment_amount -- SUCCESS payments by the DC in the window (added here;
the command never recorded collection, so the Promise-To-Pay half of the system was
unmeasurable), the (SE, DC) DCVisitStreak, and the 3+-miss escalation. Farmer
Meeting tasks (no dc_id) are left UNKNOWN, never guessed. Only tasks still UNKNOWN
are touched, so every entry point is idempotent and safe to call again any time.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.utils import timezone

from .models import DailyTask, DCVisitStreak
from .services import _sql_order_outcomes, _sql_payment_outcomes, _sql_visit_outcomes

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library

# 3+ consecutive misses on the same (SE, DC) pair triggers an escalation note + a
# priority_multiplier boost on the just-reconciled task -- a visible flag in the outcome
# table for chronic non-execution, not a silent internal-only adjustment. Threshold lives
# on DCVisitStreak itself (single source of truth -- also used by services.
# generate_plan_for_scope's Critical flag), not duplicated here.
ESCALATION_THRESHOLD = DCVisitStreak.ESCALATION_THRESHOLD
ESCALATION_BOOST = 1.5
MAX_PRIORITY_MULTIPLIER = 3.0


class ReconciliationError(Exception):
    pass


def _pending_tasks(plan_date: str, se_ids: Optional[List[str]], scope_type: Optional[str], scope_value: Optional[str]):
    qs = DailyTask.objects.filter(plan_date=plan_date, outcome_status=DailyTask.OutcomeStatus.UNKNOWN)
    if se_ids is not None:
        qs = qs.filter(se_id__in=se_ids)
    if scope_type:
        qs = qs.filter(plan_run__scope_type=scope_type.upper())
        if scope_value:
            qs = qs.filter(plan_run__scope_value=scope_value)
    return qs


def reconcile_plan_date(
    plan_date: str, *, client, se_ids: Optional[List[str]] = None,
    scope_type: Optional[str] = None, scope_value: Optional[str] = None,
) -> Dict[str, Any]:
    """Reconciles every still-UNKNOWN DailyTask for plan_date (optionally narrowed to
    se_ids and/or one PlanRun scope). Returns a summary dict; never raises for a
    failed live pull (the failure is listed under "pull_failures" and the affected
    signal is simply absent -- a visit pull failure means visits can't be confirmed,
    so tasks fall through to PARTIAL/MISSED on the other signals, same posture the
    command always had). Raises ReconciliationError only for a misuse (a plan_date
    that isn't in the past, no live client)."""
    if datetime.fromisoformat(plan_date).date() >= timezone.now().date():
        raise ReconciliationError("reconciliation needs a past plan_date -- outcomes can't exist yet for today or the future.")
    if not client.configured:
        raise ReconciliationError("Live data client not configured (REDSHIFT_HOST/USER/PASSWORD or METABASE_URL/METABASE_API_KEY) -- reconciliation needs live Visits/Sales/Payments data.")

    all_tasks = list(_pending_tasks(plan_date, se_ids, scope_type, scope_value))
    summary: Dict[str, Any] = {
        "plan_date": plan_date, "tasks": 0, "completed": 0, "partial": 0, "missed": 0,
        "escalated": 0, "farmer_meeting_skipped": 0, "payment_amount": 0.0, "pull_failures": [], "visits_planned": 0,
    }
    if not all_tasks:
        return summary

    # Farmer Meeting tasks (8.11) have no dc_id -- reconciling whether a meeting actually
    # happened needs farmer_in_meeting_vw, a genuinely separate capability. Skipped
    # honestly (stay UNKNOWN), not silently scored MISSED.
    tasks = [t for t in all_tasks if t.dc_id]
    summary["farmer_meeting_skipped"] = len(all_tasks) - len(tasks)
    if not tasks:
        return summary

    se_user_ids = sorted({int(t.se_id) for t in tasks if t.se_id.isdigit()})
    dc_ids = sorted({t.dc_id for t in tasks})

    visited: Dict[tuple, Optional[str]] = {}  # (se_id_str, dc_id) -> earliest visit date in window
    try:
        for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_visit_outcomes(dc_ids, se_user_ids, plan_date)):
            key = (str(row["se_user_id"]), row["sap_partner_id"])
            d = agent.standardize_date(row["plan_execution_date"])
            if key not in visited or (d and d < visited[key]):
                visited[key] = d
    except Exception as e:
        summary["pull_failures"].append(f"visits: {type(e).__name__}: {e}")

    ordered_value: Dict[str, Optional[float]] = {}  # dc_id -> largest order amount in window
    try:
        for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_order_outcomes(dc_ids, plan_date)):
            dc_id = agent.normalize_id(row.get("dc_id"))
            if dc_id:
                ordered_value[dc_id] = agent.parse_number(row.get("amount_total"))
    except Exception as e:
        summary["pull_failures"].append(f"orders: {type(e).__name__}: {e}")

    paid: Dict[str, float] = {}  # dc_id -> SUCCESS payments in window
    try:
        for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_payment_outcomes(dc_ids, plan_date)):
            dc_id = agent.normalize_id(row.get("dc_id"))
            amount = agent.parse_number(row.get("amount_paid"))
            if dc_id and amount:
                paid[dc_id] = amount
    except Exception as e:
        summary["pull_failures"].append(f"payments: {type(e).__name__}: {e}")

    now = timezone.now()
    # 1. Outcome per task. Every duplicate task for the same (SE, DC, day) gets its
    #    own outcome fields (they're separate rows the UI shows), but the DAY's verdict
    #    for the pair is the best of them -- see _streak_length.
    day_best: Dict[tuple, str] = {}  # (se_id, dc_id) -> best status this plan_date
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
        summary[status.lower()] += 1

        task.outcome_status = status
        task.actual_order_value = order_value
        task.actual_payment_amount = paid.get(task.dc_id)
        task.reconciled_at = now
        if _OUTCOME_RANK[status] > _OUTCOME_RANK.get(day_best.get(key), -1):
            day_best[key] = status

    # Money is counted once per (DC, day), never once per duplicate task.
    summary["payment_amount"] = sum(paid.get(dc, 0.0) or 0.0 for dc in {k[1] for k in day_best})

    # 2. Streaks, recomputed from the reconciled history rather than incremented per
    #    task. This app regenerates a scope's plan on every view load, so one SE-day
    #    routinely has 10-20 duplicate DailyTask rows per DC (confirmed live: 90 tasks
    #    for one SE on 2026-09-15, 17 of them the same DC) -- a per-task increment
    #    turned one missed day into a 17-miss "chronic" escalation. A streak is now
    #    the number of most-recent consecutive DAYS whose best outcome for the pair
    #    was MISSED, so duplicates can't inflate it and re-running repairs it.
    se_id_strs = sorted({t.se_id for t in tasks})
    history: Dict[tuple, Dict[str, str]] = {}  # (se_id, dc_id) -> {plan_date -> best status}
    for se_id, dc_id, other_date, status in (
        DailyTask.objects.filter(se_id__in=se_id_strs, dc_id__in=dc_ids, reconciled_at__isnull=False)
        .exclude(plan_date=plan_date).exclude(outcome_status=DailyTask.OutcomeStatus.UNKNOWN)
        .values_list("se_id", "dc_id", "plan_date", "outcome_status")
    ):
        key = (se_id, dc_id)
        if key not in day_best:
            continue
        d = other_date.isoformat() if hasattr(other_date, "isoformat") else str(other_date)
        days = history.setdefault(key, {})
        if _OUTCOME_RANK[status] > _OUTCOME_RANK.get(days.get(d), -1):
            days[d] = status
    for key, status in day_best.items():
        history.setdefault(key, {})[plan_date] = status

    streak_len = _apply_streaks(history, now)

    # 3. Escalation: a missed day that takes the pair to the threshold flags every task
    #    of that day (they're the same visit) -- visible in the outcome table, not a
    #    silent internal-only adjustment.
    for task in tasks:
        key = (task.se_id, task.dc_id)
        length = streak_len.get(key, 0)
        if day_best.get(key) == DailyTask.OutcomeStatus.MISSED and length >= ESCALATION_THRESHOLD:
            task.priority_multiplier = round(min(task.priority_multiplier * ESCALATION_BOOST, MAX_PRIORITY_MULTIPLIER), 2)
            base_reason = task.reason_of_visit.split(" [ESCALATED")[0]
            task.reason_of_visit = (base_reason + f" [ESCALATED: missed {length}x running]")[:255]
            summary["escalated"] += 1

    # batch_size=500 -- a STATE-scope day is thousands of tasks; one unbounded bulk
    # query at that size risks the DB's own parameter-count limits.
    DailyTask.objects.bulk_update(tasks, [
        "outcome_status", "actual_visit_date", "actual_order_value", "actual_payment_amount", "reconciled_at",
        "priority_multiplier", "reason_of_visit",
    ], batch_size=500)
    summary["tasks"] = len(tasks)
    summary["visits_planned"] = len(day_best)
    return summary


# COMPLETED beats PARTIAL beats MISSED when a day has several rows for the same pair.
_OUTCOME_RANK = {
    DailyTask.OutcomeStatus.MISSED: 0,
    DailyTask.OutcomeStatus.PARTIAL: 1,
    DailyTask.OutcomeStatus.COMPLETED: 2,
}


def _streak_length(days_by_date: Dict[str, str]) -> int:
    """Consecutive most-recent reconciled days whose best outcome was MISSED."""
    length = 0
    for d in sorted(days_by_date, reverse=True):
        if days_by_date[d] != DailyTask.OutcomeStatus.MISSED:
            break
        length += 1
    return length


def _apply_streaks(history: Dict[tuple, Dict[str, str]], now) -> Dict[tuple, int]:
    """Writes each pair's DCVisitStreak from its per-day history (create or update, only
    when the value actually changed). bulk_* don't apply auto_now, so updated_at is
    set explicitly. Returns the streak length per pair."""
    keys = list(history)
    existing = {
        (s.se_id, s.dc_id): s
        for s in DCVisitStreak.objects.filter(se_id__in={k[0] for k in keys}, dc_id__in={k[1] for k in keys})
    }
    new_rows: List[DCVisitStreak] = []
    changed: List[DCVisitStreak] = []
    lengths: Dict[tuple, int] = {}
    for key, days in history.items():
        length = _streak_length(days)
        lengths[key] = length
        latest = max(days)
        streak = existing.get(key)
        if streak is None:
            new_rows.append(DCVisitStreak(se_id=key[0], dc_id=key[1], consecutive_misses=length, last_outcome_date=latest, updated_at=now))
        elif streak.consecutive_misses != length or str(streak.last_outcome_date) != latest:
            streak.consecutive_misses, streak.last_outcome_date, streak.updated_at = length, latest, now
            changed.append(streak)
    if new_rows:
        DCVisitStreak.objects.bulk_create(new_rows, batch_size=500)
    if changed:
        DCVisitStreak.objects.bulk_update(changed, ["consecutive_misses", "last_outcome_date", "updated_at"], batch_size=500)
    return lengths


def rebuild_streaks(se_ids: Optional[List[str]] = None) -> int:
    """Recomputes every DCVisitStreak that has reconciled history (for these SEs, or
    all) purely from that history -- the repair tool for streaks written by the old
    per-task increment, and a no-op when everything is already consistent. Pairs with
    no reconciled task at all are left untouched. Returns the number of pairs
    recomputed."""
    qs = DailyTask.objects.filter(reconciled_at__isnull=False).exclude(dc_id="").exclude(outcome_status=DailyTask.OutcomeStatus.UNKNOWN)
    if se_ids is not None:
        qs = qs.filter(se_id__in=se_ids)
    history: Dict[tuple, Dict[str, str]] = {}
    for se_id, dc_id, d, status in qs.values_list("se_id", "dc_id", "plan_date", "outcome_status"):
        d = d.isoformat() if hasattr(d, "isoformat") else str(d)
        days = history.setdefault((se_id, dc_id), {})
        if _OUTCOME_RANK[status] > _OUTCOME_RANK.get(days.get(d), -1):
            days[d] = status
    if history:
        _apply_streaks(history, timezone.now())
    return len(history)


def pending_plan_dates(*, before_date: str, se_ids: Optional[List[str]] = None) -> List[str]:
    """Distinct past plan_dates that still have UNKNOWN DC-visit tasks (for these SEs, or
    network-wide), oldest first -- the work list for reconcile_past_tasks and the
    Tracking dashboard's "N tasks can be reconciled now" figure."""
    qs = DailyTask.objects.filter(plan_date__lt=before_date, outcome_status=DailyTask.OutcomeStatus.UNKNOWN).exclude(dc_id="")
    if se_ids is not None:
        qs = qs.filter(se_id__in=se_ids)
    return sorted({d.isoformat() if hasattr(d, "isoformat") else str(d) for d in qs.values_list("plan_date", flat=True).distinct()})


def reconcile_past_tasks(*, client, before_date: Optional[str] = None, se_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Reconciles every past plan_date with pending tasks (for these SEs, or everything
    when se_ids is None), one live pull set per date. Returns the per-date summaries."""
    before_date = before_date or timezone.now().date().isoformat()
    return [reconcile_plan_date(d, client=client, se_ids=se_ids) for d in pending_plan_dates(before_date=before_date, se_ids=se_ids)]


def format_summary(s: Dict[str, Any]) -> str:
    text = (
        f"Reconciled {s['tasks']} task row(s) / {s['visits_planned']} planned visit(s) for {s['plan_date']}: {s['completed']} completed, "
        f"{s['partial']} partial, {s['missed']} missed ({s['escalated']} newly escalated at "
        f"{ESCALATION_THRESHOLD}+ consecutive misses); ₹{s['payment_amount']:,.0f} collected"
    )
    if s["farmer_meeting_skipped"]:
        text += f"; {s['farmer_meeting_skipped']} Farmer Meeting task(s) skipped (no DC to reconcile against)"
    if s["pull_failures"]:
        text += "; live pulls failed: " + " | ".join(s["pull_failures"])
    return text
