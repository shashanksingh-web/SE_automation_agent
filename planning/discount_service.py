"""Discount Service (coupon-service.api.agrevolution.in) integration -- added
2026-09-18, a SUPPLEMENT to planning.services._sql_scheme_description_cards(), not a
replacement.

Why not a replacement: that SQL query already reads coupon_service.public.scheme
directly via Redshift cross-database federation (the same live database this REST API
serves), plus joins in T&C translations, booking-order status, and SKU/product-name
resolution this REST API does not expose at all. Confirmed live by inspecting a real
API response and comparing field-for-field against the SQL query before this module was
built -- replacing the SQL version with this would have been a real quality regression,
not an upgrade. This module implements the explicit "Supplement, don't replace"
decision (2026-09-18) instead: it's an available, documented building block -- a
scheme-description generator built from the REST API's raw JSON -- NOT wired into
run_pitching_and_dc_card_agents. Use it for a future lighter-weight/no-Redshift path or
a freshness cross-check against the SQL version; see the discount_service_schemes
management command for a standalone preview.

Response shape confirmed live 2026-09-18 (agent.DiscountServiceClient.fetch_active_
schemes): a bare JSON list (no pagination envelope), each scheme object carrying
benefitChannel/benefitPassDate/bookingEndDate/bookingStartDate/description (often just
repeats name, not reliably useful)/discountType/discountingSchemeType/
maxDiscountPerUser/name/nodeIdList/orderingEndDate/orderingStartDate/productIdList/
rules (a LIST of rule objects: brands/categories/nodeIds/partnerIds/skus/stateIds/
subcategories/templates -- all string-ID arrays, a scheme can have more than one rule
set)/schemeId/slabDynamicParam/slabMinMaxType/slabType/slabs (list of
{bookingAmountRate,discountRate,slabExpiryDate,slabId,slabMax,slabMin})/
termsAndConditions/unitOfMeasure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import agent  # noqa: F401 -- exposes agent.DiscountServiceClient/DiscountServiceNotConfigured to callers of this module

_SCHEME_TYPE_LABELS = {
    "ABS": "Advance booking scheme",
    "CVR": "Cumulative volume rebate",
    "CDS": "Cash discount scheme",
    "TOD": "Turnover discount",
}

_UNIT_LABELS = {"KILOGRAM": "kg", "PACKET": "packet", "LITRE": "litre"}

_SLAB_BASIS_LABELS = {"DATE": "date", "DAYS": "payment days", "VALUE": "value"}


def _fmt_amount(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _slab_range_text(slab: Dict[str, Any], basis: str, unit: str) -> str:
    lo, hi = slab.get("slabMin"), slab.get("slabMax")
    if lo in (None, ""):
        return "?"
    if basis == "DATE":
        return f"from {lo}" if hi in (None, "") else f"{lo} to {hi}"
    lo_txt, hi_txt = _fmt_amount(lo), _fmt_amount(hi)
    if basis == "VALUE":
        return f"₹{lo_txt}+" if hi_txt is None else f"₹{lo_txt} - ₹{hi_txt}"
    if basis == "DAYS":
        return f"{lo_txt}+ days" if hi_txt is None else f"{lo_txt}-{hi_txt} days"
    return f"{lo_txt}+ {unit}" if hi_txt is None else f"{lo_txt}-{hi_txt} {unit}"


def _slab_rate_text(slab: Dict[str, Any], discount_type: str, unit: str) -> str:
    rate_txt = _fmt_amount(slab.get("discountRate")) or "?"
    return f"{rate_txt}%" if discount_type == "PERCENT" else f"₹{rate_txt}/{unit}"


def build_generated_description(scheme: Dict[str, Any]) -> str:
    """Builds a plain-English description from one raw scheme JSON object, in the same
    descriptive style as planning.services._sql_scheme_description_cards'
    generated_description column (scheme type, location/product scope, slab-by-slab
    discount, key dates) -- NOT a byte-for-byte port of that query's full slab-island-
    merging/rule-name-resolution logic. Location/product scope here is reported as raw
    IDs, not names -- this function has no Redshift client to resolve them; see
    resolve_node_state_names/get_active_discount_schemes(resolve_names=True) for that."""
    scheme_type_code = scheme.get("discountingSchemeType") or ""
    scheme_type_label = _SCHEME_TYPE_LABELS.get(scheme_type_code, scheme_type_code or "Scheme")
    unit = _UNIT_LABELS.get(scheme.get("unitOfMeasure"), "unit")
    discount_type = scheme.get("discountType") or ""
    basis = scheme.get("slabMinMaxType") or ""

    rules = scheme.get("rules") or []
    node_ids = sorted({n for r in rules for n in (r.get("nodeIds") or [])})
    state_ids = sorted({s for r in rules for s in (r.get("stateIds") or [])})
    products = sorted({p for r in rules for p in (r.get("skus") or []) + (r.get("templates") or [])})

    parts = [f"{scheme_type_label} for DCs"]
    if node_ids:
        parts.append(f" in {len(node_ids)} node{'s' if len(node_ids) != 1 else ''} (node IDs: {', '.join(node_ids)})")
    elif state_ids:
        parts.append(f" in {len(state_ids)} state{'s' if len(state_ids) != 1 else ''} (state IDs: {', '.join(state_ids)})")
    else:
        parts.append(" (no location limit)")
    if products:
        parts.append(f", on {len(products)} product{'s' if len(products) != 1 else ''}")
    parts.append(". ")

    if scheme_type_code == "ABS":
        parts.append(f"Booking window {scheme.get('bookingStartDate') or '?'} - {scheme.get('bookingEndDate') or '?'}. ")

    slabs = scheme.get("slabs") or []
    if slabs:
        slab_txt = " | ".join(f"{_slab_range_text(s, basis, unit)} -> {_slab_rate_text(s, discount_type, unit)}" for s in slabs)
        parts.append(f"Discount by {_SLAB_BASIS_LABELS.get(basis, 'quantity')}: {slab_txt}")
    else:
        parts.append("Discount by quantity: no slabs set")

    if scheme.get("orderingStartDate") and scheme.get("orderingEndDate"):
        parts.append(f"; on purchases {scheme['orderingStartDate']} - {scheme['orderingEndDate']}")

    benefit_channel = scheme.get("benefitChannel")
    if benefit_channel:
        parts.append(f". Paid as {str(benefit_channel).lower().replace('_', ' ')}")
    if scheme.get("benefitPassDate"):
        parts.append(f" by {scheme['benefitPassDate']}")

    max_discount = _fmt_amount(scheme.get("maxDiscountPerUser"))
    if max_discount:
        parts.append(f". Max ₹{max_discount} per DC")

    parts.append(".")
    return "".join(parts)


def resolve_node_state_names(client: Any, node_ids: List[str], state_ids: List[str]) -> Dict[str, Dict[str, str]]:
    """Resolves this API's numeric node/state IDs (rules[].nodeIds / rules[].stateIds) to
    the Node/State NAMES this app's own DC data uses (DC_Master_Normalized carries Node/
    State as names, never common_salesoffice.id/common_state.id) -- the same
    input_backend_db.public.common_salesoffice/common_state join
    _sql_scheme_description_cards' own node_geo/state_txt CTEs use. `client` is a
    Redshift client (agent.get_client()'s return value), supplied by the caller rather
    than created here -- this module has no Redshift client of its own, by design (see
    this module's own docstring: it's a standalone building block, not wired into the
    generation pipeline, so it doesn't assume Redshift is even needed for every use)."""
    result: Dict[str, Dict[str, str]] = {"nodes": {}, "states": {}}
    numeric_node_ids = [str(int(n)) for n in node_ids if str(n).strip().isdigit()]
    numeric_state_ids = [str(int(s)) for s in state_ids if str(s).strip().isdigit()]
    if numeric_node_ids:
        rows = client.execute_sql(agent.INPUT_BACKEND_DB_ID, f"SELECT id, name FROM common_salesoffice WHERE id IN ({','.join(numeric_node_ids)})")
        result["nodes"] = {str(r["id"]): r["name"] for r in rows}
    if numeric_state_ids:
        rows = client.execute_sql(agent.INPUT_BACKEND_DB_ID, f"SELECT id, name FROM common_state WHERE id IN ({','.join(numeric_state_ids)})")
        result["states"] = {str(r["id"]): r["name"] for r in rows}
    return result


def get_active_discount_schemes(
    client: Optional["agent.DiscountServiceClient"] = None,
    resolve_names: bool = False,
    redshift_client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Top-level entry point -- fetches every currently-active DC-facing scheme from the
    live Discount Service API and attaches a generated_description to each, shaped to
    mirror the rows _fetch_scheme_description_cards() returns (scheme_id/scheme_name/
    generated_description/node_names_raw/state_names_raw/...) so a future caller could
    plug this in without a shape change -- but this function is NOT called from the
    generation pipeline today (see this module's docstring: "supplement, don't
    replace"). Any failure (not configured, HTTP error, node/state resolution failure)
    is folded into the returned "exceptions" list rather than raised, matching this
    codebase's standard convention for optional live sources (see
    planning.product_cohort.get_focus_product_campaign_targets).

    resolve_names/redshift_client: optional -- when True, also resolves each scheme's
    rule node/state IDs to names via a live Redshift lookup (same source
    _sql_scheme_description_cards uses). Off by default so a caller that only wants the
    REST API's own data doesn't pay for a Redshift round trip; when True and
    redshift_client isn't supplied, this calls agent.get_client() itself."""
    dsc = client or agent.DiscountServiceClient()
    result: Dict[str, Any] = {"schemes": [], "exceptions": []}

    if not dsc.configured:
        result["exceptions"].append({
            "source": "DiscountService", "reason_code": "Discount_Service_Not_Configured",
            "detail": "DISCOUNT_SERVICE_CLIENT_ID/DISCOUNT_SERVICE_CLIENT_SECRET not set",
        })
        return result

    try:
        raw_schemes = dsc.fetch_active_schemes()
    except Exception as e:
        result["exceptions"].append({"source": "DiscountService", "reason_code": "Discount_Service_Fetch_Failed", "detail": f"{type(e).__name__}: {e}"})
        return result

    name_map: Dict[str, Dict[str, str]] = {"nodes": {}, "states": {}}
    if resolve_names:
        all_node_ids = sorted({n for s in raw_schemes for r in (s.get("rules") or []) for n in (r.get("nodeIds") or [])})
        all_state_ids = sorted({st for s in raw_schemes for r in (s.get("rules") or []) for st in (r.get("stateIds") or [])})
        try:
            rc = redshift_client or agent.get_client()
            name_map = resolve_node_state_names(rc, all_node_ids, all_state_ids)
        except Exception as e:
            result["exceptions"].append({"source": "DiscountService", "reason_code": "Node_State_Resolution_Failed", "detail": f"{type(e).__name__}: {e}"})

    for scheme in raw_schemes:
        rules = scheme.get("rules") or []
        node_ids = sorted({n for r in rules for n in (r.get("nodeIds") or [])})
        state_ids = sorted({st for r in rules for st in (r.get("stateIds") or [])})
        node_names = [name_map["nodes"].get(n, f"node {n}") for n in node_ids] if node_ids else []
        state_names = [name_map["states"].get(s, f"state {s}") for s in state_ids] if state_ids else []

        result["schemes"].append({
            "scheme_id": scheme.get("schemeId"),
            "scheme_name": scheme.get("name"),
            "scheme_type": _SCHEME_TYPE_LABELS.get(scheme.get("discountingSchemeType"), scheme.get("discountingSchemeType")),
            "generated_description": build_generated_description(scheme),
            "node_names_raw": ",".join(node_names) if node_names else None,
            "state_names_raw": ",".join(state_names) if state_names else None,
            "booking_end": scheme.get("bookingEndDate"),
            "max_discount_per_dc": scheme.get("maxDiscountPerUser"),
        })

    return result
