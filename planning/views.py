import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from . import admin_config, dc_selection
from .directory import list_abms, list_blocks, list_dcs, list_districts, list_nodes, list_rbms, list_ses, list_states, list_zbms
from .headcount import compute_active_headcount_bifurcation
from .models import (
    DailyTask, DCCard, DCVisitStreak, ObjectiveCompletionStats, PitchScript, PlanRun, RoutePlan, RoutingScopeOverride,
    ScheduledScope,
)
from .product_cohort import ProductCohortError, build_season_weeks, split_csv
from .routing import (
    RoutingError, accept_route_plan, edit_route_stops, list_route_plans,
    reject_route_plan, select_default_route_plan,
)
from .services import PlanningError, activate_tuff_scope, generate_plan_for_scope, load_dc_master, run_normalization_step
from .services import _output_dir as _planning_output_dir
from .services import agent  # se_daily_plan_agent, imported once there as a library
from .tracking import TrackingWindowError, compute_tracking_metrics


def _focus_product_kwargs(params: dict) -> dict:
    """Shared by _generate_and_respond/tuff -- lets any scope endpoint accept the same
    focus_product/focus_node/... params activate_tuff/generate_se_plan's CLI flags do,
    so the API isn't a second, drifting implementation of the same optional Focus
    Product Campaign Targeting wiring (see planning.services.generate_plan_for_scope's
    focus_product_* docstring).

    Takes a plain params dict (from _json_body(request)) rather than the request object
    directly -- renamed from _focus_product_kwargs_from_get 2026-09-16 when its two
    callers moved from GET query params to a POST JSON body (architecture-audit fix:
    these were GET endpoints that mutate PlanRun/DailyTask/PitchScript/DCCard state)."""
    material_id = params.get("focus_product")
    if not material_id:
        return {}
    season_weeks = build_season_weeks(
        params.get("focus_product_outer_weeks", "1-52"), params.get("focus_product_buildup_weeks"),
        int(params["focus_product_peak_week"]) if params.get("focus_product_peak_week") else None,
        params.get("focus_product_closure_weeks"),
    )
    return {
        "focus_product_material_id": material_id,
        "focus_product_node_id": params.get("focus_node"),
        "focus_product_years": int(params.get("focus_product_years", 4)),
        "focus_product_season_weeks": season_weeks,
        "focus_product_crop_districts": split_csv(params.get("focus_product_crop_districts")),
        "focus_product_related_products": split_csv(params.get("focus_product_related_products")),
    }


def _routing_plan_choice(params: dict) -> "str | None":
    """Shared by _generate_and_respond/tuff -- routing_plan=A|B|C (case-insensitive),
    mirroring activate_tuff/generate_se_plan's --routing-plan CLI flag (2026-08-31 fix:
    the API previously had no way to request Plan B at all, silently always running
    Plan A). None (param omitted) is passed straight through as routing_plan_choice=None,
    same as the CLI's own "omit under cron/scripting to default to Plan A" behavior --
    the API is never interactive, so there's no routing_plan_asker equivalent here.

    C opened up 2026-09-11 (explicit user request, "open up plan C for triggering from
    the UI too") -- planning.routing.generate_route_plans_for_se/services.py's
    resolved_routing_plan already handled "C" generically since Plan C's own commit
    (54597b7); this validator was the one remaining hardcoded A/B-only gate. Note this
    makes 3 real, possibly slow/rate-limited LLM calls per SE (CHANGED 2026-09-15 from 1
    -- see se_daily_plan_agent.build_route_llm_reasoned's own docstring for why Plan C
    now produces 3 routes per SE like Plan A/B) -- fine for a single SE/day request, but
    a STATE/NODE-scope request now fans that out across every SE in scope sequentially,
    same as Plan A/B always have for their own (cheaper, local) per-SE work -- 3x the
    per-SE cost now applies here too.

    Takes a plain params dict rather than the request object directly -- see
    _focus_product_kwargs's own docstring for why (renamed from
    _routing_plan_choice_from_get 2026-09-16)."""
    raw = params.get("routing_plan")
    if not raw:
        return None
    choice = raw.strip().upper()
    if choice not in ("A", "B", "C"):
        raise ValueError(f"routing_plan must be 'A', 'B', or 'C', got {raw!r}")
    return choice


def _serialize_club_detail(club_detail: dict):
    """se_daily_plan_agent.normalize_dc_club()'s raw per-DC dict -> the API's PascalCase
    Club_Detail shape -- shared by DailyTask's own field (see _serialize_task) and
    DCCard's (dc_card view), both of which persist the identical raw dict. None when
    club data wasn't available this run. Club_Tier/Zone/TOD_Percent/Reward describe
    current standing (all null if not yet tiered); the Eligible_Tier_* trio describes
    what clearing outstanding would unlock (all null once already tiered, or if
    Qualifying_Turnover doesn't clear even Copper's entry threshold)."""
    if not club_detail:
        return None
    return {
        "Is_Club_Enrolled": club_detail.get("Is_Club_Enrolled"),
        "Qualifying_Turnover": club_detail.get("Qualifying_Turnover"),
        "Outstanding_Cleared": club_detail.get("Outstanding_Cleared"),
        "Club_Tier": club_detail.get("Club_Tier"),
        "Zone": club_detail.get("Zone"),
        "TOD_Percent": club_detail.get("TOD_Percent"),
        "Reward": club_detail.get("Reward"),
        "Eligible_Tier_If_Outstanding_Cleared": club_detail.get("Eligible_Tier_If_Outstanding_Cleared"),
        "Eligible_Tier_TOD_Percent_If_Cleared": club_detail.get("Eligible_Tier_TOD_Percent_If_Cleared"),
        "Eligible_Tier_Reward_If_Cleared": club_detail.get("Eligible_Tier_Reward_If_Cleared"),
    }


def _serialize_task(t: DailyTask) -> dict:
    return {
        # The DailyTask row's own DB id -- needed by the frontend to call
        # GET /pitch/<daily_task_id>/ for this task (PitchScript is keyed on it, not on
        # DC_ID/Sr_No). Never exposed before this, so a task row had no way to open its
        # own pitch script.
        "DailyTask_ID": t.id,
        "Sr_No": t.sr_no, "DC_Name": t.dc_name, "DC_ID": t.dc_id, "Distance_Km": t.distance_km,
        "Recommended_Task_Type": t.recommended_task_type, "Purpose_Of_Visit": t.purpose_of_visit,
        "Reason_Of_Visit": t.reason_of_visit, "Last_Visit_Date": t.last_visit_date,
        "Days_Since_Last_Visit": t.days_since_last_visit, "Present_Outstanding": t.present_outstanding,
        "Present_Overdue": t.present_overdue, "Overdue_Aging_Bucket": t.overdue_aging_bucket,
        "Avg_Repayment_Days": t.avg_repayment_days,
        "Last_Order_Date": t.last_order_date,
        "Last_Order_Value": t.last_order_value, "Last_Payment_Date": t.last_payment_date,
        "Last_Payment_Join_Key_Unconfirmed": t.last_payment_join_key_unconfirmed,
        "YTD_Private_Label": t.ytd_private_label, "DC_Club_Participation": t.dc_club_participation,
        "Club_Detail": _serialize_club_detail(t.club_detail),
        "Critical": t.critical, "Critical_Reasons": t.critical_reasons,
        "Objective": t.objective, "No_New_Orders": t.no_new_orders, "Credit_On_Hold": t.credit_on_hold,
        "Credit_On_Hold_Reason": t.credit_on_hold_reason, "Estimated_Duration": t.estimated_duration,
        "Priority_Multiplier": t.priority_multiplier, "Finance_Status": t.finance_status,
        "BO_Scores": t.bo_scores, "BO_Composite_Score": t.bo_composite_score, "BO_Rank": t.bo_rank,
        "Promise_To_Pay_Date": t.promise_to_pay_date, "Promise_To_Pay_Amount": t.promise_to_pay_amount,
        "Promise_Status": t.promise_status,
        "DC_Health_Score": t.dc_health_score, "Health_Gap": t.health_gap,
        "Health_Sub_Scores": t.health_sub_scores, "Negative_GM_Flag": t.negative_gm_flag,
        "Health_Focus_Track": t.health_focus_track, "Health_Focus_Purposes": t.health_focus_purposes,
        "Credit_Limit": t.credit_limit, "Available_Credit_Limit": t.available_credit_limit,
        "Credit_Active": t.credit_active,
        # Outcome-reconciliation block (Tier 1 feedback loop) -- populated by
        # `manage.py reconcile_outcomes` once plan_date has passed; Outcome_Status stays
        # UNKNOWN (never guessed) until that runs, same honest-degrade discipline as the
        # rest of this codebase.
        "Outcome_Status": t.outcome_status, "Actual_Visit_Date": t.actual_visit_date,
        "Actual_Order_Value": t.actual_order_value, "Actual_Payment_Amount": t.actual_payment_amount,
        "Reconciled_At": t.reconciled_at,
    }


def _serialize_plan_run(plan_run: PlanRun, se_filter: Optional[str] = None) -> dict:
    """se_filter (an SE email, added 2026-09-17): serialize only that SE's slice of the
    run -- its tasks, its exceptions, and counts describing it rather than the run's
    whole scope -- so an SE's view can be served from an ABM/STATE-scope run (e.g.
    "Generate for all states") that contains it. Served_From says which run it came
    from; None when unfiltered."""
    tasks_by_se = {}
    tasks = plan_run.tasks.all()
    if se_filter:
        tasks = tasks.filter(se_name__iexact=se_filter)
    task_list = list(tasks)
    for t in task_list:
        tasks_by_se.setdefault(t.se_id, {"SE_ID": t.se_id, "SE_Name": t.se_name, "Tasks": []})
        tasks_by_se[t.se_id]["Tasks"].append(_serialize_task(t))
    exceptions = plan_run.exceptions.all()
    if se_filter:
        own_ids = {t.dc_id for t in task_list if t.dc_id} | {se_filter.lower()}
        exceptions = [e for e in exceptions if not e.record_id or e.record_id.lower() in own_ids]
        se_count = len(tasks_by_se)
        dc_count = sum(1 for d in load_dc_master() if (d.get("Assigned_SE_Email") or "").lower() == se_filter.lower())
        task_count = len(task_list)
        skipped = [x for x in (plan_run.skipped_ses or []) if isinstance(x, dict) and (x.get("se_email") or "").lower() == se_filter.lower()]
    else:
        se_count, dc_count, task_count = plan_run.se_count, plan_run.dc_count, plan_run.task_count
        skipped = plan_run.skipped_ses
    return {
        "PlanRun_ID": plan_run.id,
        "Scope_Type": plan_run.scope_type,
        "Scope_Value": plan_run.scope_value,
        "Plan_Date": plan_run.plan_date,
        "Run_Timestamp": plan_run.run_timestamp,
        "Metabase_Configured": plan_run.metabase_configured,
        "SE_Count": se_count,
        "DC_Count": dc_count,
        "Task_Count": task_count,
        # Only when the SE's plan came out of a BROADER run - their own SE-scope run
        # needs no explanation.
        "Served_From": (
            {"Scope_Type": plan_run.scope_type, "Scope_Value": plan_run.scope_value, "Filtered_To_SE": se_filter}
            if se_filter and not (plan_run.scope_type == PlanRun.ScopeType.SE and plan_run.scope_value.lower() == se_filter.lower())
            else None
        ),
        "Dynamic_Parameters_Resolved": plan_run.dynamic_parameters,
        "Note": plan_run.note,
        "Skipped_SEs": skipped,
        # Approval-workflow / lifecycle fields -- status is an audit trail, not a filter
        # (see PlanRun's own model docstring): no reviewer workflow exists yet to
        # guarantee every run gets reviewed, so every run still appears via GET
        # regardless of status.
        "Status": plan_run.status,
        "Reviewed_By": plan_run.reviewed_by or None,
        "Reviewed_At": plan_run.reviewed_at,
        "Error_Message": plan_run.error_message or None,
        "Started_At": plan_run.started_at,
        "Finished_At": plan_run.finished_at,
        "Plans": list(tasks_by_se.values()),
        "Exceptions_Report": [
            {
                "Record_ID": e.record_id, "Source": e.source, "Reason_Code": e.reason_code,
                "Detail": e.detail, "Run_Timestamp": e.run_timestamp,
            }
            for e in exceptions
        ],
        # Empty unless this run was given focus_product=... -- see
        # _focus_product_kwargs, product-first not DC-first, opt-in per call.
        "Focus_Product_Targets": [
            {
                "ID": f.id, "Material_ID": f.material_id, "Node_ID": f.node_id,
                "Step_2A": f.step_2a, "Step_2B": f.step_2b, "Step_3": f.step_3, "Generated_At": f.generated_at,
            }
            for f in plan_run.focus_product_targets.all()
        ],
    }


@csrf_exempt
@require_http_methods(["POST"])
def _generate_and_respond(request, scope_type: str, scope_value: str):
    """Moved from GET to POST 2026-09-16 (architecture audit, round 2 -- this and
    normalize/tuff below were the 3 remaining GET-mutation endpoints missed by the
    earlier fix to Select/Accept/Reject/Add-stop/Remove-stop; this one is the heaviest
    of all 8, creating a new PlanRun + DailyTask rows + live Pitching/DC Card generation
    on every call). Body (JSON): {"date": ..., "rotation": bool, "routing_plan": "A"|"B"|"C",
    "focus_product": ..., ...} -- see _focus_product_kwargs/_routing_plan_choice for the
    full optional param set. csrf_exempt: same SameSite=Lax-cookie trust boundary as
    every other POST view in this file."""
    params = _json_body(request)
    plan_date = params.get("date")
    enable_rotation = str(params.get("rotation", "")).lower() in ("1", "true", "yes")
    try:
        focus_product_kwargs = _focus_product_kwargs(params)
        routing_plan_choice = _routing_plan_choice(params)
    except (ProductCohortError, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=422)
    try:
        plan_run = generate_plan_for_scope(
            scope_type, scope_value, plan_date, routing_plan_choice=routing_plan_choice,
            enable_rotation=enable_rotation, **focus_product_kwargs,
        )
    except PlanningError as e:
        return JsonResponse({"error": str(e)}, status=422)
    except Exception as e:  # Metabase/network errors etc. -- surface, don't swallow
        return JsonResponse({"error": f"{type(e).__name__}: {e}"}, status=502)
    return JsonResponse(_serialize_plan_run(plan_run), safe=False, json_dumps_params={"default": str})


# Which Routing Agent family a RoutePlan.plan_type belongs to -- the A/B/C the UI's
# selector and the Admin Panel's "SE view's routing plan" speak in.
_PLAN_FAMILY = {
    RoutePlan.PlanType.PRIORITY_MAX: "A", RoutePlan.PlanType.DISTANCE_MIN: "A", RoutePlan.PlanType.BALANCED: "A",
    RoutePlan.PlanType.CLUSTER_BASED: "B", RoutePlan.PlanType.CLUSTER_SCOREMAX: "B", RoutePlan.PlanType.CLUSTER_DISTMIN: "B",
    RoutePlan.PlanType.LLM_REASONED: "C", RoutePlan.PlanType.LLM_REASONED_VALUE_MAX: "C", RoutePlan.PlanType.LLM_REASONED_DISTMIN: "C",
}


def _latest_finished_run(scope_type: str, scope_value: str, plan_date: str, routing_plan: Optional[str] = None):
    """(PlanRun, se_filter, family) -- the most recent COMPLETE run to show for this
    scope and date, or (None, None, None). Candidates are every finished exact-scope
    run for the date and, for an SE, every broader finished run (ABM/RBM/NODE/STATE,
    e.g. the admin's "Generate for all states") whose tasks include the SE -- those
    are served as the SE's own slice.

    routing_plan (A/B/C, added 2026-09-17 after "why plan c is not working"): prefer
    the newest candidate whose routes were generated under that family, so an SE whose
    admin-set plan is C sees their latest Plan C run even when a Plan A run was made
    later (which is exactly what happened: a CLI Plan A regeneration hid four Plan C
    runs for the same day). Falls back to the newest run of any family when none
    match -- and the returned family lets the UI say so, rather than silently showing
    Plan A routes under a "Plan C" badge."""
    exact = list(
        PlanRun.objects.filter(scope_type=scope_type, scope_value__iexact=scope_value, plan_date=plan_date, finished_at__isnull=False)
        .order_by("-run_timestamp")[:30]
    )
    se_filter = None
    candidates = exact
    if scope_type == PlanRun.ScopeType.SE:
        se_filter = scope_value
        containing_ids = (
            DailyTask.objects.filter(se_name__iexact=scope_value, plan_date=plan_date, plan_run__finished_at__isnull=False)
            .exclude(plan_run__scope_type=PlanRun.ScopeType.SE)
            .values_list("plan_run_id", flat=True).distinct()
        )
        candidates = exact + list(PlanRun.objects.filter(id__in=list(containing_ids)).order_by("-run_timestamp")[:30])
        candidates.sort(key=lambda r: r.run_timestamp, reverse=True)
    if not candidates:
        return None, None, None

    routes = RoutePlan.objects.filter(plan_run_id__in=[r.id for r in candidates])
    if se_filter:
        se_ids = set(DailyTask.objects.filter(se_name__iexact=se_filter, plan_run_id__in=[r.id for r in candidates]).values_list("se_id", flat=True))
        routes = routes.filter(se_id__in=se_ids)
    family_by_run: Dict[int, str] = {}
    for run_id, plan_type in routes.values_list("plan_run_id", "plan_type"):
        family_by_run.setdefault(run_id, _PLAN_FAMILY.get(plan_type))

    chosen = candidates[0]
    if routing_plan:
        for r in candidates:
            if family_by_run.get(r.id) == routing_plan:
                chosen = r
                break
    return chosen, se_filter, family_by_run.get(chosen.id)


def _read_latest_and_respond(request, scope_type: str, scope_value: str):
    """GET on a scope endpoint (added 2026-09-17, explicit user request: "if backend
    complete all process why its again run for frontend - only data will capture").
    Until now a view load was a full plan generation -- every Redshift pull, routing,
    LLM calls, pitching -- so one SE-day was regenerated 7x on average just from people
    looking at it (the Tracking dashboard's "regenerations per SE-day"), and every
    duplicate run distorted the outcome/adoption numbers. A view load now only READS
    the latest finished run for the scope and date; generation is the explicit POST
    (Create / Refresh, the tuff endpoint, activate_tuff, run_all_states_tuff).
    404 with code NO_PLAN when nothing has been generated yet -- the UI shows that as
    an empty state with the Create / Refresh button, never a silent regeneration."""
    plan_date = request.GET.get("date") or timezone.now().date().isoformat()
    routing_plan = (request.GET.get("routing_plan") or "").upper() or None
    if routing_plan and routing_plan not in ("A", "B", "C"):
        return JsonResponse({"error": "routing_plan must be A, B or C"}, status=400)
    plan_run, se_filter, family = _latest_finished_run(scope_type, scope_value, plan_date, routing_plan)
    if plan_run is None:
        return JsonResponse(
            {"error": f"No plan has been generated for {scope_type} '{scope_value}' on {plan_date} yet.", "code": "NO_PLAN"},
            status=404,
        )
    payload = _serialize_plan_run(plan_run, se_filter=se_filter)
    # The family the served run's routes were generated under (None: no routes), so the
    # UI can tell "Plan A shown because no Plan C run exists" from "Plan C shown".
    payload["Routing_Plan"] = family
    return JsonResponse(payload, safe=False, json_dumps_params={"default": str})


def _scope_view(request, scope_type: str, scope_value: str):
    """GET reads the latest generated plan; POST generates a new one."""
    if request.method == "GET":
        return _read_latest_and_respond(request, scope_type, scope_value)
    return _generate_and_respond(request, scope_type, scope_value)


# One endpoint per scope, per the doc's SE -> Node -> ABM -> DC -> Block -> District ->
# State hierarchy (Source 1c). Each just fixes scope_type and forwards scope_value/date --
# GET reads the latest run (_read_latest_and_respond), POST generates
# (services.generate_plan_for_scope via _generate_and_respond).

@csrf_exempt
def se_plan(request, scope_value: str):
    """POST /api/planning/se/v1/<se_email>/ -- body: {"date": "YYYY-MM-DD"} (moved from GET
    2026-09-16, see _generate_and_respond's own docstring). csrf_exempt is repeated here
    (not just on _generate_and_respond) because CsrfViewMiddleware inspects the
    url-resolved callback -- this function, not whatever it calls internally -- so the
    marker doesn't propagate through a plain function call. require_http_methods,
    unlike csrf_exempt, IS a runtime check inside _generate_and_respond's own wrapped
    body and correctly fires regardless of call path, so it's not repeated here."""
    return _scope_view(request, PlanRun.ScopeType.SE, scope_value)


@csrf_exempt
def abm_plan(request, scope_value: str):
    """POST /api/planning/abm/v1/<abm_code>/ -- body: {"date": "YYYY-MM-DD"} -- requires live Metabase (Source 1c). Moved from GET 2026-09-16 -- see se_plan's own docstring for why csrf_exempt is repeated here."""
    return _scope_view(request, PlanRun.ScopeType.ABM, scope_value)


@csrf_exempt
def rbm_plan(request, scope_value: str):
    """POST /api/planning/rbm/v1/<rbm_code>/ -- body: {"date": "YYYY-MM-DD"} -- requires live Metabase (Source 1c). Moved from GET 2026-09-16 -- see se_plan's own docstring for why csrf_exempt is repeated here."""
    return _scope_view(request, PlanRun.ScopeType.RBM, scope_value)


@csrf_exempt
def node_plan(request, scope_value: str):
    """POST /api/planning/node/v1/<node_name>/ -- body: {"date": "YYYY-MM-DD"}. Moved from GET 2026-09-16 -- see se_plan's own docstring for why csrf_exempt is repeated here."""
    return _scope_view(request, PlanRun.ScopeType.NODE, scope_value)


@csrf_exempt
def block_plan(request, scope_value: str):
    """POST /api/planning/block/v1/<block_name>/ -- body: {"date": "YYYY-MM-DD"} -- requires live Metabase (Source 1c). Moved from GET 2026-09-16 -- see se_plan's own docstring for why csrf_exempt is repeated here."""
    return _scope_view(request, PlanRun.ScopeType.BLOCK, scope_value)


@csrf_exempt
def district_plan(request, scope_value: str):
    """POST /api/planning/district/v1/<district_name>/ -- body: {"date": "YYYY-MM-DD"} -- requires live Metabase (Source 1c). Moved from GET 2026-09-16 -- see se_plan's own docstring for why csrf_exempt is repeated here."""
    return _scope_view(request, PlanRun.ScopeType.DISTRICT, scope_value)


@csrf_exempt
def state_plan(request, scope_value: str):
    """POST /api/planning/state/v1/<state_name>/ -- body: {"date": "YYYY-MM-DD"}. Moved from GET 2026-09-16 -- see se_plan's own docstring for why csrf_exempt is repeated here."""
    return _scope_view(request, PlanRun.ScopeType.STATE, scope_value)


@csrf_exempt
@require_http_methods(["POST"])
def normalize(request):
    """POST /api/planning/normalize/ -- body: {"date": "YYYY-MM-DD", "force": bool} --
    Data Normalization Agent, once-per-day dedup (see
    planning.services.run_normalization_step). Always returns 200 with Reused
    indicating whether a live pull actually happened, so a caller can tell "ran fresh"
    from "reused today's data" without parsing free text.

    Moved from GET to POST 2026-09-16 -- see _generate_and_respond's own docstring for
    why (this triggers live Redshift pulls + writes output JSON files to disk -- a real
    mutation, was GET)."""
    params = _json_body(request)
    date = params.get("date")
    force = str(params.get("force", "")).lower() in ("1", "true", "yes")
    try:
        result = run_normalization_step(date=date, force=force)
    except PlanningError as e:
        return JsonResponse({"error": str(e)}, status=422)
    except Exception as e:
        return JsonResponse({"error": f"{type(e).__name__}: {e}"}, status=502)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def tuff(request, scope_type: str, scope_value: str):
    """POST /api/planning/tuff/<scope_type>/<scope_value>/ -- body: {"date": "YYYY-MM-DD",
    "force_normalization": bool, "skip_normalization": bool, "routing_plan": "A"|"B"|"C",
    "rotation": bool} -- Agent TUFF: Step 1 (Data Normalization, once-per-day) + Step 2
    (SE Daily Task + Pitching + Routing) in one call, mirroring `manage.py
    activate_tuff`. Response combines Step 1's outcome (Normalization) with the same
    PlanRun shape the scope endpoints (se_plan/state_plan/...) return. routing_plan
    (2026-08-31 fix): omit for Plan A (default, unattended-safe), pass B to run the Beat
    Planning / Cluster-Based Model instead -- see _routing_plan_choice. rotation (Plan B
    only, Sheet 11 Model B "Fixed Rotation"): off by default, pass true to restrict each
    SE to today's persisted beat-zone -- see planning.routing.generate_route_plans_for_se.

    Moved from GET to POST 2026-09-16 -- the single heaviest mutation in this API
    (normalization + full plan generation combined), was GET -- see
    _generate_and_respond's own docstring for the full rationale."""
    params = _json_body(request)
    plan_date = params.get("date")
    force_normalization = str(params.get("force_normalization", "")).lower() in ("1", "true", "yes")
    skip_normalization = str(params.get("skip_normalization", "")).lower() in ("1", "true", "yes")
    enable_rotation = str(params.get("rotation", "")).lower() in ("1", "true", "yes")
    try:
        focus_product_kwargs = _focus_product_kwargs(params)
        routing_plan_choice = _routing_plan_choice(params)
    except (ProductCohortError, ValueError) as e:
        return JsonResponse({"error": str(e)}, status=422)
    try:
        plan_run, normalization_info = activate_tuff_scope(
            scope_type, scope_value, plan_date,
            force_normalization=force_normalization, skip_normalization=skip_normalization,
            routing_plan_choice=routing_plan_choice, enable_rotation=enable_rotation,
            **focus_product_kwargs,
        )
    except PlanningError as e:
        return JsonResponse({"error": str(e)}, status=422)
    except Exception as e:
        return JsonResponse({"error": f"{type(e).__name__}: {e}"}, status=502)
    return JsonResponse(
        {"Normalization": normalization_info, **_serialize_plan_run(plan_run)},
        safe=False, json_dumps_params={"default": str},
    )


@require_GET
def route_plans(request, se: str, plan_date: str):
    """GET /api/planning/routes/<se>/<plan_date>/?plan_run=<id> -- the Routing Agent's
    >=3 synced RoutePlans for one SE/day (R5.2's presentation fields)."""
    plan_run_id = request.GET.get("plan_run")
    try:
        result = list_route_plans(se, plan_date, int(plan_run_id) if plan_run_id else None)
    except RoutingError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


def _json_body(request) -> Dict[str, Any]:
    """Shared POST-body parser for the 5 route-mutation views below (architecture-audit
    fix, 2026-09-16: these were all @require_GET despite mutating PlanRun/RoutePlan/
    DailyTask/PitchScript/DCCard state -- GET is supposed to be safe/idempotent, and a
    real risk existed: browser prefetch, a proxy cache, or React Query's own
    refetch-on-window-focus could silently trigger a real mutation as a side effect of
    just viewing a link. Same JSON-body convention every other POST view in this file
    already uses (see admin_generate_all_states above)."""
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return {}


@csrf_exempt
@require_http_methods(["POST"])
def select_route_plan_view(request, se: str, plan_date: str, plan_type: str):
    """POST /api/planning/routes/<se>/<plan_date>/select/<plan_type>/ -- body: {"plan_run":
    <id> (optional)} -- the trust-equivalent of the Routing Agent's R5.3 ("the SE selects
    the final plan"), same as `manage.py select_route_plan --select`. Flips
    is_default_selected and re-syncs DailyTask rows from the newly-selected plan's stops.

    Moved from GET to POST 2026-09-16 (architecture audit) -- see _json_body's own
    docstring. csrf_exempt: same SameSite=Lax-cookie trust boundary as every other POST
    endpoint in this file (see New Lead gen model/src/shared/api/client.ts's own comment
    on apiPost) -- a cross-site POST can't carry the session cookie in the first place,
    so a separate CSRF-token round trip isn't needed on top of that for this same-origin
    SPA."""
    body = _json_body(request)
    plan_run_id = body.get("plan_run")
    try:
        result = select_default_route_plan(se, plan_date, plan_type.upper(), int(plan_run_id) if plan_run_id else None)
    except RoutingError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def accept_route_plan_view(request, se: str, plan_date: str, plan_type: str):
    """POST /api/planning/routes/<se>/<plan_date>/accept/<plan_type>/ -- body: {"plan_run":
    <id>, "actor": <name>} (both optional) -- an SE's own "Accept" action (added
    2026-09-15, explicit user request). Distinct from select_route_plan_view above (which
    stays as the plain pick-among-3-alternatives action, no approval implied, still used
    by admin/CLI browsing) -- this does the same pick + DailyTask resync, then also marks
    the whole day's PlanRun APPROVED with a real reviewer record (see
    routing.accept_route_plan's own docstring).

    Moved from GET to POST 2026-09-16 -- see select_route_plan_view's own docstring for
    why, and the CSRF reasoning (unchanged)."""
    body = _json_body(request)
    plan_run_id = body.get("plan_run")
    actor = body.get("actor", "")
    try:
        result = accept_route_plan(se, plan_date, plan_type.upper(), int(plan_run_id) if plan_run_id else None, actor)
    except RoutingError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def reject_route_plan_view(request, se: str, plan_date: str):
    """POST /api/planning/routes/<se>/<plan_date>/reject/ -- body: {"plan_run": <id>,
    "actor": <name>} (both optional) -- an SE's own "Reject" action (added 2026-09-15,
    explicit user request, explicit follow-up choice: purely an audit flag, DailyTask is
    deliberately left untouched -- see routing.reject_route_plan's own docstring).
    Rejects the whole day's PlanRun, not one specific route alternative --
    PlanRun.status is a PlanRun-level field.

    Moved from GET to POST 2026-09-16 -- see select_route_plan_view's own docstring for
    why, and the CSRF reasoning (unchanged)."""
    body = _json_body(request)
    plan_run_id = body.get("plan_run")
    actor = body.get("actor", "")
    try:
        result = reject_route_plan(se, plan_date, int(plan_run_id) if plan_run_id else None, actor)
    except RoutingError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def add_route_stop_view(request, se: str, plan_date: str, plan_type: str):
    """POST /api/planning/routes/<se>/<plan_date>/<plan_type>/stops/add/ -- body: {"dc_id":
    <id> (required), "plan_run": <id> (optional)} -- an SE adding a DC to their own route
    (added 2026-09-15, explicit user request, explicit follow-up choice: any DC in the
    SE's own assigned scope, not just this route's own dropped candidates). See
    routing.edit_route_stops' own docstring for the full validation (DC must be assigned
    to this SE, have real geo, not already on the route) and how distances/times get
    recomputed for real.

    Moved from GET to POST 2026-09-16 -- see select_route_plan_view's own docstring for
    why, and the CSRF reasoning (unchanged)."""
    body = _json_body(request)
    dc_id = body.get("dc_id")
    plan_run_id = body.get("plan_run")
    if not dc_id:
        return JsonResponse({"error": "dc_id is required"}, status=400)
    try:
        result = edit_route_stops(se, plan_date, plan_type.upper(), "add", dc_id, int(plan_run_id) if plan_run_id else None)
    except RoutingError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def remove_route_stop_view(request, se: str, plan_date: str, plan_type: str):
    """POST /api/planning/routes/<se>/<plan_date>/<plan_type>/stops/remove/ -- body:
    {"dc_id": <id> (required), "plan_run": <id> (optional)} -- an SE removing a DC from
    their own route (added 2026-09-15, explicit user request). See
    routing.edit_route_stops' own docstring - at least one stop must remain (reject the
    whole route instead if none of it is wanted).

    Moved from GET to POST 2026-09-16 -- see select_route_plan_view's own docstring for
    why, and the CSRF reasoning (unchanged)."""
    body = _json_body(request)
    dc_id = body.get("dc_id")
    plan_run_id = body.get("plan_run")
    if not dc_id:
        return JsonResponse({"error": "dc_id is required"}, status=400)
    try:
        result = edit_route_stops(se, plan_date, plan_type.upper(), "remove", dc_id, int(plan_run_id) if plan_run_id else None)
    except RoutingError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


def _no_pitch_or_card_response(daily_task_id: int, model_name: str) -> JsonResponse:
    """Shared 404 body for pitch_script/dc_card -- distinguishes "not applicable"
    (Farmer Meeting task, no dc_id, never gets one by design) from "should have one but
    doesn't" (a DC-tied task whose generation failed for just that task -- see
    pitching.generate_pitches_for_plan_run/dc_card.generate_dc_cards_for_plan_run's
    per-task isolation) from "no such DailyTask at all." All three used to return the
    identical "Farmer Meeting tasks never get one" message, which read as expected
    behavior even when it meant a real generation failure."""
    try:
        task = DailyTask.objects.get(id=daily_task_id)
    except DailyTask.DoesNotExist:
        return JsonResponse({"error": f"No DailyTask {daily_task_id}.", "Reason": "no_such_task"}, status=404)
    if task.dc_id is None:
        return JsonResponse({
            "error": f"No {model_name} for DailyTask {daily_task_id} (Farmer Meeting tasks never get one).",
            "Reason": "not_applicable",
        }, status=404)
    return JsonResponse({
        "error": f"No {model_name} for DailyTask {daily_task_id} -- this task has a DC (dc_id={task.dc_id}) and should have one; "
                 "generation may have failed for this task specifically (check ExceptionRecord for this PlanRun) or hasn't run yet.",
        "Reason": "generation_failed",
    }, status=404)


def _serialize_recommended_products(products: list) -> list:
    """PitchScript.recommended_products/DCCard.recommended_products (planning/models.py)
    -> the API's PascalCase shape. Up to 5 items, highest value first, widened 2026-08-18
    from a single top product per direct instruction -- never padded, a DC with 2 real
    candidates returns a 2-item list, not 5. Scope is "block"/"node" (this DC's own
    dominant_category, peer-purchase ranked) or "nearby_radius"/"nearby_node"
    (services._attach_nearby_product_recommendations' geographic fallback, used only
    when this DC's own block+node peers had nothing to rank from)."""
    return [
        {
            "Product_Name": p.get("name"),
            "Value": p.get("value"),
            "Category": p.get("category"),
            "Sub_Category": p.get("sub_category"),
            "Brand": p.get("brand"),
            "Business_Segment": p.get("business_segment"),
            "Scope": p.get("scope"),
        }
        for p in products
    ]


def _serialize_ai_sales_forecast(forecast: dict):
    """PitchScript.ai_sales_forecast (planning.ai_sales_forecast.build_ai_pitch, added
    2026-09-12) -- everything the AI-Generated Pitch returned EXCEPT script_hindi itself
    (that's already folded into Script_Hindi above, whether or not the AI version won
    over the templated fallback -- see Data_Sources_Used's own "AI-Generated Script"
    entry for which one actually did). None when {} (the AI pitch wasn't used this run
    at all -- no provider configured, nothing real to build from, every provider
    failed, or the response had no usable script), not an empty object, so the frontend
    can tell "not applicable" apart from "AI ran but had nothing to add," same
    convention as RoutePlan.llm_reasoning."""
    if not forecast:
        return None
    return {
        "Window_Days": forecast.get("window_days"),
        "Products": [{"Name": p.get("name"), "Reason": p.get("reason")} for p in forecast.get("products") or []],
        "Reasoning": forecast.get("reasoning") or None,
        "Club_Context": forecast.get("club_context"),
        "Scheme_Context": [
            {
                "Name": s.get("name"), "Category": s.get("category"), "Brand": s.get("brand"),
                "Valid_Until": s.get("valid_until"),
            }
            for s in forecast.get("scheme_context") or []
        ],
        "Notes": forecast.get("notes") or [],
    }


@require_GET
def pitch_script(request, daily_task_id: int):
    """GET /api/planning/pitch/<daily_task_id>/ -- the Pitching Agent's output for one
    DailyTask (Hindi script + which data sources it did/didn't have). 404 if the task
    has no pitch -- Farmer Meeting tasks (no dc_id) never get one, by design. Distinct
    from a DC-tied task that SHOULD have a pitch but doesn't (generation failed for that
    one task -- see pitching.generate_pitches_for_plan_run's per-task isolation) --
    those used to get the exact same "never get one" message, which hid a real failure
    behind wording that implied it was expected."""
    try:
        pitch = PitchScript.objects.select_related("daily_task").get(daily_task_id=daily_task_id)
    except PitchScript.DoesNotExist:
        return _no_pitch_or_card_response(daily_task_id, "PitchScript")
    return JsonResponse({
        "DailyTask_ID": pitch.daily_task_id,
        "SE": pitch.daily_task.se_name or pitch.daily_task.se_id,
        "DC_Name": pitch.daily_task.dc_name,
        "Purpose_Key": pitch.purpose_key,
        "Script_Hindi": pitch.script_hindi,
        # S1 recommended products, structured - already folded into Script_Hindi's own
        # S1 sentence as free text too. Up to 5, highest value first (widened 2026-08-18
        # from a single top product); [] when this DC had nothing to recommend this run,
        # even after the geographic fallback.
        "Recommended_Products": _serialize_recommended_products(pitch.recommended_products),
        "Data_Sources_Used": pitch.data_sources_used,
        "Data_Sources_Skipped": pitch.data_sources_skipped,
        "AI_Sales_Forecast": _serialize_ai_sales_forecast(pitch.ai_sales_forecast),
        "Generated_At": pitch.generated_at,
    }, safe=False, json_dumps_params={"default": str})


def _serialize_business_area_subcats(subcats: list) -> list:
    return [
        {
            "Sub_Category": sc.get("sub_category"),
            "Total": sc.get("total"),
            "Segments": [
                {
                    "Segment": seg.get("segment"),
                    "Total": seg.get("total"),
                    "Share_Of_Subcategory": seg.get("share_of_subcat"),
                    "Products": [
                        {"Name": p.get("name"), "Brand": p.get("brand"), "Value": p.get("value")}
                        for p in seg.get("products", [])
                    ],
                }
                for seg in sc.get("segments", [])
            ],
        }
        for sc in subcats
    ]


def _serialize_business_area_detail(detail: dict):
    """DCCard.business_area_detail (planning/models.py) -> the API's PascalCase shape.
    None when this DC has no current-year business-area data at all (the dict is {} in
    that case) -- Prior/Prior_Total are independently nullable even when Current isn't,
    since prior-year comparison data can be genuinely unavailable for a DC that still
    has real current-year activity."""
    if not detail:
        return None
    return {
        "Current_Total": detail.get("current_total"),
        "Current_Branded_Total": detail.get("current_branded_total"),
        "Current_PL_Total": detail.get("current_pl_total"),
        "Current": _serialize_business_area_subcats(detail.get("current") or []),
        "Prior_Total": detail.get("prior_total"),
        "Prior": _serialize_business_area_subcats(detail["prior"]) if detail.get("prior") else None,
    }


def _serialize_turnover_detail(detail: dict):
    """DCCard.turnover_detail (planning/models.py) -> the API's PascalCase shape. None
    when none of these signals were available this run (the dict is {} in that case)."""
    if not detail:
        return None
    return {
        "Purchase_Last_FY": detail.get("purchase_last_fy"),
        "Purchase_YTD": detail.get("purchase_ytd"),
        "Qualifying_Turnover": detail.get("qualifying_turnover"),
        "YoY_PL_Growth_Pct": detail.get("yoy_pl_growth_pct"),
        "YTD_PL_Last_Year": detail.get("ytd_pl_last_year"),
    }


def _serialize_health_score_detail(detail: dict):
    """DCCard.health_score_detail (planning/models.py) -> the API's PascalCase shape.
    None when this DC had no Health Score computed this run (the dict is {} in that
    case -- e.g. failed the active/Days_Since_Last_Sale<=60 eligibility gate). Sub_Scores
    keeps its own inner per-component shape (score_pct/bucket/urgency, e.g. "NRV")
    unchanged -- same lowercase passthrough convention as DailyTask.Health_Sub_Scores/
    BO_Scores, not remapped to PascalCase like the outer keys here."""
    if not detail:
        return None
    return {
        "DC_Health_Score": detail.get("dc_health_score"),
        "Health_Gap": detail.get("health_gap"),
        "Sub_Scores": detail.get("sub_scores") or {},
        "Negative_GM_Flag": detail.get("negative_gm_flag", False),
        "Health_Focus_Track": detail.get("health_focus_track", False),
        "Health_Focus_Purposes": detail.get("health_focus_purposes", ""),
        "Credit_Limit": detail.get("credit_limit"),
        "Available_Credit_Limit": detail.get("available_credit_limit"),
        "Credit_Active": detail.get("credit_active"),
    }


@require_GET
def dc_card(request, daily_task_id: int):
    """GET /api/planning/dc-card/<daily_task_id>/ -- the DC Card (Preface, "Dehaat
    Center Ko Jaano") for one DailyTask -- shown to the SE BEFORE the pitch (see
    /api/planning/pitch/<daily_task_id>/, a separate endpoint since the two are shown at
    different points in the SE's flow, not one combined payload). 404 if the task has no
    card -- Farmer Meeting tasks (no dc_id) never get one, same rule as PitchScript."""
    try:
        card = DCCard.objects.select_related("daily_task").get(daily_task_id=daily_task_id)
    except DCCard.DoesNotExist:
        return _no_pitch_or_card_response(daily_task_id, "DCCard")
    return JsonResponse({
        "DailyTask_ID": card.daily_task_id,
        "SE": card.daily_task.se_name or card.daily_task.se_id,
        "DC_Name": card.daily_task.dc_name,
        "Who_Section": card.who_section,
        "Where_DC_Stands_Section": card.where_dc_stands_section,
        # Structured form of card_hindi's "3. Health Score" block (Source 3k, added
        # 2026-09-06) - null when this DC had no Health Score computed this run. Section
        # is the same Hindi narrative already embedded in Card_Hindi below, exposed on
        # its own so the frontend isn't forced to re-parse the combined text.
        "Health_Score_Section": card.health_score_section or None,
        "Health_Score_Detail": _serialize_health_score_detail(card.health_score_detail),
        "Card_Hindi": card.card_hindi,
        # Structured form of Who_Section's "Business Area Strength" bullet - null when
        # this DC has no current-year business-area data at all. Prior is independently
        # nullable even when Current isn't (prior-year comparison can genuinely be
        # unavailable for a DC with real current-year activity).
        "Business_Area_Detail": _serialize_business_area_detail(card.business_area_detail),
        # Structured form of Who_Section's "Turnover-wise Standing" bullet - null when
        # none of these signals were available this run.
        "Turnover_Detail": _serialize_turnover_detail(card.turnover_detail),
        # Structured form of Who_Section's "Scheme Standing" bullet - same shape/meaning
        # as DailyTask.Club_Detail (see _serialize_club_detail).
        "Club_Detail": _serialize_club_detail(card.club_detail),
        "Data_Sources_Used": card.data_sources_used,
        "Data_Sources_Skipped": card.data_sources_skipped,
        "Generated_At": card.generated_at,
    }, safe=False, json_dumps_params={"default": str})


@require_GET
def headcount_bifurcation(request):
    """GET /api/planning/headcount/?list=true -- active headcount bifurcated by SE/ABM/
    RBM role and by Node/Block/District/State, plus an overall total (see
    planning.headcount's module docstring for the resolution logic and its honest
    limitations). list=true includes each bucket's individual emails, not just counts
    -- matches `manage.py show_headcount_bifurcation --list`."""
    try:
        result = compute_active_headcount_bifurcation(_planning_output_dir())
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)
    if request.GET.get("list", "").lower() not in ("1", "true", "yes"):
        result = {
            "overall_total": result["overall_total"],
            "se_role_count": len(result["se_role"]),
            "abm_role_count": len(result["abm_role"]),
            "rbm_role_count": len(result["rbm_role"]),
            "no_role_count": len(result["no_role"]),
            "by_node": {k: len(v) for k, v in result["by_node"].items()},
            "by_block": {k: len(v) for k, v in result["by_block"].items()},
            "by_district": {k: len(v) for k, v in result["by_district"].items()},
            "by_state": {k: len(v) for k, v in result["by_state"].items()},
        }
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


@require_GET
def directory_states(request):
    """GET /api/planning/directory/states/ -- every State with a real DC, plus node/SE/DC
    counts. Populates a top-level dropdown; every other directory endpoint can be scoped
    with ?state=<value> from here."""
    try:
        return JsonResponse(list_states(_planning_output_dir()), safe=False)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)


@require_GET
def directory_nodes(request):
    """GET /api/planning/directory/nodes/?state= -- every Node, optionally scoped to a State."""
    try:
        return JsonResponse(list_nodes(_planning_output_dir(), request.GET.get("state")), safe=False)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)


@require_GET
def directory_districts(request):
    """GET /api/planning/directory/districts/?state= -- live-only (Geo_Mapping/Source 1c)."""
    try:
        return JsonResponse(list_districts(_planning_output_dir(), request.GET.get("state")), safe=False)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)


@require_GET
def directory_blocks(request):
    """GET /api/planning/directory/blocks/?state=&district= -- live-only (Geo_Mapping/Source 1c)."""
    try:
        return JsonResponse(list_blocks(_planning_output_dir(), request.GET.get("state"), request.GET.get("district")), safe=False)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)


@require_GET
def directory_zbms(request):
    """GET /api/planning/directory/zbms/ -- "State Head" = ZBM, the closest real role to
    that term in this data model (no dedicated State-Head field exists anywhere)."""
    try:
        return JsonResponse(list_zbms(_planning_output_dir()), safe=False)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)


@require_GET
def directory_rbms(request):
    """GET /api/planning/directory/rbms/"""
    try:
        return JsonResponse(list_rbms(_planning_output_dir()), safe=False)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)


@require_GET
def directory_abms(request):
    """GET /api/planning/directory/abms/"""
    try:
        return JsonResponse(list_abms(_planning_output_dir()), safe=False)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)


@require_GET
def directory_ses(request):
    """GET /api/planning/directory/ses/?state=&node= -- SEs with at least one assigned
    DC (resolve_scope_dcs' own definition of an SE), not planning.headcount's broader
    "active in the last 90 days" definition."""
    try:
        return JsonResponse(list_ses(_planning_output_dir(), request.GET.get("state"), request.GET.get("node")), safe=False)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)


@require_GET
def directory_dcs(request):
    """GET /api/planning/directory/dcs/?state=&node=&se=&limit=200&offset=0 -- paginated,
    limit capped at 1000 (DC_Master has 19k+ rows network-wide)."""
    try:
        limit = int(request.GET.get("limit", 200))
        offset = int(request.GET.get("offset", 0))
    except ValueError:
        return JsonResponse({"error": "limit/offset must be integers"}, status=400)
    try:
        result = list_dcs(_planning_output_dir(), request.GET.get("state"), request.GET.get("node"), request.GET.get("se"), limit, offset)
    except FileNotFoundError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(result, safe=False)


def _pagination_params(request, default_limit: int, max_limit: int):
    """Shared limit/offset parsing for the list endpoints below. Returns (limit, offset),
    or None if either query param isn't a valid integer (caller returns 400). Response
    body shape is left alone (still a bare JSON array, for backward compatibility with
    existing callers) -- the true total/limit/offset are exposed via X-Total-Count/
    X-Limit/X-Offset headers instead, same reasoning as directory.list_dcs's total/
    limit/offset/returned fields: a caller previously had no way to tell a full result
    from a silently truncated one."""
    try:
        limit = int(request.GET.get("limit", default_limit))
        offset = int(request.GET.get("offset", 0))
    except ValueError:
        return None
    return max(1, min(limit, max_limit)), max(0, offset)


def _paginated_json_response(qs, total: int, limit: int, offset: int, row_fn):
    response = JsonResponse([row_fn(r) for r in qs], safe=False, json_dumps_params={"default": str})
    response["X-Total-Count"] = str(total)
    response["X-Limit"] = str(limit)
    response["X-Offset"] = str(offset)
    return response


@require_GET
def visit_streaks(request):
    """GET /api/planning/streaks/?se=&dc=&min_misses=&limit=&offset= -- DCVisitStreak:
    consecutive-miss tracking per (SE, DC), independent of any single PlanRun. Feeds a
    priority_multiplier escalation the next time a DC is scored (see reconcile_outcomes).
    limit defaults to 500, capped at 2000; see X-Total-Count on the response to tell a
    full result from a truncated one."""
    qs = DCVisitStreak.objects.all()
    if request.GET.get("se"):
        qs = qs.filter(se_id=request.GET["se"])
    if request.GET.get("dc"):
        qs = qs.filter(dc_id=request.GET["dc"])
    if request.GET.get("min_misses"):
        try:
            qs = qs.filter(consecutive_misses__gte=int(request.GET["min_misses"]))
        except ValueError:
            return JsonResponse({"error": "min_misses must be an integer"}, status=400)
    pagination = _pagination_params(request, default_limit=500, max_limit=2000)
    if pagination is None:
        return JsonResponse({"error": "limit/offset must be integers"}, status=400)
    limit, offset = pagination
    qs = qs.order_by("-consecutive_misses")
    total = qs.count()
    page = qs[offset : offset + limit]
    return _paginated_json_response(page, total, limit, offset, lambda s: {
        "SE_ID": s.se_id, "DC_ID": s.dc_id, "Consecutive_Misses": s.consecutive_misses,
        "Last_Outcome_Date": s.last_outcome_date, "Updated_At": s.updated_at,
    })


@require_GET
def completion_stats(request):
    """GET /api/planning/completion-stats/?se=&objective=&limit=&offset= --
    ObjectiveCompletionStats: trailing-30d completion rate per (SE, objective), rolled up
    by `compute_completion_stats` and consumed as a bounded (0.7x-1.3x) weighting
    multiplier in BO1/BO3 scoring (Tier 2 adaptive weighting). limit defaults to 500,
    capped at 2000; see X-Total-Count on the response to tell a full result from a
    truncated one."""
    qs = ObjectiveCompletionStats.objects.all()
    if request.GET.get("se"):
        qs = qs.filter(se_id=request.GET["se"])
    if request.GET.get("objective"):
        qs = qs.filter(objective=request.GET["objective"])
    pagination = _pagination_params(request, default_limit=500, max_limit=2000)
    if pagination is None:
        return JsonResponse({"error": "limit/offset must be integers"}, status=400)
    limit, offset = pagination
    qs = qs.order_by("se_id", "objective")
    total = qs.count()
    page = qs[offset : offset + limit]
    return _paginated_json_response(page, total, limit, offset, lambda s: {
        "SE_ID": s.se_id, "Objective": s.objective, "Completion_Rate_30d": s.completion_rate_30d,
        "Sample_Size": s.sample_size, "Computed_At": s.computed_at,
    })


@require_GET
def scheduled_scopes(request):
    """GET /api/planning/scheduled-scopes/?active=true&scope_type= -- ScheduledScope:
    the (scope_type, scope_value) pairs `run_scheduled_tuff` runs TUFF for once daily via
    cron, independent of any ad-hoc activate_tuff/generate_se_plan call."""
    qs = ScheduledScope.objects.all()
    if request.GET.get("active") is not None:
        qs = qs.filter(active=request.GET["active"].lower() in ("1", "true", "yes"))
    if request.GET.get("scope_type"):
        qs = qs.filter(scope_type=request.GET["scope_type"].upper())
    qs = qs.order_by("scope_type", "scope_value")
    return JsonResponse([
        {
            "Scope_Type": s.scope_type, "Scope_Value": s.scope_value, "Active": s.active,
            "Last_Run_At": s.last_run_at, "Created_At": s.created_at,
        }
        for s in qs
    ], safe=False, json_dumps_params={"default": str})


@require_GET
def plan_run_detail(request, plan_run_id: int):
    """GET /api/planning/runs/<id>/ -- re-fetch a previously generated & persisted plan."""
    try:
        plan_run = PlanRun.objects.get(id=plan_run_id)
    except PlanRun.DoesNotExist:
        return JsonResponse({"error": f"PlanRun {plan_run_id} not found"}, status=404)
    return JsonResponse(_serialize_plan_run(plan_run), safe=False, json_dumps_params={"default": str})


@require_GET
def admin_tracking(request):
    """GET /api/planning/admin/tracking/?days=7 | ?from=YYYY-MM-DD&to=YYYY-MM-DD -- the
    Tracking dashboard's numbers (added 2026-09-16, see planning.tracking's own
    docstring for what each tier means and why). Explicit from/to (inclusive plan
    dates, span <= 90 days) wins; else the last `days` days ending today (default 7).
    Repeatable &se=<email> / &abm=<code> narrow Outcomes and Adoption to those SEs (an
    ABM expands to the SEs under it) and add a per-SE table; Quality / Data health /
    Ops stay network-wide. Read-only,
    computed at request time from what the pipeline already persists -- ~1.5s over a
    week of runs. ADMIN-only in the frontend nav, same as the rest of the admin/ family
    (the API itself enforces nothing, per this app's stated RBAC convention)."""
    try:
        days = int(request.GET["days"]) if request.GET.get("days") else None
    except ValueError:
        return JsonResponse({"error": "days must be an integer"}, status=400)
    try:
        metrics = compute_tracking_metrics(
            days, request.GET.get("from") or None, request.GET.get("to") or None,
            se_emails=request.GET.getlist("se"), abm_codes=request.GET.getlist("abm"),
        )
    except TrackingWindowError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(metrics, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def admin_reconcile(request):
    """POST /api/planning/admin/reconcile/ -- the Tracking dashboard's "Reconcile now":
    reconciles every past plan_date that still has UNKNOWN DC-visit tasks, network-
    wide, one live pull set per date (planning.reconciliation.reconcile_past_tasks).
    Idempotent; a second click finds nothing to do. Returns the per-date summaries.
    Same @csrf_exempt + POST convention as the rest of the admin/ family."""
    from .reconciliation import ReconciliationError, format_summary, reconcile_past_tasks
    client = agent.get_client()
    try:
        summaries = reconcile_past_tasks(client=client)
    except ReconciliationError as e:
        return JsonResponse({"error": str(e)}, status=400)
    finally:
        client.close()
    return JsonResponse({
        "Dates": len(summaries),
        "Tasks": sum(s["tasks"] for s in summaries),
        "Completed": sum(s["completed"] for s in summaries),
        "Partial": sum(s["partial"] for s in summaries),
        "Missed": sum(s["missed"] for s in summaries),
        "Escalated": sum(s["escalated"] for s in summaries),
        "Payment_Amount": sum(s["payment_amount"] for s in summaries),
        "Pull_Failures": [f for s in summaries for f in s["pull_failures"]],
        "Lines": [format_summary(s) for s in summaries if s["tasks"]],
    }, json_dumps_params={"default": str})


@require_GET
def plan_run_list(request):
    """GET /api/planning/runs/?scope_type=NODE&scope_value=Jaipur&status=PENDING_REVIEW&plan_date=YYYY-MM-DD&limit=&offset=
    -- list past runs, newest first. limit defaults to 50, capped at 500; see
    X-Total-Count on the response to tell a full result from a truncated one.

    plan_date (added 2026-09-10, explicit user request -- "all system plan created with
    all filter"): exact-date match, same convention as scope_value/status (no range
    filter -- a PlanRun is always generated for one specific plan_date, never a span)."""
    qs = PlanRun.objects.all()
    if request.GET.get("scope_type"):
        qs = qs.filter(scope_type=request.GET["scope_type"].upper())
    if request.GET.get("scope_value"):
        qs = qs.filter(scope_value=request.GET["scope_value"])
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"].upper())
    if request.GET.get("plan_date"):
        qs = qs.filter(plan_date=request.GET["plan_date"])
    pagination = _pagination_params(request, default_limit=50, max_limit=500)
    if pagination is None:
        return JsonResponse({"error": "limit/offset must be integers"}, status=400)
    limit, offset = pagination
    total = qs.count()
    page = qs[offset : offset + limit]
    return _paginated_json_response(page, total, limit, offset, lambda r: {
        "PlanRun_ID": r.id, "Scope_Type": r.scope_type, "Scope_Value": r.scope_value,
        "Plan_Date": r.plan_date, "Run_Timestamp": r.run_timestamp,
        "SE_Count": r.se_count, "DC_Count": r.dc_count, "Task_Count": r.task_count,
        "Status": r.status,
    })


@csrf_exempt
@require_http_methods(["POST"])
def admin_generate_all_states(request):
    """POST /api/planning/admin/generate-all-states/ -- explicit user request via the
    System Plan Runs page ("system run plan means it will generate the plan for all se
    with eligible dc"). Launches planning.management.commands.run_all_states_tuff as a
    DETACHED background subprocess and returns immediately (body: {"plan_date":
    "YYYY-MM-DD"} optional, defaults to today inside the command; "actor" optional,
    logged only) -- a full pass runs Data Normalization once plus the SE Daily Task
    Agent for every STATE currently in DC_Master_Normalized (~11-12 states, each already
    covering every SE under it), taking real minutes against live data sources. New
    PlanRuns simply appear in GET /runs/ (System Plan Runs) as each state finishes --
    this endpoint has no separate progress/status shape of its own, by design (explicit
    user choice over building a dedicated progress view).

    stdout/stderr redirected to a timestamped file under logs/ so a run is inspectable
    after the fact even though nothing streams it back to the request. No concurrency
    guard against a second trigger overlapping a still-running one -- same accepted
    posture as run_scheduled_tuff's own cron invocation, which has never had one either.

    csrf_exempt: same unauthenticated trust boundary as every other admin write in this
    file -- this app has no session/login system anywhere."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    plan_date = body.get("plan_date")
    actor = str(body.get("actor") or "")

    base_dir = Path(settings.SE_DAILY_PLAN_AGENT_PATH)
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"generate_all_states_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    cmd = [sys.executable, "manage.py", "run_all_states_tuff"]
    if plan_date:
        cmd += ["--date", plan_date]

    with open(log_path, "w") as log_file:
        log_file.write(f"# Triggered by {actor or 'unknown'} at {datetime.now().isoformat()}\n")
        log_file.flush()
        subprocess.Popen(
            cmd, cwd=base_dir, stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=True,  # detach -- must outlive this request/response
        )

    return JsonResponse({
        "started": True,
        "log_file": str(log_path.relative_to(base_dir)),
        "message": "Generation started in the background for every state -- watch System Plan Runs for new entries.",
    })


_ROUTING_OVERRIDE_FIELDS = (
    "r1_2_max_travel_minutes", "plan_a_max_round_trip_distance_km",
    "plan_b_max_daily_distance_km", "plan_b_max_daily_travel_minutes",
)


def _serialize_routing_override(o) -> Dict[str, Any]:
    return {
        "Scope_Type": o.scope_type, "Scope_Value": o.scope_value,
        **{f: getattr(o, f) for f in _ROUTING_OVERRIDE_FIELDS},
        "Updated_At": o.updated_at, "Updated_By": o.updated_by,
    }


@csrf_exempt
@require_http_methods(["GET", "POST"])
def admin_routing_overrides(request):
    """/api/planning/admin/routing-overrides/ -- per-scope Routing ceiling overrides
    (added 2026-09-11, explicit user request -- "in routing parameter rule may be
    different for node, district, state or overall"). See planning.models.
    RoutingScopeOverride's own docstring for the NODE/STATE-only scope, per-parameter
    nullability, and why DISTRICT isn't offered yet.

    GET: every configured override, newest-updated first.

    POST: body {"scope_type": "NODE"|"STATE", "scope_value": "...", plus any subset of
    r1_2_max_travel_minutes/plan_a_max_round_trip_distance_km/
    plan_b_max_daily_distance_km/plan_b_max_daily_travel_minutes (each a number or null
    to clear it -- omitted keys are left untouched, not reset), "actor": "..."} --
    upserts by (scope_type, scope_value), same partial-update convention as every other
    admin write in this file. Returns the updated row.

    csrf_exempt: same unauthenticated trust boundary as every other admin write here."""
    if request.method == "GET":
        qs = RoutingScopeOverride.objects.all().order_by("-updated_at")
        return JsonResponse([_serialize_routing_override(o) for o in qs], safe=False, json_dumps_params={"default": str})

    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    scope_type = str(body.get("scope_type") or "").upper()
    scope_value = str(body.get("scope_value") or "").strip()
    if scope_type not in RoutingScopeOverride.ScopeType.values:
        return JsonResponse({"error": f"scope_type must be one of {RoutingScopeOverride.ScopeType.values}, got {scope_type!r}"}, status=400)
    if not scope_value:
        return JsonResponse({"error": "scope_value is required"}, status=400)

    obj, _ = RoutingScopeOverride.objects.get_or_create(scope_type=scope_type, scope_value=scope_value)
    for field in _ROUTING_OVERRIDE_FIELDS:
        if field in body:
            value = body[field]
            if value is not None:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    return JsonResponse({"error": f"{field} must be a number or null, got {value!r}"}, status=400)
            setattr(obj, field, value)
    obj.updated_by = str(body.get("actor") or "")
    obj.save()
    return JsonResponse(_serialize_routing_override(obj), json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def admin_routing_overrides_delete(request):
    """/api/planning/admin/routing-overrides/delete/ -- body {"scope_type":,
    "scope_value":} -- removes one override row entirely (reverting that scope fully to
    whatever the next-less-specific scope/the global default resolves to). POST, not
    DELETE, matching this file's existing convention of never using the DELETE verb."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    deleted, _ = RoutingScopeOverride.objects.filter(
        scope_type=str(body.get("scope_type") or "").upper(), scope_value=str(body.get("scope_value") or ""),
    ).delete()
    if not deleted:
        return JsonResponse({"error": "No matching override found"}, status=404)
    return JsonResponse({"deleted": True})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def admin_pipeline_config(request):
    """/api/planning/admin/config/ -- Admin Control Panel (added 2026-09-07).

    GET: every admin-editable BusinessConstants field, grouped by pipeline step (see
    planning.admin_config.ADMIN_EDITABLE_FIELDS), plus the 3 Step 11 routing ceilings
    shown read-only. `Value` is the live effective value (override if one exists, else
    the hardcoded default); `Overridden` tells the UI whether to show a "reset" control.

    POST: body {"changes": {key: new_value, ...}, "reset": [key, ...], "actor": "who's
    making this change"}. Applies `changes` (validated per-field against
    ADMIN_EDITABLE_FIELDS' type/min/max) and `reset`s (revert to hardcoded default) in
    that order, then returns the same shape GET returns plus an `Errors` map for any
    rejected keys (a partial apply, not all-or-nothing -- see admin_config.apply_
    overrides's own docstring).

    csrf_exempt: this whole API is unauthenticated GET-only elsewhere (no session/login
    system exists anywhere in this app, see PipelineSettings.updated_by's own docstring)
    -- the frontend has no CSRF token to send, so requiring one here would just break the
    one write path this app has, not add real protection. Same trust boundary as every
    other endpoint in this file, just now with a mutation."""
    if request.method == "POST":
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
        reset_keys = body.get("reset") or []
        if reset_keys:
            admin_config.reset_fields(reset_keys)
        changes = body.get("changes") or {}
        errors = admin_config.apply_overrides(changes, updated_by=str(body.get("actor") or "")) if changes else {}
        state = admin_config.get_config_state()
        state["Errors"] = errors
        return JsonResponse(state, json_dumps_params={"default": str})
    return JsonResponse(admin_config.get_config_state(), json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def admin_dc_selection_preview(request):
    """/api/planning/admin/dc-selection/preview/ -- explicit user request ("reflection
    of count before save rule"): a read-only counterpart to admin_dc_selection's POST,
    for the Admin Control Panel to show a live count while the admin is still editing an
    unsaved rule. Body: {"rules": {...}, "upload_mode": "..."} (upload_mode optional,
    defaults to whatever is currently stored). Never writes ProgramDCSelection -- see
    dc_selection.preview_selection's own docstring."""
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body"}, status=400)
    result = dc_selection.preview_selection(body.get("rules") or {}, upload_mode=body.get("upload_mode"))
    return JsonResponse(result, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def admin_dc_selection(request):
    """/api/planning/admin/dc-selection/ -- DC Selection (added 2026-09-08). See
    planning.dc_selection's module docstring for the full feature.

    GET: current rule (Rules, each of the 4 criteria with enabled/combine/params),
    Manual_Includes/Manual_Excludes, and a live-computed preview (Universe_Size,
    Selected_Count, Configured -- whether a rule/manual list has actually been set, in
    which case this replaces the Excel Top DC list network-wide the next time a plan is
    generated).

    POST: body {"rules": {...}, "manual_includes": [...], "manual_excludes": [...],
    "upload_mode": "uploaded_only"|"uploaded_plus_filter", "actor": "..."} -- any subset;
    provided keys replace their whole value (rules is not merged per-criterion, see
    dc_selection.update_selection's own docstring for why). A rank_range rule matching
    zero DCs is rejected (400), see update_selection's own validation. Returns the same
    shape GET returns.

    csrf_exempt: same unauthenticated trust boundary as admin_pipeline_config above --
    this app has no session/login system anywhere."""
    if request.method == "POST":
        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON body"}, status=400)
        try:
            state = dc_selection.update_selection(
                body.get("rules"), body.get("manual_includes"), body.get("manual_excludes"),
                actor=str(body.get("actor") or ""), upload_mode=body.get("upload_mode"),
            )
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        return JsonResponse(state, json_dumps_params={"default": str})
    return JsonResponse(dc_selection.get_state(), json_dumps_params={"default": str})


@require_GET
def admin_dc_selection_search(request):
    """/api/planning/admin/dc-selection/search/?q=&limit=&offset=&filter_mode= --
    Search & toggle UX (point 3 of the DC Selection feature): searches the full
    DC_RAnk.csv universe by DC_ID/name substring, returns each match's Rank/Cohort/
    is_active/overdue plus whether it's in the currently-computed selection and/or
    manually included/excluded."""
    result = dc_selection.search_dcs(
        query=request.GET.get("q", ""),
        limit=int(request.GET.get("limit", 50) or 50),
        offset=int(request.GET.get("offset", 0) or 0),
        filter_mode=request.GET.get("filter_mode", "all"),
    )
    return JsonResponse(result, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def admin_dc_selection_upload_rank_csv(request):
    """/api/planning/admin/dc-selection/upload-rank-csv/ -- the "uploader" (point 2 of
    the DC Selection feature): multipart POST with a `file` field, replaces DC_RAnk.csv
    (se_daily_plan_agent.DC_MASTER_CSV) in place after validating it parses. See
    dc_selection.upload_rank_csv's own docstring for the validate-then-atomic-replace
    behavior; a rejected file leaves the existing DC_RAnk.csv untouched."""
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "No file uploaded (expected multipart field 'file')"}, status=400)
    try:
        state = dc_selection.upload_rank_csv(upload.read(), upload.name, actor=str(request.POST.get("actor") or ""))
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(state, json_dumps_params={"default": str})


@csrf_exempt
@require_http_methods(["POST"])
def admin_dc_selection_upload_selected_dcs(request):
    """/api/planning/admin/dc-selection/upload-selected-dcs/ -- Selected DC List
    uploader (added 2026-09-08, explicit user request -- "add one more uploader for
    selected dc"): multipart POST with a `file` field listing DC IDs (one per row, with
    or without a header), and an optional `upload_mode` form field
    ("uploaded_only"|"uploaded_plus_filter", chosen at upload time per direct
    instruction) -- each ID is checked against dc_datamart first (rejected if genuinely
    absent), then DC_RAnk.csv for Rank/Cohort (soft -- still accepted if missing).
    Accepted IDs are added to Manual_Includes (and cleared from Manual_Excludes), same
    effect as the Bulk Paste tab's "Apply includes" but from a file instead of a
    textarea. See dc_selection.upload_selected_dcs's own docstring."""
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse({"error": "No file uploaded (expected multipart field 'file')"}, status=400)
    try:
        state = dc_selection.upload_selected_dcs(
            upload.read(), upload.name, actor=str(request.POST.get("actor") or ""),
            upload_mode=request.POST.get("upload_mode") or None,
        )
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(state, json_dumps_params={"default": str})


@require_GET
def admin_dc_selection_sample_rank_csv(request):
    """/api/planning/admin/dc-selection/sample-rank-csv/ -- downloadable sample file for
    the Rank & Cohort uploader, so an admin knows the exact columns it expects."""
    response = HttpResponse(dc_selection.sample_rank_csv(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="dc_rank_cohort_sample.csv"'
    return response


@require_GET
def admin_dc_selection_sample_selected_dcs_csv(request):
    """/api/planning/admin/dc-selection/sample-selected-dcs-csv/ -- downloadable sample
    file for the Selected DC List uploader."""
    response = HttpResponse(dc_selection.sample_selected_dcs_csv(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="selected_dcs_sample.csv"'
    return response
