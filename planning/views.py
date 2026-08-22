from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .directory import list_abms, list_blocks, list_dcs, list_districts, list_nodes, list_rbms, list_ses, list_states, list_zbms
from .headcount import compute_active_headcount_bifurcation
from .models import DailyTask, DCCard, DCVisitStreak, ObjectiveCompletionStats, PitchScript, PlanRun, ScheduledScope
from .product_cohort import ProductCohortError, build_season_weeks, split_csv
from .routing import RoutingError, list_route_plans, select_default_route_plan
from .services import PlanningError, activate_tuff_scope, generate_plan_for_scope, run_normalization_step
from .services import _output_dir as _planning_output_dir


def _focus_product_kwargs_from_get(request) -> dict:
    """Shared by _generate_and_respond/tuff -- lets any scope endpoint accept the same
    ?focus_product=<materialId>&focus_node=...&... query params activate_tuff/
    generate_se_plan's CLI flags do, so the API isn't a second, drifting implementation
    of the same optional Focus Product Campaign Targeting wiring (see
    planning.services.generate_plan_for_scope's focus_product_* docstring)."""
    material_id = request.GET.get("focus_product")
    if not material_id:
        return {}
    season_weeks = build_season_weeks(
        request.GET.get("focus_product_outer_weeks", "1-52"), request.GET.get("focus_product_buildup_weeks"),
        int(request.GET["focus_product_peak_week"]) if request.GET.get("focus_product_peak_week") else None,
        request.GET.get("focus_product_closure_weeks"),
    )
    return {
        "focus_product_material_id": material_id,
        "focus_product_node_id": request.GET.get("focus_node"),
        "focus_product_years": int(request.GET.get("focus_product_years", 4)),
        "focus_product_season_weeks": season_weeks,
        "focus_product_crop_districts": split_csv(request.GET.get("focus_product_crop_districts")),
        "focus_product_related_products": split_csv(request.GET.get("focus_product_related_products")),
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
        # Structured form of DC_Club_Participation's prose summary (se_daily_plan_agent.
        # normalize_dc_club(), confirmed 2026-08-19) -- null when club data wasn't
        # available this run, same gate as DC_Club_Participation's own
        # "Config_Ambiguous" case. Club_Tier/Zone/TOD_Percent/Reward describe current
        # standing (all null if not yet tiered); the Eligible_Tier_* trio describes what
        # clearing outstanding would unlock (all null once already tiered, or if
        # Qualifying_Turnover doesn't clear even Copper's entry threshold).
        "Club_Detail": {
            "Is_Club_Enrolled": t.club_detail.get("Is_Club_Enrolled"),
            "Qualifying_Turnover": t.club_detail.get("Qualifying_Turnover"),
            "Outstanding_Cleared": t.club_detail.get("Outstanding_Cleared"),
            "Club_Tier": t.club_detail.get("Club_Tier"),
            "Zone": t.club_detail.get("Zone"),
            "TOD_Percent": t.club_detail.get("TOD_Percent"),
            "Reward": t.club_detail.get("Reward"),
            "Eligible_Tier_If_Outstanding_Cleared": t.club_detail.get("Eligible_Tier_If_Outstanding_Cleared"),
            "Eligible_Tier_TOD_Percent_If_Cleared": t.club_detail.get("Eligible_Tier_TOD_Percent_If_Cleared"),
            "Eligible_Tier_Reward_If_Cleared": t.club_detail.get("Eligible_Tier_Reward_If_Cleared"),
        } if t.club_detail else None,
        "Critical": t.critical, "Critical_Reasons": t.critical_reasons,
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
        # Empty unless this run was given ?focus_product=... -- see
        # _focus_product_kwargs_from_get, product-first not DC-first, opt-in per call.
        "Focus_Product_Targets": [
            {
                "ID": f.id, "Material_ID": f.material_id, "Node_ID": f.node_id,
                "Step_2A": f.step_2a, "Step_2B": f.step_2b, "Step_3": f.step_3, "Generated_At": f.generated_at,
            }
            for f in plan_run.focus_product_targets.all()
        ],
    }


@require_GET
def _generate_and_respond(request, scope_type: str, scope_value: str):
    plan_date = request.GET.get("date")
    try:
        focus_product_kwargs = _focus_product_kwargs_from_get(request)
    except ProductCohortError as e:
        return JsonResponse({"error": str(e)}, status=422)
    try:
        plan_run = generate_plan_for_scope(scope_type, scope_value, plan_date, **focus_product_kwargs)
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
        focus_product_kwargs = _focus_product_kwargs_from_get(request)
    except ProductCohortError as e:
        return JsonResponse({"error": str(e)}, status=422)
    try:
        plan_run, normalization_info = activate_tuff_scope(
            scope_type, scope_value, plan_date,
            force_normalization=force_normalization, skip_normalization=skip_normalization,
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
        "Private_Label_Section": card.private_label_section,
        "Card_Hindi": card.card_hindi,
        # Same recommended-products list as PitchScript's own field of the same name -
        # already folded into Private_Label_Section's prose too.
        "Recommended_Products": _serialize_recommended_products(card.recommended_products),
        # Structured form of Who_Section's "Business Area Strength" bullet - null when
        # this DC has no current-year business-area data at all. Prior is independently
        # nullable even when Current isn't (prior-year comparison can genuinely be
        # unavailable for a DC with real current-year activity).
        "Business_Area_Detail": _serialize_business_area_detail(card.business_area_detail),
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
def plan_run_list(request):
    """GET /api/planning/runs/?scope_type=NODE&scope_value=Jaipur&status=PENDING_REVIEW&limit=&offset=
    -- list past runs, newest first. limit defaults to 50, capped at 500; see
    X-Total-Count on the response to tell a full result from a truncated one."""
    qs = PlanRun.objects.all()
    if request.GET.get("scope_type"):
        qs = qs.filter(scope_type=request.GET["scope_type"].upper())
    if request.GET.get("scope_value"):
        qs = qs.filter(scope_value=request.GET["scope_value"])
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"].upper())
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
