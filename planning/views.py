from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .directory import list_abms, list_blocks, list_dcs, list_districts, list_nodes, list_rbms, list_ses, list_states, list_zbms
from .headcount import compute_active_headcount_bifurcation
from .models import DailyTask, DCVisitStreak, ObjectiveCompletionStats, PitchScript, PlanRun, ScheduledScope
from .routing import RoutingError, list_route_plans, select_default_route_plan
from .services import PlanningError, activate_tuff_scope, generate_plan_for_scope, run_normalization_step
from .services import _output_dir as _planning_output_dir


def _serialize_task(t: DailyTask) -> dict:
    return {
        "Sr_No": t.sr_no, "DC_Name": t.dc_name, "DC_ID": t.dc_id, "Distance_Km": t.distance_km,
        "Recommended_Task_Type": t.recommended_task_type, "Purpose_Of_Visit": t.purpose_of_visit,
        "Reason_Of_Visit": t.reason_of_visit, "Last_Visit_Date": t.last_visit_date,
        "Days_Since_Last_Visit": t.days_since_last_visit, "Present_Outstanding": t.present_outstanding,
        "Present_Overdue": t.present_overdue, "Overdue_Aging_Bucket": t.overdue_aging_bucket,
        "Last_Order_Date": t.last_order_date,
        "Last_Order_Value": t.last_order_value, "Last_Payment_Date": t.last_payment_date,
        "Last_Payment_Join_Key_Unconfirmed": t.last_payment_join_key_unconfirmed,
        "YTD_Private_Label": t.ytd_private_label, "DC_Club_Participation": t.dc_club_participation,
        "Objective": t.objective, "No_New_Orders": t.no_new_orders, "Credit_On_Hold": t.credit_on_hold,
        "Credit_On_Hold_Reason": t.credit_on_hold_reason, "Estimated_Duration": t.estimated_duration,
        "Priority_Multiplier": t.priority_multiplier,
        # Outcome-reconciliation block (Tier 1 feedback loop) -- populated by
        # `manage.py reconcile_outcomes` once plan_date has passed; Outcome_Status stays
        # UNKNOWN (never guessed) until that runs, same honest-degrade discipline as the
        # rest of this codebase.
        "Outcome_Status": t.outcome_status, "Actual_Visit_Date": t.actual_visit_date,
        "Actual_Order_Value": t.actual_order_value, "Actual_Payment_Amount": t.actual_payment_amount,
        "Reconciled_At": t.reconciled_at,
    }


def _serialize_plan_run(plan_run: PlanRun) -> dict:
    tasks_by_se = {}
    for t in plan_run.tasks.all():
        tasks_by_se.setdefault(t.se_id, {"SE_ID": t.se_id, "SE_Name": t.se_name, "Tasks": []})
        tasks_by_se[t.se_id]["Tasks"].append(_serialize_task(t))
    return {
        "PlanRun_ID": plan_run.id,
        "Scope_Type": plan_run.scope_type,
        "Scope_Value": plan_run.scope_value,
        "Plan_Date": plan_run.plan_date,
        "Run_Timestamp": plan_run.run_timestamp,
        "Metabase_Configured": plan_run.metabase_configured,
        "SE_Count": plan_run.se_count,
        "DC_Count": plan_run.dc_count,
        "Task_Count": plan_run.task_count,
        "Dynamic_Parameters_Resolved": plan_run.dynamic_parameters,
        "Note": plan_run.note,
        "Skipped_SEs": plan_run.skipped_ses,
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
            for e in plan_run.exceptions.all()
        ],
    }


@require_GET
def _generate_and_respond(request, scope_type: str, scope_value: str):
    plan_date = request.GET.get("date")
    try:
        plan_run = generate_plan_for_scope(scope_type, scope_value, plan_date)
    except PlanningError as e:
        return JsonResponse({"error": str(e)}, status=422)
    except Exception as e:  # Metabase/network errors etc. -- surface, don't swallow
        return JsonResponse({"error": f"{type(e).__name__}: {e}"}, status=502)
    return JsonResponse(_serialize_plan_run(plan_run), safe=False, json_dumps_params={"default": str})


# One endpoint per scope, per the doc's SE -> Node -> ABM -> DC -> Block -> District ->
# State hierarchy (Source 1c). Each just fixes scope_type and forwards scope_value/date --
# all the real logic lives in services.generate_plan_for_scope().

def se_plan(request, scope_value: str):
    """GET /api/planning/se/<se_email>/?date=YYYY-MM-DD"""
    return _generate_and_respond(request, PlanRun.ScopeType.SE, scope_value)


def abm_plan(request, scope_value: str):
    """GET /api/planning/abm/<abm_code>/?date=YYYY-MM-DD -- requires live Metabase (Source 1c)."""
    return _generate_and_respond(request, PlanRun.ScopeType.ABM, scope_value)


def rbm_plan(request, scope_value: str):
    """GET /api/planning/rbm/<rbm_code>/?date=YYYY-MM-DD -- requires live Metabase (Source 1c)."""
    return _generate_and_respond(request, PlanRun.ScopeType.RBM, scope_value)


def node_plan(request, scope_value: str):
    """GET /api/planning/node/<node_name>/?date=YYYY-MM-DD"""
    return _generate_and_respond(request, PlanRun.ScopeType.NODE, scope_value)


def block_plan(request, scope_value: str):
    """GET /api/planning/block/<block_name>/?date=YYYY-MM-DD -- requires live Metabase (Source 1c)."""
    return _generate_and_respond(request, PlanRun.ScopeType.BLOCK, scope_value)


def district_plan(request, scope_value: str):
    """GET /api/planning/district/<district_name>/?date=YYYY-MM-DD -- requires live Metabase (Source 1c)."""
    return _generate_and_respond(request, PlanRun.ScopeType.DISTRICT, scope_value)


def state_plan(request, scope_value: str):
    """GET /api/planning/state/<state_name>/?date=YYYY-MM-DD"""
    return _generate_and_respond(request, PlanRun.ScopeType.STATE, scope_value)


@require_GET
def normalize(request):
    """GET /api/planning/normalize/?date=YYYY-MM-DD&force=true -- Data Normalization
    Agent, once-per-day dedup (see planning.services.run_normalization_step). Always
    returns 200 with Reused indicating whether a live pull actually happened, so a
    caller can tell "ran fresh" from "reused today's data" without parsing free text."""
    date = request.GET.get("date")
    force = request.GET.get("force", "").lower() in ("1", "true", "yes")
    try:
        result = run_normalization_step(date=date, force=force)
    except PlanningError as e:
        return JsonResponse({"error": str(e)}, status=422)
    except Exception as e:
        return JsonResponse({"error": f"{type(e).__name__}: {e}"}, status=502)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


@require_GET
def tuff(request, scope_type: str, scope_value: str):
    """GET /api/planning/tuff/<scope_type>/<scope_value>/?date=YYYY-MM-DD&force_normalization=true&skip_normalization=true
    -- Agent TUFF: Step 1 (Data Normalization, once-per-day) + Step 2 (SE Daily Task +
    Pitching + Routing) in one call, mirroring `manage.py activate_tuff`. Response
    combines Step 1's outcome (Normalization) with the same PlanRun shape the scope
    endpoints (se_plan/state_plan/...) return."""
    plan_date = request.GET.get("date")
    force_normalization = request.GET.get("force_normalization", "").lower() in ("1", "true", "yes")
    skip_normalization = request.GET.get("skip_normalization", "").lower() in ("1", "true", "yes")
    try:
        plan_run, normalization_info = activate_tuff_scope(
            scope_type, scope_value, plan_date,
            force_normalization=force_normalization, skip_normalization=skip_normalization,
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


@require_GET
def select_route_plan_view(request, se: str, plan_date: str, plan_type: str):
    """GET /api/planning/routes/<se>/<plan_date>/select/<plan_type>/?plan_run=<id> --
    the trust-equivalent of the Routing Agent's R5.3 ("the SE selects the final plan"),
    same as `manage.py select_route_plan --select`. Flips is_default_selected and
    re-syncs DailyTask rows from the newly-selected plan's stops."""
    plan_run_id = request.GET.get("plan_run")
    try:
        result = select_default_route_plan(se, plan_date, plan_type.upper(), int(plan_run_id) if plan_run_id else None)
    except RoutingError as e:
        return JsonResponse({"error": str(e)}, status=422)
    return JsonResponse(result, safe=False, json_dumps_params={"default": str})


@require_GET
def pitch_script(request, daily_task_id: int):
    """GET /api/planning/pitch/<daily_task_id>/ -- the Pitching Agent's output for one
    DailyTask (Hindi script + which data sources it did/didn't have). 404 if the task
    has no pitch -- Farmer Meeting tasks (no dc_id) never get one, by design."""
    try:
        pitch = PitchScript.objects.select_related("daily_task").get(daily_task_id=daily_task_id)
    except PitchScript.DoesNotExist:
        return JsonResponse({"error": f"No PitchScript for DailyTask {daily_task_id} (Farmer Meeting tasks never get one)."}, status=404)
    return JsonResponse({
        "DailyTask_ID": pitch.daily_task_id,
        "SE": pitch.daily_task.se_name or pitch.daily_task.se_id,
        "DC_Name": pitch.daily_task.dc_name,
        "Purpose_Key": pitch.purpose_key,
        "Script_Hindi": pitch.script_hindi,
        "Data_Sources_Used": pitch.data_sources_used,
        "Data_Sources_Skipped": pitch.data_sources_skipped,
        "Generated_At": pitch.generated_at,
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


@require_GET
def visit_streaks(request):
    """GET /api/planning/streaks/?se=&dc=&min_misses= -- DCVisitStreak: consecutive-miss
    tracking per (SE, DC), independent of any single PlanRun. Feeds a priority_multiplier
    escalation the next time a DC is scored (see reconcile_outcomes)."""
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
    qs = qs.order_by("-consecutive_misses")[:500]
    return JsonResponse([
        {
            "SE_ID": s.se_id, "DC_ID": s.dc_id, "Consecutive_Misses": s.consecutive_misses,
            "Last_Outcome_Date": s.last_outcome_date, "Updated_At": s.updated_at,
        }
        for s in qs
    ], safe=False, json_dumps_params={"default": str})


@require_GET
def completion_stats(request):
    """GET /api/planning/completion-stats/?se=&objective= -- ObjectiveCompletionStats:
    trailing-30d completion rate per (SE, objective), rolled up by
    `compute_completion_stats` and consumed as a bounded (0.7x-1.3x) weighting
    multiplier in BO1/BO3 scoring (Tier 2 adaptive weighting)."""
    qs = ObjectiveCompletionStats.objects.all()
    if request.GET.get("se"):
        qs = qs.filter(se_id=request.GET["se"])
    if request.GET.get("objective"):
        qs = qs.filter(objective=request.GET["objective"])
    qs = qs.order_by("se_id", "objective")[:500]
    return JsonResponse([
        {
            "SE_ID": s.se_id, "Objective": s.objective, "Completion_Rate_30d": s.completion_rate_30d,
            "Sample_Size": s.sample_size, "Computed_At": s.computed_at,
        }
        for s in qs
    ], safe=False, json_dumps_params={"default": str})


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
def plan_run_list(request):
    """GET /api/planning/runs/?scope_type=NODE&scope_value=Jaipur&status=PENDING_REVIEW -- list past runs, newest first."""
    qs = PlanRun.objects.all()
    if request.GET.get("scope_type"):
        qs = qs.filter(scope_type=request.GET["scope_type"].upper())
    if request.GET.get("scope_value"):
        qs = qs.filter(scope_value=request.GET["scope_value"])
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"].upper())
    qs = qs[:50]
    return JsonResponse([
        {
            "PlanRun_ID": r.id, "Scope_Type": r.scope_type, "Scope_Value": r.scope_value,
            "Plan_Date": r.plan_date, "Run_Timestamp": r.run_timestamp,
            "SE_Count": r.se_count, "DC_Count": r.dc_count, "Task_Count": r.task_count,
            "Status": r.status,
        }
        for r in qs
    ], safe=False, json_dumps_params={"default": str})
