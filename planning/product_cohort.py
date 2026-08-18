"""Focus Product Campaign Targeting -- orchestration over se_daily_plan_agent's
ProductCohortClient (Product _cohort/, Postman collection "Product Cohort -- -sps-v1-fp").

A DIFFERENT, real capability from the DC Card's "Crop Type/Style" gap (see the pitch_
config Focus Product Targeting CSV) -- product-first, not DC-first. Shares no output
tables with the Routing/Pitching Agents and isn't wired into generate_plan_for_scope();
it's a standalone, on-demand lookup (given a Focus Product + Node, which DCs to push it
to and when), invoked via `manage.py focus_product_targets` or get_focus_product_
campaign_targets() directly.

Auth boundary (see se_daily_plan_agent.py's ProductCohortClient docstring): this module
never signs in to the Product Cohort service. It only ever reads an already-issued
session (PRODUCT_COHORT_SESSION / PRODUCT_COHORT_GO_ADMIN_SESSION env vars) that a human
obtained themselves via the documented Postman flow. If those aren't set, every call
here fails loud with a Product_Cohort_Not_Configured exception entry, never a silent
empty result.

Response schema for Step 2B and Step 3 is unconfirmed (empty in the saved Postman
collection) -- both are returned as opaque raw JSON under "Step_2B"/"Step_3", flagged,
not reshaped into a guessed structure.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from django.conf import settings

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library


def parse_week_range(value: Optional[str], opt_name: str) -> Optional[tuple]:
    """'14-20' -> (14, 20). Shared by every CLI/API entry point that takes a Step 2B seed
    week range, so the FROM-TO parsing/error message can't drift between them."""
    if value is None:
        return None
    try:
        lo, hi = value.split("-", 1)
        return int(lo), int(hi)
    except ValueError:
        raise ProductCohortError(f"--{opt_name} must be FROM-TO, e.g. 14-20 (got {value!r})")


def build_season_weeks(outer: Optional[str], buildup: Optional[str], peak: Optional[int], closure: Optional[str]) -> Optional[Dict[str, int]]:
    """Builds the season_weeks dict get_focus_product_campaign_targets() expects, or None
    if buildup/peak/closure weren't all supplied (Step 2B is then skipped upstream, not
    defaulted -- see that function's docstring on why no default exists)."""
    if not (buildup and peak is not None and closure):
        return None
    outer_r = parse_week_range(outer, "focus-product-outer-weeks") or (1, 52)
    buildup_r = parse_week_range(buildup, "focus-product-buildup-weeks")
    closure_r = parse_week_range(closure, "focus-product-closure-weeks")
    return {
        "outer_from": outer_r[0], "outer_to": outer_r[1],
        "buildup_from": buildup_r[0], "buildup_to": buildup_r[1],
        "peak": peak,
        "closure_from": closure_r[0], "closure_to": closure_r[1],
    }


def split_csv(value: Optional[str]) -> Optional[List[str]]:
    return [v.strip() for v in value.split(",")] if value else None


class ProductCohortError(RuntimeError):
    """Raised for caller-facing failures (bad input) -- mirrors PlanningError/RoutingError's
    role for the SE Daily Task / Routing Agents. Not raised for the auth-not-configured
    case, which is reported via the returned exceptions list instead (same convention as
    every other live source in this pipeline -- see run_pipeline()'s Live_Pull_Failed)."""


def get_focus_product_campaign_targets(
    material_id: str,
    node_id: str,
    years: int = 4,
    season_weeks: Optional[Dict[str, int]] = None,
    crop_districts: Optional[List[str]] = None,
    related_product_names: Optional[List[str]] = None,
    client: Optional["agent.ProductCohortClient"] = None,
    deadline_seconds: float = 180.0,
) -> Dict[str, Any]:
    """Runs Steps 2A -> 2B -> 3 for one Focus Product (materialId) at one Node, fanning
    out to the confirmed live queries and folding any failure (not-configured, HTTP
    error, or a genuinely-unconfirmed required input left unsupplied) into "exceptions"
    rather than raising past the first, so a caller always gets back whatever steps did
    succeed instead of losing all 3 to one failure.

    season_weeks: the Step 2B seed -- keys outer_from/outer_to/buildup_from/buildup_to/
    peak/closure_from/closure_to. Genuinely unconfirmed whether these should be supplied
    by the caller or are themselves the thing Step 2B infers (see ProductCohortClient.
    step2b_crop_seasonality's docstring) -- if omitted, this function does NOT invent a
    default; it skips Step 2B and flags Product_Cohort_Season_Weeks_Not_Supplied instead.

    crop_districts / related_product_names: Step 3's inputs, same situation -- no
    confirmed source (Step 2A/2B output? a separate lookup?) exists for deriving them, so
    they're required caller inputs here too; omitted means Step 3 is skipped and flagged.

    deadline_seconds: this function can make up to 4 sequential-ish live HTTP calls
    (Step 2A's two + 2B + 3), each individually timing out at 60s -- worst case ~240s
    inside a single synchronous request cycle when triggered via the ?focus_product= API
    (planning.views._generate_and_respond). Once this wall-clock budget is spent, any
    remaining unattempted step is skipped and flagged rather than started, bounding the
    worst case instead of letting 4 unlucky timeouts stack silently.
    """
    if not material_id or not node_id:
        raise ProductCohortError("material_id and node_id are both required")

    pc = client or agent.ProductCohortClient()
    result: Dict[str, Any] = {
        "Material_ID": material_id, "Node_ID": node_id,
        "Step_2A": None, "Step_2B": None, "Step_3": None,
        "exceptions": [],
    }

    if not pc.configured:
        result["exceptions"].append({
            "source": "ProductCohort", "reason_code": "Product_Cohort_Not_Configured",
            "detail": (
                "PRODUCT_COHORT_SESSION/PRODUCT_COHORT_GO_ADMIN_SESSION not set -- this agent does not "
                "sign in to obtain them; see Product _cohort/PRODUCT_COHORT_AUTH.md and export an "
                "already-issued session yourself"
            ),
        })
        return result

    deadline = time.monotonic() + deadline_seconds

    def _flag_deadline(step_name: str) -> None:
        result["exceptions"].append({
            "source": "ProductCohort", "reason_code": "Product_Cohort_Deadline_Exceeded",
            "detail": f"{step_name} skipped -- {deadline_seconds:.0f}s overall budget for this call already spent by earlier steps",
        })

    # Step 2A's two calls are independent (same materialId/nodeId/years, different
    # analysis endpoints) -- run concurrently instead of back-to-back, and keep whichever
    # succeeds even if the other fails, instead of discarding both on a single failure.
    step_2a_errors: List[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        weekly_future = pool.submit(pc.get_ib_weekly_sales, material_id, node_id, years)
        raw_future = pool.submit(pc.get_ib_raw_records, material_id, node_id, years)
        try:
            weekly_sales = weekly_future.result()
        except Exception as e:
            weekly_sales = None
            step_2a_errors.append(f"ib_weekly_sales: {type(e).__name__}: {e}")
        try:
            raw_records = raw_future.result()
        except Exception as e:
            raw_records = None
            step_2a_errors.append(f"ib_raw_records: {type(e).__name__}: {e}")
    if weekly_sales is not None or raw_records is not None:
        result["Step_2A"] = {"ib_weekly_sales": weekly_sales, "ib_raw_records": raw_records}
    if step_2a_errors:
        result["exceptions"].append({"source": "ProductCohort", "reason_code": "Step_2A_Failed", "detail": "; ".join(step_2a_errors)})

    if season_weeks is None:
        result["exceptions"].append({
            "source": "ProductCohort", "reason_code": "Product_Cohort_Season_Weeks_Not_Supplied",
            "detail": "Step 2B skipped -- no season_weeks given and this function does not guess buildup/peak/closure weeks (see step2b_crop_seasonality's docstring on why a default would be a fabricated methodology, not a confirmed one)",
        })
    elif time.monotonic() >= deadline:
        _flag_deadline("Step 2B")
    else:
        try:
            result["Step_2B"] = pc.step2b_crop_seasonality(
                node_id=node_id, material_id=material_id,
                outer_from_week=season_weeks["outer_from"], outer_to_week=season_weeks["outer_to"],
                buildup_from_week=season_weeks["buildup_from"], buildup_to_week=season_weeks["buildup_to"],
                peak_week=season_weeks["peak"],
                closure_from_week=season_weeks["closure_from"], closure_to_week=season_weeks["closure_to"],
            )
        except Exception as e:
            result["exceptions"].append({"source": "ProductCohort", "reason_code": "Step_2B_Failed", "detail": f"{type(e).__name__}: {e}"})

    if not crop_districts or not related_product_names:
        result["exceptions"].append({
            "source": "ProductCohort", "reason_code": "Product_Cohort_Step3_Inputs_Not_Supplied",
            "detail": "Step 3 skipped -- crop_districts/related_product_names not given and no confirmed source exists in this pipeline to derive them (see step3_dc_cohort's docstring)",
        })
    elif time.monotonic() >= deadline:
        _flag_deadline("Step 3")
    else:
        try:
            result["Step_3"] = pc.step3_dc_cohort(material_id, node_id, crop_districts, related_product_names)
        except Exception as e:
            result["exceptions"].append({"source": "ProductCohort", "reason_code": "Step_3_Failed", "detail": f"{type(e).__name__}: {e}"})

    return result
