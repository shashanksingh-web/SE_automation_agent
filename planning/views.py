from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .models import DailyTask, PlanRun
from .services import PlanningError, generate_plan_for_scope


def _serialize_task(t: DailyTask) -> dict:
    return {
        "Sr_No": t.sr_no, "DC_Name": t.dc_name, "DC_ID": t.dc_id, "Distance_Km": t.distance_km,
        "Recommended_Task_Type": t.recommended_task_type, "Purpose_Of_Visit": t.purpose_of_visit,
        "Reason_Of_Visit": t.reason_of_visit, "Last_Visit_Date": t.last_visit_date,
        "Days_Since_Last_Visit": t.days_since_last_visit, "Present_Outstanding": t.present_outstanding,
        "Present_Overdue": t.present_overdue, "Last_Order_Date": t.last_order_date,
        "Last_Order_Value": t.last_order_value, "Last_Payment_Date": t.last_payment_date,
        "Last_Payment_Join_Key_Unconfirmed": t.last_payment_join_key_unconfirmed,
        "YTD_Private_Label": t.ytd_private_label, "DC_Club_Participation": t.dc_club_participation,
        "Objective": t.objective, "No_New_Orders": t.no_new_orders, "Credit_On_Hold": t.credit_on_hold,
        "Credit_On_Hold_Reason": t.credit_on_hold_reason, "Estimated_Duration": t.estimated_duration,
        "Priority_Multiplier": t.priority_multiplier,
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
        "Plans": list(tasks_by_se.values()),
        "Exceptions_Report": [
            {"Source": e.source, "Reason_Code": e.reason_code, "Detail": e.detail, "Run_Timestamp": e.run_timestamp}
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
def plan_run_detail(request, plan_run_id: int):
    """GET /api/planning/runs/<id>/ -- re-fetch a previously generated & persisted plan."""
    try:
        plan_run = PlanRun.objects.get(id=plan_run_id)
    except PlanRun.DoesNotExist:
        return JsonResponse({"error": f"PlanRun {plan_run_id} not found"}, status=404)
    return JsonResponse(_serialize_plan_run(plan_run), safe=False, json_dumps_params={"default": str})


@require_GET
def plan_run_list(request):
    """GET /api/planning/runs/?scope_type=NODE&scope_value=Jaipur -- list past runs, newest first."""
    qs = PlanRun.objects.all()
    if request.GET.get("scope_type"):
        qs = qs.filter(scope_type=request.GET["scope_type"].upper())
    if request.GET.get("scope_value"):
        qs = qs.filter(scope_value=request.GET["scope_value"])
    qs = qs[:50]
    return JsonResponse([
        {
            "PlanRun_ID": r.id, "Scope_Type": r.scope_type, "Scope_Value": r.scope_value,
            "Plan_Date": r.plan_date, "Run_Timestamp": r.run_timestamp,
            "SE_Count": r.se_count, "DC_Count": r.dc_count, "Task_Count": r.task_count,
        }
        for r in qs
    ], safe=False, json_dumps_params={"default": str})
