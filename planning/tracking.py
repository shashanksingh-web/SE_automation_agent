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
Windowed by PlanRun.plan_date -- the day the visits were FOR -- over an inclusive
[from, to] range (changed 2026-09-16 from run_timestamp when "Yesterday" and a custom
range were added: "how did yesterday go" means the visits planned for yesterday, not
the plans generated yesterday). A rolling "last N days" is [today-(N-1), today]; a
tomorrow-dated run only shows in a range that includes tomorrow.
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


MAX_WINDOW_DAYS = 90


class TrackingWindowError(ValueError):
    pass


def resolve_window(days: Optional[int] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> tuple:
    """(from_date, to_date) as ISO strings, inclusive. Explicit from/to win; otherwise
    the last `days` days ending today (default 7). Raises TrackingWindowError for a
    malformed date, from > to, or a span past MAX_WINDOW_DAYS."""
    today = timezone.now().date()
    if date_from or date_to:
        try:
            f = datetime.fromisoformat(date_from).date() if date_from else None
            t = datetime.fromisoformat(date_to).date() if date_to else None
        except ValueError:
            raise TrackingWindowError("from/to must be YYYY-MM-DD dates")
        f = f or t
        t = t or f
        if f > t:
            raise TrackingWindowError("from must not be after to")
    else:
        n = max(1, min(int(days or 7), MAX_WINDOW_DAYS))
        f, t = today - timedelta(days=n - 1), today
    if (t - f).days + 1 > MAX_WINDOW_DAYS:
        raise TrackingWindowError(f"window must be at most {MAX_WINDOW_DAYS} days")
    return f.isoformat(), t.isoformat()


_OUTCOME_RANK = {"UNKNOWN": -1, "MISSED": 0, "PARTIAL": 1, "COMPLETED": 2}


def _outcomes(runs, tasks) -> Dict[str, Any]:
    # Counted per PLANNED VISIT -- a distinct (SE, DC, plan_date) -- not per DailyTask
    # row. This app regenerates a scope's plan on every view load, so one SE-day
    # routinely carries 10-20 duplicate rows per DC (2,826 rows for 762 visits on
    # 2026-09-05); per-row counting would let a much-regenerated day dominate every
    # rate, and would sum the same DC's payment once per duplicate. A visit's outcome
    # is the best of its rows (COMPLETED > PARTIAL > MISSED); money is taken once per
    # (DC, day). Same dedup rule planning.reconciliation applies to streaks.
    visits: Dict[tuple, str] = {}
    money_by_dc_day: Dict[tuple, tuple] = {}
    overdue_by_dc_day: Dict[tuple, float] = {}
    ptp_by_dc_day: Dict[tuple, float] = {}
    for se_id, dc_id, d, status, paid, ordered, overdue, ptp_date, ptp_amount in tasks.exclude(dc_id="").values_list(
        "se_id", "dc_id", "plan_date", "outcome_status", "actual_payment_amount", "actual_order_value",
        "present_overdue", "promise_to_pay_date", "promise_to_pay_amount",
    ):
        key = (se_id, dc_id, str(d))
        if _OUTCOME_RANK.get(status, -1) > _OUTCOME_RANK.get(visits.get(key), -2):
            visits[key] = status
        dc_day = (dc_id, str(d))
        prev_paid, prev_ordered = money_by_dc_day.get(dc_day, (None, None))
        money_by_dc_day[dc_day] = (
            paid if paid is not None else prev_paid,
            max(ordered, prev_ordered or 0) if ordered else prev_ordered,
        )
        if overdue:
            overdue_by_dc_day[dc_day] = max(overdue, overdue_by_dc_day.get(dc_day, 0))
        if ptp_date:
            ptp_by_dc_day[dc_day] = max(ptp_amount or 0, ptp_by_dc_day.get(dc_day, 0))

    total = len(visits)
    reconciled = {k: s for k, s in visits.items() if s != "UNKNOWN"}
    n_rec = len(reconciled)
    breakdown: Dict[str, int] = {}
    for s in reconciled.values():
        breakdown[s] = breakdown.get(s, 0) + 1
    completed = breakdown.get("COMPLETED", 0) + breakdown.get("PARTIAL", 0)
    paid_total = sum(p for p, _ in money_by_dc_day.values() if p)
    ordered_total = sum(o for _, o in money_by_dc_day.values() if o)
    return {
        "Tasks_Planned": total,
        "Task_Rows": tasks.count(),
        "Tasks_Reconciled": n_rec,
        "Reconciliation_Rate_Pct": _pct(n_rec, total),
        # Across ALL time, not just the window -- "has this ever run" is the question.
        "Reconciliation_Last_Run_At": DailyTask.objects.aggregate(m=Max("reconciled_at"))["m"],
        "Visit_Execution_Rate_Pct": _pct(completed, n_rec),
        "Outcome_Status_Breakdown": breakdown,
        "Overdue_Pitched": sum(overdue_by_dc_day.values()) or None,
        "Collection_Realised": paid_total if n_rec else None,
        "Sales_After_Visit": ordered_total if n_rec else None,
        "PTP_Promises": len(ptp_by_dc_day),
        "PTP_Promised_Amount": sum(ptp_by_dc_day.values()) or None,
        "Chronic_Non_Execution_Pairs": DCVisitStreak.objects.filter(consecutive_misses__gte=DCVisitStreak.ESCALATION_THRESHOLD).count(),
        "Escalation_Threshold_Misses": DCVisitStreak.ESCALATION_THRESHOLD,
        # All-time: past DC-visit tasks still UNKNOWN -- what "Reconcile now" would act on.
        "Reconcilable_Now": DailyTask.objects.filter(
            plan_date__lt=timezone.now().date().isoformat(), outcome_status=DailyTask.OutcomeStatus.UNKNOWN,
        ).exclude(dc_id="").count(),
    }


def _adoption(runs) -> Dict[str, Any]:
    # Counted per SE-DAY -- a distinct (SE, plan_date) among SE-scope runs, the plan an
    # SE actually sees in their own view -- not per PlanRun. Every view load regenerates
    # the plan (134 SE-scope runs for 23 SE-days in one week; one SE's day regenerated
    # 30 times), and an SE reviews ONE plan a day, so per-run counting buried the
    # reviewed rate at 3%. A day's verdict is its LATEST review (an SE who rejected,
    # regenerated and then approved has approved); it's manually edited if any of its
    # runs' routes were; its route model of record is the selected plan on its latest
    # run. Admin-generated STATE/NODE/... runs aren't something an SE reviews, so they
    # count toward Plan_Runs but not toward adoption.
    total_runs = runs.count()
    days: Dict[tuple, Dict[str, Any]] = {}
    for run_id, se, d, status, reviewed_by, reviewed_at, ts in (
        runs.filter(scope_type=PlanRun.ScopeType.SE)
        .values_list("id", "scope_value", "plan_date", "status", "reviewed_by", "reviewed_at", "run_timestamp")
    ):
        day = days.setdefault((se, str(d)), {"runs": 0, "run_ids": [], "latest": (None, None), "review": (None, None)})
        day["runs"] += 1
        day["run_ids"].append(run_id)
        if day["latest"][0] is None or ts > day["latest"][0]:
            day["latest"] = (ts, run_id)
        if reviewed_by and reviewed_at and (day["review"][0] is None or reviewed_at > day["review"][0]):
            day["review"] = (reviewed_at, status)

    se_run_ids = [rid for day in days.values() for rid in day["run_ids"]]
    edited_runs = set(RoutePlan.objects.filter(plan_run_id__in=se_run_ids, manually_edited=True).values_list("plan_run_id", flat=True))
    latest_ids = [day["latest"][1] for day in days.values() if day["latest"][1] is not None]
    selected_by_run = dict(
        RoutePlan.objects.filter(plan_run_id__in=latest_ids, is_default_selected=True).values_list("plan_run_id", "plan_type")
    )

    n_days = len(days)
    by_status: Dict[str, int] = {}
    edited_days = 0
    plan_types: Dict[str, int] = {}
    for day in days.values():
        verdict = day["review"][1] or PlanRun.Status.PENDING_REVIEW
        by_status[verdict] = by_status.get(verdict, 0) + 1
        if any(rid in edited_runs for rid in day["run_ids"]):
            edited_days += 1
        pt = selected_by_run.get(day["latest"][1])
        if pt:
            plan_types[pt] = plan_types.get(pt, 0) + 1
    reviewed = n_days - by_status.get(PlanRun.Status.PENDING_REVIEW, 0)

    return {
        "Plan_Runs": total_runs,
        "SE_Runs": len(se_run_ids),
        "SE_Days": n_days,
        "Runs_Per_SE_Day": round(len(se_run_ids) / n_days, 1) if n_days else None,
        "By_Status": by_status,
        "Approved": by_status.get(PlanRun.Status.APPROVED, 0),
        "Rejected": by_status.get(PlanRun.Status.REJECTED, 0),
        "Reviewed": reviewed,
        "Reviewed_Rate_Pct": _pct(reviewed, n_days),
        "Route_Plans": RoutePlan.objects.filter(plan_run__in=runs).count(),
        "Manually_Edited_Days": edited_days,
        "Manual_Edit_Rate_Pct": _pct(edited_days, n_days),
        "Selected_Plan_Type_Breakdown": plan_types,
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
    # When the newest failure happened -- the difference between "an incident in the
    # window" and "still broken right now" (the 14-15 Sep permission outage looked
    # identical to a live problem on the tile until this was shown).
    last_failure_at = exc.filter(reason_code__in=list(failures)).aggregate(m=Max("run_timestamp"))["m"] if failures else None

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
        "Last_Failure_At": last_failure_at,
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


def compute_tracking_metrics(days: Optional[int] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> Dict[str, Any]:
    f, t = resolve_window(days, date_from, date_to)
    runs = PlanRun.objects.filter(plan_date__gte=f, plan_date__lte=t)
    tasks = DailyTask.objects.filter(plan_run__in=runs)
    return {
        "Window": {
            "From": f, "To": t, "Days": (datetime.fromisoformat(t) - datetime.fromisoformat(f)).days + 1,
            "Plan_Runs": runs.count(),
        },
        "Outcomes": _outcomes(runs, tasks),
        "Adoption": _adoption(runs),
        "Quality": _quality(runs),
        "Data_Health": _data_health(runs),
        "Ops": _ops(),
        "Generated_At": datetime.now(dt_timezone.utc).isoformat(),
    }
