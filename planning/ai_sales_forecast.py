"""AI-Generated Pitch (added 2026-09-12, explicit user request -- "use claudeai for sale
purpose create the pitch with targeted product used by near by dc and near farmer dc" ->
"provide what important product for 15 to 20 days for sale" -> "product recomendation
from product product cohort but script and scheme and benifit of sales and outstanding
clearance from ai by using data feed from ssytem" -> "drop product cohort, keep
recommended_products as-is"). Generates the ENTIRE pitch script (script_hindi) via one
AI call per task, replacing planning.pitching._compose()'s hand-written Hindi templates
as the primary source -- but never as a hard replacement: planning.pitching still
computes the templated script unconditionally as a safety-net fallback (see that
module's own generate_pitches_for_plan_run), used whenever this returns {} for any
reason. Same fail-safe design as Plan C's AI-Reasoned routing (se_daily_plan_agent.
build_route_llm_reasoned): the AI is only ever trusted to pick products from and reason
about REAL, already-computed data -- structured product picks are hallucination-checked
exactly like Plan C's DC_ID validation; the free-text script itself is instructed never
to state a figure/fact not given to it, though that instruction (unlike the structured
product-name check) cannot be mechanically verified the same way -- flagged here as a
real, accepted limitation, not silently assumed airtight. Every provider call goes
through the exact same 3-provider fallback chain (Gemini -> OpenRouter -> Anthropic) and
hard timeout Plan C already uses.

Data used (see planning.services' extra_data_by_dc / ctx, already computed by the time
planning.pitching.generate_pitches_for_plan_run runs):
  - This DC's own profile: dominant_category, dc_category_purchase, purchase_last_fy,
    purchase_ytd -- aggregate figures only, no per-product breakdown exists for the DC's
    OWN purchases anywhere in this pipeline today (only for its peers, see below).
  - present_outstanding / present_overdue / overdue_aging_bucket / avg_repayment_days --
    the same real payment-status figures the template's own _tp_outstanding renders,
    used here to ground the "benefit of clearing outstanding" part of the script.
  - last_discount / suggested_discount -- same S2a/S2b figures the template already uses.
  - recommended_products -- the Pitching Agent's existing nearby-DC/peer product feed
    (planning.services._attach_nearby_product_recommendations + the block/node peer
    logic above it), already live and already shown in the template pitch today. This
    remains the ONLY source of real, per-product data, and therefore the entire
    candidate pool the AI is allowed to recommend from -- Product Cohort was
    investigated as an alternative/additional product source and explicitly dropped
    (see below).
  - club (DC Club / loyalty-tier standing -- see _club_summary) and active_schemes
    (Sales/ABS Schemes -- see _active_schemes_context and planning.services._sql_active_
    schemes_for_nodes) -- CONFIRMED SEPARATE systems (explicit user correction: "dc club
    and scheme are different in the system"), both given to the model as context.

Data/integrations deliberately NOT used:
  - "nearby farmer DC" purchase data: investigated live against the actual Redshift
    cluster. The one candidate table shaped right for it (hyperlocal_order_data --
    farmer_id + dc_sap_id + product_template_name) is confirmed frozen since
    2024-04-22 (zero rows in the last 20 days); a second candidate (farmer_sale) has no
    DC-level join key at all and is similarly stale/sparse. Both confirmed dead ends.
  - Product Cohort (planning.product_cohort) as a product-candidate source: investigated
    and dropped after THREE separate confirmed blockers, not a preference call --
    (1) it's product-first (given a product+Node, which DCs), the opposite direction
    pitching needs (given a DC, which products); (2) seeding candidate products from
    this DC's active Schemes has nothing to seed with -- confirmed live that 0 of
    9,744,713 abs_scheme rows have a material_id at all, including all 7 currently-active
    scheme_codes; (3) seeding from recommended_products instead has no product-ID bridge
    either -- products_template/products_product carry only an internal Odoo id, no SAP-
    style material_id column, and the one table that has both ID spaces together
    (hyperlocal_order_data) is the same table already confirmed frozen above. All three
    flagged as genuine gaps, not silently worked around."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import se_daily_plan_agent as agent

logger = logging.getLogger(__name__)

AI_PITCH_CACHE_PATH = Path(
    __import__("os").environ.get(
        "SE_AGENT_AI_PITCH_CACHE", agent.BASE_DIR / "output" / "ai_sales_forecast_cache.json"
    )
)

# Bump whenever build_ai_pitch's response shape or script_hindi's meaning changes -- a
# stale-shape cache entry (from before the bump) would otherwise resolve as if valid,
# silently returning a wrong-looking pitch with no error. Pulled out into one named,
# visible constant (architecture-audit fix, 2026-09-16) instead of a literal buried
# inside _cache_key's own f-string -- the obligation to bump it now sits at the point
# someone is most likely to be editing when it applies.
#   v2 (2026-09-15): script_hindi's meaning changed from a full free-form script to
#     just the [बताना]/Tell sentences (assembled into the full Ask/Tell/Wish script by
#     the caller).
#   v3 (2026-09-16): the Tell became a list of pointers rendered as bullets
#     (_tell_pointers/_tell_lines) instead of one paragraph.
#   v4 (2026-09-16): product pointers started carrying the product's benefit text.
#   v5 (2026-09-16): the peer-summed rupee value left the prompt and the pointers
#     (see _candidate_lines) -- a v4 script quotes "₹3.52 लाख की मांग" per product.
CACHE_SCHEMA_VERSION = "v5"

_pitch_cache_store = agent.JsonFileCache(AI_PITCH_CACHE_PATH)


def _load_pitch_cache() -> Dict[str, Dict[str, Any]]:
    return _pitch_cache_store.load()


def _save_pitch_cache() -> None:
    _pitch_cache_store.save()  # JsonFileCache.save() is already best-effort (swallows OSError)


def _cache_key(
    dc_id: str, purpose_label: str, window_days: int, candidates: List[Dict[str, Any]],
    club_context: Optional[str], schemes: List[Dict[str, Any]], ctx: Dict[str, Any],
) -> str:
    # Includes every real figure that can change the prompt's content -- a DC crossing a
    # club tier, a scheme starting/ending, or its outstanding/overdue changing between
    # runs must never silently reuse a decision made against different facts, same
    # "must bust the cache" reasoning se_daily_plan_agent's own LLM caches already apply.
    _model_by_provider = {
        "anthropic": agent.ANTHROPIC_ROUTING_MODEL, "openrouter": agent.OPENROUTER_ROUTING_MODEL, "gemini": agent.GEMINI_ROUTING_MODEL,
    }
    parts = [
        # See CACHE_SCHEMA_VERSION's own docstring/history above -- bump that constant,
        # not this literal, whenever this function's output shape changes meaning.
        f"{CACHE_SCHEMA_VERSION}:{dc_id}:{purpose_label}:{agent.LLM_ROUTING_PROVIDER}:{_model_by_provider.get(agent.LLM_ROUTING_PROVIDER, '')}:"
        f"{window_days}:{club_context or ''}:{ctx.get('present_outstanding')}:{ctx.get('present_overdue')}:"
        f"{ctx.get('last_discount')}:{ctx.get('suggested_discount')}"
    ]
    for c in sorted(candidates, key=lambda c: str(c.get("name"))):
        # The description is prompt content too: a template gaining/changing its text
        # must re-prompt, not replay a script written without it. Length + a short
        # digest rather than the text itself keeps the key readable.
        desc = c.get("description") or ""
        parts.append(f"{c.get('name')}:{round(float(c.get('value') or 0.0), 2)}:d{len(desc)}:{hashlib.sha1(desc.encode('utf-8')).hexdigest()[:8]}")
    for s in sorted(schemes, key=lambda s: str(s.get("name"))):
        parts.append(f"scheme:{s.get('name')}:{s.get('valid_until')}")
    return "|".join(parts)


def _parse_json_object(text: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Shared JSON-extraction step for both _parse_pitch_response and
    _parse_combo_pitch_response below - strips a markdown code fence if the model
    wrapped its response in one, then json.loads. Returns (None, [note]) on any
    failure, never raises, same tolerant-of-nonsense posture as se_daily_plan_agent.
    _parse_llm_route_response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if len(cleaned.split("```")) > 1 else cleaned
        cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None, ["Could not parse a JSON object out of the model's response"]
    if not isinstance(parsed, dict):
        return None, ["Response was not a JSON object"]
    return parsed, []


def _validate_products(raw_products: Any, valid_names: set) -> Tuple[List[Dict[str, str]], List[str]]:
    """Shared product-hallucination check for both _parse_pitch_response and
    _parse_combo_pitch_response below - same check Plan C uses for DC_IDs, applied to
    product names instead."""
    notes: List[str] = []
    if not isinstance(raw_products, list):
        raw_products = []
    seen: set = set()
    validated: List[Dict[str, str]] = []
    for raw in raw_products:
        if not isinstance(raw, dict):
            notes.append(f"Dropped a non-object product entry: {raw!r}")
            continue
        name = raw.get("name")
        if not isinstance(name, str):
            notes.append(f"Dropped a non-string product name: {name!r}")
            continue
        key = name.strip().lower()
        if key not in valid_names:
            notes.append(f"Dropped {name!r} -- not in this DC's real candidate product pool (hallucinated)")
            continue
        if key in seen:
            notes.append(f"Dropped a duplicate of {name!r}")
            continue
        if len(validated) >= 5:
            notes.append(f"Dropped {name!r} -- model proposed more than 5 products")
            continue
        validated.append({"name": name, "reason": str(raw.get("reason") or "")[:300]})
        seen.add(key)
    return validated, notes


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[।?!.])\s+")
_LIST_MARKER_RE = re.compile(r"^(?:[-•*]|\d+[.)])\s+")


def _tell_pointers(value: Any) -> List[str]:
    """Normalizes a model-written [बताना]/Tell content into a list of talking points,
    one per bullet (added 2026-09-16, explicit user request on a live pitch whose Tell
    came back as one dense paragraph: "provider in pointers"). The prompts ask for a
    JSON array of pointers; a model that answers with a single string anyway gets it
    split on sentence boundaries (Hindi danda "।", plus ?/!/. followed by whitespace --
    a "." glued to the next character, as in 12.5%, is never a boundary) so the result
    still renders as bullets rather than silently falling back to the paragraph. Any
    list marker the model prefixed itself ("- ", "• ", "1. ") is stripped so
    planning.pitching._tell_lines can apply the one convention PitchPanel.tsx parses."""
    if isinstance(value, list):
        raw_points = [v for v in value if isinstance(v, str)]
    elif isinstance(value, str):
        raw_points = _SENTENCE_BOUNDARY_RE.split(value)
    else:
        return []
    points: List[str] = []
    for p in raw_points:
        cleaned = _LIST_MARKER_RE.sub("", p.strip()).strip()
        if cleaned:
            points.append(cleaned)
    return points


def _parse_pitch_response(text: str, valid_names: set) -> Dict[str, Any]:
    """Never raises -- malformed/unparseable JSON returns an empty result with a note.
    tell is taken as free text (can't be mechanically validated word-for-word the way a
    product NAME can), normalized into pointers by _tell_pointers; products go through
    _validate_products. "script_hindi" is still accepted as the Tell key -- the field's
    name in this prompt until 2026-09-16 -- so a model echoing the old contract isn't
    treated as an empty response."""
    parsed, parse_notes = _parse_json_object(text)
    if parsed is None:
        return {"tell": [], "products": [], "reasoning": "", "notes": parse_notes}

    tell = _tell_pointers(parsed.get("tell", parsed.get("script_hindi")))
    reasoning = (parsed.get("reasoning") if isinstance(parsed.get("reasoning"), str) else "") or ""

    notes: List[str] = []
    if not tell:
        notes.append("Response had no non-empty 'tell'")
    validated, product_notes = _validate_products(parsed.get("products"), valid_names)
    notes.extend(product_notes)
    return {"tell": tell, "products": validated, "reasoning": reasoning, "notes": notes}


def _parse_combo_pitch_response(text: str, valid_names: set) -> Dict[str, Any]:
    """Sale + Promise To Pay / Collection combo variant of _parse_pitch_response (added
    2026-09-15, explicit user request - "bifurcate the sales and promise to pay ... all
    pointers in batana part") - expects collection_tell/sales_tell as two SEPARATE Tell
    contents instead of one script_hindi, matching build_ai_pitch's own combo prompt.
    Either piece can legitimately be empty (a DC with no real outstanding has nothing
    genuine for collection_tell; the caller decides which piece(s) it actually needs
    based on ctx, same as the deterministic template's own overdue>0 branch)."""
    parsed, parse_notes = _parse_json_object(text)
    if parsed is None:
        return {"collection_tell": [], "sales_tell": [], "products": [], "reasoning": "", "notes": parse_notes}

    collection_tell = _tell_pointers(parsed.get("collection_tell"))
    sales_tell = _tell_pointers(parsed.get("sales_tell"))
    reasoning = (parsed.get("reasoning") if isinstance(parsed.get("reasoning"), str) else "") or ""

    validated, notes = _validate_products(parsed.get("products"), valid_names)
    return {"collection_tell": collection_tell, "sales_tell": sales_tell, "products": validated, "reasoning": reasoning, "notes": notes}


def _club_summary(ctx: Dict[str, Any]) -> Optional[str]:
    """Plain-English summary of this DC's current DC Club (loyalty-tier) standing, for
    the AI's own context. Reads the exact same ctx["club"] dict planning.pitching._tp_
    club_standing already renders into the template's own Hindi "Club" talking point --
    no new data source, no new query, just re-expressed in English for this prompt.
    Never invents a tier/reward the DC doesn't actually have; returns None when there's
    simply no club data for this DC (same as the template's own None case).

    RENAMED 2026-09-12 from the original _scheme_summary/scheme_context (explicit user
    correction -- "dc club and scheme are different in the system"): this function was
    always describing DC Club (dc_mapping_club_scheme/dc_club_slabs), a loyalty-tier
    program, NOT the separate Scheme system (scheme_details/abs_scheme -- see
    _active_schemes_context below) -- the original name conflated the two."""
    club = ctx.get("club")
    if not club:
        return None
    if not club.get("Is_Club_Enrolled"):
        return "Not currently enrolled in the Club Scheme."
    tier = club.get("Club_Tier")
    if tier:
        bits = [f"Currently in Club Tier '{tier}'"]
        if club.get("TOD_Percent") is not None:
            bits.append(f"{club['TOD_Percent']:.2f}% TOD")
        if club.get("Reward"):
            bits.append(f"reward: {club['Reward']}")
        return ", ".join(bits) + "."
    if club.get("Outstanding_Cleared") is False:
        eligible = club.get("Eligible_Tier_If_Outstanding_Cleared")
        if eligible:
            return (
                f"No current Club Tier (outstanding not yet cleared) -- would qualify for "
                f"Tier '{eligible}' as soon as outstanding is cleared."
            )
        return "No current Club Tier (outstanding not yet cleared)."
    turnover = club.get("Qualifying_Turnover")
    if turnover is not None:
        return f"No current Club Tier (qualifying turnover ₹{turnover:,.0f} is below the entry threshold)."
    return "No current Club Tier (no qualifying turnover recorded this scheme year)."


def _active_schemes_context(ctx: Dict[str, Any]) -> List[Dict[str, Optional[str]]]:
    """This DC's currently-active Sales/ABS Schemes. Genuinely separate from DC Club
    above: reads ctx["active_schemes"] (planning.services._sql_active_schemes_for_nodes,
    a live join of scheme_details + abs_scheme scoped to this DC's own Node -- confirmed
    live before building, see that query's own docstring). Bounded to 10 -- an
    audit/context list for the prompt, not itself the candidate product pool (the AI
    still only ever recommends products from recommended_products)."""
    schemes = ctx.get("active_schemes") or []
    return [
        {"name": s.get("name"), "category": s.get("category"), "brand": s.get("brand"), "valid_until": s.get("valid_until")}
        for s in schemes[:10] if s.get("name")
    ]


# How the sales pointers must talk about a product (added 2026-09-16, explicit user
# request: "in sales - batana part product benifits will be described by SE to DC for
# maximise trust and sales by using llm"). Until now a product pointer only carried the
# demand signal ("nearby centres are buying IFFCO UREA -- stock it"), which gives the
# SE nothing to say about WHY the DC's farmers would want it. The benefit claim is
# grounded in the product's own products_template description (planning.services.
# _attach_product_descriptions -- composition, use stage, target crops, dosage) where
# one exists; where none exists the model is held to general, widely-known benefits of
# that product TYPE and told so, rather than left to improvise specifics. Same
# never-invent-a-figure posture as the rest of this module, extended to compositions
# and dosages, which are exactly the "facts" a made-up benefit would fabricate.
_PRODUCT_BENEFIT_RULE = (
    "For every product pointer, describe the product's BENEFIT the way the SE should "
    "explain it to the DC so the DC can convince farmers and trust the recommendation: "
    "what it does for the crop or animal (nutrient/composition, growth stage or season it "
    "is used in, target crops, dosage when given) and then the business case (nearby-centre "
    "demand, season fit) -- benefit first, demand second. State demand only qualitatively "
    "('nearby centres are selling this well') -- NEVER a rupee sales or demand figure for a "
    "product; there is none above and the DC has no use for one. Take every specific benefit "
    "claim (composition, percentage, dosage, target crop) ONLY from that product's 'benefits' "
    "text above. If a product's benefits say '(none on file)', state only the general, "
    "widely-known benefit of that type of product (e.g. a cattle feed supports milk yield) "
    "in one clause and never a specific composition, percentage or dosage for it."
)


def _candidate_lines(candidates: List[Dict[str, Any]], ctx: Dict[str, Any]) -> List[str]:
    """The prompt's candidate-product block, shared by both AI paths. One line per
    product with its attributes and, since 2026-09-16, its benefits text (see
    _PRODUCT_BENEFIT_RULE) -- '(none on file)' spelled out when the template has no
    description, so the model is told the gap rather than reading an empty field as
    licence to fill it in.

    The peer-summed purchase value each candidate carries is deliberately NOT in the
    line (removed 2026-09-16, explicit user request on a live pitch quoting "₹3.52 लाख
    की भारी मांग": "value should be removed its no sense") -- it's the pipeline's
    internal ranking signal, meaningless to a DC, and while it was in the prompt the
    model quoted it in nearly every product pointer. The list is already highest-demand
    first, which is all the model needs to pick which products to feature."""
    if not candidates:
        return []
    lines = [
        "Candidate products (recently purchased by geographically/categorically similar "
        "DCs, listed highest nearby demand first -- you may ONLY recommend products from "
        "this exact list, never invent one):"
    ]
    for c in candidates:
        lines.append(
            f"- {c.get('name')} | category={c.get('category')} | brand={c.get('brand')} | "
            f"segment={c.get('business_segment')} | scope={c.get('scope')} | "
            f"benefits={c.get('description') or '(none on file)'}"
        )
    if ctx.get("suggested_discount") is not None:
        lines.append(f"Suggested discount on the top candidate: ₹{ctx['suggested_discount']:.0f}/unit")
    lines.append("")
    return lines


def _build_ai_pitch_combo(
    dc_id: str, ctx: Dict[str, Any], window_days: int, dc_name: Optional[str],
    candidates: List[Dict[str, Any]], club_context: Optional[str], schemes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Sale + Promise To Pay / Collection combo branch of build_ai_pitch (added
    2026-09-15, explicit user request -- "bifurcate the sales and promise to pay ...
    all pointers in batana part"). Without this branch, build_ai_pitch's normal single
    free-form [बताना] block was silently collapsing planning.pitching._compose_sale_
    ptp_combo's carefully-sequenced two-section structure (found live: a real pitch
    mixed a ₹1,94,068 overdue figure and a ₹12,69,300 YTD sales figure into one
    undifferentiated paragraph) -- a structural rule the DC Visit Pitch (Multi-Purpose)
    sheet itself specifies (see planning.pitching's own module docstring), not
    optional flavor text. Asks the model for TWO separate Tell contents instead of one,
    then assembles them into the EXACT same greeting/header/Ask/Wish skeleton
    ptp_sale_combo_fixed_lines gives the deterministic template, so an SE sees the
    identical structure regardless of which path produced the pitch -- only the
    persuasive sentences inside each section differ."""
    from .pitching import _tell_lines, ptp_sale_combo_fixed_lines

    overdue = ctx.get("present_overdue") or 0
    outstanding = ctx.get("present_outstanding")
    aging = ctx.get("overdue_aging_bucket")
    name = (dc_name or "").strip() or "जी"
    fixed = ptp_sale_combo_fixed_lines(name, overdue, outstanding, aging)

    lines = [
        "You are writing a short, persuasive Hindi pitch script for a Sales Executive (SE) "
        "visiting this Dehaat Center (DC) today. Visit purpose: Sale + Promise To Pay / Collection.",
        "",
        "Use ONLY the real facts given below -- never invent a number, product, scheme, or "
        "benefit that isn't explicitly stated here. Skip any topic below that has no real data "
        "-- never fabricate to fill a gap.",
        "",
        "This visit covers TWO distinct topics that must stay in TWO SEPARATE pieces of text, "
        "never merged into one paragraph: collecting an overdue/outstanding payment, and "
        "pitching new sales. Return them as two separate JSON fields (see the exact shape "
        "below) so the app can keep them in their own labeled sections of the script.",
        "",
    ]
    if overdue > 0:
        lines += [
            "Collection topic - DC's overdue payment status:",
            f"- Present outstanding: ₹{outstanding}",
            f"- Present overdue: ₹{overdue} ({aging or 'no aging bucket'})",
            f"- Typical repayment time: {ctx.get('avg_repayment_days')} days",
            "",
        ]
    elif outstanding:
        lines += ["Billing topic - DC's current outstanding (not yet overdue):", f"- Present outstanding: ₹{outstanding}", ""]
    lines.append(f"DC Club (loyalty-tier) standing: {club_context or 'no club data available'}")
    lines.append("")
    if schemes:
        lines.append("Currently-active Sales/ABS Schemes available to this DC (a separate system from DC Club above):")
        for s in schemes:
            lines.append(f"- {s['name']} | category={s.get('category')} | brand={s.get('brand')} | valid until {s.get('valid_until')}")
        lines.append("")
    lines += [
        "This DC's own purchase profile (aggregate figures only -- no per-product breakdown "
        "exists for this DC's own purchases):",
        f"- Dominant purchase category: {ctx.get('dominant_category') or 'unknown'}",
        f"- This DC's own purchase value in that category (last 30 days): {ctx.get('dc_category_purchase')}",
        f"- Total purchase last fiscal year: {ctx.get('purchase_last_fy')}",
        f"- Total purchase year-to-date: {ctx.get('purchase_ytd')}",
        "",
    ]
    if ctx.get("last_discount") is not None:
        lines.append(f"Last discount given to this DC: {ctx['last_discount']:.0f}%")
    lines += _candidate_lines(candidates, ctx)

    lines += [
        "Write TWO SEPARATE Tell contents, each as a JSON array of short pointers -- every "
        "pointer is ONE complete persuasive Hindi sentence about ONE thing (one product, one "
        "scheme, one benefit), written to be read aloud as a bullet, never a paragraph:",
        '- collection_tell: 1-2 pointers ONLY about the overdue/outstanding payment and the '
        'benefit of clearing it now (e.g. club tier eligibility, avoiding further aging). Leave '
        'this as an empty array if there is no real overdue/outstanding figure above -- never '
        'invent one.',
        f"- sales_tell: 2-5 pointers ONLY about products/scheme/Club benefit for the next "
        f"{window_days} days' worth of business: one pointer per featured candidate product (up "
        "to 3), then the benefit of any active Scheme (tied to a real product where possible), "
        "and this DC's Club standing and what acting today could earn it. Never mention the "
        "overdue/outstanding payment in this field -- that belongs only in collection_tell.",
        _PRODUCT_BENEFIT_RULE,
        "Also separately list which of the candidate products (if any) you featured in sales_tell.",
        "",
        'Respond with ONLY this JSON, no other text: {"collection_tell": ["...", ...], '
        '"sales_tell": ["...", ...], "products": [{"name": "...", "reason": "..."}, ...], '
        '"reasoning": "1 sentence in English summarizing your approach"}.',
    ]
    prompt = "\n".join(lines)

    raw_text, attempted_providers = agent._call_llm_for_routing(prompt)
    if raw_text is None:
        logger.warning("AI Pitch (combo): every configured LLM provider failed for DC %s (%s)", dc_id, attempted_providers)
        return {}

    valid_names = {c.get("name", "").strip().lower() for c in candidates}
    parsed = _parse_combo_pitch_response(raw_text, valid_names)
    notes = parsed["notes"]
    if len(attempted_providers) > 1:
        notes = [f"Routed via {attempted_providers[-1]} after {', '.join(attempted_providers[:-1])} failed"] + notes

    # Same required-piece rule the deterministic template enforces: a genuine overdue
    # needs a real collection_tell (empty would silently drop the whole collection ask
    # this combo exists to raise, leaving a header with nothing under it); missing
    # either required piece falls back to the template entirely rather than shipping a
    # visibly broken half-script.
    if overdue > 0 and not parsed["collection_tell"]:
        return {}
    if not parsed["sales_tell"]:
        return {}

    # Tell pointers go through the same _tell_lines the templated script uses -- one
    # inline sentence stays on the [बताना] line, 2+ become "- " bullets under it, which
    # is the one shape PitchPanel.tsx's parseScript renders as a list.
    lines_out: List[str] = []
    if overdue > 0:
        lines_out += [fixed["greeting_collection_led"], "", fixed["collection_header"], fixed["ask_collection"]]
        lines_out += _tell_lines(parsed["collection_tell"])
        lines_out += [fixed["wish_collection"], "", fixed["sales_header_after_collection"], fixed["ask_sales_after_collection"]]
        lines_out += _tell_lines(parsed["sales_tell"])
        lines_out.append(fixed["wish_sales_after_collection"])
    else:
        lines_out += [fixed["greeting_sales_led"], "", fixed["sales_header_led"], fixed["ask_sales_led"]]
        lines_out += _tell_lines(parsed["sales_tell"])
        lines_out.append(fixed["wish_sales_led"])
        if outstanding:
            lines_out.append("")
            lines_out.append(fixed["billing_header"])
            lines_out.append(fixed["ask_billing"])
            lines_out += _tell_lines(parsed["collection_tell"])
            lines_out.append(fixed["wish_billing"])

    return {
        "script_hindi": "\n".join(lines_out).strip(), "window_days": window_days, "products": parsed["products"],
        "reasoning": parsed["reasoning"], "club_context": club_context, "scheme_context": schemes, "notes": notes,
    }


def build_ai_pitch(
    dc_id: str, purpose_label: str, ctx: Dict[str, Any], window_days: Optional[int] = None,
    purposes: Optional[List[str]] = None, dc_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Returns {} (never raises) when: no LLM provider is configured, this DC has
    nothing real to build a pitch from at all (no candidate products AND no outstanding/
    overdue AND no club data AND no active schemes -- the same "nothing to say" case the
    template itself would also produce no content for), every provider call fails, or
    the model's response had no usable script_hindi after verification. Otherwise
    returns {"script_hindi": str, "window_days": int, "products": [{"name", "reason"}],
    "reasoning": str, "club_context": str|None, "scheme_context": [{"name", "category",
    "brand", "valid_until"}, ...], "notes": [str]}.

    Caller contract (see planning.pitching.generate_pitches_for_plan_run): this is a
    safety-netted OVERRIDE, not the sole source -- the caller always computes the
    templated script first (cheap, deterministic, already-tested) and only swaps in
    this result's script_hindi when it's non-empty, falling back to the template
    otherwise. products/reasoning/club_context/scheme_context/notes are stored
    separately (PitchScript.ai_sales_forecast) regardless of which script won.

    Sale + Promise To Pay / Collection combo (added 2026-09-15, explicit user request --
    "bifurcate the sales and promise to pay ... all pointers in batana part") delegates
    entirely to _build_ai_pitch_combo, which asks for TWO separate Tell contents
    (collection_tell/sales_tell) instead of one and assembles them into the same
    two-section skeleton planning.pitching._compose_sale_ptp_combo uses -- see that
    function's own docstring for why this exists (this branch previously mixed
    collection and sales pointers into one undifferentiated paragraph).

    Ask/Tell/Wish structure (added 2026-09-15, explicit follow-up request -- "use that
    pattern in Pitching agent which use ai token"): script_hindi returned here is the
    SAME 3-part bracketed structure planning.pitching._compose() builds for the
    templated script -- a fixed greeting, then [पूछना] (Ask), [बताना] (Tell), [विश/क्लोज़]
    (Wish/Close). Ask and Wish/Close reuse the EXACT SAME fixed _ASK_HINDI/_WISH_HINDI
    lines the template uses (imported locally below to avoid a circular import --
    planning.pitching already imports build_ai_pitch at module level) -- the model is
    never asked to write those, so there's zero hallucination risk in the two sections
    that matter most for a consistent, on-brand ask/close. The model's ONLY job is the
    middle [बताना]/Tell content: the persuasive, data-grounded prose this function's
    whole prompt below is built to produce (candidate products, scheme/club benefit,
    outstanding-clearance benefit). purposes/dc_name are optional purely for backward
    compatibility with any other caller -- planning.pitching always passes both; a
    caller that omits them gets purpose_label treated as a single purpose and "जी" as
    the greeting name, same fallback _compose() itself uses for a missing dc_name."""
    window_days = window_days if window_days is not None else agent.PITCH_AI_FORECAST_WINDOW_DAYS
    if not agent.LLM_ROUTING_ENABLED:
        return {}

    from .pitching import _ASK_HINDI, _WISH_HINDI, _tell_lines, is_sale_ptp_combo  # local
    # import -- avoids a circular import, since planning.pitching already imports
    # build_ai_pitch at module level.
    ask_texts = [_ASK_HINDI[p] for p in (purposes or [purpose_label]) if p in _ASK_HINDI]
    wish_texts = [_WISH_HINDI[p] for p in (purposes or [purpose_label]) if p in _WISH_HINDI]

    candidates = [c for c in (ctx.get("recommended_products") or []) if c.get("name")]
    club_context = _club_summary(ctx)
    schemes = _active_schemes_context(ctx)
    has_payment_data = ctx.get("present_outstanding") is not None or ctx.get("present_overdue") is not None
    if not candidates and not club_context and not schemes and not has_payment_data:
        return {}

    cache = _load_pitch_cache()
    key = _cache_key(dc_id, purpose_label, window_days, candidates, club_context, schemes, ctx)
    # Sale + Promise To Pay / Collection combo (added 2026-09-15, see
    # _build_ai_pitch_combo's own docstring) needs its own cache namespace -- its
    # response shape (collection_tell/sales_tell) is different from every other
    # purpose's single script_hindi, so it must never collide with (or be collided
    # into by) a plain single-purpose cache entry for the same dc_id/window/figures.
    # is_sale_ptp_combo (architecture audit fix, 2026-09-16) -- this used to
    # independently re-derive the same combo check pitching.py's own _compose() uses,
    # with no signal linking the two. Already imported above, alongside _ASK_HINDI/
    # _WISH_HINDI -- same deferred-import (avoids a circular import).
    is_ptp_sale_combo = is_sale_ptp_combo(purposes or [purpose_label])
    if is_ptp_sale_combo:
        key += ":ptp_sale_combo"
    cached = cache.get(key)
    if cached is not None:
        return cached

    if is_ptp_sale_combo:
        result = _build_ai_pitch_combo(dc_id, ctx, window_days, dc_name, candidates, club_context, schemes)
        if result:
            cache[key] = result
            _save_pitch_cache()
        return result

    lines = [
        f"You are writing a short, persuasive Hindi pitch script for a Sales Executive (SE) "
        f"visiting this Dehaat Center (DC) today. Visit purpose: {purpose_label}.",
        "",
        "Use ONLY the real facts given below -- never invent a number, product, scheme, or "
        "benefit that isn't explicitly stated here. Skip any topic below that has no real data "
        "-- never fabricate to fill a gap.",
        "",
    ]
    if has_payment_data:
        lines += [
            "DC's outstanding/payment status:",
            f"- Present outstanding: ₹{ctx.get('present_outstanding')}",
            f"- Present overdue: ₹{ctx.get('present_overdue')} ({ctx.get('overdue_aging_bucket') or 'no aging bucket'})",
            f"- Typical repayment time: {ctx.get('avg_repayment_days')} days",
            "",
        ]
    lines.append(f"DC Club (loyalty-tier) standing: {club_context or 'no club data available'}")
    lines.append("")
    if schemes:
        lines.append("Currently-active Sales/ABS Schemes available to this DC (a separate system from DC Club above):")
        for s in schemes:
            lines.append(f"- {s['name']} | category={s.get('category')} | brand={s.get('brand')} | valid until {s.get('valid_until')}")
        lines.append("")
    lines += [
        "This DC's own purchase profile (aggregate figures only -- no per-product breakdown "
        "exists for this DC's own purchases):",
        f"- Dominant purchase category: {ctx.get('dominant_category') or 'unknown'}",
        f"- This DC's own purchase value in that category (last 30 days): {ctx.get('dc_category_purchase')}",
        f"- Total purchase last fiscal year: {ctx.get('purchase_last_fy')}",
        f"- Total purchase year-to-date: {ctx.get('purchase_ytd')}",
        "",
    ]
    if ctx.get("last_discount") is not None:
        lines.append(f"Last discount given to this DC: {ctx['last_discount']:.0f}%")
    lines += _candidate_lines(candidates, ctx)

    lines += [
        "This pitch script always follows a fixed 3-part structure: [पूछना] (Ask) opens "
        "the conversation, [बताना] (Tell) is the persuasive data-driven pitch, [विश/क्लोज़] "
        "(Wish/Close) asks for the commitment. The Ask and Wish/Close lines are ALREADY "
        "fixed -- do not write them, they are added separately after your response. Your "
        "ONLY job is the [बताना]/Tell section: write 2-5 short pointers for the next "
        f"{window_days} days' worth of business, as a JSON array -- every pointer is ONE "
        "complete persuasive Hindi sentence about ONE thing (one product, one scheme, one "
        "benefit), written to be read aloud as a bullet, never a paragraph. Depending on "
        "which real facts exist above, cover: the benefit of clearing outstanding/overdue now "
        "(e.g. club tier eligibility, avoiding further aging), one pointer per featured "
        "candidate product (up to 3), the benefit of any active Scheme (tied to a real "
        "product where possible), and this DC's Club standing and what acting today could "
        "earn it.",
        _PRODUCT_BENEFIT_RULE,
        "Also separately list which of the candidate products (if any) you featured.",
        "",
        'Respond with ONLY this JSON, no other text: {"tell": ["<one [बताना]/Tell pointer>", '
        '...], "products": [{"name": "...", "reason": "..."}, ...], "reasoning": "1 sentence '
        'in English summarizing your approach"}. No greeting and no [पूछना]/[विश] labels '
        "anywhere in the pointers.",
    ]
    prompt = "\n".join(lines)

    # _call_llm_for_routing is private to se_daily_plan_agent (leading underscore) --
    # reused here deliberately rather than duplicated: it already IS provider-agnostic
    # (prompt in, text out), and is the single place the 3-provider fallback chain +
    # hard per-call timeout is implemented. Same cross-module reuse pattern this
    # codebase already accepts for other agent.* internals (e.g. planning.routing calls
    # agent.resolve_routing_ceilings).
    raw_text, attempted_providers = agent._call_llm_for_routing(prompt)
    if raw_text is None:
        logger.warning("AI Pitch: every configured LLM provider failed for DC %s (%s)", dc_id, attempted_providers)
        return {}

    valid_names = {c.get("name", "").strip().lower() for c in candidates}
    parsed = _parse_pitch_response(raw_text, valid_names)
    notes = parsed["notes"]
    if len(attempted_providers) > 1:
        notes = [f"Routed via {attempted_providers[-1]} after {', '.join(attempted_providers[:-1])} failed"] + notes
    if not parsed["tell"]:
        return {}

    # Assemble the full Ask/Tell/Wish script -- same structure/spacing
    # planning.pitching._compose() builds, greeting + fixed [पूछना] + the model's own
    # [बताना] pointers (parsed["tell"], ONLY the Tell content per the prompt above,
    # laid out by the same _tell_lines the template uses: one inline, 2+ as bullets)
    # + fixed [विश/क्लोज़].
    greeting = f"नमस्ते {(dc_name or '').strip() or 'जी'}! कैसे हैं आप, दुकान का हाल-चाल बताइए?"
    script_lines = [greeting, ""]
    if ask_texts:
        script_lines.append("[पूछना] " + " ".join(ask_texts))
        script_lines.append("")
    script_lines.extend(_tell_lines(parsed["tell"]))
    script_lines.append("")
    if wish_texts:
        script_lines.append("[विश/क्लोज़] " + " ".join(wish_texts))
    assembled_script = "\n".join(script_lines)

    result = {
        "script_hindi": assembled_script, "window_days": window_days, "products": parsed["products"],
        "reasoning": parsed["reasoning"], "club_context": club_context, "scheme_context": schemes, "notes": notes,
    }
    cache[key] = result
    _save_pitch_cache()
    return result
