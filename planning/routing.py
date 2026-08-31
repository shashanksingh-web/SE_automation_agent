"""
The Routing Agent -- sequences the SE Daily Task Agent's Ranked_Pool into route plans
per SE per day (Routing_Agent_Configuration_Sheet_v5_FINAL, v5, all 20 open items
resolved). Mirrors planning/pitching.py's shape: a pure orchestration/persistence layer
over se_daily_plan_agent's algorithmic functions (Models 1-3, see that file's "10a.
ROUTING AGENT" section), receiving already-fetched candidates/origin data from
planning/services.py rather than querying Redshift itself.

Position in the pipeline (confirmed, SE_DC_Data_Normalization_Agent_Prompt_1.docx):
Data Normalization Agent -> BO Scoring Engine (Ranked_Pool) -> THIS AGENT -> SE App /
Manager UI (plan selection) -> Pathik execution tables. The Pitching Agent and this
agent "share no output tables at all" (same doc) -- Pitching only ever sees the
Priority-Max plan's stops synced into DailyTask by planning/services.py, never a
RoutePlan/RouteStop row directly.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db.models import Q

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library

from .models import PlanRun, RouteDroppedDC, RoutePlan, RouteStop  # noqa: E402


def generate_route_plans_for_se(
    plan_run: PlanRun,
    se_id: str,
    se_email: Optional[str],
    plan_date: str,
    candidates: List[Dict[str, Any]],
    origin: Optional[Tuple[float, float]],
    origin_basis: str,
    constants: "agent.BusinessConstants",
    plan_choice: str = "A",
) -> Dict[str, Any]:
    """candidates: the exact shape generate_se_daily_plan()'s route_selector branch
    builds -- [{"row": DailyTaskRow, "dc": dict, "priority_score": float, "matched":
    [str]}, ...], UNFILTERED (every Ranked_Pool entry, no capacity trimming yet -- that's
    this function's job, via the model builders).

    origin: resolved by the caller (planning/services.py, per R0.4) -- None means no
    punch-in exists at all yet for this SE, in which case this function defers entirely
    (no RoutePlan rows persisted, empty Tasks returned) rather than guessing an
    Origin_Point, matching R0.4's own "waits for today's real punch-in instead" rule.

    plan_choice: "A" (default) runs the existing Models 1-3 (Priority-Max/Distance-Min/
    Balanced) exactly as before. "B" runs Plan B instead -- the Beat Planning / Cluster-
    Based Model (Beat_Planning_Routing_Agent_Cluster_Model.xlsx, confirmed 2026-08-28,
    see se_daily_plan_agent.build_route_cluster_based) -- ONE RoutePlan, not three,
    since Plan B is a single alternate strategy, not a family of 3 the way Plan A is.
    Chosen explicitly per call (see planning.services.make_routing_plan_asker) --
    never auto-selected, since the source doc itself states the exact Plan A -> Plan B
    trigger condition is "not yet confirmed."

    Returns {"Tasks": [DailyTaskRow...], "Sequencing_Basis": str, "Travel_Cap_Exceeded":
    bool, "exceptions": [{"source", "reason_code", "detail"}, ...]} -- the first three
    keys are exactly generate_se_daily_plan()'s route_selector contract; "exceptions" is
    a convenience the caller folds into its own run_exceptions list (same shape used
    throughout planning/services.py)."""
    exceptions: List[Dict[str, str]] = []
    who = se_email or se_id

    if origin is None:
        exceptions.append({
            "source": "RoutingAgent", "reason_code": "Origin_Point_Unresolved",
            "detail": f"{who} @ {plan_date}: no punch-in exists yet (R0.4) -- route generation deferred, no plan produced this run",
        })
        return {"Tasks": [], "Sequencing_Basis": "routing_agent_deferred_no_origin", "Travel_Cap_Exceeded": False, "exceptions": exceptions}

    # GR-R1/GR-R2/R0.7 -- independent pre-generation guardrail pass (same "second check
    # even though upstream should already handle it" philosophy as GR-14 elsewhere in
    # this pipeline). Guardrail-level, not model-level -- applies identically ahead of
    # all 3 models below, so these drops get attached to every RoutePlan this call
    # produces. R0.7's recency leg (Days_Since_Last_Visit < min) mirrors Legal_Hold below
    # -- apply_dc_exclusion_rules() upstream should already exclude too-recent DCs from
    # the ranked pool entirely, but this is the same redundant safety check, not a
    # substitute for it.
    pre_dropped: List[Dict[str, str]] = []
    filtered: List[Dict[str, Any]] = []
    for c in candidates:
        dc = c["dc"]
        if dc.get("Latitude") is None or dc.get("Longitude") is None:
            pre_dropped.append({"dc_id": dc["DC_ID"], "reason": "Geo_Incomplete"})
            continue
        if dc.get("DC_Status") == "Legal_Hold":
            pre_dropped.append({"dc_id": dc["DC_ID"], "reason": "Legal_Hold"})
            continue
        days_since = dc.get("Days_Since_Last_Visit")
        if days_since is not None and days_since < constants.min_days_since_last_visit:
            pre_dropped.append({"dc_id": dc["DC_ID"], "reason": "Visited_Too_Recently"})
            continue
        filtered.append(c)

    if plan_choice == "B":
        model_results = {RoutePlan.PlanType.CLUSTER_BASED: agent.build_route_cluster_based(filtered, origin, constants)}
        default_plan_type = RoutePlan.PlanType.CLUSTER_BASED
    else:
        model_results = {
            RoutePlan.PlanType.PRIORITY_MAX: agent.build_route_priority_max(filtered, origin, constants),
            RoutePlan.PlanType.DISTANCE_MIN: agent.build_route_distance_min(filtered, origin, constants),
            RoutePlan.PlanType.BALANCED: agent.build_route_balanced(filtered, origin, constants),
        }
        default_plan_type = RoutePlan.PlanType.PRIORITY_MAX

    # GR-R6 -- flag (don't silently pretend) when the 3 Plan A models converge on
    # materially the same stop set instead of genuinely offering 3 distinct choices --
    # expected and fine for a thin candidate pool, but worth surfacing, not hiding.
    # Doesn't apply to Plan B, which only ever produces one RoutePlan.
    if plan_choice != "B":
        stop_sets = {ptype: tuple(s["row"].DC_ID for s in r["stops"]) for ptype, r in model_results.items()}
        distinct_sets = {s for s in stop_sets.values() if s}
        if len(distinct_sets) < 3 and distinct_sets:
            exceptions.append({
                "source": "RoutingAgent", "reason_code": "Insufficient_Candidates_For_3_Plans",
                "detail": f"{who} @ {plan_date}: only {len(distinct_sets)} genuinely distinct stop set(s) across Models 1-3 (candidate pool too small/uniform for real variety)",
            })

    default_tasks: List[Any] = []
    default_basis = "routing_agent"
    default_cap_exceeded = False

    for plan_type, result in model_results.items():
        route_plan = RoutePlan.objects.create(
            plan_run=plan_run, se_id=se_id, se_name=se_email, plan_date=plan_date,
            plan_type=plan_type, origin_lat=origin[0], origin_lon=origin[1], origin_basis=origin_basis,
            total_distance_km=result["total_distance_km"], total_travel_minutes=result["total_travel_min"],
            total_visit_minutes=result["total_visit_min"],
            total_minutes=result["total_travel_min"] + result["total_visit_min"],
            priority_score_captured=result["priority_score_captured"],
            feasible=result["feasible"], infeasibility_reason=result.get("infeasibility_reason", ""),
            is_default_selected=(plan_type == default_plan_type),
            # GR-R10 audit trail (see RoutePlan.avg_speed_kmph_used/alpha_used docstring)
            # -- none of the 3 model builders above are called with an explicit
            # avg_speed_kmph override, so R3_2_DEFAULT_AVG_SPEED_KMPH is what every plan
            # actually used; alpha_used only exists in BALANCED's own result dict.
            avg_speed_kmph_used=agent.R3_2_DEFAULT_AVG_SPEED_KMPH,
            alpha_used=result.get("alpha_used"),
        )
        RouteStop.objects.bulk_create([
            RouteStop(
                route_plan=route_plan, dc_id=stop["row"].DC_ID, sequence_no=i,
                purposes=stop["row"].Purpose_Of_Visit, eta_minutes_from_origin=stop["eta_minutes"],
                distance_from_prev_km=stop["distance_from_prev_km"],
                travel_time_from_prev_min=stop["travel_time_from_prev_min"],
                visit_duration_min=stop["row"].Estimated_Duration,
            )
            for i, stop in enumerate(result["stops"], start=1)
        ])
        RouteDroppedDC.objects.bulk_create([
            RouteDroppedDC(route_plan=route_plan, dc_id=d["dc_id"], reason=d["reason"])
            for d in result["dropped"] + pre_dropped
        ])

        if result.get("infeasibility_reason"):
            exceptions.append({
                "source": "RoutingAgent", "reason_code": "Travel_Floor_Not_Met",
                "detail": f"{who} @ {plan_date} ({plan_type}): {result['infeasibility_reason']}",
            })

        if plan_type == default_plan_type:
            default_tasks = [s["row"] for s in result["stops"]]
            cumulative_km = 0.0
            for i, r in enumerate(default_tasks, start=1):
                cumulative_km += result["stops"][i - 1]["distance_from_prev_km"]
                r.Sr_No = i
                # DailyTaskRow.Distance_Km stays CUMULATIVE-from-origin -- same
                # convention se_daily_plan_agent.sequence_with_distance() already used,
                # for backward compatibility with reporting.py/the API's existing "Km"
                # column. RouteStop.distance_from_prev_km (above) keeps the real per-leg
                # figure for the new multi-plan schema.
                r.Distance_Km = round(cumulative_km, 2)
            default_basis = f"routing_agent_{'cluster_based' if plan_choice == 'B' else 'priority_max'}_{origin_basis}"
            default_cap_exceeded = not result["feasible"]

    return {"Tasks": default_tasks, "Sequencing_Basis": default_basis, "Travel_Cap_Exceeded": default_cap_exceeded, "exceptions": exceptions}


def resync_daily_tasks_from_selected_plan(plan_run: PlanRun, se_id: str) -> int:
    """Used by manage.py select_route_plan when an SE (via the ops CLI stand-in, see
    that command's docstring) picks a different one of the >=3 synced plans than the
    default. Deletes and recreates this SE's DailyTask rows from the newly-selected
    RoutePlan's stops, so Pitching Agent / reporting / the API don't need to know a
    selection ever happened. Returns the number of DailyTask rows created.

    Known limitation: RouteStop only carries R6.1's confirmed fields (DC_ID, sequence,
    purposes, timing) -- not the rich per-DC financial/reason context (Present_Outstanding,
    Reason_Of_Visit, YTD_Private_Label, etc.) that only exists transiently on the
    DailyTaskRow objects built during generation. Re-synced DailyTask rows after a
    plan switch will have those fields blank until a re-run of activate_tuff/
    generate_se_plan regenerates the full candidate set fresh. Not silently
    papered over -- worth knowing before relying on this for anything beyond
    confirming which DCs/order the SE will actually visit."""
    from .models import DailyTask

    selected = plan_run.route_plans.filter(se_id=se_id, is_default_selected=True).first()
    if selected is None:
        return 0

    DailyTask.objects.filter(plan_run=plan_run, se_id=se_id).delete()

    dc_by_id = {}
    for stop in selected.stops.order_by("sequence_no"):
        dc_by_id.setdefault(stop.dc_id, stop)

    created = 0
    cumulative_km = 0.0
    for stop in selected.stops.order_by("sequence_no"):
        cumulative_km += stop.distance_from_prev_km or 0.0
        DailyTask.objects.create(
            plan_run=plan_run, se_id=se_id, se_name=selected.se_name, plan_date=selected.plan_date,
            sr_no=stop.sequence_no, dc_name=None, dc_id=stop.dc_id, distance_km=round(cumulative_km, 2),
            recommended_task_type="DC Visit", purpose_of_visit=stop.purposes, reason_of_visit="",
            last_visit_date=None, days_since_last_visit=None, present_outstanding=None, present_overdue=None,
            last_order_date=None, last_order_value=None, last_payment_date=None,
            last_payment_join_key_unconfirmed=True, ytd_private_label=None, dc_club_participation="",
            objective="", no_new_orders=False, credit_on_hold=False, credit_on_hold_reason=None,
            estimated_duration=int(stop.visit_duration_min), priority_multiplier=1.0,
        )
        created += 1
    return created


class RoutingError(RuntimeError):
    """Raised for route lookup/selection failures a caller should see as a 4xx, not a
    500 -- no RoutePlans for the given SE/date, or a plan_type that doesn't exist for
    them. Mirrors planning.services.PlanningError's role for the SE Daily Task Agent."""


def _se_filter(se: str) -> Q:
    """--se / se accepts either an SE_ID or an SE email -- matches whichever
    RoutePlan.se_id/se_name actually holds it, same dual-lookup convenience the rest of
    this app's SE-facing commands/endpoints already use."""
    return Q(se_id=se) | Q(se_name=se) | Q(se_name__iexact=se)


def resolve_route_plan_run(se: str, plan_date: str, plan_run_id: Optional[int] = None) -> PlanRun:
    """Disambiguates which PlanRun's RoutePlans to use for se/plan_date -- an explicit
    plan_run_id if given, else the most recent PlanRun that actually has RoutePlans for
    this SE/day. Shared by `manage.py select_route_plan` and the routing endpoints."""
    if plan_run_id is not None:
        try:
            return PlanRun.objects.get(id=plan_run_id)
        except PlanRun.DoesNotExist:
            raise RoutingError(f"PlanRun #{plan_run_id} not found.")
    candidate = (
        RoutePlan.objects.filter(plan_date=plan_date).filter(_se_filter(se))
        .order_by("-plan_run__run_timestamp").first()
    )
    if candidate is None:
        raise RoutingError(f"No RoutePlans found for se={se!r} on {plan_date} -- run activate_tuff/generate_se_plan first.")
    return candidate.plan_run


def list_route_plans(se: str, plan_date: str, plan_run_id: Optional[int] = None) -> Dict[str, Any]:
    """Returns {"plan_run_id", "se_id", "plans": [...]} -- R5.2's presentation fields for
    each of the SE's synced RoutePlans (>=3 per R5.1), each with its stops and dropped
    DCs. Used by both `select_route_plan` (no --select) and
    GET /api/planning/routes/<se>/<plan_date>/."""
    plan_run = resolve_route_plan_run(se, plan_date, plan_run_id)
    routes = RoutePlan.objects.filter(plan_run=plan_run, plan_date=plan_date).filter(_se_filter(se))
    if not routes.exists():
        raise RoutingError(f"PlanRun #{plan_run.id} has no RoutePlans for se={se!r} on {plan_date}.")

    plans = []
    for r in routes.order_by("plan_type"):
        plans.append({
            "plan_type": r.plan_type,
            "is_default_selected": r.is_default_selected,
            "stop_count": r.stops.count(),
            "total_distance_km": r.total_distance_km,
            "total_travel_minutes": r.total_travel_minutes,
            "total_visit_minutes": r.total_visit_minutes,
            "total_minutes": r.total_minutes,
            "priority_score_captured": r.priority_score_captured,
            # GR-R10 audit trail -- the ops assumptions behind the numbers above.
            "avg_speed_kmph_used": r.avg_speed_kmph_used,
            "alpha_used": r.alpha_used,
            "feasible": r.feasible,
            "infeasibility_reason": r.infeasibility_reason or None,
            # R0.4's Origin_Point -- where/why this route starts where it does. See
            # RoutePlan.ORIGIN_BASIS_CHOICES for what each origin_basis value means.
            "origin_lat": r.origin_lat, "origin_lon": r.origin_lon, "origin_basis": r.origin_basis,
            "generated_at": r.generated_at,
            "stops": [
                {
                    "sequence_no": s.sequence_no, "dc_id": s.dc_id, "purposes": s.purposes,
                    "distance_from_prev_km": s.distance_from_prev_km,
                    "travel_time_from_prev_min": s.travel_time_from_prev_min,
                }
                for s in r.stops.order_by("sequence_no")
            ],
            "dropped_dcs": [{"dc_id": d.dc_id, "reason": d.reason} for d in r.dropped_dcs.all()],
        })
    first = routes.first()
    return {"plan_run_id": plan_run.id, "se_id": first.se_id, "se_name": first.se_name, "plan_date": plan_date, "plans": plans}


def select_default_route_plan(se: str, plan_date: str, plan_type: str, plan_run_id: Optional[int] = None) -> Dict[str, Any]:
    """Flips is_default_selected to plan_type and re-syncs DailyTask rows from its stops
    -- the trust-equivalent of R5.3's "the SE selects the final plan" (no SE-facing
    mobile app exists in this repo yet, see select_route_plan's docstring). Used by both
    `manage.py select_route_plan --select` and
    GET /api/planning/routes/<se>/<plan_date>/select/<plan_type>/."""
    plan_run = resolve_route_plan_run(se, plan_date, plan_run_id)
    routes = RoutePlan.objects.filter(plan_run=plan_run, plan_date=plan_date).filter(_se_filter(se))
    if not routes.exists():
        raise RoutingError(f"PlanRun #{plan_run.id} has no RoutePlans for se={se!r} on {plan_date}.")

    target = routes.filter(plan_type=plan_type).first()
    if target is None:
        raise RoutingError(f"No {plan_type} plan exists for se={se!r} on {plan_date} in PlanRun #{plan_run.id}.")

    se_id = routes.first().se_id
    routes.update(is_default_selected=False)
    target.is_default_selected = True
    target.save(update_fields=["is_default_selected"])
    created = resync_daily_tasks_from_selected_plan(plan_run, se_id)
    return {"plan_run_id": plan_run.id, "se_id": se_id, "selected": plan_type, "daily_tasks_resynced": created}
