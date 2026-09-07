"""The Pitching Agent -- generates a per-task sales pitch (PitchScript), activated
automatically right after DailyTask rows are created for a PlanRun (see
planning.services.generate_plan_for_scope). Config-driven from pitch_config/'s 5 CSVs
via planning.pitch_config_loader.

Real scoping note, confirmed against se_daily_plan_agent.py's own PURPOSE_BY_OBJECTIVE:
the task engine only ever produces "Sale" or "Promise To Pay / Collection" (single or
combined) as a real Purpose -- the other 3 Purposes/scripts in pitch_config
(Promise To Bill (P2B), Query Resolution, Stock at DC) can't be reached by any task this
engine currently generates. This module still builds full generic lookup/composition for
all 12 script cases (5 single + 7 combo) so it's ready if the task engine is ever
extended, but only ~3 combinations are live-testable today.

Missing-data handling: a talking point is included ONLY if its underlying data is both
(a) applicable per pitch_config's Applicable-Sources list for that Purpose, AND (b)
actually present in this run's pulled data. S4 (Current Inventory) is never included --
not a per-run skip, structurally absent, confirmed exhaustively by the normalization
doc that no DC-level data source exists for it anywhere in this system. S2b (Suggested
Discount, wired 2026-08-24) is a real per-run skip like any other source -- shown when
this DC's own coupon_analysis history and/or its block/node peers' has a real discount
figure for the #1 recommended product, silently absent (not fabricated) when neither
does. This is the same honest-degrade discipline as everywhere else in this codebase --
never fabricate a number to fill a talking point, and never claim something is
impossible once it stops being true.

Sale + Promise To Pay / Collection combo, built directly off the DC Visit Pitch
(Multi-Purpose) sheet's own worked example (that sheet's "Promise To Pay / Collection
+ Sale" row), not the generic single-block composer below -- see
_compose_sale_ptp_combo(). That sheet structures a combined visit as two
section-labeled segments ("— कलेक्शन हिस्सा —" / "— सेल्स हिस्सा —"), sequenced by its
own stated rule: collection-led with sales folded in after when a real overdue exists
(its Sequence Rationale: "पहले पेमेंट की बात करना ज़रूरी है क्योंकि ओवरड्यू है"), else
sales-led with billing folded in after (the same structural idea as its "Sale +
Promise To Bill (P2B)" row -- P2B there plays the same role a not-yet-overdue,
in-cycle Outstanding balance plays here). This is the one combo the task engine
actually produces live; every other Purpose/combo still goes through the generic
composer, unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .models import DailyTask, PitchScript, PlanRun
from .pitch_config_loader import get_pitch_config

logger = logging.getLogger(__name__)

# --- Hindi Ask/Wish phrasing per single Purpose ------------------------------------------
# The CSVs' own "What to Ask"/"What to Wish" columns are English guidance on WHAT ground
# to cover, not Hindi text to insert verbatim -- inserting them directly would leave the
# Ask/Wish sections in English while only the Tell section (built from real data below)
# is Hindi, breaking the "natural Hindi throughout" requirement. These phrase the same
# guidance in Hindi, in the tone of pitch_config's own worked examples (DC Visit Pitch
# (Hindi).csv) -- content-matched to that file's Ask/Wish columns, not copied verbatim
# (those carry fabricated DC-specific numbers this module never reuses).
_ASK_HINDI = {
    "Promise To Bill (P2B)": "इस बार बिलिंग में कोई दिक्कत तो नहीं आ रही? स्टॉक मूवमेंट सही है या पेमेंट साइकिल में कोई अड़चन है?",
    "Promise To Pay / Collection": "ये पेमेंट अभी तक पेंडिंग है — कोई खास वजह है क्या? कहीं फंड की दिक्कत तो नहीं?",
    "Query Resolution": "आपने जो कंप्लेंट डाली थी, उसमें एग्जैक्टली क्या दिक्कत आ रही है?",
    "Sale": "इस सीजन में क्या चल रहा है, कौन सी चीज़ की सबसे ज़्यादा डिमांड आ रही है? कुछ शॉर्टेज तो नहीं है स्टॉक में?",
    "Stock at DC": "ज़रा स्टॉक चेक कर लेते हैं — कौन सा आइटम ज़्यादा पड़ा है और कौन सा कम चल रहा है?",
}
_WISH_HINDI = {
    "Promise To Bill (P2B)": "तो बताइए — बिलिंग कब तक कर पाएंगे, एक पक्की तारीख और अमाउंट बता दीजिए।",
    "Promise To Pay / Collection": "बताइए, किस तारीख तक ये पेमेंट क्लियर कर पाएंगे — एक पक्की तारीख और अमाउंट बता दीजिए।",
    "Query Resolution": "मैं आज ही इसे रिज़ॉल्व करने की कोशिश करता हूं, अगर आज नहीं हुआ तो कल तक का टाइमलाइन बता दूंगा।",
    "Sale": "चलिए आज ही एक ऑर्डर बुक कर लेते हैं जो सीधे प्राइवेट लेबल टारगेट को आगे बढ़ाए।",
    "Stock at DC": "ठीक है, जो कम है उसका रीऑर्डर लगवा देता हूं और जो ज़्यादा है वो नोट कर लेता हूं।",
}

# --- Per-source Hindi talking-point builders -------------------------------------------
# Each takes the per-task context dict and returns (sentence, source_code) or None if
# that source's data isn't available for this task. "Available" and "applicable" are
# checked separately by _compose() -- a builder returning None here is what actually
# causes a source to be skipped, not a config decision alone.

def _tp_outstanding(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    outstanding = ctx.get("present_outstanding")
    if outstanding is None:
        return None
    overdue = ctx.get("present_overdue")
    # dc_datamart's weighted_avg_repayment_days -- matches the DC Visit Pitch
    # (Multi-Purpose) sheet's "आमतौर पर आप X दिन में क्लियर कर देते हैं" pattern. <= 0
    # isn't a genuine "pays same-day" signal -- confirmed live the DCs showing 0 are
    # exactly the ones whose entire balance is currently overdue (no completed
    # repayment cycle to average over), so it's omitted rather than fabricate a false
    # reassurance, same honest-degrade discipline as every other talking point here.
    avg_days = ctx.get("avg_repayment_days")
    avg_days_note = f" आमतौर पर आप {avg_days:.0f} दिन के अंदर पेमेंट क्लियर कर देते हैं।" if avg_days and avg_days > 0 else ""
    if overdue:
        aging = ctx.get("overdue_aging_bucket")
        aging_note = f" ({aging})" if aging else ""
        text = f"अभी आपका आउटस्टैंडिंग ₹{outstanding:,.0f} है, जिसमें से ₹{overdue:,.0f}{aging_note} ओवरड्यू है।{avg_days_note}"
    else:
        text = f"अभी आपका आउटस्टैंडिंग ₹{outstanding:,.0f} है, लेकिन अभी कुछ भी ओवरड्यू नहीं है — आप टाइम पर हैं।{avg_days_note}"
    return text, "S5"


def _tp_ytd_pl(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    val = ctx.get("ytd_private_label")
    if not val:
        return None
    return f"इस साल का आपका प्राइवेट लेबल सेल अभी तक ₹{val:,.0f} हुआ है।", "S8"


def _tp_club_standing(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """DC Club/Scheme motivation, added 2026-09-07 (explicit user request, "add the dc
    club motivation part and status"). Not one of pitch_config's own S1-S8 Applicable
    Sources for the Sale purpose (that column -- DC Visit Pitch Scripts.csv -- lists only
    S1/S2a/S2b/S3/S4/S7/S8), but the same CSV's "Dehaat Center Ko Jaano (Heading)" column
    for Sale explicitly says "Who: Business Area Strength + Turnover + Scheme Tier" --
    Scheme Tier IS part of the confirmed spec for this purpose, it was just documented as
    belonging to the DC Card shown before the pitch (planning.dc_card._scheme_standing),
    never surfaced inside the pitch script's own Ask/Tell/Wish text. Added here as a
    motivational hook the SE can use directly in the Sale conversation, not a duplicate
    of the DC Card -- phrased as "what you get" rather than the DC Card's plainer status
    statement. Same club dict, same normalize_dc_club() cases as dc_card.py's own
    _scheme_standing (shared extra_data_by_dc context) -- not applied to Promise To Pay /
    Collection, whose own CSV row cites only Repayment Cycle, no Scheme Tier mention."""
    club = ctx.get("club")
    if not club:
        return None
    if not club.get("Is_Club_Enrolled"):
        return "अभी क्लब स्कीम में एनरोल्ड नहीं हैं -- एनरोल होते ही टर्नओवर के हिसाब से TOD और रिवॉर्ड्स मिलने शुरू हो जाएंगे।", "Club"
    tier = club.get("Club_Tier")
    if tier:
        bits = [f"अभी {tier} टियर में हैं"]
        if club.get("TOD_Percent") is not None:
            bits.append(f"{club['TOD_Percent']:.2f}% TOD मिल रहा है")
        if club.get("Reward"):
            bits.append(f"रिवॉर्ड: {club['Reward']}")
        return ", ".join(bits) + "।", "Club"
    if club.get("Outstanding_Cleared") is False:
        eligible = club.get("Eligible_Tier_If_Outstanding_Cleared")
        if eligible:
            benefit_bits = []
            if club.get("Eligible_Tier_TOD_Percent_If_Cleared") is not None:
                benefit_bits.append(f"{club['Eligible_Tier_TOD_Percent_If_Cleared']:.2f}% TOD")
            if club.get("Eligible_Tier_Reward_If_Cleared"):
                benefit_bits.append(club["Eligible_Tier_Reward_If_Cleared"])
            benefit_note = f" ({', '.join(benefit_bits)})" if benefit_bits else ""
            return (
                f"अभी क्लब स्कीम में कोई टियर नहीं है (आउटस्टैंडिंग क्लियर नहीं है) -- "
                f"आउटस्टैंडिंग क्लियर होते ही {eligible} टियर मिल जाएगा{benefit_note} -- आज इसी बात पर ऑर्डर/पेमेंट पुश करें।",
                "Club",
            )
        return "अभी क्लब स्कीम में कोई टियर नहीं है (आउटस्टैंडिंग क्लियर नहीं है)।", "Club"
    turnover = club.get("Qualifying_Turnover")
    if turnover is not None:
        return f"अभी क्लब स्कीम में कोई टियर नहीं है (टर्नओवर ₹{turnover:,.0f} एंट्री थ्रेशोल्ड से कम है) -- आज का ऑर्डर टियर के करीब ले जाएगा।", "Club"
    return "अभी क्लब स्कीम में कोई टियर नहीं है (इस स्कीम वर्ष में कोई क्वालिफाइंग टर्नओवर दर्ज नहीं है)।", "Club"


def _tp_historical_purchase(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    py, ytd = ctx.get("purchase_last_fy"), ctx.get("purchase_ytd")
    parts = []
    if py:
        parts.append(f"पिछले साल आपने ₹{py:,.0f} का परचेज़ किया था")
    if ytd:
        parts.append(f"इस साल अभी तक ₹{ytd:,.0f} का परचेज़ हो चुका है")
    if not parts:
        return None
    return " और ".join(parts) + "।", "S3/S6/S7"


def _tp_last_discount(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """S2a as of the 2026-08-12 pitch_config re-export (was bare "S2" before that split
    -- see pitch_config_loader's module docstring)."""
    discount = ctx.get("last_discount")
    if discount is None:
        return None
    return f"पिछली बार आपको {discount:.0f}% का डिस्काउंट मिला था।", "S2a"


def _tp_suggested_discount(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """S2b, wired 2026-08-24 (planning.services._suggested_discount) -- combines this
    DC's own coupon_analysis discount history on the #1 recommended product with the
    block-then-node peer average discount on that same product. ₹/unit, NOT a
    percentage -- coupon_unit_benefit (the confirmed source) is a currency figure,
    distinct from S2a's % (a different table/formula, discount_price_unit vs
    price_unit) -- never conflated into the same sentence. Only shown when a real
    recommended product exists, since the discount is FOR that specific product, same
    dependency S1's own sentence has."""
    discount = ctx.get("suggested_discount")
    products = ctx.get("recommended_products") or []
    if discount is None or not products:
        return None
    return f"आपकी और आसपास के दुकानदारों की हिस्ट्री के हिसाब से, {products[0]['name']} पर अभी ₹{discount:.0f} प्रति यूनिट डिस्काउंट सजेस्ट हो रहा है।", "S2b"


def _format_product_list(products: List[Dict[str, Any]]) -> str:
    """Renders recommended_products (planning.services' _peer_stats/
    _attach_nearby_product_recommendations, 0-5 items, highest value first) as one
    "- "-prefixed line per product, newline-joined -- CHANGED 2026-09-07 (explicit user
    request, "these thing also in pointer"): was one comma-joined clause ('NAME (Brand:
    X, Sub-category: Y) (₹V), NAME2 (...), ...') that read as a single dense run-on
    sentence with up to 5 real products in it, same class of issue as the multi-
    talking-point [बताना] join fixed earlier the same day (see _tell_lines). Callers
    (_tp_block_comparison) embed this multi-line result inside their own sentence text;
    _tell_lines splits on "\\n" before deciding how to bullet the overall Tell block, so
    each product surfaces as its own bullet rather than one clause of a longer one."""
    parts = []
    for p in products:
        bits = []
        if p.get("brand"):
            bits.append(f"Brand: {p['brand']}")
        if p.get("sub_category"):
            bits.append(f"Sub-category: {p['sub_category']}")
        if p.get("business_segment"):
            bits.append(f"Segment: {p['business_segment']}")
        enrichment = f" ({', '.join(bits)})" if bits else ""
        value_note = f" (₹{p['value']:,.0f})" if p.get("value") else ""
        parts.append(f"- {p['name']}{enrichment}{value_note}")
    return "\n".join(parts)


def _tp_block_comparison(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """recommended_products (planning.services, widened 2026-08-18 to up to 5 products
    per direct instruction -- was a single block_top_product before) drives this talking
    point. scope on the first item tells you which tier actually produced the list:
    "block"/"node" (this DC's own dominant_category, peer-purchase ranked) or
    "nearby_radius"/"nearby_node" (planning.services._attach_nearby_product_
    recommendations' geographic fallback -- this DC's own block+node peers had nothing,
    widened outward rather than showing nothing, per direct instruction 2026-08-18).
    Falls back to category-average-only phrasing when block_category_avg exists but
    recommended_products doesn't (a real, if rare, granularity gap between the
    category-total and product-level peer queries). None if neither exists."""
    products = ctx.get("recommended_products") or []
    block_avg, category = ctx.get("block_category_avg"), ctx.get("dominant_category")
    if products:
        scope = products[0].get("scope")
        if scope in ("block", "node"):
            scope_label = "नोड" if scope == "node" else "ब्लॉक"
            dc_amt = ctx.get("dc_category_purchase") or 0
            return (
                f"आपके {scope_label} में बाकी दुकानदारों ने इस महीने {category} में औसतन ₹{block_avg:,.0f} का बिज़नेस किया है -- "
                f"सबसे ज़्यादा बिकने वाले प्रोडक्ट्स ({scope_label} में):\n"
                f"{_format_product_list(products)}\n"
                f"आपकी तरफ से अभी तक ₹{dc_amt:,.0f} हुआ है।",
                "S1",
            )
        # Geographic fallback -- this DC's own block+node peers had nothing to rank from
        # (either no dominant_category at all, or a category with zero product-level
        # peer data), so this widened outward rather than showing nothing.
        basis_label = "आसपास के (200km के अंदर) DCs" if scope == "nearby_radius" else "आसपास के नज़दीकी Nodes"
        return (
            f"इस DC/ब्लॉक/नोड में इस महीने कोई खरीद डेटा नहीं है -- {basis_label} में लोकप्रिय प्रोडक्ट्स के आधार पर सुझाव:\n"
            f"{_format_product_list(products)}",
            "S1",
        )
    if not block_avg or not category:
        return None
    scope_label = "नोड" if ctx.get("peer_comparison_scope") == "node" else "ब्लॉक"
    dc_amt = ctx.get("dc_category_purchase") or 0
    return (
        f"आपके {scope_label} में बाकी दुकानदारों ने इस महीने {category} में औसतन ₹{block_avg:,.0f} का बिज़नेस किया है, "
        f"आपकी तरफ से अभी तक ₹{dc_amt:,.0f} हुआ है।",
        "S1",
    )


# Data-source code -> builder. Order here is the default Tell-section order when no
# purpose-specific sequencing rule applies (see _order_for_purposes()).
_TALKING_POINTS = {
    "S1": _tp_block_comparison,
    "S2a": _tp_last_discount,
    "S2b": _tp_suggested_discount,
    "S3": _tp_historical_purchase,
    "S5": _tp_outstanding,
    "S6": _tp_historical_purchase,
    "S7": _tp_historical_purchase,
    "S8": _tp_ytd_pl,
    # "Club" is NOT one of pitch_config's own S1-S8 sources (no CSV Applicable-Sources
    # entry) -- added 2026-09-07, see _tp_club_standing's own docstring for why it's
    # still wired in for Sale specifically. Registered here so it's reachable via the
    # same sentence_for()/_TALKING_POINTS.get() lookup every other code uses; never
    # reached through _applicable_sources() (CSV-driven), only appended explicitly by
    # _compose()/_compose_sale_ptp_combo() for the Sale purpose.
    "Club": _tp_club_standing,
}

# Human-readable label for codes with no CSV Applicable-Sources entry (currently just
# "Club" -- see _TALKING_POINTS's own comment) -- without this, cfg.data_source_labels.
# get(code, code) falls back to the bare code itself, and Data_Sources_Used would show
# the redundant "Club Club" instead of a real label.
_EXTRA_LABELS = {"Club": "DC Club / Scheme Standing"}


def _order_for_purposes(purposes: List[str], ctx: Dict[str, Any]) -> List[str]:
    """Which talking points to lead with, and in what order. Real rule for the one
    combo this engine can actually produce today (Sale + Promise To Pay / Collection):
    lead with Collection/Outstanding if there's a genuine overdue amount (matches the
    doc's own worked example -- acknowledge the overdue before adding a sales ask, so
    the visit doesn't read as payment-avoidant), otherwise lead with the Sale/PL push.
    For every other Purpose/combo (not reachable today), falls back to the fixed order
    S5, S8, S1, S2, S3 -- a reasonable default, not a specifically confirmed sequence."""
    default_order = ["S5", "S8", "S1", "S2a", "S3"]
    if "Promise To Pay / Collection" in purposes and "Sale" in purposes:
        if ctx.get("present_overdue"):
            return ["S5", "S1", "S2a", "S3", "S8"]
        return ["S8", "S1", "S2a", "S3", "S5"]
    return default_order


def _match_script(purpose_of_visit: str) -> Tuple[List[str], Optional[str], Optional[str]]:
    """Returns (purposes, matched_key_label, win_condition_or_rationale). purposes is
    the parsed list from DailyTask.purpose_of_visit; matched_key_label is what actually
    matched (for PitchScript.purpose_key), None if nothing in pitch_config matches at all
    (shouldn't happen given the confirmed taxonomy match, but never assumed)."""
    cfg = get_pitch_config()
    purposes = [p.strip() for p in purpose_of_visit.split(" + ") if p.strip()]
    if len(purposes) == 1:
        single = cfg.single_purpose.get(purposes[0])
        if single:
            return purposes, purposes[0], single["win_condition"]
        return purposes, None, None
    combo = cfg.combo.get(frozenset(purposes))
    if combo:
        return purposes, combo["purposes_combined_label"], combo["win_conditions"]
    # No exact combo row for this exact set -- fall back to the union of each individual
    # purpose's own single-purpose script rather than failing to generate a pitch at all.
    return purposes, " + ".join(purposes) + " (no combo script -- composed from single-purpose scripts)", None


def _applicable_sources(purposes: List[str]) -> List[str]:
    """Union of each individual purpose's Applicable Sources (file #1) -- file #2 (combos)
    has no S1-S8 columns of its own, so this is the defensible source of truth even for
    a matched combo."""
    cfg = get_pitch_config()
    sources: set = set()
    for p in purposes:
        entry = cfg.single_purpose.get(p)
        if entry:
            sources |= entry["sources"]
    return sorted(sources)


def _tell_lines(sentences: List[str]) -> List[str]:
    """Renders a [बताना] (Tell) block's talking points as script lines, added 2026-09-07
    per direct instruction ("things in pointer for sale and collection") -- previously
    every sentence was space-joined into one dense run-on paragraph under a single
    [बताना] label (e.g. product recommendation + suggested discount + purchase trend +
    YTD target all mashed together for Sale, or S3/S5/S6 for Collection), hard to scan
    at a glance same as the DC Card's own product-list join fixed earlier. A single
    point still renders inline on the [बताना] line itself (no bullet needed for one
    point); 2+ points get their own "- "-prefixed line each, with [बताना] on its own
    line above them -- PitchPanel.tsx's parseScript() detects an empty-text label line
    followed by "- "-prefixed lines and renders them as a bullet list, same convention
    DCCardPanel.tsx's parseSectionItems() already uses for its own bulleted sections.

    A "point" isn't always one whole sentence from the caller's list -- a single sentence
    can itself be multi-line (_tp_block_comparison's product recommendation embeds
    _format_product_list's own "- "-per-product lines, added same day per direct
    instruction "these thing also in pointer"), so every sentence is split on "\\n" first
    and each resulting line counted as its own point, rather than nesting a whole
    product list inside one bullet of the outer list."""
    points = [line for s in sentences for line in s.split("\n") if line]
    if not points:
        return []
    if len(points) == 1:
        return [f"[बताना] {points[0]}"]
    return ["[बताना]"] + [p if p.startswith("- ") else f"- {p}" for p in points]


def _compose_sale_ptp_combo(task: DailyTask, ctx: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    """DC Visit Pitch (Multi-Purpose) sheet's own "Promise To Pay / Collection + Sale"
    worked example, structurally: two section-labeled segments, sequenced by whether a
    real overdue amount exists. See module docstring for the sheet's stated rationale."""
    cfg = get_pitch_config()
    dc_name = (task.dc_name or "").strip() or "जी"
    overdue = ctx.get("present_overdue") or 0
    outstanding = ctx.get("present_outstanding")
    aging = ctx.get("overdue_aging_bucket")

    used: List[str] = []
    skipped: List[str] = []

    def sentence_for(code: str) -> Optional[str]:
        builder = _TALKING_POINTS[code]
        label = cfg.data_source_labels.get(code) or _EXTRA_LABELS.get(code, code)
        result = builder(ctx)
        if result is None:
            skipped.append(f"{code} {label} (no data available this run)")
            return None
        text, _ = result
        used.append(f"{code} {label}")
        return text

    # Sales talking points -- same reachable sources as the generic composer, in the
    # sheet's own worked-example order (block/peer comparison, suggested discount on
    # that same product, historical purchase trend, then YTD-vs-target). S2b wired
    # 2026-08-24, right after S1 since it's a discount ON the product S1 just named.
    # Club wired 2026-09-07, last -- a motivational closer once the product ask and
    # numbers are already on the table, not competing with them for attention.
    sales_sentences = [s for s in (sentence_for(c) for c in ("S1", "S2b", "S3", "S8", "Club")) if s]

    skipped.append("S4 Current Inventory (no DC-level data source exists anywhere in this system)")

    lines: List[str] = []
    if overdue > 0:
        # Collection-led -- sheet's Sequence Rationale: acknowledge + get a commitment
        # on the overdue first, then fold the sales ask into the same conversation so
        # it doesn't read as payment-only.
        lines.append(f"नमस्ते {dc_name}! कैसे हैं आप?")
        lines.append("")
        collection_sentence = sentence_for("S5")
        lines.append("— कलेक्शन हिस्सा —")
        aging_note = f" ({aging})" if aging else ""
        lines.append(f"[पूछना] {dc_name}, ₹{overdue:,.0f}{aging_note} का पेमेंट पेंडिंग है — कोई दिक्कत आ रही है क्या फंड की तरफ से?")
        if collection_sentence:
            lines.append(f"[बताना] {collection_sentence}")
        lines.append("[विश] बताइए, इस हफ्ते के अंदर कब तक क्लियर कर पाएंगे?")
        lines.append("")
        lines.append("— सेल्स हिस्सा (पेमेंट कमिट होने के बाद) —")
        lines.append("[पूछना] वैसे इस सीजन में क्या चल रहा है, किस चीज़ की डिमांड सबसे ज़्यादा आ रही है?")
        lines.extend(_tell_lines(sales_sentences))
        lines.append("[विश/क्लोज़] तो चलिए, पुराना पेमेंट क्लियर होते ही एक ऑर्डर भी साथ में डाल देते हैं ताकि स्टॉक टाइम पर आ जाए।")
    else:
        # Sales-led, no urgency to open with -- same structural idea as the sheet's
        # "Sale + Promise To Bill (P2B)" row: value first, billing folded in after.
        lines.append(f"नमस्ते {dc_name}! कैसे हैं आप, बिज़नेस का क्या हाल है?")
        lines.append("")
        lines.append("— सेल्स हिस्सा —")
        lines.append("[पूछना] इस सीजन में किस चीज़ की डिमांड सबसे ज़्यादा आ रही है?")
        lines.extend(_tell_lines(sales_sentences))
        lines.append("[विश] चलिए आज एक ऑर्डर बुक कर लेते हैं।")
        if outstanding:
            billing_sentence = sentence_for("S5")
            lines.append("")
            lines.append("— बिलिंग हिस्सा (सेल के बाद) —")
            lines.append(f"[पूछना] वैसे अभी का जो ₹{outstanding:,.0f} है, उसकी बिलिंग किस टाइम तक हो जाएगी?")
            if billing_sentence:
                lines.append(f"[बताना] {billing_sentence}")
            lines.append(f"[विश/क्लोज़] तो आज के नए ऑर्डर के साथ-साथ, पुराना ₹{outstanding:,.0f} भी इसी हफ्ते क्लियर कर दीजिएगा — दोनों साथ में सेटल हो जाएंगे।")
        else:
            skipped.append("S5 Outstanding (no outstanding balance to raise this run)")

    return "\n".join(lines).strip(), used, skipped


def _compose(task: DailyTask, ctx: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    purposes, matched_key, win_condition_or_rationale = _match_script(task.purpose_of_visit or "")
    if set(purposes) == {"Sale", "Promise To Pay / Collection"}:
        script, used, skipped = _compose_sale_ptp_combo(task, ctx)
        return script, used, skipped
    applicable = _applicable_sources(purposes)
    ordered = _order_for_purposes(purposes, ctx)
    ordered_codes = [c for c in ordered if c in applicable] + [c for c in applicable if c not in ordered]
    # Club, added 2026-09-07 -- not in pitch_config's own Applicable Sources (`applicable`
    # above is CSV-driven), so never reachable through the two list comprehensions above;
    # appended directly, last, only for a standalone Sale purpose (see _tp_club_standing's
    # docstring for why Sale specifically, not Promise To Pay / Collection).
    if "Sale" in purposes:
        ordered_codes = ordered_codes + ["Club"]

    cfg = get_pitch_config()
    used, skipped, tell_sentences = [], [], []
    # S3/S6/S7 all resolve to the same _tp_historical_purchase sentence (actual_code
    # "S3/S6/S7" regardless of which one triggered it) -- seen_labels maps that combined
    # code to whichever individual code (e.g. "S3") first produced the sentence, so a
    # later collision (e.g. "S6") can be recorded as merged into it rather than silently
    # vanishing. Bug fixed 2026-08-27: a later collision used to just `continue`,
    # dropping it from BOTH used and skipped -- pitch_config lists it as a real
    # applicable source for some Purposes (e.g. "Promise To Pay / Collection" lists both
    # S3 and S6), so it should always show up somewhere in the trace, never disappear
    # with zero record either way. The pitch text itself was never affected (no
    # duplicate sentence either way) -- this only restores the audit trail.
    seen_labels: Dict[str, str] = {}
    for code in ordered_codes:
        builder = _TALKING_POINTS.get(code)
        label = cfg.data_source_labels.get(code) or _EXTRA_LABELS.get(code, code)
        if not builder:
            # An applicable code pitch_config lists but this module has no builder for
            # yet -- recorded as skipped, not silently dropped, so a future pitch_config
            # addition can never vanish from the trace without a visible trace of why.
            skipped.append(f"{code} {label} (confirmed data source, no _TALKING_POINTS builder wired yet)")
            continue
        result = builder(ctx)
        if result is None:
            skipped.append(f"{code} {label} (no data available this run)")
            continue
        sentence, actual_code = result
        if actual_code in seen_labels:
            used.append(f"{code} {label} (same sentence as {seen_labels[actual_code]})")
            continue
        seen_labels[actual_code] = code
        tell_sentences.append(sentence)
        used.append(f"{code} {label}")

    # S4 (Current Inventory, structurally absent) no longer gets a hardcoded
    # unconditional skip line here -- it's now correctly reported (or not) by the
    # ordered_codes loop above, driven by each Purpose's real Applicable Sources
    # (matches Query Resolution/Stock at DC actually listing S4, while P2B/Promise To
    # Pay don't -- the old unconditional message claimed S4 was missing even on pitches
    # where it was never relevant, and duplicated the loop's own message on ones where
    # it was).

    dc_name = (task.dc_name or "").strip() or "जी"
    ask_texts = [_ASK_HINDI[p] for p in purposes if p in _ASK_HINDI]
    wish_texts = [_WISH_HINDI[p] for p in purposes if p in _WISH_HINDI]

    lines = [f"नमस्ते {dc_name}! कैसे हैं आप, दुकान का हाल-चाल बताइए?", ""]
    if ask_texts:
        lines.append("[पूछना] " + " ".join(ask_texts))
        lines.append("")
    if tell_sentences:
        lines.extend(_tell_lines(tell_sentences))
        lines.append("")
    if wish_texts:
        lines.append("[विश/क्लोज़] " + " ".join(wish_texts))

    return "\n".join(lines).strip(), used, skipped


def generate_pitches_for_plan_run(plan_run: PlanRun, extra_data_by_dc: Dict[str, Dict[str, Any]]) -> Tuple[int, List[Dict[str, str]]]:
    """Called automatically from generate_plan_for_scope() right after DailyTask rows
    exist for this run. extra_data_by_dc carries the newly-wired sources (S1/S2/S3/S6/S7)
    keyed by dc_id -- S5 (Outstanding) and S8 (YTD PL) are read directly off DailyTask's
    own already-persisted fields, not duplicated here. Returns (created_count, failures)
    -- each task is isolated in its own try/except so one bad DC's data can't blank out
    every other task's pitch in the same run (previously a single unhandled exception
    here aborted the whole loop, silently leaving every task after it with no PitchScript
    at all); failures is a list the caller can fold into its own run_exceptions."""
    created = 0
    failures: List[Dict[str, str]] = []
    for task in plan_run.tasks.filter(dc_id__isnull=False):  # Farmer Meeting tasks have no DC -- no pitch to generate
        try:
            ctx = dict(extra_data_by_dc.get(task.dc_id, {}))
            ctx["present_outstanding"] = task.present_outstanding
            ctx["present_overdue"] = task.present_overdue
            ctx["overdue_aging_bucket"] = task.overdue_aging_bucket
            ctx["ytd_private_label"] = task.ytd_private_label
            script, used, skipped = _compose(task, ctx)
            _, matched_key, _ = _match_script(task.purpose_of_visit or "")
            # Same list already folded into script's own S1 sentence via
            # _format_product_list - captured structured here too. Empty list means
            # neither this DC's own category-scoped peers nor the geographic fallback
            # had anything to recommend this run.
            PitchScript.objects.update_or_create(
                daily_task=task,
                defaults={
                    "purpose_key": matched_key or task.purpose_of_visit, "script_hindi": script,
                    "data_sources_used": used, "data_sources_skipped": skipped,
                    "recommended_products": ctx.get("recommended_products") or [],
                },
            )
            created += 1
        except Exception as e:
            logger.warning("PitchingAgent: failed to generate a pitch for DC %s (task %s): %s: %s", task.dc_id, task.id, type(e).__name__, e)
            failures.append({"dc_id": task.dc_id, "detail": f"{type(e).__name__}: {e}"})
    return created, failures
