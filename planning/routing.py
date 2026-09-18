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
from datetime import date as _date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library

from .data_cache import load_output_json  # noqa: E402
from .models import BeatZoneAssignment, PlanRun, RouteDroppedDC, RoutePlan, RouteStop  # noqa: E402


def _parse_plan_date(plan_date) -> _date:
    return plan_date if isinstance(plan_date, _date) else datetime.strptime(plan_date, "%Y-%m-%d").date()


# Beat_Planning_Routing_Agent_Cluster_Model.xlsx, Sheet 11 "Route Distinction / Beat
# Cycle Rule", Model A (Repeat-Avoidance): the doc's own recommended cool-down window
# when layered under Model B (Fixed Rotation, see _get_or_assign_zones below). Plan-B-
# scoped only (Sheet 11 lives in the Beat Planning workbook, not the Plan A config
# sheet) -- confirmed with the user 2026-08-31.
PLAN_B_COOLDOWN_DAYS = 2


def _cooling_down_dc_ids(se_id: str, plan_date: str, window_days: int = PLAN_B_COOLDOWN_DAYS) -> set:
    """DC_IDs this SE actually visited (synced to their default-selected RoutePlan) on
    any of the window_days calendar days immediately before plan_date, across any
    plan_type -- starvation risk is about real visit history, not which model picked it.
    No new schema needed: RouteStop/RoutePlan already carry everything this needs."""
    plan_date_obj = _parse_plan_date(plan_date)
    window_start = plan_date_obj - timedelta(days=window_days)
    return set(
        RouteStop.objects.filter(
            route_plan__se_id=se_id, route_plan__is_default_selected=True,
            route_plan__plan_date__gte=window_start, route_plan__plan_date__lt=plan_date_obj,
        ).values_list("dc_id", flat=True)
    )


def _get_or_assign_zones(se_id: str, candidates: List[Dict[str, Any]], plan_date) -> Dict[str, int]:
    """Sheet 11 Model B (Fixed Rotation): {dc_id: zone_index} for this SE, geography-only
    (no priority_score involved, per the sheet's own "drawn up front from geography/
    density... not from daily scores"). Bootstraps a fresh assignment the first time this
    SE needs one, reusing the exact same density-clustering Stage 1 already uses
    (se_daily_plan_agent._cluster_candidates_by_density, which never actually touches
    priority_score internally -- confirmed by reading it, not assumed) -- anchor_date is
    set to plan_date (this call's own day = cycle day 0), then persisted. After that,
    EXISTING assignments are never reclustered or reshuffled (that would break the whole
    point of a predictable cadence) -- only a genuinely new dc_id gets added, to whichever
    existing zone's centroid it's nearest to (using each zone's stored lat/lon, not
    today's live candidate coordinates, so this works even on a day most of that zone's
    other members aren't in the current Ranked_Pool at all)."""
    with_coords = [c for c in candidates if c["dc"].get("Latitude") is not None and c["dc"].get("Longitude") is not None]
    if not with_coords:
        return {}

    existing = list(BeatZoneAssignment.objects.filter(se_id=se_id))
    if not existing:
        plan_date_obj = _parse_plan_date(plan_date)
        clusters = agent._cluster_candidates_by_density(with_coords)
        rows = [
            BeatZoneAssignment(
                se_id=se_id, dc_id=c["dc"]["DC_ID"], zone_index=zi, num_zones=len(clusters),
                anchor_date=plan_date_obj, latitude=c["dc"]["Latitude"], longitude=c["dc"]["Longitude"],
            )
            for zi, cluster in enumerate(clusters) for c in cluster
        ]
        BeatZoneAssignment.objects.bulk_create(rows, ignore_conflicts=True)
        # Re-read rather than trust `rows`: if a genuinely concurrent call bootstrapped
        # this se_id first, ignore_conflicts=True silently drops OUR rows, and `rows`
        # would then describe a clustering that was never actually persisted -- always
        # return what the DB actually has, so this call's own filtering can't disagree
        # with what a concurrent call already committed.
        return {e.dc_id: e.zone_index for e in BeatZoneAssignment.objects.filter(se_id=se_id)}

    zone_by_dc = {e.dc_id: e.zone_index for e in existing}
    unzoned = [c for c in with_coords if c["dc"]["DC_ID"] not in zone_by_dc]
    if unzoned:
        # Nearest-centroid assignment: mean lat/lon of each existing zone's members
        # (already-persisted coordinates), not a fresh reclustering.
        zone_points: Dict[int, List[Tuple[float, float]]] = {}
        for e in existing:
            zone_points.setdefault(e.zone_index, []).append((e.latitude, e.longitude))
        zone_centroids = {
            zi: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            for zi, pts in zone_points.items()
        }
        new_rows = []
        for c in unzoned:
            lat, lon = c["dc"]["Latitude"], c["dc"]["Longitude"]
            nearest_zone = min(
                zone_centroids,
                key=lambda zi: agent.circuity_distance_km(lat, lon, *zone_centroids[zi]) or 1e18,
            )
            zone_by_dc[c["dc"]["DC_ID"]] = nearest_zone
            new_rows.append(BeatZoneAssignment(
                se_id=se_id, dc_id=c["dc"]["DC_ID"], zone_index=nearest_zone,
                num_zones=existing[0].num_zones, anchor_date=existing[0].anchor_date,
                latitude=lat, longitude=lon,
            ))
        BeatZoneAssignment.objects.bulk_create(new_rows, ignore_conflicts=True)
        # Same re-read-don't-trust-local-state reasoning as the bootstrap branch above --
        # a concurrent call could have already zoned one of these dc_ids differently.
        return {e.dc_id: e.zone_index for e in BeatZoneAssignment.objects.filter(se_id=se_id)}

    return zone_by_dc


def _today_zone_index(se_id: str, plan_date) -> Optional[Tuple[int, int]]:
    """Returns (zone_index_today, num_zones) for this SE, or None if no zone assignment
    exists yet (caller is responsible for bootstrapping via _get_or_assign_zones first)."""
    row = BeatZoneAssignment.objects.filter(se_id=se_id).first()
    if row is None:
        return None
    plan_date_obj = _parse_plan_date(plan_date)
    days_since_anchor = (plan_date_obj - row.anchor_date).days
    return days_since_anchor % row.num_zones, row.num_zones


def _dc_display_name(candidate: Dict[str, Any]) -> str:
    return str(candidate["dc"].get("DC_Name") or candidate["dc"]["DC_ID"])


def _enforce_distinct_routes(
    model_results: Dict[str, Dict[str, Any]], filtered: List[Dict[str, Any]], origin: Tuple[float, float],
) -> List[Tuple[str, str]]:
    """(notes, unresolved) -- notes: [(plan_type, plain-language note)] per route this
    changed; unresolved: plan_types left duplicate because the whole ladder failed.

    Guarantee (added 2026-09-18, explicit user request -- "there always must be
    distinct route plan like plan a/b/c") that no two of a family's 3 routes are the
    same route. Runs on every family (Plan A Models 1-3, Plan B's 3 routes, Plan C's 3
    LLM calls) right after the builders, BEFORE the Google Directions overlay / ROI
    attach so a changed route gets those computed for what's actually kept.

    The builders already TRY to differ (exclude_stop_sets + _distinctness_swap), but
    that swap only works when a spare eligible DC exists and its swapped route fits the
    caps; otherwise the duplicate was kept and merely logged (Insufficient_Candidates_
    For_3_Plans) -- confirmed live 2026-09-18 (sushil.ojha, 2 eligible candidates, all
    3 Plan C routes identical). Routes are compared as ORDERED DC_ID tuples, the same
    definition the GR-R10 check below uses: a different visit order of the same stops is
    a different drive and counts as distinct. Route 1 (each family's default) is always
    kept as built; a later route that repeats an earlier one is changed by the first
    rung of this ladder that yields an unseen, within-caps route:
      1. swap its lowest-priority stop for the best excluded eligible DC (rank order),
      2. reverse the visit order,
      3. rotate the loop (start from a different stop), both directions,
      4. drop the lowest-priority stop (repeatedly) -- a shorter, different route.
    Only a pool too thin for any of that (a single eligible DC: reversing a 1-stop loop
    is the same loop, dropping it is no route) leaves the duplicate in place, and the
    honest GR-R7 note below still says so. Every change is reported back as a plain-
    language, DC-NAME (never DC_ID) note per plan_type for the exceptions list / the
    route card. Mutates the affected result dicts in place (stops/totals/dropped)."""
    by_id = {c["dc"]["DC_ID"]: c for c in filtered}
    speed = agent.R3_2_DEFAULT_AVG_SPEED_KMPH
    seen: List[Tuple[str, ...]] = []
    notes: List[Tuple[str, str]] = []
    unresolved: List[str] = []

    def _try(order: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not order or tuple(c["dc"]["DC_ID"] for c in order) in seen:
            return None
        metrics = agent._route_metrics(order, origin, speed)
        return metrics if agent._within_caps(metrics) else None

    for plan_type, result in model_results.items():
        seq = tuple(s["row"].DC_ID for s in result["stops"])
        if not seq or seq not in seen or any(dc_id not in by_id for dc_id in seq):
            seen.append(seq)
            continue
        order = [by_id[dc_id] for dc_id in seq]
        lowest = min(order, key=lambda c: c["priority_score"])
        low_idx = order.index(lowest)
        others = [c for c in order if c is not lowest]
        chosen: Optional[Tuple[Dict[str, Any], str, List[Dict[str, Any]], Optional[Dict[str, Any]], str]] = None

        # 1. Swap in a different DC -- same spare-candidate idea as _distinctness_swap,
        #    but re-verified against caps right here.
        excluded = sorted((c for c in filtered if c["dc"]["DC_ID"] not in seq), key=lambda c: -c["priority_score"])
        for alt in excluded:
            for trial in (others[:low_idx] + [alt] + others[low_idx:], others + [alt]):
                metrics = _try(trial)
                if metrics:
                    chosen = (
                        metrics,
                        f"{_dc_display_name(alt)} was put in place of {_dc_display_name(lowest)} so this route "
                        "differs from the other route options.",
                        [lowest], alt, "Route_Diversity_Swap",
                    )
                    break
            if chosen:
                break
        # 2. Reverse the loop.
        if chosen is None and len(order) >= 2:
            metrics = _try(order[::-1])
            if metrics:
                chosen = (metrics, "The visit order was reversed so this route differs from the other route options.", [], None, "")
        # 3. Rotate the loop -- start from a different shop.
        if chosen is None and len(order) >= 3:
            for i in range(1, len(order)):
                rotated = order[i:] + order[:i]
                for trial in (rotated, rotated[::-1]):
                    metrics = _try(trial)
                    if metrics:
                        chosen = (
                            metrics,
                            f"The route starts from {_dc_display_name(trial[0])} instead so it differs from the "
                            "other route options.", [], None, "",
                        )
                        break
                if chosen:
                    break
        # 4. Drop the lowest-priority stop(s) -- a shorter, different route.
        if chosen is None:
            trial, removed = list(order), []
            while trial and chosen is None:
                low = min(trial, key=lambda c: c["priority_score"])
                trial = [c for c in trial if c is not low]
                removed.append(low)
                metrics = _try(trial)
                if metrics:
                    names = ", ".join(_dc_display_name(c) for c in removed)
                    chosen = (
                        metrics,
                        f"{names} {'was' if len(removed) == 1 else 'were'} left out so this route differs from the "
                        "other route options.", removed, None, "Route_Diversity_Trim",
                    )
        if chosen is None:
            seen.append(seq)  # genuinely impossible -- the GR-R7 note downstream reports it
            unresolved.append(plan_type)
            continue

        metrics, note, removed, swapped_in, drop_reason = chosen
        result["stops"] = metrics["stops"]
        for key in ("total_distance_km", "total_travel_min", "total_visit_min", "priority_score_captured"):
            result[key] = metrics[key]
        dropped = list(result.get("dropped", []))
        if swapped_in is not None:
            dropped = [d for d in dropped if d["dc_id"] != swapped_in["dc"]["DC_ID"]]
        dropped += [{"dc_id": c["dc"]["DC_ID"], "reason": drop_reason} for c in removed]
        result["dropped"] = dropped
        if not result.get("feasible", True):
            # The replacement passed _within_caps, so an earlier real infeasibility no
            # longer applies (Plan B's informational Exceptional-DC note has feasible=True
            # and is left untouched).
            result["feasible"] = True
            result["infeasibility_reason"] = ""
        if result.get("llm_reasoning"):
            # Plan C: keep the SE-facing reasoning consistent with the route it now
            # describes (same plain-language reader-note convention as
            # build_route_llm_reasoned's own trim/swap notes), ahead of any audit bracket.
            text = result["llm_reasoning"]
            head, sep, tail = text.partition(" [Audit:")
            result["llm_reasoning"] = head.rstrip() + " " + note + sep + tail
        seen.append(tuple(s["row"].DC_ID for s in result["stops"]))
        notes.append((plan_type, note))
    return notes, unresolved


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
    enable_rotation: bool = False,
    routing_overrides: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
    dc_district_lookup: Optional[Dict[str, str]] = None,
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
    see se_daily_plan_agent.build_route_cluster_based) -- THREE RoutePlans (2026-08-31
    fix: the workbook's own Sheet 7 "3-Route Comparison Summary" runs Plan B's same
    selection logic 3 times, once per ranking criterion -- Efficiency-Balanced/
    Score-Maximizing/Distance-Minimizing -- mirroring Plan A's Models 1-3, per R5.1's
    "minimum 3 per SE/day"; a prior version of this docstring wrongly claimed Plan B
    only ever produces one RoutePlan). Chosen explicitly per call (see
    planning.services.make_routing_plan_asker) -- never auto-selected, since the source
    doc itself states the exact Plan A -> Plan B trigger condition is "not yet confirmed."

    enable_rotation: opt-in, Plan B only (Beat_Planning_Routing_Agent_Cluster_Model.xlsx
    Sheet 11 Model B, "Fixed Rotation") -- False (default) leaves today's behavior
    unchanged. When True, this SE's candidates are first restricted to whichever
    persisted beat-zone (see _get_or_assign_zones/_today_zone_index) is "on" for
    plan_date, before any of the usual ranking/budget logic runs inside that zone. Off by
    default since it's a real behavior change (a DC outside today's zone is invisible
    this cycle even if it would otherwise rank #1) that should only ever be explicitly
    requested, same posture as plan_choice itself.

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

    # Per-scope Routing ceiling resolution (added 2026-09-11, explicit user request --
    # "in routing parameter rule may be different for node, district, state or
    # overall"). This SE's own Node/State come off its first DC candidate (every
    # candidate for one SE shares the same Node/State -- see load_dc_master()'s own
    # DC_Master row shape) -- free, no new query. District isn't itself a DC_Master
    # field, so it's joined via dc_district_lookup (dc_id -> district, built once by the
    # caller from Geo_Mapping_Normalized.json -- see planning.services). Monkey-patches
    # se_daily_plan_agent's module attributes directly, same mechanism (and same accepted
    # process-global-mutation caveat) planning.admin_config already uses for the
    # network-wide case -- every model builder below reads these as bare module globals,
    # so this is the only way to make them vary per-SE without threading a new parameter
    # through the ~25 call sites inside se_daily_plan_agent.py that read them today. Left
    # in place after this call rather than restored -- harmless, since the next
    # generate_plan_for_scope call re-resets them via load_business_constants() before
    # any SE is processed.
    if routing_overrides is not None and candidates:
        first_dc = candidates[0]["dc"]
        district = (dc_district_lookup or {}).get(first_dc.get("DC_ID"))
        ceilings = agent.resolve_routing_ceilings(
            first_dc.get("Node"), first_dc.get("State"),
            routing_overrides.get("node", {}), routing_overrides.get("state", {}),
            district, routing_overrides.get("district", {}),
        )
        for attr, value in ceilings.items():
            setattr(agent, attr, value)

    # GR-R1/GR-R2/R0.7 -- independent pre-generation guardrail pass (same "second check
    # even though upstream should already handle it" philosophy as GR-14 elsewhere in
    # this pipeline). Guardrail-level, not model-level -- applies identically ahead of
    # all 3 models below, so these drops get attached to every RoutePlan this call
    # produces. R0.7's recency leg (Days_Since_Last_Visit < min) mirrors Legal_Hold below
    # -- apply_dc_exclusion_rules() upstream should already exclude too-recent DCs from
    # the ranked pool entirely, but this is the same redundant safety check, not a
    # substitute for it.
    #
    # Sheet 11 Model A (Repeat-Avoidance, Plan B only): a DC visited on this SE's actual
    # selected route in the last PLAN_B_COOLDOWN_DAYS days is excluded from ranking this
    # cycle too, converting the previously-only-soft recency-decay nudge (Section 2) into
    # a real guarantee that the same DCs don't get picked every single cycle, starving
    # everything else assigned to this SE. cooled_out is kept separate from pre_dropped
    # so the escape hatch below can still reach into it.
    cooling_down_dc_ids = _cooling_down_dc_ids(se_id, plan_date) if plan_choice == "B" else set()
    # Sheet 11 Model B (Fixed Rotation, Plan B only, opt-in via enable_rotation):
    # bootstraps/extends this SE's persisted zone assignment, then restricts candidates
    # to whichever zone is "on" today -- structural, not reactive (no escape hatch here,
    # unlike Model A's; an occasionally-thin zone-day is the documented trade-off for a
    # real coverage guarantee, not a bug to route around).
    today_zone = None
    zone_by_dc: Dict[str, int] = {}
    if enable_rotation and plan_choice == "B":
        zone_by_dc = _get_or_assign_zones(se_id, candidates, plan_date)
        zone_info = _today_zone_index(se_id, plan_date)
        today_zone = zone_info[0] if zone_info else None
    pre_dropped: List[Dict[str, str]] = []
    filtered: List[Dict[str, Any]] = []
    cooled_out: List[Dict[str, Any]] = []
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
        if today_zone is not None and zone_by_dc.get(dc["DC_ID"]) != today_zone:
            pre_dropped.append({"dc_id": dc["DC_ID"], "reason": "Rotation_Zone_Not_Today"})
            continue
        if dc["DC_ID"] in cooling_down_dc_ids:
            cooled_out.append(c)
            continue
        filtered.append(c)

    # Escape hatch (Sheet 11 Model A's own wording): if the cool-down would leave the
    # day's route empty, waive it for the single highest-scoring candidate it excluded
    # rather than silently producing nothing.
    if not filtered and cooled_out:
        rescued = max(cooled_out, key=lambda c: c["priority_score"])
        filtered.append(rescued)
        cooled_out.remove(rescued)
    pre_dropped += [{"dc_id": c["dc"]["DC_ID"], "reason": "Cooling_Down_Repeat_Avoidance"} for c in cooled_out]

    # Candidate-pool-wide Google Maps accuracy (added 2026-09-11, explicit user request
    # -- "distance is also the parameter [for] DC selection", not just the final reported
    # number). Primed ONCE per SE with the origin + every filtered candidate's coords --
    # every real-DC-pair distance comparison inside the model builders below (Plan A
    # Models 2/3's construction+2-opt/Or-opt via _route_metrics, and Plan B's clustering/
    # Stage 3) transparently prefers this real matrix over Haversine x 1.4 wherever it's
    # been primed; falls back per-pair on any miss. No-op when GOOGLE_API_KEY isn't
    # configured. Cleared at the end of this function so a stale matrix never leaks into
    # the next SE's candidate pool.
    agent.prime_google_distance_matrix([origin] + [(c["dc"].get("Latitude"), c["dc"].get("Longitude")) for c in filtered])

    if plan_choice == "B":
        # 3 routes, same greedy budget-constrained selection, ranked by a different
        # criterion each time -- Sheet 7's "3-Route Comparison Summary" (Route 1
        # Efficiency-Balanced default, Route 2 Score-Maximizing, Route 3
        # Distance-Minimizing), mirroring Plan A's Models 1-3 below.
        #
        # Built sequentially, not independently (2026-09-01, explicit user request:
        # "force 3 different routes even if 2 are worse") -- each later route is told
        # every earlier route's own stop-set via exclude_stop_sets, so a dominant
        # Exceptional-DC cluster (which previously made all 3 collapse to the identical
        # BO Rule route regardless of ranking_criterion, since Steps 2-4 never ran) now
        # surfaces the next-best genuinely different alternative instead. Route 1
        # (efficiency, the default) is computed first, unconstrained -- its own pick is
        # never sacrificed for the sake of Route 2/3's distinctness.
        exclude_stop_sets: List[Tuple[str, ...]] = []

        def _build_plan_b_route(ranking_criterion: str) -> Dict[str, Any]:
            result = agent.build_route_cluster_based(
                filtered, origin, constants, ranking_criterion=ranking_criterion,
                exclude_stop_sets=list(exclude_stop_sets),
            )
            exclude_stop_sets.append(tuple(s["row"].DC_ID for s in result["stops"]))
            return result

        model_results = {
            RoutePlan.PlanType.CLUSTER_BASED: _build_plan_b_route("efficiency"),
            RoutePlan.PlanType.CLUSTER_SCOREMAX: _build_plan_b_route("score_max"),
            RoutePlan.PlanType.CLUSTER_DISTMIN: _build_plan_b_route("distance_min"),
        }
        default_plan_type = RoutePlan.PlanType.CLUSTER_BASED
    elif plan_choice == "C":
        # Plan C (added 2026-09-11, explicit user request -- "create the separate system
        # where system use anthropic api to create the route not the system logic with
        # reason why these route suggested"). CHANGED 2026-09-15, explicit user request
        # ("in plan c provide all routes") -- 3 RoutePlans now, same sequential
        # exclude_stop_sets-forced-distinctness pattern as Plan B above (and Plan A
        # below): route 1 (the admin's own configured PLAN_C_DECISION_STYLE, unconstrained
        # -- its pick is never sacrificed for 2/3's distinctness) is computed first, then
        # routes 2/3 each get an explicit, different objective (value_focused/
        # distance_focused) AND every earlier route's stop-set to avoid, rather than
        # asking the same LLM the same question 3 times and hoping for genuinely
        # different answers. Triples Plan C's real per-call LLM API cost -- an explicitly
        # accepted tradeoff, see build_route_llm_reasoned's own docstring.
        exclude_stop_sets_c: List[Tuple[str, ...]] = []

        def _build_plan_c_route(decision_style_override: Optional[str] = None) -> Dict[str, Any]:
            result = agent.build_route_llm_reasoned(
                filtered, origin, constants, exclude_stop_sets=list(exclude_stop_sets_c),
                decision_style_override=decision_style_override,
            )
            exclude_stop_sets_c.append(tuple(s["row"].DC_ID for s in result["stops"]))
            return result

        model_results = {
            RoutePlan.PlanType.LLM_REASONED: _build_plan_c_route(),
            RoutePlan.PlanType.LLM_REASONED_VALUE_MAX: _build_plan_c_route("value_focused"),
            RoutePlan.PlanType.LLM_REASONED_DISTMIN: _build_plan_c_route("distance_focused"),
        }
        default_plan_type = RoutePlan.PlanType.LLM_REASONED
    else:
        # Built sequentially, not independently (2026-09-07, explicit user request --
        # extends Plan B's own 2026-09-01 "force 3 different routes even if 2 are
        # worse" fix to Plan A's Models 1-3, which previously only detected convergence
        # after the fact via GR-R10/Plans_Converged below rather than avoiding it).
        # Same exclude_stop_sets threading as Plan B above: Model 1 (Priority-Max, the
        # default) is computed first, unconstrained -- its own pick is never sacrificed
        # for Model 2/3's distinctness.
        exclude_stop_sets_a: List[Tuple[str, ...]] = []

        def _build_plan_a_route(builder) -> Dict[str, Any]:
            result = builder(filtered, origin, constants, exclude_stop_sets=list(exclude_stop_sets_a))
            exclude_stop_sets_a.append(tuple(s["row"].DC_ID for s in result["stops"]))
            return result

        model_results = {
            RoutePlan.PlanType.PRIORITY_MAX: _build_plan_a_route(agent.build_route_priority_max),
            RoutePlan.PlanType.DISTANCE_MIN: _build_plan_a_route(agent.build_route_distance_min),
            RoutePlan.PlanType.BALANCED: _build_plan_a_route(agent.build_route_balanced),
        }
        default_plan_type = RoutePlan.PlanType.PRIORITY_MAX

    # Family-wide distinctness guarantee -- see _enforce_distinct_routes. Before the
    # Google overlay / ROI attach below so a changed route gets those for what's kept.
    diversity_notes, diversity_unresolved = _enforce_distinct_routes(model_results, filtered, origin)
    for plan_type, note in diversity_notes:
        exceptions.append({
            "source": "RoutingAgent", "reason_code": "Route_Diversity_Enforced",
            "detail": f"{who} @ {plan_date} ({plan_type}): {note}",
        })

    # Google Maps route-accuracy overlay (added 2026-09-10, explicit user request).
    # Distinct from the candidate-pool priming above: that one feeds real distances into
    # SELECTION (which stops/order win); this one re-fetches the real Directions-API
    # sequence for the route actually chosen, since a full point-to-point matrix leg and
    # an in-order multi-stop Directions leg aren't always byte-identical (turn
    # restrictions specific to arrival direction, etc.) -- this is the more precise
    # number for what's actually reported. Mutates each result in place; a no-op (falls
    # back to "haversine_x1.4") whenever GOOGLE_API_KEY isn't configured or the live call
    # fails, so this never blocks plan generation. Applied BEFORE the stop_sets/GR-R10
    # convergence check below since that only reads DC_ID tuples, never distance/time --
    # unaffected either way.
    for result in model_results.values():
        agent.apply_google_route_accuracy(result, origin)
        # ROI overlay (added 2026-09-15, explicit user request -- "provide proper how
        # its effect the roi in no [number]") -- real-Rupee expected_value_captured/
        # value_per_km for every route in every plan family, not just Plan C, since
        # they all share the identical _route_metrics stop shape (see
        # agent.attach_roi_metrics' own docstring for the formula and why it's NOT the
        # Pitching Agent's AI Sales Forecast).
        agent.attach_roi_metrics(result)
    agent.clear_google_distance_matrix()  # this SE's primed matrix must not leak into the next SE's candidate pool

    # GR-R7 (Routing_Agent_Configuration_Sheet_v8, "Never generate fewer than 3 feasible
    # algorithm-generated plans without flagging why") + GR-R10 ("Plan distinctness",
    # 2026-08-31 addition) -- flag when the 3 models (either family -- Plan A's Models
    # 1-3, or Plan B's 3-route fix) converge on materially the same stop set/sequence
    # instead of genuinely offering 3 distinct choices. v8 splits this into two
    # genuinely different causes, distinguished by whether the candidate pool actually
    # had room to differ (more eligible candidates existed than any single plan used):
    #   GR-R7: the pool itself was too small/thin -- every plan had to use essentially
    #     everything available, so there was no real selection to differ over. Expected,
    #     not a defect -- still flagged, never silently hidden.
    #   GR-R10: the pool WAS large enough that the 3 models could plausibly have
    #     differed, yet all 3 nonetheless produced an IDENTICAL stop-set in an IDENTICAL
    #     sequence. A different visit ORDER of the same stops still counts as distinct
    #     (that's Model 2/Distance-Min's whole purpose) -- only full (stop-set AND
    #     sequence) agreement across all 3 triggers this. Per R5.6's failure behavior,
    #     the SE should see this as one real route with an explanatory note, not 3
    #     duplicate-looking alternatives -- since no SE-facing app exists in this repo
    #     yet (see this function's own docstring), that "one route, not three" framing is
    #     surfaced here as a note appended to the persisted infeasibility_reason of the 2
    #     duplicate plans below, not by suppressing their RoutePlan rows outright (GR-R12
    #     still requires every model's own output stay logged).
    # GR-R7/GR-R10 apply to every plan family the same way now (CHANGED 2026-09-15 --
    # Plan C used to deliberately produce exactly 1 route, exempting it from this
    # 3-way comparison entirely; now that it produces 3 like Plan A/B, an LLM
    # genuinely converging on the same stop-set across 3 differently-framed prompts is
    # just as worth flagging as Plan A/B's models converging).
    family = {"B": "Plan B's 3 routes", "C": "Plan C's 3 routes"}.get(plan_choice, "Models 1-3")
    stop_sets = {ptype: tuple(s["row"].DC_ID for s in r["stops"]) for ptype, r in model_results.items()}
    non_empty_sets = {s for s in stop_sets.values() if s}
    max_stops_used = max((len(s) for s in stop_sets.values()), default=0)
    pool_had_room_to_differ = len(filtered) > max_stops_used
    # all_three_produced_stops guards against a real, confirmed case: Plan A's 3 models
    # can legitimately disagree on FEASIBILITY itself (e.g. Distance-Min/Balanced both
    # infeasible with 0 stops while Priority-Max succeeds) -- that collapses
    # non_empty_sets to size 1 too, but it is NOT "3 models independently agreeing," it's
    # 2 of 3 failing outright. Without this guard, that case would be mislabeled
    # Plans_Converged; it now correctly falls through to the generic GR-R7 branch below.
    all_three_produced_stops = all(len(s) > 0 for s in stop_sets.values())
    plans_converged = pool_had_room_to_differ and len(non_empty_sets) == 1 and all_three_produced_stops
    if plans_converged and diversity_unresolved:
        # The pool had more candidates by COUNT, but _enforce_distinct_routes proved none
        # of them yields a second within-caps route from this SE's start point (confirmed
        # live 2026-09-18: dk.s, 17 eligible DCs, only one reachable inside the 100 km /
        # 180 min day) -- that is a thin pool after the caps, not 3 models agreeing.
        converged_note = None
        exceptions.append({
            "source": "RoutingAgent", "reason_code": "Insufficient_Candidates_For_3_Plans",
            "detail": (
                f"{who} @ {plan_date}: only 1 route is possible across {family} -- of {len(filtered)} eligible "
                f"candidate(s), no other fits the day's distance/time caps from this SE's start point, "
                "even after swapping, re-ordering and trimming"
            ),
        })
    elif plans_converged:
        converged_note = (
            f"Plans_Converged (GR-R10): all 3 {family} independently produced the identical stop-set and "
            f"sequence despite {len(filtered)} eligible candidates being available ({max_stops_used} used) -- "
            f"this is one genuine route, not 3 distinct alternatives."
        )
        exceptions.append({"source": "RoutingAgent", "reason_code": "Plans_Converged", "detail": f"{who} @ {plan_date}: {converged_note}"})
    elif len(non_empty_sets) < 3 and non_empty_sets:
        converged_note = None
        exceptions.append({
            "source": "RoutingAgent", "reason_code": "Insufficient_Candidates_For_3_Plans",
            "detail": (
                f"{who} @ {plan_date}: only {len(non_empty_sets)} genuinely distinct route(s) possible across {family} "
                f"({len(filtered)} eligible candidate(s) -- too few to make 3 different routes even after swapping, "
                "re-ordering and trimming)"
            ),
        })
    else:
        converged_note = None

    default_tasks: List[Any] = []
    default_basis = "routing_agent"
    default_cap_exceeded = False

    for plan_type, result in model_results.items():
        own_reason = result.get("infeasibility_reason", "")
        if plan_type != default_plan_type and converged_note:
            # The default plan already carries the full converged_note via the
            # exceptions list above; the 2 duplicate plans get it here directly on their
            # own row so a reader looking at just this RoutePlan (not the exceptions
            # list) still sees why it's a duplicate, not a 3rd real alternative.
            own_reason = f"{own_reason} | {converged_note}" if own_reason else converged_note
        route_plan = RoutePlan.objects.create(
            plan_run=plan_run, se_id=se_id, se_name=se_email, plan_date=plan_date,
            plan_type=plan_type, origin_lat=origin[0], origin_lon=origin[1], origin_basis=origin_basis,
            total_distance_km=result["total_distance_km"], total_travel_minutes=result["total_travel_min"],
            total_visit_minutes=result["total_visit_min"],
            total_minutes=result["total_travel_min"] + result["total_visit_min"],
            priority_score_captured=result["priority_score_captured"],
            feasible=result["feasible"], infeasibility_reason=own_reason,
            is_default_selected=(plan_type == default_plan_type),
            # Speed/alpha assumption audit trail (see RoutePlan.avg_speed_kmph_used/
            # alpha_used docstring) -- none of the 3 model builders above are called
            # with an explicit avg_speed_kmph override, so R3_2_DEFAULT_AVG_SPEED_KMPH is
            # what every plan actually used; alpha_used only exists in BALANCED's own
            # result dict.
            avg_speed_kmph_used=agent.R3_2_DEFAULT_AVG_SPEED_KMPH,
            alpha_used=result.get("alpha_used"),
            distance_source=result.get("distance_source", "haversine_x1.4"),
            google_exceeds_cap=result.get("google_exceeds_cap", False),
            expected_value_captured=result.get("expected_value_captured"),
            value_per_km=result.get("value_per_km"),
            expected_value_dc_count=result.get("expected_value_dc_count", 0),
            llm_reasoning=result.get("llm_reasoning", ""),
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
            # Plan B's Exceptional-DC single-DC-fallback path (CORRECTED 2026-09-06,
            # see se_daily_plan_agent.build_route_cluster_based) sets infeasibility_
            # reason as an informational ceiling-breach note with feasible=True -- it
            # is by-design, not a failure, so it gets its own reason_code rather than
            # being lumped in with genuine Travel_Ceiling_Exceeded infeasibility (which
            # always has feasible=False).
            reason_code = "Exceptional_DC_Single_DC_Fallback" if result.get("is_exceptional_dc") else "Travel_Ceiling_Exceeded"
            exceptions.append({
                "source": "RoutingAgent", "reason_code": reason_code,
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


def resync_daily_tasks_from_selected_plan(plan_run: PlanRun, se_id: str) -> Dict[str, Any]:
    """Used by manage.py select_route_plan when an SE (via the ops CLI stand-in, see
    that command's docstring) picks a different one of the >=3 synced plans than the
    default -- and, as of 2026-09-15, by every SE Accept/add-stop/remove-stop call too
    (routing.accept_route_plan/edit_route_stops). Upserts this SE's DailyTask rows from
    the newly-selected RoutePlan's stops by dc_id, so Pitching Agent / reporting / the
    API don't need to know a selection or edit ever happened.

    Returns a status dict -- CHANGED 2026-09-15 from a bare stop-count int, explicit
    follow-up request ("if route plan a plan b and plan c create than all pitching data
    should be fetched... if SE add the dc data after accepting the route data should be
    fetched in all agent saved and go to feedback"). Also now runs Pitching Agent + DC
    Card (services.run_pitching_and_dc_card_agents) for whatever DCs are on the route
    after this resync -- previously a genuinely new DC (one not in the run's original
    DailyTask set) got neither, per this function's own former "Known limitation" below.
    Stays fully synchronous (no background job/polling -- explicit follow-up choice):
    the caller's single API response now carries `pitch_card_status` (`"regenerated"` /
    `"failed"` / `"skipped_no_stops"`), `dcs_refreshed`, and `pitch_failures` so the
    frontend can show "fetching data / creating pitch" for the call's duration and then
    confirm the result, without a separate status-polling mechanism. Keys:
      stop_count: int -- number of stops on the resulting route (unchanged meaning).
      pitch_card_status / dcs_refreshed / pitch_failures: see above.

    CHANGED 2026-09-15, explicit user request ("why dc card and pitch empty") --
    previously deleted EVERY existing DailyTask row for this SE/PlanRun and recreated
    all of them from scratch on every call. Since PitchScript/DCCard are each a
    OneToOneField(DailyTask, on_delete=CASCADE), that silently destroyed already-
    generated Pitch/DC Card data for EVERY DC still on the route, not just ones
    genuinely removed -- harmless when this only ran occasionally via the CLI ops
    override, but a real, constantly-hit problem once SE Accept/add/remove-stop started
    calling this on every single interaction. Now a DC that stays on the route keeps
    its existing DailyTask row (and therefore its PitchScript/DCCard) with only its
    route-position fields (sr_no/distance_km/purpose_of_visit/estimated_duration)
    updated; only a DC no longer on the route gets its row (and cascaded pitch/card)
    deleted, and only a genuinely new DC gets a bare new row - still with the rich
    fields blank, per this function's own "Known limitation" below, unchanged for
    those specifically since they really do have no pitch/card yet.

    Known limitation (narrowed 2026-09-15 -- Pitching/DC Card are now fresh, this part
    isn't): RouteStop only carries R6.1's confirmed fields (DC_ID, sequence, purposes,
    timing) -- not the rich per-DC financial/reason context (Present_Outstanding,
    Reason_Of_Visit, YTD_Private_Label, Finance_Status, BO_Scores, etc.) that only
    exists transiently on the DailyTaskRow objects built during generation. A genuinely
    NEW DailyTask row created here will still have those specific fields blank until a
    re-run of activate_tuff/generate_se_plan regenerates the full candidate set fresh
    (that's a whole-SE ranking pass that would reshuffle every other DC's priority too
    if re-run for one ad-hoc added stop -- deliberately out of scope here). Not silently
    papered over -- worth knowing before relying on a freshly-added stop's route-ranking
    fields for anything beyond confirming which DC/order the SE will visit. Its
    PitchScript/DCCard, however, ARE fresh as of this call -- see pitch_card_status."""
    from .models import DailyTask

    selected = plan_run.route_plans.filter(se_id=se_id, is_default_selected=True).first()
    if selected is None:
        return {"stop_count": 0, "pitch_card_status": "skipped_no_stops", "dcs_refreshed": [], "pitch_failures": []}

    stops = list(selected.stops.order_by("sequence_no"))
    stop_dc_ids = {stop.dc_id for stop in stops}

    # Only a DC no longer on the route loses its DailyTask row (and, via cascade, any
    # pitch/card it had) - everything else is upserted below, never blanket-deleted.
    DailyTask.objects.filter(plan_run=plan_run, se_id=se_id).exclude(dc_id__in=stop_dc_ids).delete()
    existing_by_dc = {
        t.dc_id: t for t in DailyTask.objects.filter(plan_run=plan_run, se_id=se_id, dc_id__in=stop_dc_ids)
    }

    cumulative_km = 0.0
    for stop in stops:
        cumulative_km += stop.distance_from_prev_km or 0.0
        existing = existing_by_dc.get(stop.dc_id)
        if existing is not None:
            existing.sr_no = stop.sequence_no
            existing.distance_km = round(cumulative_km, 2)
            existing.purpose_of_visit = stop.purposes
            existing.estimated_duration = int(stop.visit_duration_min)
            existing.save(update_fields=["sr_no", "distance_km", "purpose_of_visit", "estimated_duration"])
            continue
        DailyTask.objects.create(
            plan_run=plan_run, se_id=se_id, se_name=selected.se_name, plan_date=selected.plan_date,
            sr_no=stop.sequence_no, dc_name=None, dc_id=stop.dc_id, distance_km=round(cumulative_km, 2),
            recommended_task_type="DC Visit", purpose_of_visit=stop.purposes, reason_of_visit="",
            last_visit_date=None, days_since_last_visit=None, present_outstanding=None, present_overdue=None,
            last_order_date=None, last_order_value=None, last_payment_date=None,
            last_payment_join_key_unconfirmed=True, ytd_private_label=None, dc_club_participation="",
            objective="", no_new_orders=False, credit_on_hold=False, credit_on_hold_reason=None,
            estimated_duration=int(stop.visit_duration_min), priority_multiplier=1.0, finance_status=None,
            bo_scores={}, bo_composite_score=None, bo_rank=None,
            promise_to_pay_date=None, promise_to_pay_amount=None, promise_status=None,
            dc_health_score=None, health_gap=None, health_sub_scores={},
            negative_gm_flag=False, health_focus_track=False, health_focus_purposes="",
        )

    result: Dict[str, Any] = {
        "stop_count": len(stops), "pitch_card_status": "skipped_no_stops",
        "dcs_refreshed": [], "pitch_failures": [],
    }
    if stops:
        from .services import persist_exceptions, run_pitching_and_dc_card_agents
        client = agent.get_client()
        try:
            run_exceptions = run_pitching_and_dc_card_agents(plan_run, str(selected.plan_date), client, {})
        finally:
            client.close()
        result["pitch_card_status"] = "failed" if run_exceptions else "regenerated"
        result["dcs_refreshed"] = sorted(stop_dc_ids)
        result["pitch_failures"] = run_exceptions
        persist_exceptions(plan_run, run_exceptions)
    return result


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
    # finished_at filter added 2026-09-16: generate_plan_for_scope no longer runs inside
    # one transaction (see services._discard_plan_run_on_failure), so a run still being
    # built is visible here with its RoutePlans already written but its tasks not yet
    # -- "newest" must mean newest COMPLETE run, never the one mid-generation.
    candidate = (
        RoutePlan.objects.filter(plan_date=plan_date, plan_run__finished_at__isnull=False).filter(_se_filter(se))
        .order_by("-plan_run__run_timestamp").first()
    )
    if candidate is None:
        raise RoutingError(f"No RoutePlans found for se={se!r} on {plan_date} -- run activate_tuff/generate_se_plan first.")
    return candidate.plan_run


def _dc_geo_lookup() -> Dict[str, Dict[str, Any]]:
    """dc_id -> {dc_name, latitude, longitude} from DC_Master_Normalized.json.
    RouteStop persists no lat/lon of its own (see that model's own docstring - only
    directory/dcs/ has DC geo) so list_route_plans attaches it here rather than making
    the frontend do a second round-trip per stop. Added 2026-09-15, explicit user
    request ("real dc mapped and route visible according to google map api") - RouteMap
    previously plotted only the origin pin, nothing else on the map."""
    output_dir = Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output"
    dc_master = load_output_json(output_dir, "DC_Master_Normalized.json")
    return {
        str(r.get("DC_ID")): {
            "dc_name": r.get("DC_Name"), "latitude": r.get("Latitude"), "longitude": r.get("Longitude"),
        }
        for r in dc_master
    }


def list_route_plans(se: str, plan_date: str, plan_run_id: Optional[int] = None) -> Dict[str, Any]:
    """Returns {"plan_run_id", "se_id", "plans": [...]} -- R5.2's presentation fields for
    each of the SE's synced RoutePlans (>=3 per R5.1), each with its stops and dropped
    DCs. Used by both `select_route_plan` (no --select) and
    GET /api/planning/routes/<se>/<plan_date>/."""
    plan_run = resolve_route_plan_run(se, plan_date, plan_run_id)
    routes = RoutePlan.objects.filter(plan_run=plan_run, plan_date=plan_date).filter(_se_filter(se))
    if not routes.exists():
        raise RoutingError(f"PlanRun #{plan_run.id} has no RoutePlans for se={se!r} on {plan_date}.")

    # Live geo (input_partner_details, see _live_geo_lookup's own docstring) overlaid
    # onto the DC_Master static fallback -- added 2026-09-15, fixing a real accuracy gap
    # found live while building edit_route_stops: DC_Master_Normalized.json's own
    # Latitude/Longitude columns are noticeably less complete than this live table for
    # the exact same DCs, so RouteMap was showing more "no location on file" stops than
    # necessary. Scoped to only the dc_ids actually on these routes, not the whole
    # network, to keep the live query cheap.
    dc_geo = _dc_geo_lookup()
    all_stop_ids = list({s.dc_id for r in routes for s in r.stops.all()})
    live_coords = _live_geo_lookup(all_stop_ids) if all_stop_ids else {}
    for dc_id, (lat, lon) in live_coords.items():
        dc_geo.setdefault(dc_id, {})["latitude"] = lat
        dc_geo[dc_id]["longitude"] = lon

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
            # Speed/alpha assumption audit trail -- the ops assumptions behind the numbers above.
            "avg_speed_kmph_used": r.avg_speed_kmph_used,
            "alpha_used": r.alpha_used,
            # Google Maps route-accuracy overlay (added 2026-09-10, see RoutePlan.
            # distance_source's own docstring) -- "haversine_x1.4" (default) or
            # "google_maps" tells the caller whether total_distance_km/total_travel_
            # minutes above are the cheap estimate or a real Directions API result for
            # this exact already-selected route. google_exceeds_cap is only meaningful
            # when distance_source is "google_maps": it means the real number breaches
            # the cap the Haversine estimate had satisfied, flagged rather than
            # re-deciding stops (see apply_google_route_accuracy).
            "distance_source": r.distance_source,
            "google_exceeds_cap": r.google_exceeds_cap,
            # ROI overlay (added 2026-09-15, see RoutePlan.expected_value_captured's own
            # docstring for the exact formula) -- expected_value_captured is null (not 0)
            # whenever NONE of this route's stops had a real Present_Outstanding/
            # Last_Order_Value figure on file; expected_value_dc_count says how many of
            # stop_count actually contributed, so "Rs.0 from 0 of 5" is never confused
            # with "Rs.0 from 5 of 5 that genuinely have no value at stake."
            "expected_value_captured": r.expected_value_captured,
            "value_per_km": r.value_per_km,
            "expected_value_dc_count": r.expected_value_dc_count,
            # True once an SE has added/removed a stop via edit_route_stops (see
            # RoutePlan.manually_edited's own docstring) - the frontend should caveat
            # priority_score_captured/expected_value_captured above as reflecting the
            # ORIGINAL algorithm stop set, not this route's current (edited) one.
            "manually_edited": r.manually_edited,
            # Plan C only (planning/models.py RoutePlan.llm_reasoning) -- the model's own
            # explanation for these stops/order, plus any system notes (a hallucinated
            # DC_ID dropped, a cap-breach trim) appended by build_route_llm_reasoned.
            # "" for every Plan A/B row (the field's own default), returned as None here
            # so the frontend can tell "not applicable" apart from "explanation was
            # empty," same convention as infeasibility_reason below.
            "llm_reasoning": r.llm_reasoning or None,
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
                    # Real DC name/geo (added 2026-09-15) - None/None when this dc_id
                    # isn't in DC_Master_Normalized.json (shouldn't happen for a stop
                    # that was actually selected from it, but never assumed).
                    "dc_name": dc_geo.get(s.dc_id, {}).get("dc_name"),
                    "latitude": dc_geo.get(s.dc_id, {}).get("latitude"),
                    "longitude": dc_geo.get(s.dc_id, {}).get("longitude"),
                }
                for s in r.stops.order_by("sequence_no")
            ],
            # dc_name added 2026-09-15 (explicit user request - "list of dc when we
            # select only those which are eligible pool") - the frontend's add-a-DC
            # picker now sources its options from THIS list (RouteStop already has a
            # dc_name; dropped_dcs previously didn't, so a dropped candidate had no
            # readable label to show).
            "dropped_dcs": [
                {"dc_id": d.dc_id, "reason": d.reason, "dc_name": dc_geo.get(d.dc_id, {}).get("dc_name")}
                for d in r.dropped_dcs.all()
            ],
        })
    first = routes.first()
    return {
        "plan_run_id": plan_run.id, "se_id": first.se_id, "se_name": first.se_name, "plan_date": plan_date,
        # Whole-day approval state (added 2026-09-15, see accept_route_plan/
        # reject_route_plan's own docstrings) - a PlanRun-level field, not per-route, so
        # it's returned once here rather than repeated on every plan dict below.
        "status": plan_run.status, "reviewed_by": plan_run.reviewed_by or None, "reviewed_at": plan_run.reviewed_at,
        "plans": plans,
    }


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
    sync_result = resync_daily_tasks_from_selected_plan(plan_run, se_id)
    return {
        "plan_run_id": plan_run.id, "se_id": se_id, "selected": plan_type,
        "daily_tasks_resynced": sync_result["stop_count"],
        "pitch_card_status": sync_result["pitch_card_status"],
        "dcs_refreshed": sync_result["dcs_refreshed"],
        "pitch_failures": sync_result["pitch_failures"],
    }


def accept_route_plan(se: str, plan_date: str, plan_type: str, plan_run_id: Optional[int] = None, actor: str = "") -> Dict[str, Any]:
    """SE's own "Accept" action (added 2026-09-15, explicit user request -- "In se have
    the right ... if he wants to accept any route he will have accept and reject cta").
    Distinct from select_default_route_plan above (which stays as-is, still used
    standalone by `manage.py select_route_plan`/an admin browsing alternatives for some
    SE without that implying approval) -- this calls it for the actual pick + DailyTask
    resync, then ALSO marks the whole day's PlanRun APPROVED with a real reviewer record.
    PlanRun.status/reviewed_by/reviewed_at existed since this model was first written but
    nothing ever set them (see PlanRun.status's own "no reviewer workflow exists yet"
    comment) -- this is the first real writer."""
    result = select_default_route_plan(se, plan_date, plan_type, plan_run_id)
    plan_run = PlanRun.objects.get(id=result["plan_run_id"])
    plan_run.status = PlanRun.Status.APPROVED
    plan_run.reviewed_by = actor or se
    plan_run.reviewed_at = timezone.now()
    plan_run.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    result["status"] = plan_run.status
    return result


def reject_route_plan(se: str, plan_date: str, plan_run_id: Optional[int] = None, actor: str = "") -> Dict[str, Any]:
    """SE's own "Reject" action (added 2026-09-15, explicit user request, explicit
    follow-up choice: "Marks it rejected, keeps existing tasks untouched" -- i.e. purely
    an audit/flag, DailyTask is deliberately left alone here; an admin has to notice the
    rejection and act on it separately, same trust posture as this app's other
    admin-reviews-later fields). Does not touch is_default_selected/RouteStop/DailyTask
    at all -- if the SE later Accepts a (possibly different) route, accept_route_plan
    flips status back to APPROVED same as any other call."""
    plan_run = resolve_route_plan_run(se, plan_date, plan_run_id)
    plan_run.status = PlanRun.Status.REJECTED
    plan_run.reviewed_by = actor or se
    plan_run.reviewed_at = timezone.now()
    plan_run.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    return {"plan_run_id": plan_run.id, "se_id": se, "status": plan_run.status}


def _live_geo_lookup(dc_ids: List[str]) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    """Live per-DC lat/lon from input_partner_details.lat_2/long_2 (Redshift "dev" DB) --
    the SAME source planning.services._sql_geo/generate_plan_for_scope's own candidate-
    building step uses to populate the `dc["Latitude"]/["Longitude"]` every real Model
    1-3/Plan B/C route is actually built from. Added 2026-09-15, fixing a real bug found
    live: edit_route_stops originally reused _dc_geo_lookup() (DC_Master_Normalized.
    json's OWN Latitude/Longitude columns, built for the map feature) to reconstruct an
    existing route's stops before recomputing distances -- but that static, normalized
    snapshot is a much less complete source than this live table (confirmed live: a
    route recomputed from DC_Master alone silently produced 0km/0min legs for DCs this
    live query has real coordinates for, understating a 76km route as 12km). Falls back
    to DC_Master's own value per-DC (via the `fallback` param) when the live client
    isn't configured, the query fails, or a specific DC_ID isn't in the live result --
    fail-open, never raises, same posture as every other live-pull site in this app."""
    from .services import _sql_geo  # local import - services.py imports this module at
    # its own top level, so a module-level import here would be circular.

    result: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    try:
        client = agent.get_client()
    except Exception:
        return result
    try:
        for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_geo(dc_ids)):
            lat, lon = agent.parse_number(row.get("latitude")), agent.parse_number(row.get("longitude"))
            if lat is not None and lon is not None:
                result[row["sap_partner_id"]] = (lat, lon)
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass
    return result


def _reconstruct_metrics_candidates(
    stops: List[RouteStop], coords_by_id: Dict[str, Tuple[Optional[float], Optional[float]]],
) -> List[Dict[str, Any]]:
    """Turns persisted RouteStop rows back into the candidate-dict shape
    se_daily_plan_agent._route_metrics expects (dc lat/lon, row.Estimated_Duration,
    priority_score - the only 3 things that function and _candidate_coords actually
    read). Used by edit_route_stops to recompute a manually-edited route's real
    distances/times through the EXACT same shared math Models 1-3/Plan B/C already use,
    rather than hand-rolling leg arithmetic a second time. priority_score is always 0.0
    here, never fabricated -- RouteStop persists no per-stop BO Scoring Engine
    breakdown to recover it from (same gap resync_daily_tasks_from_selected_plan's own
    docstring already documents), which is exactly why RoutePlan.manually_edited exists:
    to tell the frontend this route's priority_score_captured/expected_value_captured
    no longer reflect its current (edited) stop set."""
    out = []
    for s in stops:
        lat, lon = coords_by_id.get(s.dc_id, (None, None))
        out.append({
            "dc": {"DC_ID": s.dc_id, "Latitude": lat, "Longitude": lon},
            "row": SimpleNamespace(DC_ID=s.dc_id, Estimated_Duration=s.visit_duration_min, Purpose_Of_Visit=s.purposes),
            "priority_score": 0.0,
            "matched": [],
        })
    return out


def edit_route_stops(
    se: str, plan_date: str, plan_type: str, action: str, dc_id: str,
    plan_run_id: Optional[int] = None,
) -> Dict[str, Any]:
    """SE's own route-editing right (added 2026-09-15, explicit user request -- "if se
    wants add the dc in route plan than he will add or wants to delete the route he
    will"; NARROWED same day, explicit follow-up -- "list of dc when we select only
    those which are eligible pool" -- from "any DC in the SE's own assigned scope" to
    just this route's own eligible pool, see below). Edits are allowed up to and
    including plan_date itself, never for a date that has already passed. action is
    "add" or "remove".

    "add": dc_id must be a DC THIS route's own generation already scored and considered
    -- i.e. it must appear in target.dropped_dcs (RouteDroppedDC, "every candidate the
    Ranked_Pool offered either appears in RouteStop or here, with why" - see that
    model's own docstring). This is stricter than "assigned to this SE" (a DC excluded
    by Program DC Selection, an inactive-status gate, or any other eligibility rule
    never reaches dropped_dcs at all, so it can never be added this way either) AND
    stricter than "considered somewhere in this PlanRun" (each of the 3 sibling
    RoutePlans in a PlanRun can score/drop the same DC differently, e.g. via
    exclude_stop_sets-forced distinctness - only THIS plan_type's own dropped_dcs
    counts). A dropped_dcs entry reasoned "Geo_Incomplete" is rejected same as a DC
    with no coordinates - the routing agent already knows it can't be routed to; not
    already being on this route is still checked separately. Appended at the end of
    the visit order with a default 45-minute visit (RouteStop.visit_duration_min's own
    model default) - the system has no BO-scored duration for a DC that wasn't
    actually selected by this route's own generation.

    "remove": dc_id must currently be on this route, and at least one stop must remain
    (a route can't be edited down to zero stops - reject the whole route instead if
    none of it is wanted).

    Recomputes total_distance_km/total_travel_minutes/total_visit_minutes/total_minutes/
    feasible via the exact same _route_metrics + apply_google_route_accuracy real-road
    overlay every algorithm-built route already uses (see _reconstruct_metrics_
    candidates), replaces this RoutePlan's RouteStop rows, sets manually_edited=True,
    and resyncs DailyTask if this route is the SE's currently-selected one (same
    resync_daily_tasks_from_selected_plan every other mutation already uses) - so an
    edit to the plan the SE is actually going to work from today takes effect
    immediately, not just in the RoutePlan row."""
    if action not in ("add", "remove"):
        raise RoutingError(f"Unknown action {action!r} - must be 'add' or 'remove'.")

    parsed_date = _parse_plan_date(plan_date)
    if parsed_date < _date.today():
        raise RoutingError(f"{plan_date} has already passed - route edits are only allowed for today or a future date.")

    plan_run = resolve_route_plan_run(se, plan_date, plan_run_id)
    routes = RoutePlan.objects.filter(plan_run=plan_run, plan_date=plan_date).filter(_se_filter(se))
    if not routes.exists():
        raise RoutingError(f"PlanRun #{plan_run.id} has no RoutePlans for se={se!r} on {plan_date}.")
    target = routes.filter(plan_type=plan_type).first()
    if target is None:
        raise RoutingError(f"No {plan_type} plan exists for se={se!r} on {plan_date} in PlanRun #{plan_run.id}.")

    se_id = routes.first().se_id
    existing_stops = list(target.stops.order_by("sequence_no"))

    dc_master_geo = _dc_geo_lookup()  # static fallback only - see _live_geo_lookup's own docstring

    needed_ids = list({s.dc_id for s in existing_stops} | {dc_id})
    coords_by_id = _live_geo_lookup(needed_ids)
    for needed_id in needed_ids:
        if needed_id not in coords_by_id:
            fallback = dc_master_geo.get(needed_id, {})
            coords_by_id[needed_id] = (fallback.get("latitude"), fallback.get("longitude"))

    if action == "add":
        if any(s.dc_id == dc_id for s in existing_stops):
            raise RoutingError(f"DC {dc_id} is already on this route.")
        dropped_entry = target.dropped_dcs.filter(dc_id=dc_id).first()
        if dropped_entry is None:
            raise RoutingError(
                f"DC {dc_id} is not in this route's eligible pool - only a DC the Routing Agent already "
                "scored and considered for this exact route (and dropped, e.g. for a capacity/distance "
                "ceiling) can be manually added."
            )
        if dropped_entry.reason == "Geo_Incomplete":
            raise RoutingError(f"DC {dc_id} has no location on file - cannot compute a route to it.")
        new_lat, new_lon = coords_by_id.get(dc_id, (None, None))
        if new_lat is None or new_lon is None:
            raise RoutingError(f"DC {dc_id} has no location on file - cannot compute a route to it.")
        candidates = _reconstruct_metrics_candidates(existing_stops, coords_by_id) + [{
            "dc": {"DC_ID": dc_id, "Latitude": new_lat, "Longitude": new_lon},
            "row": SimpleNamespace(DC_ID=dc_id, Estimated_Duration=45.0, Purpose_Of_Visit="Manually Added by SE"),
            "priority_score": 0.0,
            "matched": [],
        }]
    else:
        if not any(s.dc_id == dc_id for s in existing_stops):
            raise RoutingError(f"DC {dc_id} is not on this route.")
        if len(existing_stops) <= 1:
            raise RoutingError("Cannot remove the only stop on this route - reject the whole route instead if none of it is wanted.")
        candidates = _reconstruct_metrics_candidates([s for s in existing_stops if s.dc_id != dc_id], coords_by_id)

    origin = (target.origin_lat, target.origin_lon)
    route_result = agent._route_metrics(candidates, origin, agent.R3_2_DEFAULT_AVG_SPEED_KMPH)
    agent.apply_google_route_accuracy(route_result, origin)  # best-effort real-road overlay, fails open

    target.stops.all().delete()
    RouteStop.objects.bulk_create([
        RouteStop(
            route_plan=target, dc_id=stop["row"].DC_ID, sequence_no=i,
            purposes=stop["row"].Purpose_Of_Visit, eta_minutes_from_origin=stop["eta_minutes"],
            distance_from_prev_km=stop["distance_from_prev_km"],
            travel_time_from_prev_min=stop["travel_time_from_prev_min"],
            visit_duration_min=stop["row"].Estimated_Duration,
        )
        for i, stop in enumerate(route_result["stops"], start=1)
    ])
    target.total_distance_km = route_result["total_distance_km"]
    target.total_travel_minutes = route_result["total_travel_min"]
    target.total_visit_minutes = route_result["total_visit_min"]
    target.total_minutes = route_result["total_travel_min"] + route_result["total_visit_min"]
    target.distance_source = route_result.get("distance_source", "haversine_x1.4")
    target.google_exceeds_cap = route_result.get("google_exceeds_cap", False)
    target.feasible = agent._within_caps(route_result)
    target.manually_edited = True
    target.save(update_fields=[
        "total_distance_km", "total_travel_minutes", "total_visit_minutes", "total_minutes",
        "distance_source", "google_exceeds_cap", "feasible", "manually_edited",
    ])

    sync_result = {"stop_count": 0, "pitch_card_status": "skipped_not_selected", "dcs_refreshed": [], "pitch_failures": []}
    if target.is_default_selected:
        sync_result = resync_daily_tasks_from_selected_plan(plan_run, se_id)

    return {
        "plan_run_id": plan_run.id, "plan_type": plan_type, "action": action, "dc_id": dc_id,
        "stop_count": len(route_result["stops"]), "total_distance_km": target.total_distance_km,
        "total_minutes": target.total_minutes, "feasible": target.feasible,
        "daily_tasks_resynced": sync_result["stop_count"],
        "pitch_card_status": sync_result["pitch_card_status"],
        "dcs_refreshed": sync_result["dcs_refreshed"],
        "pitch_failures": sync_result["pitch_failures"],
    }
