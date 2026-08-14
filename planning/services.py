"""
Service layer wrapping se_daily_plan_agent.py (project root) as a reusable planning
engine for the Django endpoints in this app. Does not reimplement any agent logic --
every scoring/ranking/capacity/sequencing decision is delegated to
se_daily_plan_agent.generate_se_daily_plan() and its normalize_* helpers, so bug fixes
and rule changes made there apply here automatically. See AGENT_OPERATING_PROMPTS.md
(project root) for what the agent guarantees and its known open gaps.

Scope resolution:
    SE       -- scope_value is the SE's email (Assigned_SE_Email in DC_Master_Normalized)
    NODE     -- scope_value is the Node name
    STATE    -- scope_value is the State name
    ABM      -- scope_value is the ABM employee code (from Geo_Mapping / Source 1c)
    RBM      -- scope_value is the RBM employee code (from Geo_Mapping / Source 1c)
    BLOCK    -- scope_value is the Block name (from Geo_Mapping / Source 1c)
    DISTRICT -- scope_value is the District name (from Geo_Mapping / Source 1c)

NODE/STATE/SE resolve directly against DC_Master_Normalized.json (local, produced by a
prior `python se_daily_plan_agent.py` run). ABM/RBM/BLOCK/DISTRICT need the canonical
geo hierarchy (Source 1c, question 4647) which only exists live -- those four scopes
pull input_partner_details + input_se_node_mapping fresh on every call rather than
caching, so they will error clearly if METABASE_URL/METABASE_API_KEY aren't set, rather
than silently resolving against stale or absent data.
"""

from __future__ import annotations

import calendar
import json
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone

sys.path.insert(0, str(settings.SE_DAILY_PLAN_AGENT_PATH))
import se_daily_plan_agent as agent  # noqa: E402  -- project-root script, imported as a library

from . import data_cache, routing
from .models import DailyTask, ExceptionRecord, ObjectiveCompletionStats, PlanRun
from .notify import send_alert


class PlanningError(RuntimeError):
    """Raised for scope-resolution failures the caller should see as a 4xx, not a 500 --
    e.g. DC_Master_Normalized.json missing, or an ABM/Block/District scope requested
    without live Metabase access."""


def _dc_master_path() -> Path:
    return Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output" / "DC_Master_Normalized.json"


def load_dc_master() -> agent.Table:
    # Cached by planning.data_cache -- called once per generate_plan_for_scope(), and
    # run_scheduled_tuff calls that in a loop over every active ScheduledScope (93 in
    # this deployment) without normalization changing in between; re-parsing this
    # ~12MB/19k-row file fresh on every scope cost ~84ms x 93 = ~7.8s of pure redundant
    # I/O per cron run before this cache existed.
    try:
        return data_cache.load_output_json(_output_dir(), "DC_Master_Normalized.json")
    except FileNotFoundError as e:
        raise PlanningError(
            f"{e} Run the Data Normalization Agent first: "
            "`python se_daily_plan_agent.py` from the project root (see AGENT_OPERATING_PROMPTS.md Prompt 1)."
        )


def load_aop_targets() -> agent.Table:
    """AOP data is a supplementary enrichment for PL scoring's AOP-target leg (see the
    PL scoring block in generate_plan_for_scope), not a hard requirement the way
    DC_Master is -- missing/absent output degrades PL_Expected to its trailing-average
    leg only, same honest-degrade pattern as everywhere else, rather than raising."""
    try:
        return data_cache.load_output_json(_output_dir(), "AOP_Target_Normalized.json")
    except FileNotFoundError:
        return []


def _sql_list(ids: List[str]) -> str:
    return ",".join("'" + str(i).replace("'", "''") + "'" for i in ids)


# --- Scoped SQL builders, aliased to match se_daily_plan_agent's normalize_* input shapes
# so the existing dedup/casting logic is reused as-is rather than duplicated. ---

def _sql_geo_mapping_full() -> str:
    return agent.SQL_GEO_MAPPING_1C  # Source 1c has no DC-list filter hook; pulled in full.


def _sql_last_visit(dc_ids: List[str], se_user_ids: List[int], lookback_days: int) -> str:
    return f"""
    SELECT cc.partner_id AS sap_partner_id, p.user_id AS se_user_id, p.plan_execution_date, t.status AS task_status
    FROM task_management_task t
    JOIN task_management_plan p ON p.id = t.plan_id
    JOIN customer_management_customer cc ON cc.id = t.partner_id
    WHERE t.visit_type_id = 1 AND p.user_id IN ({",".join(str(u) for u in se_user_ids)})
      AND cc.partner_id::text IN ({_sql_list(dc_ids)})
      AND p.plan_execution_date >= CURRENT_DATE - INTERVAL '{lookback_days} days'
    ORDER BY cc.partner_id, p.plan_execution_date DESC
    """


def _sql_geo(dc_ids: List[str]) -> str:
    # is_dc=true dropped from this filter -- confirmed live 2026-08-06 that several DCs
    # already scoped via dc_ids (a real, confirmed DC per DC_Master/Source 2) carry real
    # lat_2/long_2 in input_partner_details but is_dc=false, so the old filter was
    # silently discarding usable geo data (this is what caused Distance to come back
    # N/A for whole SE lists even when most of their DCs had real coordinates). dc_ids
    # is already the authoritative DC filter here; re-filtering by is_dc is redundant
    # and, in these cases, actively wrong.
    return f"""
    SELECT sap_partner_id, lat_2 AS latitude, long_2 AS longitude
    FROM input_partner_details
    WHERE sap_partner_id IN ({_sql_list(dc_ids)})
    """


def _sql_outstanding(dc_ids: List[str]) -> str:
    # dc_datamart (dev, Redshift) supersedes customer_management_input_outstanding, which
    # is confirmed absent from every database on this cluster -- see
    # se_daily_plan_agent.SQL_OUTSTANDING_3D for the full finding. Already keyed by
    # sap_partner_id directly, no customer_management_customer bridge needed.
    return f"""
    SELECT sap_partner_id AS dc_id, total_outstanding, total_overdue, current_month_os,
           os_1_to_90, os_90_plus, weighted_avg_repayment_days, last_invoice_date, is_mismatch
    FROM dc_datamart
    WHERE sap_partner_id IN ({_sql_list(dc_ids)})
    """


def _sql_orders(dc_ids: List[str]) -> str:
    # Latest order (any status) per DC -- covers both Last_Order_* (filtered to
    # 'processed' inside normalize_sales_transactions) and Credit_On_Hold in one pull.
    # Uses ROW_NUMBER() rather than Postgres's DISTINCT ON -- confirmed live 2026-08-04
    # that Redshift (this cluster) does not support DISTINCT ON at all ("FeatureNotSupported").
    return f"""
    SELECT dc_id, amount_total, created_at, status, credit_on_hold, credit_on_hold_reason, partner_finance_status
    FROM (
        SELECT cc.partner_id AS dc_id, o.amount_total, o.created_at, o.status,
               o.credit_on_hold, o.credit_on_hold_reason, o.partner_finance_status,
               ROW_NUMBER() OVER (PARTITION BY cc.partner_id ORDER BY o.created_at DESC) AS rn
        FROM sale_orderrequest o
        JOIN customer_management_customer cc ON cc.id = o.partner_id
        WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
    ) ranked
    WHERE rn = 1
    """


def _sql_payments(dc_ids: List[str]) -> str:
    # Join key CONFIRMED live 2026-08-06: payments_paymenttransaction.customer_id
    # matches customer_management_customer.id (internal PK), NOT sap_partner_id
    # directly (521,345 of 538,039 rows matched via .id, 0 via .partner_id) -- same
    # bridging pattern as _sql_orders(). The prior version filtered customer_id::text
    # IN (dc_ids) directly against sap_partner_id strings, which always returned zero
    # rows -- that's why Last_Payment was N/A on every single Django-generated plan.
    #
    # No lookback window (fixed 2026-08-06) -- confirmed live that a 90-day cutoff was
    # hiding real, older SUCCESS payments and showing N/A instead (e.g. a DC whose most
    # recent payment was 100-300 days ago). Unlike the CLI's SQL_PAYMENTS_3F (which
    # pulls a full-network Payments_Normalized table and genuinely needs a window for
    # performance/scope), this query is already scoped to a handful of specific dc_ids,
    # so there's no cost to finding the true most recent payment -- same unrestricted
    # design as _sql_orders() above, which is why Last_Order_Date has always correctly
    # shown dates from many months back while Last_Payment_Date didn't.
    return f"""
    SELECT cc.partner_id AS dc_id, p.id, p.status, p.created_at
    FROM payments_paymenttransaction p
    JOIN customer_management_customer cc ON cc.id = p.customer_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
    """


def _sql_club_mapping(dc_ids: List[str]) -> str:
    return f"""
    SELECT partner_id AS dc_id, partner_name, node, state
    FROM dc_mapping_club_scheme
    WHERE partner_id::text IN ({_sql_list(dc_ids)})
    """


def _sql_club_qualifying_turnover(dc_ids: List[str]) -> str:
    """Scoped counterpart of se_daily_plan_agent.SQL_DC_CLUB_QUALIFYING_TURNOVER_3G --
    same confirmed filter (status='confirmed', 2026 calendar-year window, the 3
    reliably-identifiable T&C exclusions), just WHERE-restricted to this request's
    dc_ids instead of a full-network GROUP BY. See that query's own docstring for the
    honest-partial-exclusion caveat -- unchanged here, still applies."""
    return f"""
    SELECT partner_id AS dc_id, SUM(order_value) AS qualifying_turnover
    FROM coupon_analysis
    WHERE status = 'confirmed'
      AND created_at >= '2026-01-01' AND created_at < '2027-01-01'
      AND partner_id::text IN ({_sql_list(dc_ids)})
      AND NOT (
            (product_category = 'Crop Nutrition' AND product_sub_category = 'WSF')
         OR (product_category = 'Tools & Machinery')
         OR (product_sub_category = 'Cattle Feed' AND (product_name ILIKE '%khurak%' OR product_name ILIKE '%chokar%'))
      )
    GROUP BY partner_id
    """


def _sql_users(emails: List[str]) -> str:
    return f"SELECT id AS user_id, email FROM users_user WHERE email IN ({_sql_list(emails)})"


def _fiscal_year_start(plan_date: str) -> str:
    # Indian FY (April-March) -- matches the "FY-25-26"-style labels already used
    # throughout DC_RAnk.csv (Source 2)'s NRV/GM columns.
    d = datetime.fromisoformat(plan_date).date()
    year = d.year if d.month >= 4 else d.year - 1
    return f"{year}-04-01"


def _sql_ytd_pl(dc_ids: List[str], fy_start: str, plan_date: str) -> str:
    # Real PL source, confirmed 2026-08-12 (Data Norm Agent doc, Source 3h, live query):
    # products_template.business_segment_name = 'PRIVATE LABEL' is the actual PL tag --
    # replaces the pathik_report.pl_billed_amount proxy this used before. Join chain
    # (input_backend_db, same as sale_orderrequest elsewhere in this file):
    # sale_orderrequestline -> sale_orderrequest (order date/status/DC bridge) ->
    # products_product -> products_template (the PL flag). status='processed' excludes
    # non-real/cancelled orders, per the confirmed trap on sale_orderrequest elsewhere.
    return f"""
    SELECT cc.partner_id AS dc_id, SUM(sol.price_unit * sol.quantity) AS ytd_pl
    FROM sale_orderrequestline sol
    JOIN sale_orderrequest sor ON sor.id = sol.order_request_id
    JOIN customer_management_customer cc ON cc.id = sor.partner_id
    JOIN products_product pp ON pp.id = sol.product_id
    JOIN products_template pt ON pt.id = pp.template_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
      AND sor.status = 'processed'
      AND pt.business_segment_name = 'PRIVATE LABEL'
      AND sor.created_at >= '{fy_start}' AND sor.created_at <= '{plan_date}'
    GROUP BY cc.partner_id
    """


def _sql_pl_metrics(dc_ids: List[str], plan_date: str) -> str:
    # Real per-DC BO1 (PL) scoring -- same confirmed PRIVATE LABEL source as
    # _sql_ytd_pl above (see its comment for the join chain/status filter), replacing
    # pathik_report.pl_billed_amount as of 2026-08-12. Same honest-degrade pattern as
    # BO3: 1.2's PL_Expected combination method (90-day-average vs AOP target) is itself
    # still TBD in Source 5, so this uses ONLY the 90-day-average leg, scaled to a
    # 30-day-equivalent baseline, compared against the actual trailing-30-day PL.
    # recent_start/ninety_start computed in Python to keep this SQL simple.
    d = datetime.fromisoformat(plan_date).date()
    recent_start = (d - timedelta(days=30)).isoformat()
    ninety_start = (d - timedelta(days=90)).isoformat()
    return f"""
    SELECT cc.partner_id AS dc_id,
           SUM(CASE WHEN sor.created_at >= '{recent_start}' THEN sol.price_unit * sol.quantity ELSE 0 END) AS pl_actual_30d,
           SUM(sol.price_unit * sol.quantity) AS pl_sum_90d
    FROM sale_orderrequestline sol
    JOIN sale_orderrequest sor ON sor.id = sol.order_request_id
    JOIN customer_management_customer cc ON cc.id = sor.partner_id
    JOIN products_product pp ON pp.id = sol.product_id
    JOIN products_template pt ON pt.id = pp.template_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
      AND sor.status = 'processed'
      AND pt.business_segment_name = 'PRIVATE LABEL'
      AND sor.created_at >= '{ninety_start}' AND sor.created_at <= '{plan_date}'
    GROUP BY cc.partner_id
    """


def _sql_bo4_momentum(dc_ids: List[str], plan_date: str) -> str:
    # Real per-DC BO4 (Sales Momentum) scoring, wired 2026-08-06 -- 4.2's Momentum =
    # Total_Sales_This_Period / Total_Working_Days_In_Period, graded against
    # Prior_Momentum x Category_Multiplier (4.4, GR-25). Same invoice_liquidation_with_pog
    # source as Liquidation_Normalized (SQL_LIQUIDATION_3D in se_daily_plan_agent.py) --
    # partner_id IS sap_partner_id directly here, no customer_management_customer bridge
    # needed (confirmed live via a join to input_partner_details), unlike orders/payments.
    # Grouped by business_category since 4.4's multiplier is category-specific; caller
    # picks each DC's dominant category (highest combined this+prior sales) in Python.
    d = datetime.fromisoformat(plan_date).date()
    period_start = (d - timedelta(days=30)).isoformat()
    prior_start = (d - timedelta(days=60)).isoformat()
    return f"""
    SELECT partner_id AS dc_id, business_category,
           SUM(CASE WHEN invoice_date >= '{period_start}' THEN net_billed_amount ELSE 0 END) AS sum_this_30d,
           SUM(CASE WHEN invoice_date < '{period_start}' THEN net_billed_amount ELSE 0 END) AS sum_prior_30d
    FROM invoice_liquidation_with_pog
    WHERE partner_id IN ({_sql_list(dc_ids)})
      AND invoice_date >= '{prior_start}' AND invoice_date <= '{plan_date}'
    GROUP BY partner_id, business_category
    """


def _sql_bo5_meetings(se_emails: List[str], plan_date: str) -> str:
    # Real per-SE BO5 (Long-Term: farmer meetings) scoring, wired 2026-08-06 --
    # farmer_in_meeting_vw (Redshift dev db) is real, live (max meeting_date = today),
    # one row per (meeting, farmer). email matches Assigned_SE_Email directly, no bridge
    # table needed (confirmed live -- a real SE from this project's own Jaipur data
    # appears in it). The materialized view farmer_in_meeting_mv is permission-blocked;
    # vw_extension_meeting's partner/agent_id columns don't match customer_management_
    # customer.id/users_user.id at all (confirmed live, zero matches) -- this view is the
    # one that actually joins. Attendee count aggregated in Python (caller groups by
    # meeting_id) since Mega-tier (>=50 attendees) vs Regular-tier (>=10) determines
    # what counts toward 5.3's "2 Mega meetings/month" target -- see score_bo5_long_term
    # docstring for why the real meeting_type column can't be used for this (no value
    # ever literally equals "Mega"/"Regular").
    d = datetime.fromisoformat(plan_date).date()
    period_start = (d - timedelta(days=30)).isoformat()
    return f"""
    SELECT email, meeting_id, COUNT(*) AS attendee_count
    FROM farmer_in_meeting_vw
    WHERE email IN ({_sql_list(se_emails)})
      AND meeting_date >= '{period_start}' AND meeting_date <= '{plan_date}'
    GROUP BY email, meeting_id
    """


def _sql_bo5_meetings_mtd(se_emails: List[str], plan_date: str) -> str:
    # 8.11 Layer 0 (FM_Urgency), wired 2026-08-06 -- same farmer_in_meeting_vw source as
    # _sql_bo5_meetings() above, but calendar-month-to-date, NOT a rolling 30 days.
    # FM_Urgency's own formula is explicitly "Days_Remaining_In_Month" -- a calendar
    # concept a rolling window can't represent -- so this gets its own MTD query rather
    # than reusing BO5's scoring window. COMPUTE-AND-LOG ONLY, see compute_fm_urgency().
    d = datetime.fromisoformat(plan_date).date()
    month_start = d.replace(day=1).isoformat()
    return f"""
    SELECT email, meeting_id, COUNT(*) AS attendee_count
    FROM farmer_in_meeting_vw
    WHERE email IN ({_sql_list(se_emails)})
      AND meeting_date >= '{month_start}' AND meeting_date <= '{plan_date}'
    GROUP BY email, meeting_id
    """


def _sql_bo5_first_orders(dc_ids: List[str]) -> str:
    # 5.2: Onboarded = DC's first order ever placed. Reuses the customer_management_
    # customer bridge _sql_orders() already established (sale_orderrequest.partner_id is
    # the internal customer id, not sap_partner_id directly). Unrestricted by date --
    # need the TRUE earliest order, not one inside a lookback window, same reasoning
    # _sql_orders()/_sql_payments() already documented for why an unrestricted pull is
    # fine once already scoped to a handful of dc_ids.
    return f"""
    SELECT dc_id, MIN(created_at) AS first_order_date
    FROM (
        SELECT cc.partner_id AS dc_id, o.created_at
        FROM sale_orderrequest o
        JOIN customer_management_customer cc ON cc.id = o.partner_id
        WHERE cc.partner_id::text IN ({_sql_list(dc_ids)}) AND o.status = 'processed'
    ) x
    GROUP BY dc_id
    """


def _sql_dc_purchase_summary(dc_ids: List[str], plan_date: str) -> str:
    # Pitching Agent (S3 purchase-half / S6 / S7), wired 2026-08-08 -- reuses the
    # customer_management_customer bridge and status='processed' rule already
    # established by _sql_orders()/_sql_bo5_first_orders(). Fiscal year = April-March,
    # same inference _fiscal_year_start() already uses elsewhere in this file (confirmed
    # live query pattern from the normalization doc's own "Last Year/YTD DC Purchase"
    # sections -- not independently re-derived, same FY assumption, same caveat: an
    # inference from "FY26-27"-style naming, not an independently confirmed business rule).
    d = datetime.fromisoformat(plan_date).date()
    month_start = (d - timedelta(days=30)).isoformat()
    fy_start = _fiscal_year_start(plan_date)
    py_start = _fiscal_year_start((d.replace(year=d.year - 1)).isoformat())
    return f"""
    SELECT cc.partner_id AS dc_id,
           SUM(CASE WHEN o.created_at >= '{month_start}' THEN sol.price_unit * sol.quantity ELSE 0 END) AS purchase_30d,
           SUM(CASE WHEN o.created_at >= '{py_start}' AND o.created_at < '{fy_start}' THEN sol.price_unit * sol.quantity ELSE 0 END) AS purchase_last_fy,
           SUM(CASE WHEN o.created_at >= '{fy_start}' THEN sol.price_unit * sol.quantity ELSE 0 END) AS purchase_ytd
    FROM sale_orderrequest o
    JOIN customer_management_customer cc ON cc.id = o.partner_id
    JOIN sale_orderrequestline sol ON sol.order_request_id = o.id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)}) AND o.status = 'processed'
      AND o.created_at >= '{py_start}'
    GROUP BY cc.partner_id
    """


def _sql_dc_sale_summary(dc_ids: List[str], plan_date: str) -> str:
    # Pitching Agent (S3 sale-half), wired 2026-08-08 -- pathik_report.total_billed_amount
    # confirmed live 2026-08-08 (this column exists; the normalization doc had flagged it
    # "not re-verified this round"). Same pathik_report table already used for YTD PL
    # (_sql_ytd_pl) and BO1 (_sql_pl_metrics) -- this is a DIFFERENT column on that same
    # table (total billed, not PL-tagged billed), so DC selling to farmers overall, not
    # just the PL portion.
    d = datetime.fromisoformat(plan_date).date()
    month_start = (d - timedelta(days=30)).isoformat()
    return f"""
    SELECT sap_partner_id AS dc_id,
           SUM(CASE WHEN transaction_date >= '{month_start}' THEN total_billed_amount ELSE 0 END) AS sale_30d
    FROM pathik_report
    WHERE sap_partner_id IN ({_sql_list(dc_ids)})
      AND transaction_date >= '{month_start}' AND transaction_date <= '{plan_date}'
    GROUP BY sap_partner_id
    """


def _sql_last_discount(dc_ids: List[str], plan_date: str) -> str:
    # Pitching Agent (S2, Last Discount only -- Suggested Discount has no source table
    # anywhere in this system, confirmed exhaustively by the normalization doc; never
    # invented here). Reuses the Source 3h join chain confirmed live 2026-08-08:
    # sale_orderrequestline -> products_product -> products_template, back to
    # sale_orderrequest for the DC and invoice date. discount_price_unit is null when no
    # discount was applied (confirmed live) -- NOT coerced to 0 here, left as NULL so the
    # Pitching Agent can tell "no discount recorded" apart from "confirmed zero discount."
    return f"""
    SELECT dc_id, discount_price_unit, price_unit FROM (
        SELECT cc.partner_id AS dc_id, sol.discount_price_unit, sol.price_unit,
               ROW_NUMBER() OVER (PARTITION BY cc.partner_id ORDER BY o.created_at DESC) AS rn
        FROM sale_orderrequest o
        JOIN customer_management_customer cc ON cc.id = o.partner_id
        JOIN sale_orderrequestline sol ON sol.order_request_id = o.id
        WHERE cc.partner_id::text IN ({_sql_list(dc_ids)}) AND o.status = 'processed'
    ) ranked
    WHERE rn = 1
    """


def _sql_block_category_purchase(dc_ids: List[str], plan_date: str) -> str:
    # Pitching Agent (S1, Same-Block Purchase), wired 2026-08-08 -- trailing-30d purchase
    # summed by (dc_id, category), for the caller to aggregate into a block-level peer
    # average in Python once each DC's Block is known (from Geo_Mapping_1c, resolved
    # separately -- this query has no block column of its own, sale_orderrequest has no
    # geo data). Category granularity (not per-SKU) matches the doc's own example
    # phrasing ("PL फर्टिलाइज़र ₹15,000") reasonably well without full per-product detail.
    d = datetime.fromisoformat(plan_date).date()
    month_start = (d - timedelta(days=30)).isoformat()
    return f"""
    SELECT cc.partner_id AS dc_id, cat.name AS category_name,
           SUM(sol.price_unit * sol.quantity) AS purchase_30d
    FROM sale_orderrequest o
    JOIN customer_management_customer cc ON cc.id = o.partner_id
    JOIN sale_orderrequestline sol ON sol.order_request_id = o.id
    JOIN products_product prod ON prod.id = sol.product_id
    JOIN products_template tmpl ON tmpl.id = prod.template_id
    LEFT JOIN products_category cat ON cat.id = tmpl.category_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)}) AND o.status = 'processed'
      AND o.created_at >= '{month_start}'
    GROUP BY cc.partner_id, cat.name
    """


def _sql_punch_in(se_user_ids: List[int], plan_date: str) -> str:
    # Earliest check-in of the plan date per SE, from attendance_attendance
    # (input-backend) -- the actual punch-in point sequence_with_distance() needs to
    # sequence Punch-in -> DC1 -> DC2 -> ... instead of starting from the first DC.
    return f"""
    SELECT user_id AS se_user_id, check_in_latitude AS lat, check_in_longitude AS lon, check_in_time
    FROM attendance_attendance
    WHERE user_id IN ({",".join(str(u) for u in se_user_ids)})
      AND check_in_time >= '{plan_date}' AND check_in_time < DATE '{plan_date}' + INTERVAL '1 day'
    ORDER BY user_id, check_in_time ASC
    """


def _sql_prev_punch_in(se_user_ids: List[int], plan_date: str) -> str:
    # Routing Agent R0.4, CONFIRMED (OQ-2/OQ-11): Origin_Point = the SE's previous
    # working day's punch-in, not today's -- distinct from _sql_punch_in() above (which
    # is plan_date's own punch-in, used as a fallback by planning.routing when no
    # prior-day one exists). No DISTINCT ON (unsupported on this cluster's Postgres
    # dialect, per every other query in this file) -- two window functions instead:
    # day_rank picks each SE's most recent date strictly before plan_date, rn_in_day
    # picks that date's earliest check-in (same "earliest of the day" convention as
    # _sql_punch_in).
    return f"""
    SELECT se_user_id, lat, lon FROM (
        SELECT user_id AS se_user_id, check_in_latitude AS lat, check_in_longitude AS lon,
               ROW_NUMBER() OVER (PARTITION BY user_id, check_in_time::date ORDER BY check_in_time ASC) AS rn_in_day,
               DENSE_RANK() OVER (PARTITION BY user_id ORDER BY check_in_time::date DESC) AS day_rank
        FROM attendance_attendance
        WHERE user_id IN ({",".join(str(u) for u in se_user_ids)})
          AND check_in_time < '{plan_date}'
    ) t
    WHERE rn_in_day = 1 AND day_rank = 1
    """


# --- Reconciliation SQL builders (feedback loop, Tier 1) -- same join keys/tables as
# _sql_last_visit()/_sql_orders() above, just windowed forward [plan_date, plan_date+2]
# instead of a trailing lookback, since these answer "did the assigned task actually
# happen" rather than "what happened before this plan was made". Payments are
# deliberately NOT reconciled here -- payments_paymenttransaction has no amount column
# confirmed live anywhere in this codebase (see _sql_payments()), so
# DailyTask.actual_payment_amount is left honestly unpopulated rather than guessed. ---

def _sql_visit_outcomes(dc_ids: List[str], se_user_ids: List[int], plan_date: str) -> str:
    return f"""
    SELECT cc.partner_id AS sap_partner_id, p.user_id AS se_user_id, p.plan_execution_date, t.status AS task_status
    FROM task_management_task t
    JOIN task_management_plan p ON p.id = t.plan_id
    JOIN customer_management_customer cc ON cc.id = t.partner_id
    WHERE t.visit_type_id = 1 AND p.user_id IN ({",".join(str(u) for u in se_user_ids)})
      AND cc.partner_id::text IN ({_sql_list(dc_ids)})
      AND p.plan_execution_date >= DATE '{plan_date}' AND p.plan_execution_date <= DATE '{plan_date}' + INTERVAL '2 days'
    ORDER BY cc.partner_id, p.plan_execution_date ASC
    """


def _sql_order_outcomes(dc_ids: List[str], plan_date: str) -> str:
    return f"""
    SELECT dc_id, amount_total, created_at
    FROM (
        SELECT cc.partner_id AS dc_id, o.amount_total, o.created_at,
               ROW_NUMBER() OVER (PARTITION BY cc.partner_id ORDER BY o.amount_total DESC) AS rn
        FROM sale_orderrequest o
        JOIN customer_management_customer cc ON cc.id = o.partner_id
        WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
          AND o.created_at >= DATE '{plan_date}' AND o.created_at <= DATE '{plan_date}' + INTERVAL '2 days'
    ) ranked
    WHERE rn = 1
    """


def _resolve_geo_mapping(client: "agent.MetabaseClient", geo_mapping_cache: Optional[Dict[str, agent.Table]] = None) -> agent.Table:
    """geo_mapping_cache, when passed, is a single-request cache shared with callers
    later in the same generate_plan_for_scope() run (e.g. the Pitching Agent's block
    resolution) -- Source 1c is a full-table pull with no filter args, so a second call
    within one request would otherwise re-fetch byte-identical rows over the network."""
    if geo_mapping_cache is not None and "value" in geo_mapping_cache:
        return geo_mapping_cache["value"]
    if not client.configured:
        raise PlanningError(
            "ABM/RBM/BLOCK/DISTRICT scopes require the canonical geo hierarchy (Source 1c), "
            "which only exists live. Set METABASE_URL/METABASE_API_KEY to use this scope."
        )
    geo_mapping = client.execute_sql(agent.REDSHIFT_DB_ID, _sql_geo_mapping_full())
    if geo_mapping_cache is not None:
        geo_mapping_cache["value"] = geo_mapping
    return geo_mapping


def resolve_scope_dcs(
    scope_type: str, scope_value: str, dc_master: agent.Table, client: "agent.MetabaseClient",
    geo_mapping_cache: Optional[Dict[str, agent.Table]] = None,
) -> agent.Table:
    scope_type = scope_type.upper()
    if scope_type == PlanRun.ScopeType.NODE:
        dcs = [d for d in dc_master if d.get("Node") == scope_value]
    elif scope_type == PlanRun.ScopeType.STATE:
        dcs = [d for d in dc_master if d.get("State") == scope_value]
    elif scope_type == PlanRun.ScopeType.SE:
        dcs = [d for d in dc_master if d.get("Assigned_SE_Email") == scope_value]
    elif scope_type in (PlanRun.ScopeType.ABM, PlanRun.ScopeType.RBM, PlanRun.ScopeType.BLOCK, PlanRun.ScopeType.DISTRICT):
        geo_mapping = _resolve_geo_mapping(client, geo_mapping_cache)
        # Geo_Mapping_1c is returned with its raw SQL column aliases (lowercase) -- it is
        # NEVER passed through a normalize_* step in the agent (see SQL_GEO_MAPPING_1C /
        # run_pipeline's tables dict), unlike every other _Normalized table. Don't assume
        # Capitalized keys here.
        field = {"ABM": "abm_e_code", "RBM": "rbm_e_code", "BLOCK": "block", "DISTRICT": "district"}[scope_type]
        matching_dc_ids = {g["dc_id"] for g in geo_mapping if g.get(field) == scope_value}
        dcs = [d for d in dc_master if d.get("DC_ID") in matching_dc_ids]
    else:
        raise PlanningError(f"Unknown scope_type '{scope_type}' -- expected one of {[c.value for c in PlanRun.ScopeType]}")

    if not dcs:
        raise PlanningError(f"No DCs found for {scope_type}='{scope_value}'. Check the value against DC_Master_Normalized.json (and Source 1c for ABM/RBM/BLOCK/DISTRICT).")
    return dcs


def make_farmer_meeting_asker(stdout, style) -> Optional[Callable[[str, Dict[str, Any]], bool]]:
    """Builds an interactive Farmer Meeting confirmation prompt for CLI commands
    (activate_tuff/generate_se_plan) -- wired 2026-08-06 per direct instruction: FM_Urgency
    is a signal to ask about, never an auto-trigger. Returns None (no asking, DC Visit
    always prioritized) when stdin isn't a real TTY -- never blocks/EOFErrors under cron,
    scripting, or piped output, same honest-degrade instinct as the rest of this codebase.
    Shared by both interactive commands rather than duplicated."""
    if not sys.stdin.isatty():
        return None

    def ask(email: str, fm_result: Dict[str, Any]) -> bool:
        stdout.write(style.WARNING(f"\n  {email}: {fm_result['reason']}"))
        answer = input(f"  Schedule a Farmer Meeting for {email} today instead of DC Visits? [y/N]: ").strip().lower()
        return answer == "y"

    return ask


@transaction.atomic
def generate_plan_for_scope(
    scope_type: str, scope_value: str, plan_date: Optional[str] = None,
    farmer_meeting_asker: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
    farmer_meeting_confirmed_emails: Optional[set] = None,
) -> PlanRun:
    """The single entry point every endpoint calls. Resolves scope -> DCs -> SEs, pulls
    live Sources 1/3/4 data scoped to just those DCs/SEs (not a full pipeline run), calls
    the real generate_se_daily_plan() per SE, and persists PlanRun + DailyTask +
    ExceptionRecord rows. Returns the saved PlanRun (tasks/exceptions via related_name).

    farmer_meeting_asker: optional, see make_farmer_meeting_asker(). Callers that don't
    pass one (run_scheduled_tuff, any Django API caller) get the safe default -- FM_Urgency
    is still computed and logged, but farmer_meeting_scheduled_today stays False for every
    SE, so DC Visit is always prioritized when no human is present to ask.

    farmer_meeting_confirmed_emails: explicit per-run override (--confirm-farmer-meeting on
    activate_tuff/generate_se_plan), wired 2026-08-07 -- lets a human confirm a specific
    SE's Farmer Meeting without needing a live interactive terminal (e.g. instructing the
    agent to run it on their behalf). Takes priority over farmer_meeting_asker and applies
    even to an SE that isn't FM_Urgency-flagged -- an explicit human instruction is a
    stronger signal than the pacing algorithm's opinion."""
    started_at = timezone.now()
    plan_date = plan_date or timezone.now().date().isoformat()
    constants = agent.BusinessConstants()
    client = agent.get_client()

    geo_mapping_cache: Dict[str, agent.Table] = {}
    dc_master = load_dc_master()
    scoped_dcs = resolve_scope_dcs(scope_type, scope_value, dc_master, client, geo_mapping_cache)
    dc_ids = [d["DC_ID"] for d in scoped_dcs]
    se_emails = sorted({d["Assigned_SE_Email"] for d in scoped_dcs if d.get("Assigned_SE_Email")})

    run_exceptions: List[Dict[str, Any]] = []

    if not se_emails:
        client.close()
        raise PlanningError(f"{len(scoped_dcs)} DC(s) found for {scope_type}='{scope_value}', but none have an assigned SE (Unassigned_DC) -- nothing to plan.")

    se_user_ids: Dict[str, int] = {}
    if client.configured:
        try:
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_users(se_emails)):
                se_user_ids[row["email"]] = row["user_id"]
        except Exception as e:
            run_exceptions.append({"source": "users_user", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

    if client.configured:
        # Only flag per-SE resolution failures when resolution was actually attempted --
        # when Metabase isn't configured at all, that's already one clear flag above,
        # not N misleading "we tried and failed" ones.
        for email in se_emails:
            if email not in se_user_ids:
                run_exceptions.append({"source": "users_user", "reason_code": "SE_User_ID_Unresolved", "detail": f"Could not resolve user_id for {email} -- excluded from this run"})
        se_emails = [e for e in se_emails if e in se_user_ids]

    last_visit_by_dc: Dict[str, str] = {}
    recent_attempts_by_se_dc: Dict[int, Dict[str, int]] = {}
    visits_last30_by_se: Dict[int, set] = {}
    dc_financials: Dict[str, Dict[str, Any]] = {}
    last_payment_by_dc: Dict[str, str] = {}
    dc_club_by_id: Dict[str, Dict[str, Any]] = {}
    geo_by_dc: Dict[str, tuple] = {}
    ytd_pl_by_dc: Dict[str, float] = {}
    punch_in_by_se: Dict[int, tuple] = {}
    prev_punch_in_by_se: Dict[int, tuple] = {}  # Routing Agent R0.4 -- Origin_Point
    attendance_ok_by_se: Dict[int, bool] = {}
    dc_bo_scores: Dict[str, Dict[str, Any]] = {}
    meetings_held_by_se: Dict[str, int] = {}
    dcs_onboarded_by_se: Dict[str, int] = {}
    fm_urgency_by_se: Dict[str, Dict[str, Any]] = {}
    farmer_meeting_confirmed_by_se: Dict[str, bool] = {}

    if client.configured and se_emails:
        uids = [se_user_ids[e] for e in se_emails]
        fatigue_start = (datetime.fromisoformat(plan_date) - timedelta(days=constants.contact_fatigue_window_days)).date().isoformat()

        try:
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_last_visit(dc_ids, uids, agent.LOOKBACK_DAYS)):
                dc_id, uid, date, status = row["sap_partner_id"], row["se_user_id"], agent.standardize_date(row["plan_execution_date"]), row["task_status"]
                if dc_id not in last_visit_by_dc or date > last_visit_by_dc[dc_id]:
                    last_visit_by_dc[dc_id] = date
                if fatigue_start <= date < plan_date:
                    recent_attempts_by_se_dc.setdefault(uid, {})
                    recent_attempts_by_se_dc[uid][dc_id] = recent_attempts_by_se_dc[uid].get(dc_id, 0) + 1
                visits_last30_by_se.setdefault(uid, set()).add(dc_id)
        except Exception as e:
            run_exceptions.append({"source": "task_management_task", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        try:
            for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_geo(dc_ids)):
                lat, lon = agent.parse_number(row.get("latitude")), agent.parse_number(row.get("longitude"))
                if lat is not None and lon is not None:
                    geo_by_dc[row["sap_partner_id"]] = (lat, lon)
        except Exception as e:
            run_exceptions.append({"source": "input_partner_details", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        # dc_datamart (dev, Redshift) via the same `client` as everything else -- see
        # _sql_outstanding() for why this replaced customer_management_input_outstanding.
        outstanding_raw: agent.Table = []
        try:
            outstanding_raw = client.execute_sql(agent.REDSHIFT_DB_ID, _sql_outstanding(dc_ids))
        except Exception as e:
            run_exceptions.append({"source": "dc_datamart", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        orders_raw: agent.Table = []
        try:
            orders_raw = client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_orders(dc_ids))
        except Exception as e:
            run_exceptions.append({"source": "sale_orderrequest", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        fin_exc = agent.Exceptions(agent.utc_now_iso())
        _, dc_financials = agent.normalize_sales_transactions([], outstanding_raw, orders_raw, fin_exc)
        run_exceptions.extend({"source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in fin_exc.rows)

        # Feedback loop, Tier 2 (adaptive weighting) -- dc_id -> owning SE's user_id, so
        # a DC's BO score can be weighted by ITS SE's trailing-30d completion rate for
        # that objective, not a network-wide average. Stats come from
        # compute_completion_stats (run daily via cron, see run_scheduled_tuff); neutral
        # (1.0x) for any SE/objective with no stats yet -- see agent.completion_multiplier().
        dc_to_se_uid: Dict[str, int] = {
            dc["DC_ID"]: se_user_ids[dc["Assigned_SE_Email"]]
            for dc in scoped_dcs
            if dc.get("Assigned_SE_Email") in se_user_ids
        }
        completion_stats = {
            (s.se_id, s.objective): (s.completion_rate_30d, s.sample_size)
            for s in ObjectiveCompletionStats.objects.filter(se_id__in=[str(u) for u in se_user_ids.values()])
        }

        def _mult(dc_id: str, objective: str) -> float:
            uid = dc_to_se_uid.get(dc_id)
            if uid is None:
                return 1.0
            rate, n = completion_stats.get((str(uid), objective), (None, 0))
            return agent.completion_multiplier(rate, n)

        # Real per-DC BO3 (Outstanding) scoring -- see score_bo3_outstanding_live_proxy()
        # docstring for why this is a live-data substitute for the literal 3.1-3.6
        # formula, not that formula itself (Expected_Outstanding needs last month's
        # outstanding balance, which has no historical/time-series source in this
        # pipeline). Wired 2026-08-06 so Outstanding-qualifying DCs get a real,
        # DC-specific severity in Layer 3's ranking instead of one shared SE-level floor
        # score that made every Outstanding match lose to Visits regardless of amount.
        dc_bo_scores = {
            dc_id: {"Outstanding": agent.score_bo3_outstanding_live_proxy(
                fin.get("Current_Outstanding"), fin.get("Current_Overdue"), fin.get("OS_90_Plus"), constants,
                weight_multiplier=_mult(dc_id, "Outstanding"),
            )}
            for dc_id, fin in dc_financials.items()
        }

        # Real per-DC BO1 (PL) scoring, wired 2026-08-06, AOP-target leg added 2026-08-09.
        # score_bo1_private_label() (PL_Ratio -> A/B/C/D per 1.5's confirmed cutoffs) is
        # fed a PL_Expected blended from up to two legs:
        #   1. Trailing-average leg (original, 2026-08-06): pl_sum_90d/3, a 90-day
        #      baseline scaled to a 30-day equivalent.
        #   2. AOP-target leg (new): the AOP source (Niyojan dashboard export) has no
        #      DC/SE dimension at all, only Node x Material x Month (confirmed live
        #      2026-08-09, see aop_pl_target_by_node() docstring) -- so this leg is each
        #      DC's Node-level PL AOP target, ALLOCATED down by the DC's share of its
        #      Node's trailing-90d PL sales. This is an ESTIMATE, not a confirmed per-DC
        #      AOP figure -- every DC that gets it flagged in its Reason_Of_Visit, never
        #      silently blended in unlabeled.
        # Combining both legs via simple average is an engineering default, not a
        # confirmed formula -- 1.2's own combination method (90-day-average vs AOP
        # target) is still TBD in Source 5, unconfirmed either way. Falls back to
        # whichever single leg is available, same honest-degrade pattern as before, if
        # only one exists; None (Config_Ambiguous) if neither does.
        #
        # Allocation-share denominator (fixed 2026-08-10): a NODE/STATE-scoped plan
        # already covers every DC under its nodes, so dc_ids IS the full node -- no
        # extra query needed there. An SE/BLOCK-scoped plan only sees its own DCs, so
        # the trailing-PL total is topped up with a second, node-wide query for the
        # sibling DCs it's missing (looked up locally from the full dc_master, not a
        # live join) -- sibling DCs feed the denominator only, never dc_bo_scores, since
        # they're outside this PlanRun's scope.
        try:
            aop_targets = load_aop_targets()
            node_pl_aop_targets = agent.aop_pl_target_by_node(aop_targets, plan_date)
            dc_node_by_id = {dc["DC_ID"]: (dc.get("Node") or "").strip().upper() for dc in scoped_dcs}

            pl_actual_30d_by_dc: Dict[str, float] = {}
            pl_sum_90d_by_dc: Dict[str, float] = {}
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_pl_metrics(dc_ids, plan_date)):
                dc_id = agent.normalize_id(row.get("dc_id"))
                if not dc_id:
                    continue
                pl_actual_30d_by_dc[dc_id] = agent.parse_number(row.get("pl_actual_30d")) or 0.0
                pl_sum_90d = agent.parse_number(row.get("pl_sum_90d"))
                if pl_sum_90d is not None:
                    pl_sum_90d_by_dc[dc_id] = pl_sum_90d

            nodes_in_scope = {n for n in dc_node_by_id.values() if n}
            dc_node_by_id_full = dict(dc_node_by_id)
            sibling_dc_ids = sorted({
                dc["DC_ID"] for dc in dc_master
                if (dc.get("Node") or "").strip().upper() in nodes_in_scope and dc["DC_ID"] not in dc_node_by_id
            })
            node_total_pl_sum_90d_by_dc = dict(pl_sum_90d_by_dc)
            if sibling_dc_ids:
                dc_node_by_id_full.update({
                    dc["DC_ID"]: (dc.get("Node") or "").strip().upper()
                    for dc in dc_master if dc["DC_ID"] in sibling_dc_ids
                })
                try:
                    for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_pl_metrics(sibling_dc_ids, plan_date)):
                        sib_dc_id = agent.normalize_id(row.get("dc_id"))
                        pl_sum_90d = agent.parse_number(row.get("pl_sum_90d"))
                        if sib_dc_id and pl_sum_90d is not None:
                            node_total_pl_sum_90d_by_dc[sib_dc_id] = pl_sum_90d
                except Exception as e:
                    # Node-wide total falls back to the scope-limited one (old behavior,
                    # still correct for NODE/STATE) rather than losing the whole PL block.
                    run_exceptions.append({
                        "source": "sale_orderrequestline", "reason_code": "AOP_Node_Total_Sibling_Pull_Failed",
                        "detail": f"{type(e).__name__}: {e} -- AOP-allocated leg falls back to scope-limited (not node-wide) trailing-PL total",
                    })

            node_trailing_pl_total: Dict[str, float] = {}
            for dc_id, val in node_total_pl_sum_90d_by_dc.items():
                if val > 0:
                    node = dc_node_by_id_full.get(dc_id, "")
                    node_trailing_pl_total[node] = node_trailing_pl_total.get(node, 0.0) + val

            leg_aop_by_dc: Dict[str, float] = {}
            for dc_id in set(pl_actual_30d_by_dc) | set(pl_sum_90d_by_dc):
                pl_sum_90d = pl_sum_90d_by_dc.get(dc_id)
                leg_trailing = (pl_sum_90d / 3.0) if pl_sum_90d else None

                leg_aop = None
                node = dc_node_by_id.get(dc_id, "")
                node_target = node_pl_aop_targets.get(node)
                node_total = node_trailing_pl_total.get(node)
                if node_target and node_total and pl_sum_90d and pl_sum_90d > 0:
                    leg_aop = node_target * (pl_sum_90d / node_total)
                    leg_aop_by_dc[dc_id] = leg_aop

                legs = [v for v in (leg_trailing, leg_aop) if v is not None]
                pl_expected = (sum(legs) / len(legs)) if legs else None

                result = agent.score_bo1_private_label(
                    pl_actual_30d_by_dc.get(dc_id, 0.0), pl_expected, constants, weight_multiplier=_mult(dc_id, "PL"),
                )
                if leg_aop is not None:
                    result["reason"] += "; AOP-allocated leg blended in (Node target x trailing-PL share, estimate, not a confirmed per-DC AOP figure)"
                dc_bo_scores.setdefault(dc_id, {})["PL"] = result

            # SE-level AOP PL target rollup (new 2026-08-10): the AOP source has no SE
            # dimension either, so this is simply each SE's assigned-and-in-scope DCs'
            # leg_aop values summed via DC_Master's Assigned_SE_Email -- same estimate,
            # one level up, surfaced the same Provisional way as every other unconfirmed
            # figure in this pipeline (never silently treated as a real incentive target).
            if leg_aop_by_dc:
                se_by_dc = {dc["DC_ID"]: dc.get("Assigned_SE_Email") for dc in scoped_dcs}
                se_aop_totals: Dict[str, float] = {}
                se_aop_dc_counts: Dict[str, int] = {}
                for dc_id, val in leg_aop_by_dc.items():
                    se = se_by_dc.get(dc_id)
                    if not se:
                        continue
                    se_aop_totals[se] = se_aop_totals.get(se, 0.0) + val
                    se_aop_dc_counts[se] = se_aop_dc_counts.get(se, 0) + 1
                for se, total in se_aop_totals.items():
                    run_exceptions.append({
                        "source": "AOP_Target_Normalized", "record_id": se,
                        "reason_code": "SE_AOP_PL_Target_Estimate",
                        "detail": (
                            f"{se}: allocated PL AOP target ~Rs.{total:,.0f} across {se_aop_dc_counts[se]} "
                            "assigned DC(s) this month (estimate: sum of each DC's Node-share allocation, "
                            "not a confirmed per-SE AOP figure)"
                        ),
                    })
        except Exception as e:
            run_exceptions.append({"source": "sale_orderrequestline", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        # Real per-DC BO4 (Sales Momentum) scoring, wired 2026-08-06 -- see
        # _sql_bo4_momentum()/score_bo4_sales_momentum() docstrings. Deliberately NOT
        # added to QUALIFIERS in se_daily_plan_agent.py -- scored and available (e.g. via
        # --json), zero effect on which DCs get selected for a daily task list, per
        # 8.12/GR-25 (Sales/Liquidation stay out of the DC Visit candidate pool).
        try:
            bo4_rows_by_dc: Dict[str, List[Dict[str, Any]]] = {}
            for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_bo4_momentum(dc_ids, plan_date)):
                dc_id = agent.normalize_id(row.get("dc_id"))
                if dc_id:
                    bo4_rows_by_dc.setdefault(dc_id, []).append(row)
            for dc_id, rows in bo4_rows_by_dc.items():
                # Dominant category = highest combined this+prior sales -- a DC selling
                # across multiple categories is graded on its largest one (documented
                # simplification, same single-tag-per-DC pattern DC_RAnk's Cohort uses).
                dominant = max(
                    rows,
                    key=lambda r: (agent.parse_number(r.get("sum_this_30d")) or 0.0) + (agent.parse_number(r.get("sum_prior_30d")) or 0.0),
                )
                momentum_this = (agent.parse_number(dominant.get("sum_this_30d")) or 0.0) / constants.bo4_momentum_period_days
                momentum_prior = (agent.parse_number(dominant.get("sum_prior_30d")) or 0.0) / constants.bo4_momentum_period_days
                dc_bo_scores.setdefault(dc_id, {})["Sales"] = agent.score_bo4_sales_momentum(
                    momentum_this, momentum_prior, dominant.get("business_category"), constants,
                )
        except Exception as e:
            run_exceptions.append({"source": "invoice_liquidation_with_pog", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        # Real per-SE BO5 (Long-Term) scoring, wired 2026-08-06 -- see
        # _sql_bo5_meetings()/_sql_bo5_first_orders() docstrings. Deliberately NOT added
        # to QUALIFIERS in se_daily_plan_agent.py -- Long-Term was already dropped from
        # Candidate_DCs in a prior session (GR-12: DC Visit / Farmer Meeting day-type
        # exclusivity) to fix a real Day_Type-mixing bug. BO5's real effect on task
        # generation runs through the separate farmer_meeting_scheduled_today gate
        # (FM_Urgency, below), now live -- see that block's comment for why.
        try:
            meeting_attendee_counts: Dict[str, Dict[str, int]] = {}
            for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_bo5_meetings(se_emails, plan_date)):
                email = row.get("email")
                meeting_id = row.get("meeting_id")
                if email and meeting_id is not None:
                    meeting_attendee_counts.setdefault(email, {})[meeting_id] = int(agent.parse_number(row.get("attendee_count")) or 0)
            for email, meetings in meeting_attendee_counts.items():
                meetings_held_by_se[email] = sum(1 for count in meetings.values() if count >= constants.bo5_mega_meeting_min_farmers)
        except Exception as e:
            run_exceptions.append({"source": "farmer_in_meeting_vw", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        try:
            onboard_window_start = (datetime.fromisoformat(plan_date) - timedelta(days=30)).date().isoformat()
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_bo5_first_orders(dc_ids)):
                dc_id = agent.normalize_id(row.get("dc_id"))
                first_order_date = agent.standardize_date(row.get("first_order_date"))
                if not dc_id or not first_order_date or not (onboard_window_start <= first_order_date <= plan_date):
                    continue
                dc = next((d for d in scoped_dcs if d.get("DC_ID") == dc_id), None)
                owner_email = dc.get("Assigned_SE_Email") if dc else None
                if owner_email:
                    dcs_onboarded_by_se[owner_email] = dcs_onboarded_by_se.get(owner_email, 0) + 1
        except Exception as e:
            run_exceptions.append({"source": "sale_orderrequest", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        # 8.11 Layer 0 (FM_Urgency), wired 2026-08-06, extended 2026-08-07 with an
        # explicit-override channel -- DC Visit is always prioritized by default; a
        # Farmer Meeting only happens if a human explicitly confirms it, either live
        # (farmer_meeting_asker, an interactive terminal prompt) or via
        # farmer_meeting_confirmed_emails (--confirm-farmer-meeting, no terminal needed).
        # The explicit-emails channel wins outright and applies even to an SE that isn't
        # FM_Urgency-flagged this run -- a direct human instruction outranks the pacing
        # algorithm's opinion. Any caller that passes neither (run_scheduled_tuff, Django
        # API) gets confirmed=False for everyone, no special-casing needed for
        # "unattended" -- the default already means DC Visit always wins.
        confirmed_emails = farmer_meeting_confirmed_emails or set()
        try:
            plan_date_obj = datetime.fromisoformat(plan_date).date()
            days_left_in_month = calendar.monthrange(plan_date_obj.year, plan_date_obj.month)[1] - plan_date_obj.day + 1
            mtd_meetings: Dict[str, set] = {}
            for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_bo5_meetings_mtd(se_emails, plan_date)):
                email = row.get("email")
                if email and (agent.parse_number(row.get("attendee_count")) or 0) >= constants.bo5_mega_meeting_min_farmers:
                    mtd_meetings.setdefault(email, set()).add(row.get("meeting_id"))
            for email in se_emails:
                held_mtd = len(mtd_meetings.get(email, set()))
                fm_urgency_by_se[email] = agent.compute_fm_urgency(held_mtd, days_left_in_month, constants)
                is_urgent = fm_urgency_by_se[email]["fm_urgency"]
                explicitly_confirmed = email in confirmed_emails
                if not (is_urgent or explicitly_confirmed):
                    continue
                if explicitly_confirmed:
                    confirmed = True
                elif farmer_meeting_asker:
                    confirmed = farmer_meeting_asker(email, fm_urgency_by_se[email])
                else:
                    confirmed = False
                farmer_meeting_confirmed_by_se[email] = confirmed
                fm_urgency_by_se[email]["confirmed"] = confirmed
                if confirmed:
                    basis = "explicitly confirmed via --confirm-farmer-meeting" if explicitly_confirmed else "confirmed by operator (interactive prompt)"
                    run_exceptions.append({
                        "source": "farmer_in_meeting_vw", "reason_code": "FM_Meeting_Confirmed",
                        "detail": f"{email}: {fm_urgency_by_se[email]['reason']} -- Farmer Meeting day {basis}, no DC Visit tasks today (GR-12)",
                    })
                else:
                    run_exceptions.append({
                        "source": "farmer_in_meeting_vw", "reason_code": "FM_Urgency_Provisional",
                        "detail": f"{email}: {fm_urgency_by_se[email]['reason']} -- DC Visit prioritized (no manual confirmation this run)",
                    })
        except Exception as e:
            run_exceptions.append({"source": "farmer_in_meeting_vw", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        pay_exc = agent.Exceptions(agent.utc_now_iso())
        try:
            payments_raw = client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_payments(dc_ids))
            _, last_payment_by_dc = agent.normalize_payments(payments_raw, pay_exc)
        except Exception as e:
            run_exceptions.append({"source": "payments_paymenttransaction", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})
        run_exceptions.extend({"source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in pay_exc.rows)

        club_exc = agent.Exceptions(agent.utc_now_iso())
        try:
            club_raw = client.execute_sql(agent.REDSHIFT_DB_ID, _sql_club_mapping(dc_ids))
            turnover_raw = client.execute_sql(agent.REDSHIFT_DB_ID, _sql_club_qualifying_turnover(dc_ids))
            club_rows = agent.normalize_dc_club(club_raw, [], turnover_raw, dc_financials, club_exc)
            dc_club_by_id = {row["DC_ID"]: row for row in club_rows}
        except Exception as e:
            run_exceptions.append({"source": "dc_mapping_club_scheme", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})
        run_exceptions.extend({"source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in club_exc.rows)

        try:
            fy_start = _fiscal_year_start(plan_date)
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_ytd_pl(dc_ids, fy_start, plan_date)):
                dc_id = agent.normalize_id(row.get("dc_id"))
                if dc_id:
                    ytd_pl_by_dc[dc_id] = agent.parse_number(row.get("ytd_pl"))
        except Exception as e:
            run_exceptions.append({"source": "sale_orderrequestline", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        try:
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_punch_in(uids, plan_date)):
                uid = row["se_user_id"]
                # Any check-in row on plan_date = attendance gate passes for that SE,
                # independent of whether its lat/long parsed cleanly below.
                attendance_ok_by_se[uid] = True
                if uid in punch_in_by_se:
                    continue  # keep earliest check-in only, rows already ordered ASC
                lat, lon = agent.parse_number(row.get("lat")), agent.parse_number(row.get("lon"))
                if lat is not None and lon is not None:
                    punch_in_by_se[uid] = (lat, lon)
        except Exception as e:
            run_exceptions.append({"source": "attendance_attendance", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        try:
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_prev_punch_in(uids, plan_date)):
                uid = row["se_user_id"]
                lat, lon = agent.parse_number(row.get("lat")), agent.parse_number(row.get("lon"))
                if lat is not None and lon is not None:
                    prev_punch_in_by_se[uid] = (lat, lon)
        except Exception as e:
            run_exceptions.append({"source": "attendance_attendance", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e} -- Routing Agent Origin_Point (R0.4) falls back to today's punch-in or defers"})
    else:
        run_exceptions.append({
            "source": "Source1/3/4", "reason_code": "Metabase_Not_Configured",
            "detail": "METABASE_URL/METABASE_API_KEY not set -- plan generated from DC_Master_Normalized.json only; "
                      "no live visit history, geo, financials, payments, or club data. Every DC treated as eligible "
                      "(In_Scope_Flag not re-checked against 6.2 recency), and every task is Provisional.",
        })

    excl_exc = agent.Exceptions(agent.utc_now_iso())
    agent.apply_dc_exclusion_rules(scoped_dcs, excl_exc, constants, last_visit_by_dc, plan_date)
    run_exceptions.extend({"source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in excl_exc.rows)
    for dc in scoped_dcs:
        lat_lon = geo_by_dc.get(dc["DC_ID"])
        dc["Latitude"], dc["Longitude"] = lat_lon if lat_lon else (None, None)
        dc["Last_Visit_Date"] = last_visit_by_dc.get(dc["DC_ID"])

    dynamic_params = agent.resolve_dynamic_parameters(scoped_dcs, [], {}, [], constants)
    # 8.11 Layer 0 (FM_Urgency) -- see compute_fm_urgency()/the block above. Each per-SE
    # entry now carries a "confirmed" key (only present if fm_urgency was True) showing
    # whether a human actually confirmed a Farmer Meeting this run -- the urgency signal
    # and the actual decision are both visible here, not just the signal.
    dynamic_params["8.11_fm_urgency"] = fm_urgency_by_se

    # Attendance gating (Section 3a), wired 2026-08-06. A forward-dated plan (planning
    # for a date that hasn't happened yet) can never have real attendance -- gate stays
    # None (unknowable, Provisional) exactly as before, matching the doc's own guidance
    # for "tomorrow"-style plans. For today-or-earlier, the gate is real: an SE with no
    # attendance_attendance row on plan_date gets attendance_gate_ok=False, and
    # generate_se_daily_plan() returns an empty Tasks list with Skipped_Reason for them
    # -- this is Section 3a's actual design ("attendance gates whether a plan is
    # generated at all"), not a bug, but it does mean a same-day plan run before an SE's
    # morning punch-in will legitimately come back empty for that SE.
    # Also stays unknowable (None) when the live client isn't configured at all --
    # attendance_ok_by_se would be empty not because no one punched in, but because we
    # never checked. Defaulting to False in that case would wrongly zero out every plan.
    attendance_unknowable = plan_date > timezone.now().date().isoformat() or not client.configured

    plan_run = PlanRun.objects.create(
        scope_type=scope_type.upper(), scope_value=scope_value, plan_date=plan_date,
        metabase_configured=client.configured, se_count=len(se_emails), dc_count=len(scoped_dcs),
        dynamic_parameters=dynamic_params, started_at=started_at,
        note="Attendance gating IS wired (as of 2026-08-06): for a forward-dated plan_date, "
             "attendance_gate_ok stays None (Provisional -- can't be known yet); for today or "
             "earlier, an SE with no attendance_attendance check-in on plan_date gets an empty "
             "Tasks list with Skipped_Reason, per Section 3a's design -- this is expected "
             "behavior for an SE who hasn't punched in yet, not a bug. Punch-in-based route "
             "sequencing and YTD Private Label are wired: distance sequences from the SE's actual "
             "first check-in of plan_date when available, else falls back to starting from the "
             "first DC; YTD PL sums pathik_report.pl_billed_amount from fiscal-year start to "
             "plan_date, per DC. Task generation uses the Daily Task Assignment Formula "
             "(8.9-8.12): max 5 bundled Visit tasks/day, DC-level Priority_Score ranking. Real "
             "per-DC BO3 (Outstanding) scoring is wired (score_bo3_outstanding_live_proxy) -- a "
             "live-data substitute for the literal 3.1-3.6 formula (no historical "
             "Expected_Outstanding source exists). Real per-DC BO1 (PL) and BO4 (Sales Momentum) "
             "scoring are also wired -- PL_Expected blends a 90-day trailing average with an "
             "AOP-target leg (as of 2026-08-09): the AOP source has no DC/SE dimension, only "
             "Node x Material x Month, so that leg is each DC's Node-level PL AOP target "
             "allocated by its trailing-PL sales share within the Node -- an estimate, flagged "
             "on every DC's Reason_Of_Visit that gets it, not a confirmed per-DC figure. Sales "
             "Momentum is scored but deliberately excluded from DC Visit candidate selection per "
             "8.12/GR-25. Long-Term (BO5) is correctly SE-level, not a gap -- it's not a "
             "DC-scoped objective and routes through the separate FM_Urgency gate, not dc_bo_scores.",
    )

    total_tasks = 0
    skipped_ses: List[Dict[str, Any]] = []
    # Collected across every SE and bulk_create()'d once after the loop, instead of one
    # DailyTask.objects.create() per task (up to 5/SE, capped by the Daily Task
    # Assignment Formula) -- a STATE-scoped run over 100+ SEs previously issued a
    # separate INSERT round-trip per task (370 for a real Bihar run) inside the same
    # atomic transaction anyway, all avoidable ORM/query-building overhead.
    pending_tasks: List[DailyTask] = []
    for email in se_emails:
        uid = se_user_ids.get(email, email)
        se_dcs = [dc for dc in scoped_dcs if dc.get("Assigned_SE_Email") == email]
        in_scope = [dc for dc in se_dcs if dc.get("In_Scope_Flag")]
        bo_scores = {
            "Visits": agent.score_bo2_visits(len(visits_last30_by_se.get(uid, set())), len(se_dcs), constants),
            "PL": {"score_pct": None, "grade": None, "reason": "PL_Value/PL_Expected not wired into this endpoint"},
            "Outstanding": {"ratio": None, "grade": None, "reason": "BO3 ratio needs last-month-OS/growth% -- not wired"},
            "Sales": {"score_pct": None, "grade": None},
            "Liquidation": {"score_pct": None, "grade": None, "reason": "no confirmed scoring formula exists (Source 3d Provisional)"},
            "Long-Term": agent.score_bo5_long_term(meetings_held_by_se.get(email, 0), dcs_onboarded_by_se.get(email, 0), constants),
        }
        attendance_gate_ok = None if attendance_unknowable else attendance_ok_by_se.get(uid, False)

        # Routing Agent hookup (R0.4 Origin_Point): prefer the previous working day's
        # punch-in; fall back to plan_date's own punch-in (punch_in_coords, already
        # fetched above) only if no prior-day one exists; defer entirely (None) if
        # neither does, per R0.4's "waits for today's real punch-in instead of
        # guessing" rule -- planning.routing.generate_route_plans_for_se() honors that
        # by producing no RoutePlan/DailyTask rows for this SE this run.
        def _route_selector(candidates, se_id_str, plan_date_, punch_in_coords, constants_, dc_by_id_, _uid=uid, _email=email):
            prev = prev_punch_in_by_se.get(_uid)
            if prev is not None:
                origin, origin_basis = prev, "prev_day_punch_in"
            elif punch_in_coords is not None:
                origin, origin_basis = punch_in_coords, "today_punch_in"
            else:
                origin, origin_basis = None, "waiting_for_today"
            result = routing.generate_route_plans_for_se(
                plan_run, str(_uid), _email, plan_date_, candidates, origin, origin_basis, constants_,
            )
            run_exceptions.extend(result["exceptions"])
            return result

        plan = agent.generate_se_daily_plan(
            str(uid), email, plan_date, in_scope, bo_scores, dynamic_params, constants,
            attendance_gate_ok=attendance_gate_ok, recent_attempts_by_dc=recent_attempts_by_se_dc.get(uid, {}),
            dc_financials=dc_financials, last_payment_by_dc=last_payment_by_dc, dc_club_by_id=dc_club_by_id,
            ytd_pl_by_dc=ytd_pl_by_dc, punch_in_coords=punch_in_by_se.get(uid), dc_bo_scores=dc_bo_scores,
            farmer_meeting_scheduled_today=farmer_meeting_confirmed_by_se.get(email, False),
            route_selector=_route_selector,
        )
        tasks = plan.get("Tasks", [])
        if not tasks:
            skipped_ses.append({
                "se_id": str(uid), "se_email": email,
                "reason": plan.get("Skipped_Reason") or "No in-scope DC qualified for any objective this run",
            })
        for t in tasks:
            pending_tasks.append(DailyTask(
                plan_run=plan_run, se_id=str(uid), se_name=email, plan_date=plan_date,
                sr_no=t["Sr_No"], dc_name=t["DC_Name"], dc_id=t["DC_ID"], distance_km=t["Distance_Km"],
                recommended_task_type=t["Recommended_Task_Type"], purpose_of_visit=t["Purpose_Of_Visit"],
                reason_of_visit=t["Reason_Of_Visit"], last_visit_date=t["Last_Visit_Date"],
                days_since_last_visit=t["Days_Since_Last_Visit"], present_outstanding=t["Present_Outstanding"],
                present_overdue=t["Present_Overdue"], overdue_aging_bucket=t.get("Overdue_Aging_Bucket"), last_order_date=t["Last_Order_Date"],
                last_order_value=t["Last_Order_Value"], last_payment_date=t["Last_Payment_Date"],
                last_payment_join_key_unconfirmed=t["Last_Payment_Join_Key_Unconfirmed"],
                ytd_private_label=t["YTD_Private_Label"], dc_club_participation=t["DC_Club_Participation"],
                objective=t["Objective"], no_new_orders=t["No_New_Orders"], credit_on_hold=t["Credit_On_Hold"],
                credit_on_hold_reason=t["Credit_On_Hold_Reason"], estimated_duration=t["Estimated_Duration"],
                priority_multiplier=t["Priority_Multiplier"],
            ))

    DailyTask.objects.bulk_create(pending_tasks)
    total_tasks = len(pending_tasks)

    plan_run.task_count = total_tasks
    plan_run.finished_at = timezone.now()
    plan_run.skipped_ses = skipped_ses
    plan_run.save(update_fields=["task_count", "finished_at", "skipped_ses"])

    # Pitching Agent, wired 2026-08-08 -- activates automatically right after task
    # assignment, per direct instruction. Needs live data (Block resolution for S1's
    # peer comparison, plus the new S2/S3/S6/S7 sources) -- skipped honestly when
    # Metabase isn't configured, same treatment as everything else in this function
    # that depends on client.configured. Real DCs only (Farmer Meeting tasks have no
    # dc_id, see pitching.generate_pitches_for_plan_run's own filter).
    if client.configured and total_tasks > 0:
        try:
            task_dc_ids = list(plan_run.tasks.exclude(dc_id__isnull=True).values_list("dc_id", flat=True).distinct())
            if task_dc_ids:
                # Block resolution -- unconditional now (previously only pulled for
                # ABM/BLOCK/DISTRICT scopes). Unfiltered pull, matched to dc_ids in
                # Python. Routed through geo_mapping_cache -- an ABM/BLOCK/DISTRICT scoped
                # run already fetched this exact full-table query in resolve_scope_dcs(),
                # so this reuses it instead of hitting Redshift a second time. Known
                # limitation carried over from SQL_GEO_MAPPING_1C's own is_dc=true filter
                # (a previously-identified bug class in a sibling query, _sql_geo()) -- a
                # DC missing here just means S1 gets skipped for it, same honest-degrade
                # path as any other missing source.
                geo_mapping = _resolve_geo_mapping(client, geo_mapping_cache)
                block_by_dc = {row["dc_id"]: row["block"] for row in geo_mapping if row.get("block")}
                task_blocks = {block_by_dc[d] for d in task_dc_ids if d in block_by_dc}
                peer_dc_ids = sorted({row["dc_id"] for row in geo_mapping if row.get("block") in task_blocks}) if task_blocks else []
                pull_dc_ids = sorted(set(task_dc_ids) | set(peer_dc_ids)) if peer_dc_ids else task_dc_ids

                purchase_by_dc: Dict[str, Dict[str, Any]] = {}
                for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_dc_purchase_summary(pull_dc_ids, plan_date)):
                    dc_id = agent.normalize_id(row.get("dc_id"))
                    if dc_id:
                        purchase_by_dc[dc_id] = {
                            "purchase_30d": agent.parse_number(row.get("purchase_30d")),
                            "purchase_last_fy": agent.parse_number(row.get("purchase_last_fy")),
                            "purchase_ytd": agent.parse_number(row.get("purchase_ytd")),
                        }

                discount_by_dc: Dict[str, float] = {}
                for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_last_discount(task_dc_ids, plan_date)):
                    dc_id = agent.normalize_id(row.get("dc_id"))
                    # discount_price_unit is the post-discount unit price (confirmed live
                    # 2026-08-08: always <= price_unit, typically 85-100% of it -- never
                    # the discount amount itself, which would make e.g. 65/69 read as a
                    # 94% discount instead of the real ~6% discount off list price).
                    discounted_price, list_price = agent.parse_number(row.get("discount_price_unit")), agent.parse_number(row.get("price_unit"))
                    if dc_id and discounted_price is not None and list_price:
                        discount_by_dc[dc_id] = ((list_price - discounted_price) / list_price) * 100.0

                per_dc_category: Dict[str, Dict[str, float]] = {}
                for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_block_category_purchase(pull_dc_ids, plan_date)):
                    dc_id, cat = agent.normalize_id(row.get("dc_id")), row.get("category_name")
                    if dc_id and cat:
                        per_dc_category.setdefault(dc_id, {})[cat] = agent.parse_number(row.get("purchase_30d")) or 0.0

                extra_data_by_dc: Dict[str, Dict[str, Any]] = {}
                for dc_id in task_dc_ids:
                    entry = dict(purchase_by_dc.get(dc_id, {}))
                    entry["last_discount"] = discount_by_dc.get(dc_id)
                    # dc_datamart's weighted_avg_repayment_days -- already pulled by
                    # _sql_outstanding() into dc_financials, just wasn't forwarded to the
                    # pitch context before. 0.0 isn't a genuine "pays same-day" signal --
                    # confirmed live 2026-08-08: the DCs showing 0 are exactly the ones
                    # whose entire outstanding balance is currently overdue (no completed
                    # repayment cycle to average over), so _tp_outstanding() in pitching.py
                    # treats <= 0 as "no data" and omits the sentence rather than fabricate
                    # a false reassurance.
                    entry["avg_repayment_days"] = (dc_financials.get(dc_id) or {}).get("Weighted_Avg_Repayment_Days")
                    cats = per_dc_category.get(dc_id, {})
                    dominant_category = max(cats, key=cats.get) if cats else None
                    entry["dominant_category"] = dominant_category
                    entry["dc_category_purchase"] = cats.get(dominant_category) if dominant_category else None
                    block = block_by_dc.get(dc_id)
                    if dominant_category and block:
                        peer_amounts = [
                            per_dc_category.get(peer_id, {}).get(dominant_category, 0.0)
                            for peer_id in peer_dc_ids if block_by_dc.get(peer_id) == block
                        ]
                        entry["block_category_avg"] = (sum(peer_amounts) / len(peer_amounts)) if peer_amounts else None
                    extra_data_by_dc[dc_id] = entry

                from .pitching import generate_pitches_for_plan_run
                generate_pitches_for_plan_run(plan_run, extra_data_by_dc)
        except Exception as e:
            run_exceptions.append({"source": "PitchingAgent", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

    run_ts = agent.utc_now_iso()
    ExceptionRecord.objects.bulk_create([
        ExceptionRecord(
            plan_run=plan_run, record_id=str(e.get("record_id") or e.get("dc_id") or ""),
            source=e["source"], reason_code=e["reason_code"], detail=e["detail"], run_timestamp=run_ts,
        )
        for e in run_exceptions
    ])

    # GR-17-style escalation alert -- more than 10% of in-scope DCs producing an
    # exception (live-pull failures, referential-integrity issues, etc.) is a signal the
    # underlying data/connection quality degraded for this run, not just isolated
    # one-off records; surface it rather than letting it sit unnoticed in ExceptionRecord.
    if scoped_dcs and (len(run_exceptions) / len(scoped_dcs)) > 0.10:
        send_alert(
            f"PlanRun #{plan_run.id} ({scope_type}={scope_value} @ {plan_date}): "
            f"{len(run_exceptions)} exceptions across {len(scoped_dcs)} DCs "
            f"({len(run_exceptions) / len(scoped_dcs):.0%}) -- exceeds 10% threshold.",
            severity="warning",
        )

    client.close()
    return plan_run


def _output_dir() -> Path:
    return Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output"


def _todays_run_summary(output_dir: Path) -> Optional[Dict[str, Any]]:
    """Returns the parsed Run_Summary.json if it exists and its Run_Timestamp falls on
    today's UTC calendar date (Run_Timestamp is written via agent.utc_now_iso(), and
    Django's TIME_ZONE is UTC -- same clock, no conversion needed). None otherwise, so
    the caller always has an unambiguous run-or-skip decision instead of guessing from
    file mtimes (mtime survives a copy/checkout and can lie; the timestamp inside the
    file is the actual pipeline run time). Shared by activate_tuff (CLI) and the
    /api/planning/normalize/ + /api/planning/tuff/ endpoints -- single source of truth
    for the once-per-day dedup rule (see [[tuff-once-per-day-normalization]])."""
    summary_path = output_dir / "Run_Summary.json"
    if not summary_path.exists():
        return None
    try:
        summary = json.loads(summary_path.read_text())
        run_date = datetime.fromisoformat(summary["Run_Timestamp"]).date()
    except (json.JSONDecodeError, KeyError, ValueError):
        return None
    return summary if run_date == datetime.now(dt_timezone.utc).date() else None


def run_normalization_step(date: Optional[str] = None, force: bool = False, skip: bool = False) -> Dict[str, Any]:
    """Step 1 (Data Normalization Agent), run-or-reuse per the once-per-day rule. Returns
    {"Reused": bool, "Skipped": bool, **run_summary_fields} -- Run_Timestamp/Row_Counts/
    Check_Summary/Note come straight from se_daily_plan_agent.run_pipeline()'s own return
    shape (fresh or reused). force and skip are mutually exclusive (raises PlanningError
    if both set), same as activate_tuff's --force-normalization/--skip-normalization."""
    if force and skip:
        raise PlanningError("force and skip are mutually exclusive.")
    output_dir = _output_dir()

    if skip:
        summary_path = output_dir / "Run_Summary.json"
        if not summary_path.exists():
            raise PlanningError("skip=true requested but output/Run_Summary.json doesn't exist -- nothing to reuse.")
        summary = json.loads(summary_path.read_text())
        return {"Reused": False, "Skipped": True, **summary}

    reusable = None if force else _todays_run_summary(output_dir)
    if reusable is not None:
        return {"Reused": True, "Skipped": False, **reusable}

    run_summary = agent.run_pipeline(output_dir, date)
    return {"Reused": False, "Skipped": False, **run_summary}


def _abort_if_empty_dc_master(scope_type: str, scope_value: str, row_counts: Dict[str, int]) -> None:
    """Shared guard: scope resolution hard-depends on DC_Master_Normalized having real
    rows. Must run on every path that lets Step 2 proceed against output/ -- a fresh
    Step 1 run, an auto-reused same-day run, AND an explicit skip -- not just the
    fresh-run path, or a corrupted/empty output/ from earlier in the day gets silently
    reused by every later activation instead of failing loudly once."""
    if row_counts.get("DC_Master_Normalized", 0) == 0:
        send_alert(
            f"TUFF {scope_type}={scope_value}: DC_Master_Normalized has 0 rows in "
            "output/ -- aborted before Step 2.", severity="critical",
        )
        raise PlanningError(
            "TUFF aborted: DC_Master_Normalized has 0 rows -- the SE Daily Task Agent "
            "has nothing to resolve scope against. Not proceeding to Step 2 on "
            "empty/failed normalization output (retry with force=true)."
        )


def activate_tuff_scope(
    scope_type: str, scope_value: str, plan_date: Optional[str] = None,
    force_normalization: bool = False, skip_normalization: bool = False,
    farmer_meeting_asker: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
    farmer_meeting_confirmed_emails: Optional[set] = None,
) -> Tuple[PlanRun, Dict[str, Any]]:
    """Agent TUFF's full two-step flow as a single reusable call -- Step 1 (Data
    Normalization, once-per-day, see run_normalization_step) then Step 2
    (generate_plan_for_scope, which auto-triggers Pitching + Routing). Shared by
    `manage.py activate_tuff` and GET /api/planning/tuff/<scope_type>/<scope_value>/, so
    the two can never drift on the once-per-day/abort-on-empty rules. Returns
    (plan_run, normalization_info) -- normalization_info is run_normalization_step()'s
    return value, for callers that want to report Step 1's own outcome alongside the
    PlanRun."""
    normalization_info = run_normalization_step(date=plan_date, force=force_normalization, skip=skip_normalization)
    _abort_if_empty_dc_master(scope_type, scope_value, normalization_info.get("Row_Counts", {}))

    plan_run = generate_plan_for_scope(
        scope_type, scope_value, plan_date,
        farmer_meeting_asker=farmer_meeting_asker,
        farmer_meeting_confirmed_emails=farmer_meeting_confirmed_emails,
    )
    return plan_run, normalization_info
