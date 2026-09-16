"""Tracking dashboard metrics (added 2026-09-16, explicit user request: "according to
this whole project what we have to track" -> "design this dashboard in this").

One read-only aggregation over what the pipeline already persists, in the five tiers
that matter for this system, most important first:

  1. Outcomes   -- does the plan change what SEs collect and sell (DailyTask's
                   outcome_status / actual_* fields, written only by reconcile_outcomes)
  2. Adoption   -- do SEs accept the plan or fight it (PlanRun.status, manual route edits)
  3. Quality    -- what the agents produced (AI vs template pitch, hallucination drops,
                   empty recommendations, route budget, generation latency)
  4. Data health-- live-pull failures, real vs structural exceptions, freshness, geo
                   coverage
  5. Ops        -- alert routing, Redshift reachability, DB settings

Every figure is computed from the DB / output files at request time -- nothing here is
a new write path, and nothing is estimated: a metric whose underlying data has never
been produced comes back as None with the count of what IS there (e.g. Tasks_Reconciled
0 of N), so the dashboard can say "never measured" rather than show a fabricated 0%.
Windowed by PlanRun.run_timestamp (when the plan was generated), not plan_date, so a
tomorrow-dated run generated today counts as today's work.
"""
from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db import connection
from django.db.models import Avg, Count, F, Max, Q, Sum
from django.utils import timezone

from .models import DailyTask, DCVisitStreak, ExceptionRecord, PitchScript, PlanRun, RoutePlan, ScheduledScope

# Route budget the Plan B spec and Plan C prompt both work to (Beat_Planning_Routing_
# Agent_Cluster_Model.xlsx: 80 km / 180 min) -- a route past either is "over budget".
ROUTE_BUDGET_KM = 80.0
ROUTE_BUDGET_MINUTES = 180.0

# Exception reason codes that mean "something broke" rather than "a policy applied".
# Everything else in ExceptionRecord is structural: a DC excluded by the Top-DC list,
# GR-28 bypass notes, provisional FM_Urgency, inactive-DC datamart gaps -- all expected,
# all logged by design, and together >99% of the 700k+ rows. Counting those against the
# 10% run-health threshold is why that alert fires on every single run today.
_FAILURE_CODE_RE = re.compile(r"Failed|Error|Crash|Timeout|Exhausted", re.IGNORECASE)


def _pct(numerator: float, denominator: float) -> Optional[float]:
    return round(100.0 * numerator / denominator, 1) if denominator else None


def _window(days: int) -> datetime:
    return timezone.now() - timedelta(days=days)


def _outcomes(runs, tasks) -> Dict[str, Any]:
    total = tasks.count()
    reconciled = tasks.filter(reconciled_at__isnull=False)
    n_rec = reconciled.count()
    breakdown = {row["outcome_status"]: row["n"] for row in reconciled.values("outcome_status").annotate(n=Count("id"))}
    completed = breakdown.get(DailyTask.OutcomeStatus.COMPLETED, 0) + breakdown.get(DailyTask.OutcomeStatus.PARTIAL, 0)
    money = reconciled.aggregate(payment=Sum("actual_payment_amount"), order=Sum("actual_order_value"))
    overdue_pitched = tasks.filter(present_overdue__gt=0).aggregate(s=Sum("present_overdue"))["s"]
    ptp = tasks.exclude(promise_to_pay_date__isnull=True)
    return {
        "Tasks_Planned": total,
        "Tasks_Reconciled": n_rec,
        "Reconciliation_Rate_Pct": _pct(n_rec, total),
        # Across ALL time, not just the window -- "has this ever run" is the question.
        "Reconciliation_Last_Run_At": DailyTask.objects.aggregate(m=Max("reconciled_at"))["m"],
        "Visit_Execution_Rate_Pct": _pct(completed, n_rec),
        "Outcome_Status_Breakdown": breakdown,
        "Overdue_Pitched": overdue_pitched,
        "Collection_Realised": money["payment"],
        "Sales_After_Visit": money["order"],
        "PTP_Promises": ptp.count(),
        "PTP_Promised_Amount": ptp.aggregate(s=Sum("promise_to_pay_amount"))["s"],
        "Chronic_Non_Execution_Pairs": DCVisitStreak.objects.filter(consecutive_misses__gte=DCVisitStreak.ESCALATION_THRESHOLD).count(),
        "Escalation_Threshold_Misses": DCVisitStreak.ESCALATION_THRESHOLD,
    }


def _adoption(runs) -> Dict[str, Any]:
    by_status = {row["status"]: row["n"] for row in runs.values("status").annotate(n=Count("id"))}
    total = runs.count()
    reviewed = runs.exclude(reviewed_by="").exclude(reviewed_by__isnull=True).count()
    route_plans = RoutePlan.objects.filter(plan_run__in=runs)
    selected = route_plans.filter(is_default_selected=True)
    return {
        "Plan_Runs": total,
        "By_Status": by_status,
        "Approved": by_status.get(PlanRun.Status.APPROVED, 0),
        "Rejected": by_status.get(PlanRun.Status.REJECTED, 0),
        "Reviewed": reviewed,
        "Reviewed_Rate_Pct": _pct(reviewed, total),
        "Route_Plans": route_plans.count(),
        "Manually_Edited_Routes": route_plans.filter(manually_edited=True).count(),
        "Manual_Edit_Rate_Pct": _pct(route_plans.filter(manually_edited=True).count(), selected.count()),
        "Selected_Plan_Type_Breakdown": {row["plan_type"]: row["n"] for row in selected.values("plan_type").annotate(n=Count("id"))},
    }


def _quality(runs) -> Dict[str, Any]:
    pitches = PitchScript.objects.filter(daily_task__plan_run__in=runs)
    n_pitch = pitches.count()
    ai = pitches.filter(ai_sales_forecast__isnull=False).exclude(ai_sales_forecast={})
    n_ai = ai.count()
    dropped = 0
    empty_recs = 0
    products_total = 0
    products_with_benefit = 0
    for rec, forecast in pitches.values_list("recommended_products", "ai_sales_forecast"):
        rec = rec or []
        if not rec:
            empty_recs += 1
        products_total += len(rec)
        products_with_benefit += sum(1 for p in rec if isinstance(p, dict) and p.get("description"))
        for note in (forecast or {}).get("notes") or []:
            if isinstance(note, str) and note.startswith("Dropped"):
                dropped += 1

    route_plans = RoutePlan.objects.filter(plan_run__in=runs)
    agg = route_plans.aggregate(km=Avg("total_distance_km"), minutes=Avg("total_minutes"))
    over_budget = route_plans.filter(Q(total_distance_km__gt=ROUTE_BUDGET_KM) | Q(total_minutes__gt=ROUTE_BUDGET_MINUTES)).count()

    finished = runs.filter(finished_at__isnull=False, started_at__isnull=False)
    latencies = sorted(
        (f - s).total_seconds() for s, f in finished.values_list("started_at", "finished_at") if s and f
    )
    p90 = latencies[int(len(latencies) * 0.9) - 1] if len(latencies) >= 10 else (latencies[-1] if latencies else None)
    codes = {row["reason_code"]: row["n"] for row in ExceptionRecord.objects.filter(plan_run__in=runs).values("reason_code").annotate(n=Count("id"))}
    return {
        "Pitches": n_pitch,
        "Pitches_AI": n_ai,
        "Pitches_Template": n_pitch - n_ai,
        "AI_Share_Pct": _pct(n_ai, n_pitch),
        "Hallucinated_Products_Dropped": dropped,
        "Empty_Recommendation_Pct": _pct(empty_recs, n_pitch),
        "Benefit_Text_Coverage_Pct": _pct(products_with_benefit, products_total),
        "Route_Plans": route_plans.count(),
        "Route_Avg_Distance_Km": round(agg["km"], 1) if agg["km"] is not None else None,
        "Route_Avg_Minutes": round(agg["minutes"], 0) if agg["minutes"] is not None else None,
        "Routes_Over_Budget": over_budget,
        "Routes_Over_Budget_Pct": _pct(over_budget, route_plans.count()),
        "Plans_Converged": codes.get("Plans_Converged", 0),
        "Insufficient_Candidates": codes.get("Insufficient_Candidates_For_3_Plans", 0),
        "Generation_Latency_Sec": {
            "Runs": len(latencies),
            "Avg": round(sum(latencies) / len(latencies)) if latencies else None,
            "P90": round(p90) if p90 is not None else None,
            "Max": round(latencies[-1]) if latencies else None,
        },
    }


def _data_health(runs) -> Dict[str, Any]:
    exc = ExceptionRecord.objects.filter(plan_run__in=runs)
    total = exc.count()
    by_code = {row["reason_code"]: row["n"] for row in exc.values("reason_code").annotate(n=Count("id")).order_by("-n")}
    failures = {code: n for code, n in by_code.items() if _FAILURE_CODE_RE.search(code)}
    n_fail = sum(failures.values())
    pull_failures = {
        row["source"]: row["n"]
        for row in exc.filter(reason_code="Live_Pull_Failed").values("source").annotate(n=Count("id")).order_by("-n")
    }
    runs_with_failure = exc.filter(reason_code__in=list(failures)).values("plan_run_id").distinct().count()

    output_dir = Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output"
    normalization_at = None
    geo_rows = geo_with_coords = None
    try:
        summary = json.loads((output_dir / "Run_Summary.json").read_text())
        normalization_at = summary.get("Run_Timestamp")
    except (OSError, ValueError):
        pass
    try:
        rows = json.loads((output_dir / "DC_Master_Normalized.json").read_text())
        rows = rows if isinstance(rows, list) else rows.get("records", [])
        geo_rows = len(rows)
        geo_with_coords = sum(1 for r in rows if r.get("Latitude") is not None and r.get("Longitude") is not None)
    except (OSError, ValueError, AttributeError):
        pass

    return {
        "Exceptions_Total": total,
        "Exceptions_Failures": n_fail,
        "Exceptions_Structural": total - n_fail,
        "Failure_Codes": failures,
        "Top_Structural_Codes": dict(list((c, n) for c, n in by_code.items() if c not in failures)[:6]),
        "Live_Pull_Failures_By_Source": pull_failures,
        "Runs_With_A_Failure": runs_with_failure,
        "Runs_With_A_Failure_Pct": _pct(runs_with_failure, runs.count()),
        "Normalization_Last_Run_At": normalization_at,
        "DC_Master_Rows": geo_rows,
        "DC_Master_Geo_Coverage_Pct": _pct(geo_with_coords or 0, geo_rows or 0),
        "Scheduled_Scopes": ScheduledScope.objects.count(),
    }


def _redshift_reachable() -> Optional[bool]:
    host = os.environ.get("REDSHIFT_HOST", "")
    if not host:
        return None
    try:
        with socket.create_connection((host, int(os.environ.get("REDSHIFT_PORT", "5439"))), timeout=2):
            return True
    except OSError:
        return False


def _ops() -> Dict[str, Any]:
    opts = settings.DATABASES["default"].get("OPTIONS", {})
    with connection.cursor() as cur:
        cur.execute("PRAGMA journal_mode")
        journal = cur.fetchone()[0]
    return {
        "Alert_Webhook_Configured": bool(getattr(settings, "ALERT_WEBHOOK_URL", "")),
        "Redshift_Reachable": _redshift_reachable(),
        "DB_Journal_Mode": journal,
        "DB_Busy_Timeout_Sec": opts.get("timeout"),
        "DB_Transaction_Mode": opts.get("transaction_mode"),
        "Plan_Generation_Weekly_Off_Day": getattr(__import__("se_daily_plan_agent"), "PLAN_GENERATION_WEEKLY_OFF_DAY", None),
    }


def compute_tracking_metrics(days: int = 7) -> Dict[str, Any]:
    days = max(1, min(int(days), 90))
    since = _window(days)
    runs = PlanRun.objects.filter(run_timestamp__gte=since)
    tasks = DailyTask.objects.filter(plan_run__in=runs)
    return {
        "Window": {"Days": days, "From": since.isoformat(), "To": timezone.now().isoformat(), "Plan_Runs": runs.count()},
        "Outcomes": _outcomes(runs, tasks),
        "Adoption": _adoption(runs),
        "Quality": _quality(runs),
        "Data_Health": _data_health(runs),
        "Ops": _ops(),
        "Generated_At": datetime.now(dt_timezone.utc).isoformat(),
    }
