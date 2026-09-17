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
import re
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from .ai_sales_forecast import build_ai_pitch
from .models import DailyTask, PitchScript, PlanRun
from .pitch_config_loader import get_pitch_config
from .pitch_context import ExtraDcContext

logger = logging.getLogger(__name__)

# How many build_ai_pitch() calls run concurrently in generate_pitches_for_plan_run
# (added 2026-09-17, explicit user request to speed up plan generation -- live-timed at
# 98s for a 5-task SE plan beforehand, almost entirely sequential LLM latency). Each
# call is pure network I/O (the Gemini/OpenRouter/Anthropic fallback chain), releases
# the GIL while waiting, and never touches the DB itself -- the PitchScript write
# happens afterward, sequentially, in the main thread, so this never introduces any
# Django-ORM/SQLite concurrency risk. Kept modest and env-overridable rather than
# one-thread-per-task: a STATE-scope run can have 100+ tasks, and running that many
# LLM calls at once risks tripping provider rate limits (each provider call already
# degrades gracefully on its own failure -- see build_ai_pitch's own fallback chain --
# but a rate-limit storm would just turn into a wall of degraded templated scripts for
# no speed benefit beyond this cap).
PITCH_GENERATION_MAX_WORKERS = int(os.environ.get("PITCH_GENERATION_MAX_WORKERS", "5"))

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


_HINDI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
_EN_MONTHS = {m: i + 1 for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
_UNIT_HINDI = {"kg": "किग्रा", "litre": "लीटर", "packet": "पैकेट", "unit": "यूनिट"}


def hindi_date(value: Any) -> Optional[str]:
    """'2026-10-10' / '10 Oct 2026' / a date -> '10 अक्टूबर 2026'; None when unparseable."""
    if value is None:
        return None
    from datetime import date as _date
    try:
        if isinstance(value, _date):
            d = value
        else:
            text = str(value).strip()
            parts = text.split()
            if len(parts) == 3 and parts[1][:3] in _EN_MONTHS:
                d = _date(int(parts[2]), _EN_MONTHS[parts[1][:3]], int(parts[0]))
            else:
                d = _date.fromisoformat(text[:10])
    except (TypeError, ValueError):
        return None
    return f"{d.day} {_HINDI_MONTHS[d.month - 1]} {d.year}"


def _hindi_unit(text: str) -> str:
    for en, hi in _UNIT_HINDI.items():
        text = re.sub(rf"/{en}\b", f"/{hi}", text)
        text = re.sub(rf"\b{en}\b", hi, text)
    return text


def _hindi_slab(slab: str, basis: Optional[str]) -> str:
    """One slab "cond -> ₹X/kg" from the scheme card's slabs_short into Hindi:
    '08 Aug 2026 to 07 Sep 2026 -> ₹50/kg' -> '8 अगस्त 2026 से 7 सितंबर 2026 तक बुकिंग पर ₹50/किग्रा'
    '20-50,000 kg -> ₹2.80/kg'             -> '20-50,000 किग्रा पर ₹2.80/किग्रा'.
    Anything it can't read is passed through with units translated, never dropped."""
    if "->" not in slab:
        return _hindi_unit(slab.strip())
    cond, rate = (x.strip() for x in slab.split("->", 1))
    rate = _hindi_unit(rate)
    if basis == "date" and " to " in cond:
        a, b = (x.strip() for x in cond.split(" to ", 1))
        ha, hb = hindi_date(a), hindi_date(b)
        if ha and hb:
            return f"{ha} से {hb} तक बुकिंग पर {rate}"
    return f"{_hindi_unit(cond)} पर {rate}"


def scheme_profit_hindi(scheme: Dict[str, Any]) -> Optional[str]:
    """The scheme's PROFIT, in Hindi, from the structured card facts (services.py
    attaches them as scheme["benefit"]: advance_per_unit, slabs, slab_basis,
    benefit_channel, max_discount_per_dc) -- added 2026-09-17, explicit user request
    on a pointer that named both schemes with only their validity: "add the profit of
    these scheme". E.g. "₹100/किग्रा एडवांस पर बुकिंग; छूट: 8 अगस्त 2026 से 7 सितंबर 2026
    तक बुकिंग पर ₹50/किग्रा, 8 सितंबर 2026 से 15 सितंबर 2026 तक बुकिंग पर ₹40/किग्रा; क्रेडिट
    नोट से भुगतान; अधिकतम ₹10,00,000 प्रति DC". None when the card has no slab -- the
    caller then falls back to the plain validity line rather than inventing a figure."""
    b = scheme.get("benefit") or {}
    slabs = (b.get("slabs") or "").strip()
    if not slabs or slabs == "no slabs set":
        return None
    parts = []
    if b.get("advance_per_unit"):
        parts.append(f"{_hindi_unit(str(b['advance_per_unit']))} एडवांस पर बुकिंग")
    parts.append("छूट: " + ", ".join(_hindi_slab(x, b.get("slab_basis")) for x in slabs.split("|") if x.strip()))
    channel = (b.get("benefit_channel") or "").lower()
    if "credit" in channel:
        parts.append("क्रेडिट नोट से भुगतान")
    elif channel:
        parts.append(f"{channel} से भुगतान")
    mx = b.get("max_discount_per_dc")
    try:
        if mx and float(mx) > 0:
            parts.append(f"अधिकतम ₹{inr_group(float(mx))} प्रति DC")
    except (TypeError, ValueError):
        pass
    return "; ".join(parts)


def inr_group(amount: float) -> str:
    """Indian digit grouping: 1000000 -> '10,00,000'; 2500 -> '2,500'."""
    n = f"{int(round(amount))}"
    if len(n) <= 3:
        return n
    head, tail = n[:-3], n[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def scheme_pointer_hindi(scheme: Dict[str, Any]) -> str:
    """A complete factual बताना pointer for one scheme: exact name, validity, and its
    profit (scheme_profit_hindi) when the card carries one. Shared by the templated
    fallback here and the AI pitch's missing-scheme guarantee
    (ai_sales_forecast._ensure_scheme_pointers)."""
    name = (scheme.get("name") or "").strip()
    until = hindi_date(scheme.get("valid_until"))
    validity = f" ({until} तक)" if until else ""
    profit = scheme_profit_hindi(scheme)
    if profit:
        return f"'{name}'{validity}: {profit} -- आज ही बुकिंग दर्ज कराकर यह लाभ पक्का करें।"
    return f"'{name}' योजना अभी सक्रिय है{validity} -- इसके तहत आज ही एडवांस बुकिंग दर्ज कराएं।"


def _tp_active_schemes(ctx: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """One "- " line per currently-active Sales/ABS Scheme for this DC's Node (added
    2026-09-17, explicit user request on the AI pitch - "all eligible scheme should in
    batana part" - mirrored here so the templated fallback lists every scheme too;
    until now the template mentioned none, only the DC Card did). Each line carries
    the scheme's profit from its card facts (scheme_pointer_hindi). Multi-line so
    _tell_lines splits it into one bullet per scheme, same convention as
    _format_product_list. None when no scheme is active."""
    schemes = [s for s in (ctx.get("active_schemes") or []) if s.get("name")]
    if not schemes:
        return None
    return "\n".join(f"- {scheme_pointer_hindi(s)}" for s in schemes[:10]), "Schemes"


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
    each product surfaces as its own bullet rather than one clause of a longer one.

    The per-product "(₹V)" -- the peer-summed purchase value the ranking runs on -- was
    dropped 2026-09-16 (explicit user request, on the AI pitch's equivalent "₹3.52 लाख
    की भारी मांग": "value should be removed its no sense"): it's an aggregate of OTHER
    centres' buying, meaningless read aloud to this DC. The order (highest first) still
    carries the ranking."""
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
        parts.append(f"- {p['name']}{enrichment}")
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
    # "Schemes" (added 2026-09-17) -- likewise not a CSV source; appended for Sale.
    "Schemes": _tp_active_schemes,
}

# Human-readable label for codes with no CSV Applicable-Sources entry (currently just
# "Club" -- see _TALKING_POINTS's own comment) -- without this, cfg.data_source_labels.
# get(code, code) falls back to the bare code itself, and Data_Sources_Used would show
# the redundant "Club Club" instead of a real label.
_EXTRA_LABELS = {"Club": "DC Club / Scheme Standing", "Schemes": "Active Sales/ABS Schemes"}


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


def ptp_sale_combo_fixed_lines(dc_name: str, overdue: float, outstanding: Optional[float], aging: Optional[str]) -> Dict[str, str]:
    """The Sale + Promise To Pay / Collection combo's fixed (never-model-written)
    greeting/Ask/Wish/header lines, per the DC Visit Pitch (Multi-Purpose) sheet's own
    worked example (see module docstring for the sheet's stated sequencing rationale).
    Named/shared here (added 2026-09-15, explicit user request -- "bifurcate the sales
    and promise to pay ... all pointers in batana part") so _compose_sale_ptp_combo
    below (the deterministic template) and ai_sales_forecast.build_ai_pitch's own combo
    branch (added the same day, for exactly the same reason -- the AI-Generated Pitch
    was silently discarding this bifurcation, collapsing collection and sales pointers
    into one undifferentiated [बताना] block) can never drift apart on the parts that are
    NEVER supposed to vary between the two paths -- only the persuasive [बताना]/Tell
    CONTENT inside each section differs; every greeting/Ask/Wish/header word is
    identical either way, exactly like _ASK_HINDI/_WISH_HINDI already are for the
    Ask/Wish sections of every other Purpose."""
    aging_note = f" ({aging})" if aging else ""
    return {
        "greeting_collection_led": f"नमस्ते {dc_name}! कैसे हैं आप?",
        "greeting_sales_led": f"नमस्ते {dc_name}! कैसे हैं आप, बिज़नेस का क्या हाल है?",
        "collection_header": "— कलेक्शन हिस्सा —",
        "ask_collection": f"[पूछना] {dc_name}, ₹{overdue:,.0f}{aging_note} का पेमेंट पेंडिंग है — कोई दिक्कत आ रही है क्या फंड की तरफ से?",
        "wish_collection": "[विश] बताइए, इस हफ्ते के अंदर कब तक क्लियर कर पाएंगे?",
        "sales_header_after_collection": "— सेल्स हिस्सा (पेमेंट कमिट होने के बाद) —",
        "ask_sales_after_collection": "[पूछना] वैसे इस सीजन में क्या चल रहा है, किस चीज़ की डिमांड सबसे ज़्यादा आ रही है?",
        "wish_sales_after_collection": "[विश/क्लोज़] तो चलिए, पुराना पेमेंट क्लियर होते ही एक ऑर्डर भी साथ में डाल देते हैं ताकि स्टॉक टाइम पर आ जाए।",
        "sales_header_led": "— सेल्स हिस्सा —",
        "ask_sales_led": "[पूछना] इस सीजन में किस चीज़ की डिमांड सबसे ज़्यादा आ रही है?",
        "wish_sales_led": "[विश] चलिए आज एक ऑर्डर बुक कर लेते हैं।",
        "billing_header": "— बिलिंग हिस्सा (सेल के बाद) —",
        "ask_billing": (
            f"[पूछना] वैसे अभी का जो ₹{outstanding:,.0f} है, उसकी बिलिंग किस टाइम तक हो जाएगी?" if outstanding else ""
        ),
        "wish_billing": (
            f"[विश/क्लोज़] तो आज के नए ऑर्डर के साथ-साथ, पुराना ₹{outstanding:,.0f} भी इसी हफ्ते क्लियर कर दीजिएगा — दोनों साथ में सेटल हो जाएंगे।"
            if outstanding else ""
        ),
    }


def _compose_sale_ptp_combo(task: DailyTask, ctx: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    """DC Visit Pitch (Multi-Purpose) sheet's own "Promise To Pay / Collection + Sale"
    worked example, structurally: two section-labeled segments, sequenced by whether a
    real overdue amount exists. See module docstring for the sheet's stated rationale."""
    cfg = get_pitch_config()
    dc_name = (task.dc_name or "").strip() or "जी"
    overdue = ctx.get("present_overdue") or 0
    outstanding = ctx.get("present_outstanding")
    aging = ctx.get("overdue_aging_bucket")
    fixed = ptp_sale_combo_fixed_lines(dc_name, overdue, outstanding, aging)

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
    sales_sentences = [s for s in (sentence_for(c) for c in ("S1", "S2b", "S3", "S8", "Schemes", "Club")) if s]

    skipped.append("S4 Current Inventory (no DC-level data source exists anywhere in this system)")

    lines: List[str] = []
    if overdue > 0:
        # Collection-led -- sheet's Sequence Rationale: acknowledge + get a commitment
        # on the overdue first, then fold the sales ask into the same conversation so
        # it doesn't read as payment-only.
        lines.append(fixed["greeting_collection_led"])
        lines.append("")
        collection_sentence = sentence_for("S5")
        lines.append(fixed["collection_header"])
        lines.append(fixed["ask_collection"])
        if collection_sentence:
            lines.append(f"[बताना] {collection_sentence}")
        lines.append(fixed["wish_collection"])
        lines.append("")
        lines.append(fixed["sales_header_after_collection"])
        lines.append(fixed["ask_sales_after_collection"])
        lines.extend(_tell_lines(sales_sentences))
        lines.append(fixed["wish_sales_after_collection"])
    else:
        # Sales-led, no urgency to open with -- same structural idea as the sheet's
        # "Sale + Promise To Bill (P2B)" row: value first, billing folded in after.
        lines.append(fixed["greeting_sales_led"])
        lines.append("")
        lines.append(fixed["sales_header_led"])
        lines.append(fixed["ask_sales_led"])
        lines.extend(_tell_lines(sales_sentences))
        lines.append(fixed["wish_sales_led"])
        if outstanding:
            billing_sentence = sentence_for("S5")
            lines.append("")
            lines.append(fixed["billing_header"])
            lines.append(fixed["ask_billing"])
            if billing_sentence:
                lines.append(f"[बताना] {billing_sentence}")
            lines.append(fixed["wish_billing"])
        else:
            skipped.append("S5 Outstanding (no outstanding balance to raise this run)")

    return "\n".join(lines).strip(), used, skipped


def is_sale_ptp_combo(purposes: List[str]) -> bool:
    """True iff `purposes` is exactly the Sale + Promise To Pay / Collection combo --
    the one combo the task engine actually produces live (see this module's own
    docstring) and the only one with a hand-special-cased composer
    (_compose_sale_ptp_combo below). Extracted 2026-09-16 (architecture audit finding):
    this exact `set(...) == {"Sale", "Promise To Pay / Collection"}` check used to be
    duplicated -- once here, once independently in planning.ai_sales_forecast -- so a
    future 3rd combo meant remembering to update both with no signal linking them.
    ai_sales_forecast.py imports this (deferred import, same pattern already used there
    for _ASK_HINDI/_WISH_HINDI, since planning.pitching imports build_ai_pitch at module
    level -- see that module's own docstring)."""
    return set(purposes) == {"Sale", "Promise To Pay / Collection"}


def _compose(task: DailyTask, ctx: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    purposes, matched_key, win_condition_or_rationale = _match_script(task.purpose_of_visit or "")
    if is_sale_ptp_combo(purposes):
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
        ordered_codes = ordered_codes + ["Schemes", "Club"]

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


def generate_pitches_for_plan_run(plan_run: PlanRun, extra_data_by_dc: Dict[str, ExtraDcContext]) -> Tuple[int, List[Dict[str, str]]]:
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

    # Phase 1 (sequential, cheap): build each task's ctx + the templated script. Both
    # are deterministic, in-memory, no network I/O -- nothing here benefits from
    # parallelizing, and _compose must run before build_ai_pitch regardless (its
    # purposes/matched_key feed straight into that call).
    prepared: List[Dict[str, Any]] = []
    for task in plan_run.tasks.filter(dc_id__isnull=False):  # Farmer Meeting tasks have no DC -- no pitch to generate
        try:
            ctx = dict(extra_data_by_dc.get(task.dc_id, {}))
            ctx["present_outstanding"] = task.present_outstanding
            ctx["present_overdue"] = task.present_overdue
            ctx["overdue_aging_bucket"] = task.overdue_aging_bucket
            ctx["ytd_private_label"] = task.ytd_private_label
            # Templated script computed UNCONDITIONALLY first (cheap, deterministic,
            # already-tested) -- this is the safety-net fallback the AI-generated script
            # below falls back to whenever it returns nothing, never the other way
            # around. used/skipped (the S1-S8 audit trail) always reflect this templated
            # pass regardless of which script actually gets saved.
            script, used, skipped = _compose(task, ctx)
            purposes, matched_key, _ = _match_script(task.purpose_of_visit or "")
            prepared.append({
                "task": task, "ctx": ctx, "script": script, "used": used, "skipped": skipped,
                "purposes": purposes, "matched_key": matched_key, "ai_pitch": {},
            })
        except Exception as e:
            logger.warning("PitchingAgent: failed to prepare a pitch for DC %s (task %s): %s: %s", task.dc_id, task.id, type(e).__name__, e)
            failures.append({"dc_id": task.dc_id, "detail": f"{type(e).__name__}: {e}"})

    # Phase 2 (parallel): the actual LLM calls -- pure network I/O, no DB access, so
    # nothing here needs Django-ORM thread-safety. AI-Generated Pitch (added
    # 2026-09-12, explicit user request -- "script and scheme and benifit of sales and
    # outstanding clearance from ai") -- isolated per-task exactly as before (a provider
    # outage or malformed response for one DC degrades that DC alone to its templated
    # script, never breaks another DC's pitch). Only OVERRIDES script_hindi when it
    # actually produced a non-empty one -- see build_ai_pitch's own docstring for the
    # full caller contract. purposes/dc_name passed through (added 2026-09-15, explicit
    # follow-up request -- "use that pattern in Pitching agent which use ai token") so
    # the AI script follows the exact same Ask/Tell/Wish structure the template uses.
    #
    # Parallelized 2026-09-17 (explicit user request, after live-timing a 5-task SE plan
    # at 98s -- see PITCH_GENERATION_MAX_WORKERS' own comment above): a bounded
    # ThreadPoolExecutor, not one thread per task, replacing the fully sequential loop
    # that used to make every task pay for every other task's LLM latency in full.
    def _run_ai_pitch(item: Dict[str, Any]) -> Dict[str, Any]:
        task = item["task"]
        try:
            return build_ai_pitch(
                task.dc_id, item["matched_key"] or task.purpose_of_visit or "", item["ctx"],
                purposes=item["purposes"], dc_name=task.dc_name,
            )
        except Exception as e:
            logger.warning("AI-Generated Pitch failed for DC %s (task %s): %s: %s", task.dc_id, task.id, type(e).__name__, e)
            return {}

    if prepared:
        with ThreadPoolExecutor(max_workers=min(PITCH_GENERATION_MAX_WORKERS, len(prepared))) as executor:
            future_to_item = {executor.submit(_run_ai_pitch, item): item for item in prepared}
            for future in as_completed(future_to_item):
                future_to_item[future]["ai_pitch"] = future.result()

    # Phase 3 (sequential, main thread): every PitchScript write happens here, one at a
    # time, exactly as the original all-sequential loop did -- no change to DB-write
    # behavior, only to how the (now-already-collected) AI results got produced.
    for item in prepared:
        task = item["task"]
        try:
            ai_pitch = item["ai_pitch"]
            final_script = ai_pitch.get("script_hindi") or item["script"]
            final_used = item["used"] + ["AI-Generated Script"] if ai_pitch.get("script_hindi") else item["used"]
            ai_sales_forecast = {k: v for k, v in ai_pitch.items() if k != "script_hindi"}
            PitchScript.objects.update_or_create(
                daily_task=task,
                defaults={
                    "purpose_key": item["matched_key"] or task.purpose_of_visit, "script_hindi": final_script,
                    "data_sources_used": final_used, "data_sources_skipped": item["skipped"],
                    "recommended_products": item["ctx"].get("recommended_products") or [],
                    "ai_sales_forecast": ai_sales_forecast,
                },
            )
            created += 1
        except Exception as e:
            logger.warning("PitchingAgent: failed to save a pitch for DC %s (task %s): %s: %s", task.dc_id, task.id, type(e).__name__, e)
            failures.append({"dc_id": task.dc_id, "detail": f"{type(e).__name__}: {e}"})
    return created, failures
