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

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import se_daily_plan_agent as agent

logger = logging.getLogger(__name__)

AI_PITCH_CACHE_PATH = Path(
    __import__("os").environ.get(
        "SE_AGENT_AI_PITCH_CACHE", agent.BASE_DIR / "output" / "ai_sales_forecast_cache.json"
    )
)

_pitch_cache: Optional[Dict[str, Dict[str, Any]]] = None


def _load_pitch_cache() -> Dict[str, Dict[str, Any]]:
    global _pitch_cache
    if _pitch_cache is None:
        try:
            _pitch_cache = json.loads(AI_PITCH_CACHE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _pitch_cache = {}
    return _pitch_cache


def _save_pitch_cache() -> None:
    try:
        AI_PITCH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AI_PITCH_CACHE_PATH.write_text(json.dumps(_pitch_cache), encoding="utf-8")
    except OSError:
        pass  # best-effort cache, same convention as se_daily_plan_agent's own LLM caches


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
        # v2: prefix bumped 2026-09-15 when script_hindi's meaning changed from a full
        # free-form script to just the [बताना]/Tell sentences (assembled into the full
        # Ask/Tell/Wish script by the caller) -- without this, a pre-existing cache entry
        # would resolve to the OLD full-script value under a v1 key, silently skipping the
        # new Ask/Tell/Wish assembly for every DC/purpose already cached.
        f"v2:{dc_id}:{purpose_label}:{agent.LLM_ROUTING_PROVIDER}:{_model_by_provider.get(agent.LLM_ROUTING_PROVIDER, '')}:"
        f"{window_days}:{club_context or ''}:{ctx.get('present_outstanding')}:{ctx.get('present_overdue')}:"
        f"{ctx.get('last_discount')}:{ctx.get('suggested_discount')}"
    ]
    for c in sorted(candidates, key=lambda c: str(c.get("name"))):
        parts.append(f"{c.get('name')}:{round(float(c.get('value') or 0.0), 2)}")
    for s in sorted(schemes, key=lambda s: str(s.get("name"))):
        parts.append(f"scheme:{s.get('name')}:{s.get('valid_until')}")
    return "|".join(parts)


def _parse_pitch_response(text: str, valid_names: set) -> Dict[str, Any]:
    """Never raises -- malformed/unparseable JSON returns an empty result with a note,
    same tolerant-of-nonsense posture as se_daily_plan_agent._parse_llm_route_response.
    script_hindi is taken as free text (can't be mechanically validated word-for-word
    the way a product NAME can); products go through the same hallucination check Plan
    C uses for DC_IDs."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if len(cleaned.split("```")) > 1 else cleaned
        cleaned = cleaned[4:] if cleaned.lower().startswith("json") else cleaned
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return {"script_hindi": "", "products": [], "reasoning": "", "notes": ["Could not parse a JSON object out of the model's response"]}

    if not isinstance(parsed, dict):
        return {"script_hindi": "", "products": [], "reasoning": "", "notes": ["Response was not a JSON object"]}

    script_hindi = parsed.get("script_hindi")
    script_hindi = script_hindi.strip() if isinstance(script_hindi, str) else ""
    reasoning = (parsed.get("reasoning") if isinstance(parsed.get("reasoning"), str) else "") or ""

    raw_products = parsed.get("products")
    notes: List[str] = []
    if not script_hindi:
        notes.append("Response had no non-empty 'script_hindi'")
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
    return {"script_hindi": script_hindi, "products": validated, "reasoning": reasoning, "notes": notes}


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

    from .pitching import _ASK_HINDI, _WISH_HINDI  # local import -- avoids a circular
    # import, since planning.pitching already imports build_ai_pitch at module level.
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
    cached = cache.get(key)
    if cached is not None:
        return cached

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
    if candidates:
        lines.append(
            "Candidate products (recently purchased by geographically/categorically similar "
            "DCs -- you may ONLY recommend products from this exact list, never invent one):"
        )
        for c in candidates:
            lines.append(
                f"- {c.get('name')} | value=₹{c.get('value')} | category={c.get('category')} | "
                f"brand={c.get('brand')} | segment={c.get('business_segment')} | scope={c.get('scope')}"
            )
        if ctx.get("suggested_discount") is not None:
            lines.append(f"Suggested discount on the top candidate: ₹{ctx['suggested_discount']:.0f}/unit")
        lines.append("")

    lines += [
        "This pitch script always follows a fixed 3-part structure: [पूछना] (Ask) opens "
        "the conversation, [बताना] (Tell) is the persuasive data-driven pitch, [विश/क्लोज़] "
        "(Wish/Close) asks for the commitment. The Ask and Wish/Close lines are ALREADY "
        "fixed -- do not write them, they are added separately after your response. Your "
        "ONLY job is the [बताना]/Tell section: write 2-4 persuasive Hindi sentences for the "
        f"next {window_days} days' worth of business. Depending on which real facts exist "
        "above, weave in: the benefit of clearing outstanding/overdue now (e.g. club tier "
        "eligibility, avoiding further aging), up to 3 of the candidate products worth "
        "pitching and why, the benefit of any active Scheme (tied to a real product where "
        "possible), and this DC's Club standing and what acting today could earn it. Also "
        "separately list which of the candidate products (if any) you featured.",
        "",
        'Respond with ONLY this JSON, no other text: {"script_hindi": "<just the [बताना]/Tell '
        'sentences, no greeting, no [पूछना]/[विश] labels>", "products": '
        '[{"name": "...", "reason": "..."}, ...], "reasoning": "1 sentence in English summarizing your approach"}.',
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
    if not parsed["script_hindi"]:
        return {}

    # Assemble the full Ask/Tell/Wish script -- same structure/spacing
    # planning.pitching._compose() builds, greeting + fixed [पूछना] + the model's own
    # [बताना] content (parsed["script_hindi"], which at this point is ONLY the Tell
    # sentences per the prompt above) + fixed [विश/क्लोज़].
    greeting = f"नमस्ते {(dc_name or '').strip() or 'जी'}! कैसे हैं आप, दुकान का हाल-चाल बताइए?"
    script_lines = [greeting, ""]
    if ask_texts:
        script_lines.append("[पूछना] " + " ".join(ask_texts))
        script_lines.append("")
    script_lines.append(f"[बताना] {parsed['script_hindi']}")
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
