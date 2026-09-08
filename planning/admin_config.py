"""Admin Control Panel (added 2026-09-07, explicit user request -- "add the new tab for
admin control panel... use this sheet for creating control panel", built off the
SE_Daily_Task_Agent_Pipeline_Walkthrough sheet's Steps 1-12). Live-editable overrides
onto se_daily_plan_agent.BusinessConstants' hardcoded Python defaults, persisted in
PipelineSettings (planning/models.py).

Scope, deliberately: only BusinessConstants dataclass fields are overridable here, since
that class is already freshly instantiated per plan-generation call
(planning.services.generate_plan_for_scope, the single entry point both the web app's
Create/Refresh and `manage.py generate_se_plan` go through) -- setattr-ing overrides onto
a fresh instance is a safe, request-scoped change with a working git-tracked default to
fall back to. The sheet's Step 11 (Routing) documents 3 more ceilings
(R1_2_MAX_TRAVEL_MINUTES, PLAN_B_MAX_DAILY_DISTANCE_KM, PLAN_B_MAX_DAILY_TRAVEL_MINUTES)
that are deliberately NOT included as editable here -- they're module-level constants
read directly at many call sites throughout se_daily_plan_agent.py's route-building logic,
not one instantiated object, so wiring them into live overrides needs a wider, separately
-scoped refactor. get_config_state() still surfaces them, read-only, so the panel isn't
silently missing a whole pipeline step.
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
]

_FIELD_BY_KEY: Dict[str, Dict[str, Any]] = {f["key"]: f for f in ADMIN_EDITABLE_FIELDS}

# Step 11 (Routing) ceilings -- read-only reference, see module docstring for why these
# aren't live-overridable yet.
_ROUTING_REFERENCE = [
    {"label": "Plan A travel ceiling (all 3 models)", "unit": "minutes", "source": "R1_2_MAX_TRAVEL_MINUTES"},
    {"label": "Plan B distance ceiling", "unit": "km", "source": "PLAN_B_MAX_DAILY_DISTANCE_KM"},
    {"label": "Plan B travel ceiling", "unit": "minutes", "source": "PLAN_B_MAX_DAILY_TRAVEL_MINUTES"},
]


def load_business_constants() -> "agent.BusinessConstants":
    """se_daily_plan_agent.BusinessConstants(), with any admin overrides applied on top.
    Call this instead of agent.BusinessConstants() directly at any live plan-generation
    entry point (currently: planning.services.generate_plan_for_scope, the one entry
    point both the web app and `manage.py generate_se_plan` share). Unknown/stale keys in
    PipelineSettings.overrides (e.g. a field later removed from ADMIN_EDITABLE_FIELDS)
    are silently skipped, not applied -- the whitelist here is the source of truth, not
    whatever happens to already be stored."""
    constants = agent.BusinessConstants()
    overrides = PipelineSettings.get_singleton().overrides or {}
    for key, value in overrides.items():
        if key in _FIELD_BY_KEY and hasattr(constants, key):
            setattr(constants, key, value)
    return constants


def get_config_state() -> Dict[str, Any]:
    """Current effective value + hardcoded default for every editable field, grouped for
    the Admin Control Panel UI, plus the Step 11 routing ceilings shown read-only."""
    defaults = agent.BusinessConstants()
    settings_row = PipelineSettings.get_singleton()
    overrides = settings_row.overrides or {}
    groups: Dict[str, list] = {}
    for f in ADMIN_EDITABLE_FIELDS:
        default_value = getattr(defaults, f["key"])
        is_overridden = f["key"] in overrides
        value = overrides[f["key"]] if is_overridden else default_value
        groups.setdefault(f["group"], []).append({
            **f, "default": default_value, "value": value, "overridden": is_overridden,
        })
    routing_reference = [
        {**r, "value": getattr(agent, r["source"])} for r in _ROUTING_REFERENCE
    ]
    return {
        "Groups": [{"Group": g, "Fields": fs} for g, fs in groups.items()],
        "Routing_Reference": routing_reference,
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
