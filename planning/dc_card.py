"""The DC Card (Preface) -- "Dehaat Center Ko Jaano" -- a second, complementary
pre-pitch briefing framework for the Pitching Agent (SE_DC_Data_Normalization_Agent_Prompt;
pitch_config's "DC Card (Preface)" CSV). Shown to the SE when they open a DC's card,
BEFORE any Ask/Tell/Wish pitch -- 3 sections, matching the CSV's own structure exactly:
1. कौन (Who), 2. DC कहां खड़ा है (Where DC Stands), 3. प्राइवेट लेबल (Private Label).

Generated automatically alongside PitchScript (planning/pitching.py) -- same trigger
point (planning.services.generate_plan_for_scope, right after DailyTask rows exist),
same per-DC-task cadence. NOT the same thing as Focus Product Campaign Targeting
(planning/product_cohort.py) -- the CSV is explicit that Targeting is a genuinely
different, product-first capability, not part of this DC-first card, so it isn't
referenced here.

Reuses planning/services.py's already-built extra_data_by_dc context (the same dict
Pitching reads) rather than re-fetching anything -- see that function's call site for
what's folded in: purchase_by_dc (S6/S7), avg_repayment_days, per_dc_category/
dominant_category/block_category_avg (S1), plus this feature's own two additions,
business_area_strength/business_area_strength_prior_year (Source 3h,
_sql_business_area_strength_detailed) and club (the raw
normalize_dc_club() row, for Scheme Standing).

Crop Type/Style (Section 2's DC-first "what crop does this DC serve" question) has NO
source anywhere in this pipeline -- confirmed exhaustively (a "Product Cohort" API was
investigated and runs the opposite direction, product-first). Always skipped, never
guessed -- see _where_dc_stands_section().

Section 3 (Private Label) -- opportunistic Product Cohort cross-reference, wired
2026-08-14: Section 3's own DC-first "recommended product" question can't be answered
DIRECTLY by Product Cohort (still product-first -- see above), but Step 3's OUTPUT is a
DC cohort for whichever Focus Product a caller already ran (planning.product_cohort,
opt-in per PlanRun -- see FocusProductTargetRun). So: if this DC happens to appear in
ANY FocusProductTargetRun's Step 3 cohort from the same plan_date (any scope/PlanRun --
a Focus Product evaluated for a different scope the same day is still a real signal for
this DC), use that as the recommendation instead of the category-level fallback. Most
DCs won't match anything, since Focus Product Targeting only ever runs for whatever
material a caller explicitly asked about, not automatically for every material -- see
_build_focus_product_match_index().

Section 3 fallback chain, full order (confirmed 2026-08-18): Product Cohort match ->
this DC's own block peers -> node peers -> geographic (every other DC within 200km,
then the nearest 10 Nodes by centroid distance if even that finds nothing) -- see
services._attach_nearby_product_recommendations(). The geo tier only ever fires when
every closer, more locally-relevant signal has genuinely come up empty, and is absent
entirely (not an empty recommendation) when nothing is found anywhere nearby either.

Every tier past Product Cohort match is PRIVATE LABEL-only (confirmed 2026-08-18, per
direct instruction) -- a section titled Private Label must only ever name a real
business_segment == "PRIVATE LABEL" product, never a BRANDED one just because it
outsold PL products among the same peers. Reads recommended_products_pl, a separately
re-ranked list from Pitching's own (any-segment) recommended_products -- see
planning.services._peer_stats' segment param."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library

from .models import DailyTask, DCCard, FocusProductTargetRun, PlanRun
from .pitching import _format_product_list

logger = logging.getLogger(__name__)


def _render_business_area_subcats(subcats: List[Dict[str, Any]]) -> List[str]:
    """One line per sub-category: total, then its Branded/Private Label split with
    share% and product-wise detail (up to 3 products per segment, highest value first --
    matches the worked example in pitch_config's DC Card (Preface) CSV; a DC with more
    real products than that just shows its top 3, never all of them inline)."""
    lines = []
    for sc in subcats:
        seg_bits = []
        for seg in sc["segments"]:
            prod_bits = [f"{p['name']} (₹{p['value']:,.0f})" for p in seg["products"][:3]]
            seg_bits.append(f"{seg['segment']} ₹{seg['total']:,.0f} ({seg['share_of_subcat']:.0f}%) -- {', '.join(prod_bits)}")
        lines.append(f"{sc['sub_category']} ₹{sc['total']:,.0f}: {' | '.join(seg_bits)}")
    return lines


def _business_area_strength(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """business_area_strength / business_area_strength_prior_year (services.
    _sql_business_area_strength_detailed + _build_business_area_tree) -- rebuilt
    2026-08-22 per the confirmed corrected spec (SE_DC_Data_Normalization_Agent_Prompt
    v3 re-sync + production Pitch Playbook screenshot). Replaces the old top-5/trailing-
    12-month/single-number version: now EVERY sub-category this fiscal-YTD, each split
    Branded vs. Private Label with a share% and product-wise detail, paired with the
    identical structure over the same window last fiscal year so sub-category-level
    trend is visible, not just one aggregate figure."""
    current = [sc for sc in (ctx.get("business_area_strength") or []) if sc.get("sub_category")]
    if not current:
        return None
    dc_total = sum(sc["total"] for sc in current)
    branded_total = sum(seg["total"] for sc in current for seg in sc["segments"] if seg["segment"] == "Branded")
    pl_total = dc_total - branded_total
    header = (
        f"कुल (इस साल YTD): ₹{dc_total:,.0f} -- Branded ₹{branded_total:,.0f} "
        f"({branded_total / dc_total:.0%}) / Private Label ₹{pl_total:,.0f} ({pl_total / dc_total:.0%})"
    )
    lines = [header] + _render_business_area_subcats(current)

    prior = [sc for sc in (ctx.get("business_area_strength_prior_year") or []) if sc.get("sub_category")]
    if prior:
        prior_total = sum(sc["total"] for sc in prior)
        growth = f" (पिछले साल इसी अवधि में ₹{prior_total:,.0f} था)" if prior_total else ""
        lines.append(f"पिछला साल, इतनी ही अवधि में{growth}:")
        lines.extend(_render_business_area_subcats(prior))

    return "\n".join(lines), "Business_Area_Strength"


def _turnover_standing(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    py, ytd = ctx.get("purchase_last_fy"), ctx.get("purchase_ytd")
    club = ctx.get("club") or {}
    qualifying_turnover = club.get("Qualifying_Turnover")
    parts = []
    if py:
        parts.append(f"पिछले साल ₹{py:,.0f} का परचेज़ किया था")
    if ytd:
        parts.append(f"इस साल अभी तक ₹{ytd:,.0f} का परचेज़ हो चुका है")
    if not parts and qualifying_turnover is None and ctx.get("yoy_pl_growth_pct") is None:
        return None
    text = " और ".join(parts)
    if qualifying_turnover is not None:
        text += (", " if parts else "") + f"क्लब स्कीम Qualifying Turnover ₹{qualifying_turnover:,.0f} है"
    # YoY PL comparison (confirmed 2026-08-18, planning.services._yoy_pl_growth_
    # multiplier) -- PL-specific, distinct from the general purchase figures above.
    # Like-for-like window (same days elapsed into the fiscal year on both sides), not a
    # full prior-year total. None (not shown) when there's no real last-year baseline.
    yoy_pct = ctx.get("yoy_pl_growth_pct")
    if yoy_pct is not None:
        last_year_pl = ctx.get("ytd_pl_last_year")
        direction = "ज़्यादा" if yoy_pct >= 0 else "कम"
        text += ("; " if text else "") + f"PL सेल पिछले साल की इसी अवधि (₹{last_year_pl:,.0f}) से {abs(yoy_pct):.0%} {direction} है"
    return text.strip() + "।", "Turnover_Standing"


def _repayment_cycle(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    avg_days = ctx.get("avg_repayment_days")
    # <= 0 isn't a genuine "pays same-day" signal -- same convention as pitching.py's
    # _tp_outstanding (confirmed live: DCs showing 0 are the ones whose whole balance is
    # currently overdue, no completed repayment cycle to average over).
    if not avg_days or avg_days <= 0:
        return None
    return f"औसतन {avg_days:.0f} दिन में पेमेंट क्लियर करता है।", "Repayment_Cycle"


def _scheme_standing(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    club = ctx.get("club")
    if not club or not club.get("Is_Club_Enrolled"):
        return "क्लब स्कीम में एनरोल्ड नहीं है।", "Scheme_Standing"
    copper_threshold = agent.DC_CLUB_TIER_TABLE[-1][1]  # lowest tier, table ordered highest-to-lowest
    tier = club.get("Club_Tier")
    if not tier:
        if club.get("Outstanding_Cleared") is False:
            note = "क्लब स्कीम में है, लेकिन अभी कोई टियर नहीं -- आउटस्टैंडिंग क्लियर नहीं है (एलिजिबिलिटी की शर्त)।"
            eligible = club.get("Eligible_Tier_If_Outstanding_Cleared")
            if eligible:
                benefit_bits = []
                if club.get("Eligible_Tier_TOD_Percent_If_Cleared") is not None:
                    benefit_bits.append(f"{club['Eligible_Tier_TOD_Percent_If_Cleared']:.2f}% TOD")
                if club.get("Eligible_Tier_Reward_If_Cleared"):
                    benefit_bits.append(club["Eligible_Tier_Reward_If_Cleared"])
                benefit_note = f" ({', '.join(benefit_bits)})" if benefit_bits else ""
                note += f" आउटस्टैंडिंग क्लियर करने पर {eligible} टियर के लिए एलिजिबल हो जाएगा{benefit_note} -- यह पिच पॉइंट है।"
            else:
                # Bug fixed 2026-08-19: outstanding not cleared isn't necessarily the
                # ONLY blocker -- a DC with no/below-threshold turnover has nothing to
                # become eligible for even once outstanding clears. See
                # se_daily_plan_agent.dc_club_participation_text's own fix for the
                # English-string version of this same bug.
                turnover = club.get("Qualifying_Turnover")
                if turnover is None:
                    note += " इस स्कीम वर्ष में कोई क्वालिफाइंग टर्नओवर भी दर्ज नहीं है।"
                else:
                    note += f" टर्नओवर ₹{turnover:,.0f} भी Copper के ₹{copper_threshold:,.0f} एंट्री थ्रेशोल्ड से कम है।"
            return note, "Scheme_Standing"
        turnover = club.get("Qualifying_Turnover")
        if turnover is None:
            return "क्लब स्कीम में है, लेकिन अभी कोई टियर कन्फर्म नहीं है (इस स्कीम वर्ष में कोई क्वालिफाइंग टर्नओवर दर्ज नहीं है)।", "Scheme_Standing"
        return f"क्लब स्कीम में है, लेकिन अभी कोई टियर कन्फर्म नहीं है (टर्नओवर ₹{turnover:,.0f} Copper के ₹{copper_threshold:,.0f} एंट्री थ्रेशोल्ड से कम है)।", "Scheme_Standing"
    bits = [f"{tier} टियर"]
    if club.get("Zone"):
        bits.append(f"{club['Zone']} zone")
    if club.get("TOD_Percent") is not None:
        bits.append(f"{club['TOD_Percent']:.2f}% TOD")
    # Bug fixed 2026-08-19: an already-tiered DC never had its own reward shown, only
    # DCs still working towards one above -- see normalize_dc_club's Reward field.
    if club.get("Reward"):
        bits.append(club["Reward"])
    return ", ".join(bits) + "।", "Scheme_Standing"


def _upcoming_to_sell_proxy(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """PROXY only, not a real crop-calendar answer -- same last-year-purchase + current
    Block-trend combination the CSV itself describes, with the same honesty gap it
    flags: purchase_last_fy is the whole last fiscal year, not month-matched "same time
    last year" (no month-matched historical query exists in this pipeline)."""
    py = ctx.get("purchase_last_fy")
    category, block_avg = ctx.get("dominant_category"), ctx.get("block_category_avg")
    parts = []
    if py:
        parts.append(f"पिछले साल ₹{py:,.0f} का परचेज़ किया था")
    if category and block_avg:
        parts.append(f"अभी Block में {category} में औसतन ₹{block_avg:,.0f} का ट्रेंड चल रहा है")
    if not parts:
        return None
    return " और ".join(parts) + " -- यह एक अनुमान है, पक्का crop-calendar डेटा नहीं।", "Upcoming_To_Sell_Proxy"


def _build_focus_product_match_index(plan_date) -> Dict[str, Tuple[str, str]]:
    """dc_id -> (sentence, code) for every DC that shows up in ANY FocusProductTargetRun's
    Step 3 cohort from this plan_date. Built ONCE per generate_dc_cards_for_plan_run call
    (not per DC -- would be an N+1 query pattern otherwise). First match wins if a DC
    somehow appears in more than one run's cohort the same day.

    Confirmed live response shape 2026-08-14 (Step 3's schema was previously unconfirmed
    -- empty in the saved Postman collection): step_3 = {"status": "Success", "results":
    {"materialId", "nodeId", "totalDCs", "dcs": [{"partnerId" (matches this codebase's
    DC_ID format exactly), "name", "reasons": [...], "relatedProductsBought": [...],
    "relatedProductsBreakdown": [...], "totalQty", "totalValue"}, ...]}}."""
    index: Dict[str, Tuple[str, str]] = {}
    runs = FocusProductTargetRun.objects.filter(plan_run__plan_date=plan_date, step_3__isnull=False)
    for run in runs:
        results = (run.step_3 or {}).get("results") or {}
        for dc in results.get("dcs") or []:
            dc_id = str(dc.get("partnerId") or "")
            if not dc_id or dc_id in index:
                continue
            reasons = ", ".join(dc.get("reasons") or []) or "unspecified"
            related = dc.get("relatedProductsBought") or []
            related_note = f" (पहले से खरीदते हैं: {', '.join(related[:3])})" if related else ""
            index[dc_id] = (
                f"Product Cohort ने यह DC Focus Product (Material ID: {run.material_id}) के लिए टारगेट किया है -- "
                f"कारण: {reasons}{related_note}।",
                "Product_Cohort_Step3_Match",
            )
    return index


def _pl_recommendation(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Product Cohort's opportunistic Step 3 match (see module docstring) wins when
    present -- a real, product-named recommendation beats the category-level fallback.

    Bug fixed 2026-08-14: YTD_Private_Label (S8, DailyTask's own already-persisted
    field) used to only ever appear as a bonus sentence tacked onto the category
    comparison -- if dominant_category/block_category_avg weren't BOTH available (a real
    and common gap, same S1 sparsity the Pitching Agent already experiences), the ENTIRE
    section silently dropped to "no data available," discarding a real YTD PL figure
    that was sitting right there in ctx unused. Confirmed live: 3 of 5 real DCs checked
    had a genuine YTD_Private_Label value with an empty Private Label section anyway.
    The two signals are now independent -- either one present is enough to show
    something, and both together compose into one sentence when both exist.

    recommended_products_pl (planning.services, added 2026-08-18 per direct instruction)
    is the single source of truth for the product-name half of this section -- unlike
    Pitching's S1 (any segment, a general cross-sell signal), this section is titled
    Private Label and must only ever name a real business_segment == "PRIVATE LABEL"
    product, so it reads a segment-filtered list computed separately from Pitching's
    general recommended_products (planning.services._peer_stats' segment param re-ranks
    within PL only, rather than filtering the general top-5 -- see that function's own
    docstring for why). Still shares _format_product_list() with pitching.py so the
    bracket notes can't drift in wording. scope on the first item says which tier
    produced it: "block"/"node" (this DC's own dominant_category, peer-purchase ranked,
    PL-only) or "nearby_radius"/"nearby_node" (services._attach_nearby_product_
    recommendations' geographic fallback, PL-only -- fires only when this DC's own
    block+node peers had no PL purchase data at all to rank from, even if they had
    plenty of non-PL data). Falls back to category-average-only phrasing when
    block_category_avg exists but recommended_products_pl doesn't (this DC's category
    peers buy in that category, just not any PRIVATE LABEL product within it)."""
    match = ctx.get("product_cohort_match")
    if match:
        return match
    category, block_avg = ctx.get("dominant_category"), ctx.get("block_category_avg")
    products = ctx.get("recommended_products_pl") or []
    ytd_pl = ctx.get("ytd_private_label")
    parts: List[str] = []
    if products:
        scope = products[0].get("scope")
        if scope in ("block", "node"):
            scope_label = "नोड" if scope == "node" else "ब्लॉक"
            dc_amt = ctx.get("dc_category_purchase") or 0
            parts.append(
                f"सुझाई गई कैटेगरी: {category} -- इसमें {scope_label} में सबसे ज़्यादा बिकने वाले प्राइवेट लेबल प्रोडक्ट्स: "
                f"{_format_product_list(products)}। इस DC से इस कैटेगरी में अभी तक ₹{dc_amt:,.0f} का बिज़नेस हुआ है।"
            )
        else:
            # This DC's own purchases + block peers + node peers had no PRIVATE LABEL
            # purchase data (even if they had plenty of non-PL data) -- geographic
            # fallback (services._attach_nearby_product_recommendations, confirmed
            # 2026-08-18): 200km radius first, nearest Nodes only if that itself found
            # nothing. Absent entirely (not an empty list) when even that found nothing,
            # so this branch only ever fires with real PL products to show.
            basis_label = "आसपास के (200km के अंदर) DCs" if scope == "nearby_radius" else "आसपास के नज़दीकी Nodes"
            parts.append(
                f"इस DC/ब्लॉक/नोड में कोई प्राइवेट लेबल खरीद डेटा नहीं है -- {basis_label} में लोकप्रिय प्राइवेट लेबल प्रोडक्ट्स के आधार पर सुझाव: "
                f"{_format_product_list(products)}।"
            )
    elif category and block_avg:
        scope_label = "नोड" if ctx.get("peer_comparison_scope") == "node" else "ब्लॉक"
        dc_amt = ctx.get("dc_category_purchase") or 0
        parts.append(f"सुझाई गई कैटेगरी: {category} -- {scope_label} में औसतन ₹{block_avg:,.0f}, इस DC से अभी तक ₹{dc_amt:,.0f}।")
    if ytd_pl:
        parts.append(f"इस साल का PL सेल अभी तक ₹{ytd_pl:,.0f} हुआ है।")
    if not parts:
        return None
    return " ".join(parts), "PL_Recommendation"


def build_dc_card(task: DailyTask, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Returns the DCCard.objects.create()-ready field dict for one DailyTask."""
    dc_name = (task.dc_name or "").strip() or "यह DC"
    used: List[str] = []
    skipped: List[str] = []

    def run(label_hi: str, builder, section_lines: List[str]):
        result = builder(ctx)
        if result is None:
            skipped.append(f"{label_hi} (no data available this run)")
            return
        text, code = result
        used.append(code)
        section_lines.append(f"- {label_hi}: {text}")

    who_lines: List[str] = []
    run("Business Area Strength", _business_area_strength, who_lines)
    run("Turnover-wise Standing", _turnover_standing, who_lines)
    run("Repayment Cycle", _repayment_cycle, who_lines)
    run("Scheme Standing", _scheme_standing, who_lines)

    where_lines: List[str] = []
    # Crop Type/Style -- always skipped, no source anywhere in this pipeline (see module
    # docstring). Not run through the same builder pattern since there is no builder at
    # all to call, just a permanent, honest gap.
    skipped.append("Crop Type/Style (no confirmed source anywhere in this pipeline -- flag to business directly)")
    run("Upcoming to Sell (proxy)", _upcoming_to_sell_proxy, where_lines)

    pl_lines: List[str] = []
    run("Recommended Product & Brief", _pl_recommendation, pl_lines)

    who_section = "\n".join(who_lines) if who_lines else "(कोई डेटा उपलब्ध नहीं इस रन में)"
    where_dc_stands_section = "\n".join(where_lines) if where_lines else "(कोई डेटा उपलब्ध नहीं इस रन में)"
    private_label_section = "\n".join(pl_lines) if pl_lines else "(कोई डेटा उपलब्ध नहीं इस रन में)"

    card_hindi = "\n\n".join([
        f"{dc_name} -- Dehaat Center Ko Jaano",
        "1. कौन (Who)\n" + who_section,
        "2. DC कहां खड़ा है (Where DC Stands)\n" + where_dc_stands_section,
        "3. प्राइवेट लेबल (Private Label)\n" + private_label_section,
    ])

    # PL-only recommended_products_pl (planning/services.py, added 2026-08-18 per direct
    # instruction) -- NOT the same list as PitchScript's own recommended_products field
    # (that one is any segment, a general cross-sell signal; this section is titled
    # Private Label and must only ever name a real PRIVATE LABEL product). Only the
    # category/geographic fallback path, not product_cohort_match (Focus Product
    # Targeting's own pre-formatted override, opt-in and rare) - see that function's own
    # docstring for why the two paths exist. A card built from product_cohort_match
    # leaves this structured field empty even though private_label_section's prose has a
    # recommendation; same honest-degrade tradeoff as everywhere else in this module.

    return {
        "who_section": who_section,
        "where_dc_stands_section": where_dc_stands_section,
        "recommended_products": ctx.get("recommended_products_pl") or [],
        "private_label_section": private_label_section,
        "card_hindi": card_hindi,
        "data_sources_used": used,
        "data_sources_skipped": skipped,
    }


def generate_dc_cards_for_plan_run(plan_run: PlanRun, extra_data_by_dc: Dict[str, Dict[str, Any]]) -> Tuple[int, List[Dict[str, str]]]:
    """Called automatically from generate_plan_for_scope() right after
    generate_pitches_for_plan_run() -- same context dict, same DC-tied-tasks-only filter
    (Farmer Meeting tasks have no DC, no card to show). Returns (created_count, failures)
    -- each task isolated in its own try/except (same reasoning as
    pitching.generate_pitches_for_plan_run: one bad DC's data previously aborted every
    other task's card in the same run, not just that one task's)."""
    focus_match_index = _build_focus_product_match_index(plan_run.plan_date)
    created = 0
    failures: List[Dict[str, str]] = []
    for task in plan_run.tasks.filter(dc_id__isnull=False):
        try:
            ctx = dict(extra_data_by_dc.get(task.dc_id, {}))
            ctx["ytd_private_label"] = task.ytd_private_label
            ctx["product_cohort_match"] = focus_match_index.get(str(task.dc_id))
            fields = build_dc_card(task, ctx)
            DCCard.objects.update_or_create(daily_task=task, defaults=fields)
            created += 1
        except Exception as e:
            logger.warning("DCCardAgent: failed to generate a DC Card for DC %s (task %s): %s: %s", task.dc_id, task.id, type(e).__name__, e)
            failures.append({"dc_id": task.dc_id, "detail": f"{type(e).__name__}: {e}"})
    return created, failures
