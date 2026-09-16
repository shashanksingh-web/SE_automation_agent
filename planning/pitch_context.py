"""ExtraDcContext -- the shared per-DC context dict planning.services.
run_pitching_and_dc_card_agents builds once (as extra_data_by_dc[dc_id]) and both
planning.pitching and planning.dc_card read from, each consuming a different subset of
keys with no shared type checking either side. Architecture-audit fix, 2026-09-16: this
used to be an entirely undeclared contract -- three files agreeing on a dict shape by
convention only, documented in scattered comments (e.g. services.py's own "DC Card-only
additions -- ignored by planning.pitching's builders"). Adding a new field meant editing
services.py to populate it, then whichever consumer needs it, with nothing checking the
two stay in sync except a human reading both files -- the same shape of bug already
fixed once this session in the sibling RoutePlan/DailyTask seam.

A standalone module (no imports beyond `typing`) so planning.services, planning.pitching,
and planning.dc_card can all import this without creating or risking a circular import
between any of them.

total=False throughout: every key here is genuinely optional at runtime (several are
only set when a real value exists -- e.g. block_category_avg/recommended_products only
appear when this DC's dominant_category had real peer purchase data) -- both existing
consumers already read every key via `ctx.get(...)`, never direct subscripting, which
this type matches rather than overstates."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class ExtraDcContext(TypedDict, total=False):
    # Source 6/7 -- this DC's own aggregate purchase profile (services.py's
    # _sql_dc_purchase_summary). No per-product breakdown -- see purchase_last_fy's own
    # note in ai_sales_forecast.py for why the DC's OWN purchases stay aggregate-only.
    purchase_30d: Optional[float]
    purchase_last_fy: Optional[float]
    purchase_ytd: Optional[float]

    # S2a -- this DC's most recent real discount (services.py's _sql_last_discount).
    last_discount: Optional[float]

    # dc_datamart's weighted_avg_repayment_days, forwarded from Source 3d.
    avg_repayment_days: Optional[float]

    # S1 -- this DC's dominant purchase category + its own 30d spend in it.
    dominant_category: Optional[str]
    dc_category_purchase: Optional[float]

    # S1 block/node peer comparison -- only present when dominant_category had real
    # peer purchase data to rank against (block tried first, node as fallback).
    block_category_avg: Optional[float]
    peer_comparison_scope: Optional[str]  # "block" | "node"
    recommended_products: List[Dict[str, Any]]
    # S2b -- only present alongside recommended_products, for the #1 recommended product.
    suggested_discount: Optional[float]

    # DC Card-only additions (Source 3h) -- ignored by planning.pitching's builders,
    # which only ever ctx.get() the keys they know about.
    business_area_strength: Optional[List[Dict[str, Any]]]
    business_area_strength_prior_year: Optional[List[Dict[str, Any]]]
    club: Optional[Dict[str, Any]]  # raw normalize_dc_club() row
    active_schemes: List[Dict[str, Any]]  # Node-scoped, added 2026-09-12

    # YoY PL comparison (Source 3d), confirmed 2026-08-18 -- distinct from
    # purchase_last_fy/purchase_ytd above (those are overall purchase, not PL-tagged).
    ytd_pl_last_year: Optional[float]
    yoy_pl_growth_pct: Optional[float]
