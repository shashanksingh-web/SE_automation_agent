"""Admin Control Panel (added 2026-09-07, explicit user request -- "add the new tab for
admin control panel... use this sheet for creating control panel", built off the
SE_Daily_Task_Agent_Pipeline_Walkthrough sheet's Steps 1-12). Live-editable overrides
onto se_daily_plan_agent's hardcoded Python defaults, persisted in PipelineSettings
(planning/models.py).

Two kinds of editable field, per ADMIN_EDITABLE_FIELDS' own "target":
- "constants" (the original, larger set): a BusinessConstants dataclass field.
  BusinessConstants is freshly instantiated per plan-generation call
  (planning.services.generate_plan_for_scope, the single entry point both the web app's
  Create/Refresh and `manage.py generate_se_plan` go through) -- setattr-ing overrides
  onto a fresh instance is a safe, request-scoped change with a working git-tracked
  default (a fresh BusinessConstants()) to fall back to.
- "module" (the sheet's Step 11 routing ceilings, added 2026-09-07 explicit user request
  "routing agent ceiling also configurable"): a bare module-level constant in
  se_daily_plan_agent.py, read directly at 15+ call sites throughout the Routing Agent's
  route-building logic rather than one instantiated object. load_business_constants()
  monkey-patches these directly onto the se_daily_plan_agent module (setattr on the
  module itself) every time it runs -- since every real call site reads the module
  global fresh at call time (not a def-time-bound default -- the one exception,
  _cluster_candidates_by_density's max_intra_cluster_km, was fixed 2026-09-07 to resolve
  at call time too, see that function's own comment), this reaches all of them without
  a wider parameter-threading refactor. Known limitation, accepted rather than
  engineered around: this mutates process-global state, not a per-request-scoped value
  -- fine under this app's own already-accepted concurrency posture (a single shared
  SQLite DB that already serializes/locks concurrent plan generations, see the project's
  own "database is locked" 502 issue), not safe if this app ever moves to a
  higher-concurrency deployment without addressing that first. Because a module-level
  patch has no fresh-instance fallback to read a true default from, the hardcoded
  default for these 3 fields is recorded directly in ADMIN_EDITABLE_FIELDS itself
  (a "default" key), not re-derived from the (possibly already-patched) module.
"""
from __future__ import annotations

from typing import Any, Dict, List

import se_daily_plan_agent as agent

from .models import PipelineSettings

# One entry per admin-editable BusinessConstants field. Grouped exactly the way the
# walkthrough sheet's own tabs are (Overview's Steps 1/3/5/6/8/10), so the panel's
# section headers can come straight from `group` with no separate mapping to maintain.
ADMIN_EDITABLE_FIELDS: List[Dict[str, Any]] = [
    # --- Step 1: Eligibility ---
    {
        "group": "Eligibility", "key": "min_days_since_last_visit", "type": "int",
        "label": "Not-visited-recently window", "unit": "days", "min": 0, "max": 90,
        "description": "A DC visited within this many days is excluded from all agents entirely (Section 6.2).",
    },
    # --- Step 3: BO Scoring -- Outstanding ---
    {
        "group": "BO Scoring -- Outstanding", "key": "qualify_outstanding_balance", "type": "float",
        "label": "Qualifying balance threshold", "unit": "₹", "min": 0, "max": 10_000_000,
        "description": "A DC with Current_Outstanding at or above this qualifies for the Outstanding objective (Section 8.5).",
    },
    {
        "group": "BO Scoring -- Outstanding", "key": "bo3_grade_a", "type": "float",
        "label": "Grade A cutoff", "unit": "score", "min": 0, "max": 2,
        "description": "health_pct >= this -> Grade A (1 - overdue fraction).",
    },
    {
        "group": "BO Scoring -- Outstanding", "key": "bo3_grade_b", "type": "float",
        "label": "Grade B cutoff", "unit": "score", "min": 0, "max": 2,
        "description": "health_pct >= this (and below Grade A) -> Grade B.",
    },
    {
        "group": "BO Scoring -- Outstanding", "key": "bo3_grade_c", "type": "float",
        "label": "Grade C cutoff", "unit": "score", "min": 0, "max": 2,
        "description": "health_pct >= this (and below Grade B) -> Grade C; below this -> Grade D.",
    },
    # --- Step 3: BO Scoring -- PL (Private Label) ---
    {
        "group": "BO Scoring -- PL", "key": "bo1_grade_a", "type": "float",
        "label": "Grade A cutoff", "unit": "score", "min": 0, "max": 2,
        "description": "PL_Ratio (score_pct, capped at 100%) >= this -> Grade A.",
    },
    {
        "group": "BO Scoring -- PL", "key": "bo1_grade_b", "type": "float",
        "label": "Grade B cutoff", "unit": "score", "min": 0, "max": 2,
        "description": "PL_Ratio >= this (and below Grade A) -> Grade B.",
    },
    {
        "group": "BO Scoring -- PL", "key": "bo1_grade_c", "type": "float",
        "label": "Grade C cutoff", "unit": "score", "min": 0, "max": 2,
        "description": "PL_Ratio >= this (and below Grade B) -> Grade C; below this -> Grade D.",
    },
    {
        "group": "BO Scoring -- PL", "key": "pl_trailing_leg_growth_multiplier", "type": "float",
        "label": "Trailing-90d leg growth expectation", "unit": "x", "min": 0.5, "max": 3,
        "description": "PL_Expected's trailing-90d leg is scaled by this before averaging with the AOP-target leg -- the DC is expected to beat its own recent average by this much.",
    },
    # --- Step 3: BO Scoring -- Visits ---
    {
        "group": "BO Scoring -- Visits", "key": "qualify_visits_days_since", "type": "int",
        "label": "Not-visited-recently qualifying window", "unit": "days", "min": 0, "max": 90,
        "description": "Days_Since_Last_Visit is None or above this -> qualifies for the Visits objective (Section 8.5).",
    },
    # --- Step 5: Critical Flag ---
    {
        "group": "Critical Flag", "key": "dc_visit_escalation_threshold", "type": "int",
        "label": "Chronic-miss escalation threshold", "unit": "consecutive misses", "min": 1, "max": 20,
        "description": "A DC with this many or more consecutive missed visits trips the Critical banner's \"Escalated\" reason.",
    },
    # --- Step 6: Health Score (Source 3k) ---
    {
        "group": "Health Score", "key": "health_weight_nrv", "type": "float",
        "label": "NRV weight", "unit": "share of composite", "min": 0, "max": 1,
        "description": "Weight of the Net Revenue Value component in DC_Health_Score. The 7 weights should sum to 1.0.",
    },
    {
        "group": "Health Score", "key": "health_weight_gm", "type": "float",
        "label": "GM weight", "unit": "share of composite", "min": 0, "max": 1,
        "description": "Weight of the Gross Margin component in DC_Health_Score. The 7 weights should sum to 1.0.",
    },
    {
        "group": "Health Score", "key": "health_weight_gm_pct", "type": "float",
        "label": "GM% weight", "unit": "share of composite", "min": 0, "max": 1,
        "description": "Weight of the Gross Margin % component in DC_Health_Score. The 7 weights should sum to 1.0.",
    },
    {
        "group": "Health Score", "key": "health_weight_pl_contribution", "type": "float",
        "label": "PL Contribution weight", "unit": "share of composite", "min": 0, "max": 1,
        "description": "Weight of the Private Label Contribution component in DC_Health_Score. The 7 weights should sum to 1.0.",
    },
    {
        "group": "Health Score", "key": "health_weight_return", "type": "float",
        "label": "Return weight", "unit": "share of composite", "min": 0, "max": 1,
        "description": "Weight of the Return-rate component in DC_Health_Score. The 7 weights should sum to 1.0.",
    },
    {
        "group": "Health Score", "key": "health_weight_credit", "type": "float",
        "label": "Credit weight", "unit": "share of composite", "min": 0, "max": 1,
        "description": "Weight of the Credit (payment-timeliness) component in DC_Health_Score. The 7 weights should sum to 1.0.",
    },
    {
        "group": "Health Score", "key": "health_weight_od", "type": "float",
        "label": "OD weight", "unit": "share of composite", "min": 0, "max": 1,
        "description": "Weight of the OD (90+ day debt-aging) component in DC_Health_Score. The 7 weights should sum to 1.0.",
    },
    {
        "group": "Health Score", "key": "health_focus_days_since_last_sale_max", "type": "int",
        "label": "Eligibility window", "unit": "days", "min": 0, "max": 365,
        "description": "A DC must be active and have sold within this many days to get a full Health Score composite computed at all.",
    },
    {
        "group": "Health Score", "key": "health_bucket_strong", "type": "float",
        "label": "Strong bucket cutoff", "unit": "score", "min": 0, "max": 1,
        "description": "A component's score_pct above this reads as Strong.",
    },
    {
        "group": "Health Score", "key": "health_bucket_fine", "type": "float",
        "label": "Fine bucket cutoff", "unit": "score", "min": 0, "max": 1,
        "description": "A component's score_pct above this (and at/below Strong) reads as Fine.",
    },
    {
        "group": "Health Score", "key": "health_bucket_weak", "type": "float",
        "label": "Weak bucket cutoff", "unit": "score", "min": 0, "max": 1,
        "description": "A component's score_pct above this (and at/below Fine) reads as Weak; at/below this reads as Worst. Any component Weak/Worst qualifies the DC for Health-Focus.",
    },
    # --- Step 7: Priority_Score ---
    {
        "group": "Priority Score", "key": "rank1_weight", "type": "float",
        "label": "Rank-1 objective weight", "unit": "share", "min": 0, "max": 1,
        "description": "Weight given to a DC's most-severe matched objective's gap when computing Priority_Score.",
    },
    {
        "group": "Priority Score", "key": "rank2_weight", "type": "float",
        "label": "Rank-2 objective weight", "unit": "share", "min": 0, "max": 1,
        "description": "Weight given to a DC's second-most-severe matched objective's gap.",
    },
    {
        "group": "Priority Score", "key": "rank3_weight", "type": "float",
        "label": "Rank-3 objective weight", "unit": "share", "min": 0, "max": 1,
        "description": "Weight given to a DC's third-most-severe matched objective's gap.",
    },
    {
        "group": "Priority Score", "key": "contact_fatigue_max_attempts", "type": "int",
        "label": "Fatigue trigger attempts", "unit": "attempts", "min": 1, "max": 10,
        "description": "This many failed contact attempts within the fatigue window discounts a DC's priority score.",
    },
    {
        "group": "Priority Score", "key": "contact_fatigue_window_days", "type": "int",
        "label": "Fatigue rolling window", "unit": "days", "min": 1, "max": 30,
        "description": "Rolling window over which contact attempts are counted for the fatigue discount.",
    },
    {
        "group": "Priority Score", "key": "contact_fatigue_priority_cut", "type": "float",
        "label": "Fatigue priority discount", "unit": "fraction", "min": 0, "max": 1,
        "description": "Priority_Score is multiplied by (1 - this) once a DC's contact attempts hit the fatigue trigger.",
    },
    {
        "group": "Priority Score", "key": "overdue_90_plus_priority_boost", "type": "float",
        "label": "90+ day overdue queue-jump", "unit": "additive boost", "min": 0, "max": 100,
        "description": "Added to Priority_Score (after the fatigue discount) for a DC with real overdue aged 90+ days -- guarantees it outranks any unboosted DC.",
    },
    {
        "group": "Priority Score", "key": "gr28_priority_score", "type": "float",
        "label": "GR-28 exclusive rank-#1 score", "unit": "score", "min": 0, "max": 100_000,
        "description": "A DC with any real overdue balance (pathik_report.overdue > 0) gets this Priority_Score outright -- an exclusive top rank for that SE's day. Must stay well above the 90+ day boost.",
    },
    # --- DC Selection (added 2026-09-07, explicit user request "add one more column
    # like dc selection... based on rank and condition like overdue and other") --
    # rank/condition-based qualification and priority gates, previously bare "> 0"
    # literals in the code rather than a real configurable threshold. Both default to
    # 0.0, reproducing the exact original "any positive overdue" behavior. Deliberately
    # does NOT include qualify_outstanding_days_overdue/qualify_pl_max_orders_30d
    # (defined on BusinessConstants but confirmed NOT wired into any real qualification
    # check -- see their own docstrings, "not independently computable"/"not computable
    # live yet" -- only ever surfaced as Dynamic_Parameters_Resolved metadata) -- editing
    # those wouldn't change any real behavior, which would make them a dishonest knob to
    # expose here as if they did.
    {
        "group": "DC Selection", "key": "gr28_overdue_min_threshold", "type": "float",
        "label": "GR-28 minimum overdue to force-qualify", "unit": "₹", "min": 0, "max": 1_000_000,
        "description": "A DC's real overdue balance (pathik_report.overdue) must exceed this to force-include it for Outstanding and give it GR-28's exclusive rank-#1 score, bypassing every other qualification check. 0 = any positive overdue qualifies (the original behavior).",
    },
    {
        "group": "DC Selection", "key": "overdue_90_plus_boost_min_threshold", "type": "float",
        "label": "90+ day boost minimum overdue", "unit": "₹", "min": 0, "max": 1_000_000,
        "description": "A DC's 90+-day-aged overdue (dc_datamart.os_90_plus) must exceed this to trigger the queue-jump boost below. 0 = any positive 90+ balance qualifies (the original behavior).",
    },
    # --- Step 10: Daily Caps ---
    {
        "group": "Daily Caps", "key": "daily_cap_visits", "type": "int",
        "label": "Visits cap", "unit": "tasks/day", "min": 0, "max": 20,
        "description": "Maximum Visits-objective tasks one SE can be assigned in a day.",
    },
    {
        "group": "Daily Caps", "key": "daily_cap_outstanding", "type": "int",
        "label": "Outstanding cap", "unit": "tasks/day", "min": 0, "max": 20,
        "description": "Maximum Outstanding-objective tasks one SE can be assigned in a day.",
    },
    {
        "group": "Daily Caps", "key": "daily_cap_pl", "type": "int",
        "label": "PL cap", "unit": "tasks/day", "min": 0, "max": 20,
        "description": "Maximum PL-objective tasks one SE can be assigned in a day.",
    },
    {
        "group": "Daily Caps", "key": "max_daily_tasks", "type": "int",
        "label": "Hard daily task cap", "unit": "tasks/day", "min": 1, "max": 20,
        "description": "8.10 -- overall ceiling on one SE's day, supersedes the per-objective caps above.",
    },
    {
        "group": "Daily Caps", "key": "max_objectives_per_day", "type": "int",
        "label": "Objectives bundled per visit", "unit": "objectives/task", "min": 1, "max": 5,
        "description": "Maximum distinct BO objectives that can be bundled into one DC visit (8.12).",
    },
    {
        "group": "Daily Caps", "key": "total_capacity_min", "type": "int",
        "label": "Total daily capacity", "unit": "minutes", "min": 60, "max": 1440,
        "description": "8.2 -- one SE's total working minutes/day (calls + field time combined).",
    },
    # --- Step 11: Routing (added 2026-09-07, explicit user request "routing agent
    # ceiling also configurable") -- "target": "module" + "module_attr" + an explicit
    # "default" distinguish these from every field above (which are plain
    # BusinessConstants attributes, "target" defaulting to "constants" wherever it's
    # read below). See this module's own docstring for why these need a different
    # apply mechanism (a module-level setattr, not BusinessConstants setattr) and why
    # their "default" can't just be re-derived from a fresh instance like the others.
    {
        "group": "Routing", "key": "r1_2_max_travel_minutes", "type": "float",
        "label": "Plan A travel ceiling (all 3 models)", "unit": "minutes", "min": 30, "max": 480,
        "description": "R1.2 -- an SE must not spend more than this many minutes/day travelling (Plan A's Priority-Max/Distance-Min/Balanced models all share this ceiling).",
        "target": "module", "module_attr": "R1_2_MAX_TRAVEL_MINUTES", "default": 180,
    },
    {
        "group": "Routing", "key": "plan_a_max_round_trip_distance_km", "type": "float",
        "label": "Plan A distance ceiling (all 3 models)", "unit": "km", "min": 10, "max": 500,
        "description": "Added 2026-09-09 -- Plan A's round-trip distance budget (Priority-Max/Distance-Min/Balanced all share this ceiling, same as the travel-time ceiling above). Not part of the original spec (Model 1 previously had no distance cap at all).",
        "target": "module", "module_attr": "PLAN_A_MAX_ROUND_TRIP_DISTANCE_KM", "default": 100.0,
    },
    {
        "group": "Routing", "key": "plan_b_max_daily_distance_km", "type": "float",
        "label": "Plan B distance ceiling", "unit": "km", "min": 10, "max": 500,
        "description": "Section 5 -- Plan B's (Beat Planning/Cluster-Based) round-trip distance budget. Both this and the travel-time ceiling below must be satisfied together.",
        "target": "module", "module_attr": "PLAN_B_MAX_DAILY_DISTANCE_KM", "default": 100.0,
    },
    {
        "group": "Routing", "key": "plan_b_max_daily_travel_minutes", "type": "float",
        "label": "Plan B travel ceiling", "unit": "minutes", "min": 30, "max": 480,
        "description": "Section 5 -- Plan B's round-trip travel-time budget. Both this and the distance ceiling above must be satisfied together.",
        "target": "module", "module_attr": "PLAN_B_MAX_DAILY_TRAVEL_MINUTES", "default": 180.0,
    },
]

_FIELD_BY_KEY: Dict[str, Dict[str, Any]] = {f["key"]: f for f in ADMIN_EDITABLE_FIELDS}


def load_business_constants() -> "agent.BusinessConstants":
    """se_daily_plan_agent.BusinessConstants(), with any admin overrides applied on top,
    AND (side effect) patches the Step 11 routing ceilings directly onto the
    se_daily_plan_agent module for any override on a "module"-target field -- see this
    module's own docstring for why routing needs a different apply mechanism than
    BusinessConstants' plain setattr. Call this instead of agent.BusinessConstants()
    directly at any live plan-generation entry point (currently: planning.services.
    generate_plan_for_scope, the one entry point both the web app and `manage.py
    generate_se_plan` share) -- calling it is what makes BOTH kinds of override actually
    take effect for that run. Unknown/stale keys in PipelineSettings.overrides (e.g. a
    field later removed from ADMIN_EDITABLE_FIELDS) are silently skipped, not applied --
    the whitelist here is the source of truth, not whatever happens to already be stored."""
    constants = agent.BusinessConstants()
    overrides = PipelineSettings.get_singleton().overrides or {}
    for key, value in overrides.items():
        field = _FIELD_BY_KEY.get(key)
        if field and field.get("target") != "module" and hasattr(constants, key):
            setattr(constants, key, value)
    # Module-target (routing) fields are set UNCONDITIONALLY on every call, to either
    # the override or the field's own hardcoded default -- unlike BusinessConstants
    # (a fresh instance every call, so a removed override naturally reverts), the
    # se_daily_plan_agent module is a long-lived singleton: if a reset only deleted the
    # DB override and never explicitly reapplied here, the module attribute would stay
    # stuck at its last-patched value indefinitely (until process restart), not actually
    # revert. See this module's own docstring for the broader module-patching tradeoff.
    for field in ADMIN_EDITABLE_FIELDS:
        if field.get("target") == "module":
            setattr(agent, field["module_attr"], overrides.get(field["key"], field["default"]))
    return constants


def get_config_state() -> Dict[str, Any]:
    """Current effective value + hardcoded default for every editable field, grouped for
    the Admin Control Panel UI. "module"-target fields (Step 11 routing) read their
    default from ADMIN_EDITABLE_FIELDS itself, not a fresh module import (there's no such
    thing -- the module may already be running with a prior override applied by an
    earlier load_business_constants() call in this same process)."""
    defaults = agent.BusinessConstants()
    settings_row = PipelineSettings.get_singleton()
    overrides = settings_row.overrides or {}
    groups: Dict[str, list] = {}
    for f in ADMIN_EDITABLE_FIELDS:
        is_module = f.get("target") == "module"
        default_value = f["default"] if is_module else getattr(defaults, f["key"])
        is_overridden = f["key"] in overrides
        value = overrides[f["key"]] if is_overridden else default_value
        groups.setdefault(f["group"], []).append({
            **f, "default": default_value, "value": value, "overridden": is_overridden,
        })
    return {
        "Groups": [{"Group": g, "Fields": fs} for g, fs in groups.items()],
        "Updated_At": settings_row.updated_at,
        "Updated_By": settings_row.updated_by,
    }


def apply_overrides(changes: Dict[str, Any], updated_by: str = "") -> Dict[str, str]:
    """Validates and persists `changes` (key -> new value) onto PipelineSettings.overrides.
    Returns {} on success, or {key: error_message} for any rejected keys -- a partial
    apply (valid keys saved, invalid ones reported back) rather than an all-or-nothing
    failure, so one bad value doesn't block every other real change in the same request."""
    settings_row = PipelineSettings.get_singleton()
    overrides = dict(settings_row.overrides or {})
    errors: Dict[str, str] = {}
    applied = False
    for key, raw_value in changes.items():
        field = _FIELD_BY_KEY.get(key)
        if not field:
            errors[key] = "Not an editable field"
            continue
        try:
            value = float(raw_value) if field["type"] == "float" else int(raw_value)
        except (TypeError, ValueError):
            errors[key] = f"Expected a {field['type']}"
            continue
        if field.get("min") is not None and value < field["min"]:
            errors[key] = f"Must be >= {field['min']}"
            continue
        if field.get("max") is not None and value > field["max"]:
            errors[key] = f"Must be <= {field['max']}"
            continue
        overrides[key] = value
        applied = True
    if applied:
        settings_row.overrides = overrides
        settings_row.updated_by = updated_by
        settings_row.save()
    return errors


def reset_fields(keys: List[str]) -> None:
    """Removes each of `keys` from overrides (reverts to BusinessConstants' hardcoded
    default). A key not currently overridden is silently skipped, not an error."""
    settings_row = PipelineSettings.get_singleton()
    overrides = dict(settings_row.overrides or {})
    changed = False
    for key in keys:
        if key in overrides:
            del overrides[key]
            changed = True
    if changed:
        settings_row.overrides = overrides
        settings_row.save()
