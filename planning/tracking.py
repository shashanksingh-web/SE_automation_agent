"""Tracking dashboard metrics (added 2026-09-16, explicit user request: "according to
this whole project what we have to track" -> "design this dashboard in this").

One read-only aggregation over what the pipeline already persists, in the three tiers
that matter for the business, most important first:

  1. Outcomes   -- does the plan change what SEs collect and sell (DailyTask's
                   outcome_status / actual_* fields, written only by reconcile_outcomes)
  2. Adoption   -- do SEs accept the plan or fight it (PlanRun.status, manual route edits)
  3. Quality    -- what the agents produced (AI vs template pitch, hallucination drops,
                   empty recommendations, route budget, generation latency)

Data health (live-pull failures, real vs structural exceptions, freshness, geo coverage)
and Ops (alert routing, Redshift reachability, DB settings) were tiers 4 and 5 until
2026-09-17 and were removed per direct instruction ("4th and 5th not part of this") --
this dashboard is the business view of the system, not its operations console. The
removed code is in git history (commit 6c3a799 and earlier) if an Ops page wants it.

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
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db.models import Avg, Count, Max, Q
from django.utils import timezone

from .models import DailyTask, DCVisitStreak, ExceptionRecord, PitchScript, PlanRun, RoutePlan

# Route budget the Plan B spec and Plan C prompt both work to (Beat_Planning_Routing_
# Agent_Cluster_Model.xlsx: 80 km / 180 min) -- a route past either is "over budget".
ROUTE_BUDGET_KM = 80.0
ROUTE_BUDGET_MINUTES = 180.0



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


def resolve_selection(se_emails: Optional[List[str]], abm_codes: Optional[List[str]]) -> tuple:
    """(selected SE emails as a lowercase set, or None for no filter; an info dict for
    the response). SE/ABM filtering added 2026-09-16 (explicit user request: "add SE
    and ABM wise tracking ... selection must be multiple select"). An ABM resolves to
    the SEs under it via Geo_Mapping_Normalized.json (abm_e_code -> sales_rep_email,
    the same Source 1c relationship resolve_scope_dcs uses for an ABM-scope plan);
    an ABM code with no SE in that file is reported as unmatched rather than silently
    matching nothing. Emails compare case-insensitively - DailyTask.se_name and
    PlanRun.scope_value carry them lowercase, Geo_Mapping doesn't always."""
    ses = {e.strip().lower() for e in (se_emails or []) if e and e.strip()}
    abms = [a.strip() for a in (abm_codes or []) if a and a.strip()]
    if not ses and not abms:
        return None, None
    unmatched: List[str] = []
    via_abm: set = set()
    if abms:
        by_abm: Dict[str, set] = {}
        try:
            rows = json.loads((Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output" / "Geo_Mapping_Normalized.json").read_text())
            rows = rows if isinstance(rows, list) else rows.get("records", [])
            for r in rows:
                code, email = r.get("abm_e_code"), r.get("sales_rep_email")
                if code and email:
                    by_abm.setdefault(str(code).strip(), set()).add(str(email).strip().lower())
        except (OSError, ValueError, AttributeError):
            pass
        for code in abms:
            if code in by_abm:
                via_abm |= by_abm[code]
            else:
                unmatched.append(code)
    selected = ses | via_abm
    return selected, {
        "SEs": sorted(ses), "ABMs": abms, "SEs_Via_ABM": len(via_abm), "Resolved_SEs": len(selected), "Unmatched_ABMs": unmatched,
    }


def _outcomes(runs, tasks, selected: Optional[set] = None) -> Dict[str, Any]:
    # Counted per PLANNED VISIT -- a distinct (SE, DC, plan_date) -- not per DailyTask
    # row. This app regenerates a scope's plan on every view load, so one SE-day
    # routinely carries 10-20 duplicate rows per DC (2,826 rows for 762 visits on
    # 2026-09-05); per-row counting would let a much-regenerated day dominate every
    # rate, and would sum the same DC's payment once per duplicate. A visit's outcome
    # is the best of its rows (COMPLETED > PARTIAL > MISSED); money is taken once per
    # (DC, day). Same dedup rule planning.reconciliation applies to streaks.
    # `selected` (lowercase SE emails) narrows everything to those SEs' visits, across
    # every scope's runs -- a visit is the same visit whether an SE-scope or ABM-scope
    # run planned it -- and adds a per-SE breakdown.
    visits: Dict[tuple, str] = {}
    money_by_dc_day: Dict[tuple, tuple] = {}
    overdue_by_dc_day: Dict[tuple, float] = {}
    ptp_by_dc_day: Dict[tuple, float] = {}
    task_rows = 0
    se_ids: set = set()
    for se_id, se_name, dc_id, d, status, paid, ordered, overdue, ptp_date, ptp_amount in tasks.exclude(dc_id="").values_list(
        "se_id", "se_name", "dc_id", "plan_date", "outcome_status", "actual_payment_amount", "actual_order_value",
        "present_overdue", "promise_to_pay_date", "promise_to_pay_amount",
    ):
        email = (se_name or "").lower()
        if selected is not None and email not in selected:
            continue
        task_rows += 1
        se_ids.add(se_id)
        key = (email, dc_id, str(d))
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

    # Visit execution = (COMPLETED + PARTIAL) / planned visits DUE, per direct
    # instruction 2026-09-17 ("take planned completed plus planned partial / planned").
    # Denominator changed from reconciled visits to planned ones: a past visit nobody
    # has reconciled counts as NOT executed rather than dropping out of the rate. "Due"
    # = plan_date before today -- a visit planned for today or later can't have an
    # outcome yet (reconciliation only runs for past dates), so counting it would mark
    # every not-yet-happened visit as missed; those are reported as Tasks_Not_Yet_Due.
    today = timezone.now().date().isoformat()
    total = len(visits)
    due = {k: s for k, s in visits.items() if k[2] < today}
    n_due = len(due)
    # Due visits whose outcome window hasn't closed: their status is provisional and
    # is re-checked on every reconciliation until plan_date + OUTCOME_WINDOW_DAYS
    # passes (see planning.reconciliation.outcome_window_open).
    from .reconciliation import outcome_window_open
    n_window_open = sum(1 for k in due if outcome_window_open(k[2], today))
    reconciled = {k: s for k, s in due.items() if s != "UNKNOWN"}
    n_rec = len(reconciled)
    breakdown: Dict[str, int] = {}
    for s in reconciled.values():
        breakdown[s] = breakdown.get(s, 0) + 1
    completed = breakdown.get("COMPLETED", 0) + breakdown.get("PARTIAL", 0)
    paid_total = sum(p for p, _ in money_by_dc_day.values() if p)
    ordered_total = sum(o for _, o in money_by_dc_day.values() if o)

    # Per-SE: same rules, grouped by the visit's SE. Money is attributed to the SE
    # whose visit it was (a DC belongs to one SE, so a (DC, day) has one owner here).
    per_se: Dict[str, Dict[str, Any]] = {}
    owner_by_dc_day: Dict[tuple, str] = {(k[1], k[2]): k[0] for k in visits}
    for (email, _, d), status in visits.items():
        row = per_se.setdefault(email, {"Planned": 0, "Due": 0, "Reconciled": 0, "Executed": 0, "Collection": 0.0, "Sales": 0.0})
        row["Planned"] += 1
        if d < today:
            row["Due"] += 1
            if status != "UNKNOWN":
                row["Reconciled"] += 1
                if status in ("COMPLETED", "PARTIAL"):
                    row["Executed"] += 1
    for dc_day, (paid, ordered) in money_by_dc_day.items():
        owner = owner_by_dc_day.get(dc_day)
        if owner in per_se:
            per_se[owner]["Collection"] += paid or 0.0
            per_se[owner]["Sales"] += ordered or 0.0

    streaks = DCVisitStreak.objects.filter(consecutive_misses__gte=DCVisitStreak.ESCALATION_THRESHOLD)
    pending = DailyTask.objects.filter(
        plan_date__lt=timezone.now().date().isoformat(), outcome_status=DailyTask.OutcomeStatus.UNKNOWN,
    ).exclude(dc_id="")
    if selected is not None:
        streaks = streaks.filter(se_id__in=se_ids)
        reconcilable_now = sum(1 for n in pending.values_list("se_name", flat=True) if (n or "").lower() in selected)
    else:
        reconcilable_now = pending.count()

    return {
        "Tasks_Planned": total,
        "Tasks_Due": n_due,
        "Tasks_Not_Yet_Due": total - n_due,
        "Tasks_Window_Open": n_window_open,
        "Task_Rows": task_rows,
        "Tasks_Reconciled": n_rec,
        "Reconciliation_Rate_Pct": _pct(n_rec, n_due),
        # Across ALL time, not just the window -- "has this ever run" is the question.
        "Reconciliation_Last_Run_At": DailyTask.objects.aggregate(m=Max("reconciled_at"))["m"],
        "Visit_Execution_Rate_Pct": _pct(completed, n_due),
        "Visits_Executed": completed,
        "Outcome_Status_Breakdown": breakdown,
        "Overdue_Pitched": sum(overdue_by_dc_day.values()) or None,
        "Collection_Realised": paid_total if n_rec else None,
        "Sales_After_Visit": ordered_total if n_rec else None,
        "PTP_Promises": len(ptp_by_dc_day),
        "PTP_Promised_Amount": sum(ptp_by_dc_day.values()) or None,
        "Chronic_Non_Execution_Pairs": streaks.count(),
        "Escalation_Threshold_Misses": DCVisitStreak.ESCALATION_THRESHOLD,
        # All-time: past DC-visit tasks still UNKNOWN -- what "Reconcile now" would act on.
        "Reconcilable_Now": reconcilable_now,
        "Per_SE": per_se,
    }


def _adoption(runs, selected: Optional[set] = None) -> Dict[str, Any]:
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
        email = (se or "").lower()
        if selected is not None and email not in selected:
            continue
        day = days.setdefault((email, str(d)), {"runs": 0, "run_ids": [], "latest": (None, None), "review": (None, None)})
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
    per_se: Dict[str, Dict[str, Any]] = {}
    for (email, _), day in days.items():
        verdict = day["review"][1] or PlanRun.Status.PENDING_REVIEW
        by_status[verdict] = by_status.get(verdict, 0) + 1
        edited = any(rid in edited_runs for rid in day["run_ids"])
        if edited:
            edited_days += 1
        pt = selected_by_run.get(day["latest"][1])
        if pt:
            plan_types[pt] = plan_types.get(pt, 0) + 1
        row = per_se.setdefault(email, {"SE_Days": 0, "Runs": 0, "Approved": 0, "Rejected": 0, "Edited_Days": 0})
        row["SE_Days"] += 1
        row["Runs"] += day["runs"]
        if verdict == PlanRun.Status.APPROVED:
            row["Approved"] += 1
        elif verdict == PlanRun.Status.REJECTED:
            row["Rejected"] += 1
        if edited:
            row["Edited_Days"] += 1
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
        "Per_SE": per_se,
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


def compute_tracking_metrics(
    days: Optional[int] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
    se_emails: Optional[List[str]] = None, abm_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    f, t = resolve_window(days, date_from, date_to)
    selected, selection_info = resolve_selection(se_emails, abm_codes)
    runs = PlanRun.objects.filter(plan_date__gte=f, plan_date__lte=t)
    tasks = DailyTask.objects.filter(plan_run__in=runs)
    outcomes = _outcomes(runs, tasks, selected)
    adoption = _adoption(runs, selected)
    outcome_by_se, adoption_by_se = outcomes.pop("Per_SE"), adoption.pop("Per_SE")
    # Per-SE table only for a selection: it's the answer to "which of MY SEs is
    # executing" for an ABM, not a 400-row network listing.
    by_se = None
    if selected is not None:
        by_se = []
        for email in sorted(selected | set(outcome_by_se) | set(adoption_by_se)):
            o, a = outcome_by_se.get(email, {}), adoption_by_se.get(email, {})
            by_se.append({
                "SE": email,
                "Planned": o.get("Planned", 0), "Due": o.get("Due", 0), "Reconciled": o.get("Reconciled", 0),
                "Executed": o.get("Executed", 0),
                "Execution_Rate_Pct": _pct(o.get("Executed", 0), o.get("Due", 0)),
                "Collection": o.get("Collection", 0.0), "Sales": o.get("Sales", 0.0),
                "SE_Days": a.get("SE_Days", 0), "Runs": a.get("Runs", 0),
                "Approved": a.get("Approved", 0), "Rejected": a.get("Rejected", 0), "Edited_Days": a.get("Edited_Days", 0),
            })
        by_se.sort(key=lambda r: (-r["Planned"], r["SE"]))
    return {
        "Window": {
            "From": f, "To": t, "Days": (datetime.fromisoformat(t) - datetime.fromisoformat(f)).days + 1,
            "Plan_Runs": runs.count(),
            "Selection": selection_info,
        },
        "Outcomes": outcomes,
        "Adoption": adoption,
        "By_SE": by_se,
        "Quality": _quality(runs),
        "Generated_At": datetime.now(dt_timezone.utc).isoformat(),
    }
