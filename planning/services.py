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

from . import data_cache, product_cohort, routing
from .models import DailyTask, DCVisitStreak, ExceptionRecord, FocusProductTargetRun, PlanRun
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


def load_config_rows() -> agent.Table:
    """Config_Normalized.json is Step 1's already-parsed Source 5 output (same cache as
    load_dc_master()) -- lets generate_plan_for_scope cross-check BusinessConstants
    against the live sheet (agent.check_business_constants_against_config) without
    re-parsing the raw CSV on every scope. Missing output degrades to no drift-checking
    for this run rather than raising -- same honest-degrade pattern as load_aop_targets."""
    try:
        return data_cache.load_output_json(_output_dir(), "Config_Normalized.json")
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
    # is_active mirrors SQL_OUTSTANDING_3D's own 2026-09-01 addition -- pulled as a
    # column, not filtered in SQL, so agent.normalize_sales_transactions() (shared by
    # both this scoped path and the network-wide one) can flag
    # DC_Datamart_Inactive_Outstanding_Unavailable per DC instead of silently omitting it.
    return f"""
    SELECT sap_partner_id AS dc_id, total_outstanding, total_overdue, current_month_os,
           os_1_to_90, os_90_plus, weighted_avg_repayment_days, last_invoice_date, is_mismatch,
           is_active
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


def _sql_promise_to_pay(dc_ids: List[str]) -> str:
    """Scoped counterpart of se_daily_plan_agent.SQL_PROMISE_TO_PAY_3J -- see that
    query's own docstring for the dialect trap (JSON_EXTRACT_PATH_TEXT, not ->>) and
    the "most recent promise only" / "any qualifying payment, not full amount" rules.
    No lookback window, same reasoning as _sql_payments above -- already scoped to a
    handful of dc_ids, no cost to finding each DC's true most recent promise."""
    return f"""
    WITH latest_promise AS (
        SELECT vpd.id AS record_id, cc.partner_id AS dc_id,
               JSON_EXTRACT_PATH_TEXT(vpd.visit_purpose_details, 'amount') AS promise_amount_raw,
               TIMESTAMP 'epoch' + CAST(JSON_EXTRACT_PATH_TEXT(vpd.visit_purpose_details, 'date') AS BIGINT) * INTERVAL '1 second' AS promise_date,
               vpd.created_at AS promise_created_at,
               ROW_NUMBER() OVER (PARTITION BY cc.partner_id ORDER BY vpd.created_at DESC) AS rn
        FROM task_management_visitpurposedetails vpd
        JOIN task_management_task t ON t.id = vpd.task_id
        JOIN customer_management_customer cc ON cc.id = t.partner_id
        WHERE vpd.visit_purpose_id = 4 AND cc.partner_id::text IN ({_sql_list(dc_ids)})
    )
    SELECT lp.dc_id, lp.promise_amount_raw, lp.promise_date, lp.promise_created_at,
           EXISTS (
               SELECT 1 FROM payments_paymenttransaction p
               JOIN customer_management_customer cc2 ON cc2.id = p.customer_id
               WHERE cc2.partner_id = lp.dc_id AND p.status = 'SUCCESS'
                 AND p.created_at >= lp.promise_created_at AND p.created_at <= lp.promise_date
           ) AS paid_on_time
    FROM latest_promise lp
    WHERE lp.rn = 1
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


def _prior_fy_window(plan_date: str) -> Tuple[str, str]:
    """(fy_start, plan_date) shifted back exactly one Indian fiscal year -- same number
    of days elapsed into the year, not a full prior-year total, so a YoY PL growth
    comparison (confirmed 2026-08-18) is like-for-like against _sql_ytd_pl's own window.
    Reuses _sql_ytd_pl unchanged, just called with these shifted dates."""
    d = datetime.fromisoformat(plan_date).date()
    try:
        prior_plan_date = d.replace(year=d.year - 1)
    except ValueError:  # plan_date is Feb 29 and the prior year isn't a leap year
        prior_plan_date = d.replace(year=d.year - 1, day=28)
    return _fiscal_year_start(prior_plan_date.isoformat()), prior_plan_date.isoformat()


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
    # Baseline_Momentum x Category_Multiplier (4.4, GR-25). Same invoice_liquidation_with_pog
    # source as Liquidation_Normalized (SQL_LIQUIDATION_3D in se_daily_plan_agent.py) --
    # partner_id IS sap_partner_id directly here, no customer_management_customer bridge
    # needed (confirmed live via a join to input_partner_details), unlike orders/payments.
    # Grouped by business_category since 4.4's multiplier is category-specific; caller
    # picks each DC's dominant category (highest combined this+baseline sales) in Python.
    #
    # CHANGED 2026-09-04, explicit user request: the SCORED baseline used to be "last
    # month" (the prior 30-day window) -- switched to "the same 30-day window one year
    # ago" instead. Confirmed live why this matters: a DC with an unusually quiet PRIOR
    # month could show 800%+ "momentum" that was really just recovering off a depressed
    # base, while its actual year-over-year trend was flat or declining (New Annapurna:
    # 848% vs. last month, but only 80% vs. the same period last year -- its Crop
    # Protection business is genuinely down from a year ago, the MoM number was
    # misleading).
    #
    # sum_prior_30d ADDED BACK 2026-09-04 (explicit user request, same conversation) --
    # NOT used for scoring/grading (that stays YoY-only, per the reasoning above), purely
    # informational: a separate "vs. last month" trend number surfaced alongside the
    # scored YoY percentage, so the genuine recent-momentum signal that motivated the
    # original MoM formula isn't lost, just no longer conflated with the scored grade.
    # See score_bo4_sales_momentum's caller for how mom_trend_pct gets attached.
    #
    # Three separate 30-day windows, non-contiguous (this year's, last month's, and last
    # year's matching window are all disjoint) -- the WHERE clause pulls all three
    # explicitly rather than one continuous range.
    d = datetime.fromisoformat(plan_date).date()
    period_start = (d - timedelta(days=30)).isoformat()
    prior_start = (d - timedelta(days=60)).isoformat()
    last_year_end = (d - timedelta(days=365)).isoformat()
    last_year_start = (d - timedelta(days=395)).isoformat()
    return f"""
    SELECT partner_id AS dc_id, business_category,
           SUM(CASE WHEN invoice_date >= '{period_start}' AND invoice_date <= '{plan_date}' THEN net_billed_amount ELSE 0 END) AS sum_this_30d,
           SUM(CASE WHEN invoice_date >= '{prior_start}' AND invoice_date < '{period_start}' THEN net_billed_amount ELSE 0 END) AS sum_prior_30d,
           SUM(CASE WHEN invoice_date >= '{last_year_start}' AND invoice_date <= '{last_year_end}' THEN net_billed_amount ELSE 0 END) AS sum_last_year_30d
    FROM invoice_liquidation_with_pog
    WHERE partner_id IN ({_sql_list(dc_ids)})
      AND (
        (invoice_date >= '{period_start}' AND invoice_date <= '{plan_date}')
        OR (invoice_date >= '{prior_start}' AND invoice_date < '{period_start}')
        OR (invoice_date >= '{last_year_start}' AND invoice_date <= '{last_year_end}')
      )
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


def _sql_coupon_discount_history(dc_ids: List[str], plan_date: str) -> str:
    """S2b Suggested Discount raw input -- wired 2026-08-24 per the confirmed methodology
    (Required Data Sources CSV, ✅ Ready): combine this DC's own historical discount
    pattern with the Same Block Purchase pattern (S1) -- i.e. what discount is working
    for comparable DCs in the same block, on the same product, informed by this DC's own
    history. Both sides read from coupon_analysis filtered on coupon_name IS NOT NULL (a
    real scheme was actually applied -- coupon_applied_flag is separately confirmed
    unusable, constant 'true' on 100% of rows). Called ONCE for pull_dc_ids (task DCs +
    block peers + node peers, the same pool _sql_block_category_purchase/_peer_stats
    already use) -- the caller looks up "this DC's own row" vs. "a peer's row" out of the
    same result set, no separate query per DC.

    Restricted to coupon_type='PER_UNIT' (per the Normalization Agent's live sample,
    ~83% of real scheme rows -- PER_UNIT+INSTANT dominates) so coupon_unit_benefit is
    unambiguously a rupees-per-unit figure. PERCENTAGE-type coupons' unit_benefit isn't
    the same unit and would silently corrupt a blended average if mixed in -- a
    defensible simplification, not itself a confirmed business rule, flagged here rather
    than left implicit. 180-day trailing window (not 30d like S1) since real-scheme rows
    are the sparser 36% of coupon_analysis -- a 30-day window would starve this of
    sample for most DC/product pairs.

    partner_id here is already the DC's own sap_partner_id directly, no
    customer_management_customer bridge needed -- confirmed live, same join
    _sql_club_qualifying_turnover already uses successfully in this file."""
    d = datetime.fromisoformat(plan_date).date()
    window_start = (d - timedelta(days=180)).isoformat()
    return f"""
    SELECT partner_id AS dc_id, product_name, AVG(coupon_unit_benefit) AS avg_discount_per_unit
    FROM coupon_analysis
    WHERE partner_id::text IN ({_sql_list(dc_ids)})
      AND coupon_name IS NOT NULL
      AND coupon_type = 'PER_UNIT'
      AND coupon_unit_benefit IS NOT NULL
      AND created_at >= '{window_start}'
    GROUP BY partner_id, product_name
    """


def _sql_last_discount(dc_ids: List[str], plan_date: str) -> str:
    # Pitching Agent (S2a, Last Discount). Reuses the Source 3h join chain confirmed live 2026-08-08:
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


def _sql_block_product_purchase(dc_ids: List[str], plan_date: str) -> str:
    # DC Card "Recommended Product & Brief" + Pitching Agent S1 -- product-NAME
    # granularity, wired 2026-08-14 to fill the gap _sql_block_category_purchase's own
    # docstring flags ("category granularity, not per-SKU"). Same join chain, same
    # trailing-30d window, one GROUP BY level finer (tmpl.name alongside cat.name) --
    # lets the caller find not just "which category is trending in this block" but
    # "which SPECIFIC product," matching the Required Data Sources CSV's own S1
    # description ("broken out by specific product... this is what tells the SE WHICH
    # exact product to recommend"). Confirmed live 2026-08-14 against real Kota-node data.
    #
    # S1b enrichment columns added 2026-08-15 (sub_category_name, product_brand,
    # business_segment_name) -- the Required Data Sources CSV's own S1b row
    # ("Category_Name, Sub_Category_Name, Business_Segment_Name, Business_Category,
    # Product_Brand -- attached to every product mentioned anywhere in a pitch"), never
    # wired before now. business_segment_name is a direct column on products_template
    # (confirmed live values: 'BRANDED'/'PRIVATE LABEL', occasionally null/empty --
    # left as-is, never coerced). product_brand via products_brand/brand_id, sub_category
    # via products_subcategory/sub_category_id (same table _sql_business_area_strength_
    # detailed already joins). No separate "Business_Category" column exists anywhere in this
    # schema distinct from category_name -- the CSV's own two labels ("Category_Name" /
    # "Business_Category") appear to refer to the same confirmed field, not two.
    d = datetime.fromisoformat(plan_date).date()
    month_start = (d - timedelta(days=30)).isoformat()
    return f"""
    SELECT cc.partner_id AS dc_id, cat.name AS category_name, sub.name AS sub_category_name,
           tmpl.name AS product_name, brand.name AS product_brand,
           tmpl.business_segment_name AS business_segment_name,
           SUM(sol.price_unit * sol.quantity) AS purchase_30d
    FROM sale_orderrequest o
    JOIN customer_management_customer cc ON cc.id = o.partner_id
    JOIN sale_orderrequestline sol ON sol.order_request_id = o.id
    JOIN products_product prod ON prod.id = sol.product_id
    JOIN products_template tmpl ON tmpl.id = prod.template_id
    LEFT JOIN products_category cat ON cat.id = tmpl.category_id
    LEFT JOIN products_subcategory sub ON sub.id = tmpl.sub_category_id
    LEFT JOIN products_brand brand ON brand.id = tmpl.brand_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)}) AND o.status = 'processed'
      AND o.created_at >= '{month_start}'
    GROUP BY cc.partner_id, cat.name, sub.name, tmpl.name, brand.name, tmpl.business_segment_name
    """


# BO1 PL_Expected's trailing-90d leg (confirmed 2026-08-18): raises the recent-
# performance baseline by 20% before averaging it with the AOP-target leg, i.e. this
# DC is expected to beat its own trailing average by this much, not just match it.
# Provisional business default, NOT the same as config item 1.3's growth-requirement
# language (that one asks for the AOP-target leg to carry a growth factor, not this
# leg) -- a distinct engineering decision, picked deliberately over multiplying the AOP
# leg or the final combined figure instead. Tune here if the business wants a different
# factor.
PL_TRAILING_LEG_GROWTH_MULTIPLIER = 1.2

def _sql_business_area_strength_detailed(dc_ids: List[str], window_start: str, window_end: str) -> str:
    # DC Card / "Dehaat Center Ko Jaano" Section 1 "कौन" (Who) -- Business Area Strength
    # (Source 3h). Rebuilt 2026-08-22 per the confirmed corrected spec (SE_DC_Data_
    # Normalization_Agent_Prompt v3 re-sync + production Pitch Playbook screenshot, DC
    # M/s AGAM BEEJ BHANDAR 1000016754): replaces the old top-5/trailing-12-month/no-
    # bifurcation version (deleted). New structure -- EVERY sub-category (not top-N), a
    # caller-supplied window (current-FY YTD for "Business Area Strength", prior-FY YTD
    # for the paired "Historical Performance" via _prior_fy_window), each sub-category
    # split Branded vs. Private Label with a share of that sub-category, each segment
    # broken down product-wise. Returns flat product-level rows; the caller
    # (_build_business_area_tree) aggregates sub-category/segment totals and shares in
    # Python rather than nested SQL window functions -- easier to debug, matches this
    # file's existing style (e.g. dc_financials, per_dc_category).
    #
    # Join chain fixed vs. the docx's own first draft of this query: that version filtered
    # WHERE sor.partner_id = :dc_id directly, which returns ZERO rows for a real DC_ID --
    # sale_orderrequest.partner_id is customer_management_customer's internal row id, not
    # sap_partner_id. Confirmed live 2026-08-22 (the docx's own worked example used
    # partner_id=20293, a small integer -- that IS the internal id, not a DC_ID, which is
    # what made its own test look self-consistent). Every other confirmed query in this
    # file (_sql_ytd_pl, _sql_pl_metrics, etc.) already bridges through
    # customer_management_customer -- this one now does too.
    #
    # Bug fixed 2026-09-03: this used to subtract discount_price_unit*quantity, making
    # every total here NET of discount, while _sql_ytd_pl's YTD_Private_Label (shown in
    # every outcome table) is GROSS (price_unit*quantity only) -- same DC, same window,
    # same PRIVATE LABEL filter, but two different "YTD PL" numbers that quietly
    # disagreed by the full discount amount (confirmed live: DC 1000006972 showed
    # gross=Rs695,090 vs net=Rs510,568, a 26.6% gap). Now gross throughout, matching
    # _sql_ytd_pl and every other PL figure in this pipeline.
    return f"""
    SELECT
      cc.partner_id AS dc_id, cat.name AS category_name, sub.name AS sub_category_name,
      CASE WHEN tmpl.business_segment_name = 'PRIVATE LABEL' THEN 'Private Label' ELSE 'Branded' END AS brand_tier,
      sol.product_name, sol.product_brand,
      SUM(sol.price_unit * sol.quantity) AS product_gross_value
    FROM sale_orderrequestline sol
    JOIN sale_orderrequest sor ON sor.id = sol.order_request_id
    JOIN customer_management_customer cc ON cc.id = sor.partner_id
    JOIN products_product prod ON prod.id = sol.product_id
    JOIN products_template tmpl ON tmpl.id = prod.template_id
    LEFT JOIN products_category cat ON cat.id = tmpl.category_id
    LEFT JOIN products_subcategory sub ON sub.id = tmpl.sub_category_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
      AND sor.status = 'processed'
      AND sor.created_at >= '{window_start}' AND sor.created_at <= '{window_end}'
    GROUP BY cc.partner_id, cat.name, sub.name, brand_tier, sol.product_name, sol.product_brand
    """


def _suggested_discount(
    dc_id: str, product: str, block_ids: List[str], node_ids: List[str],
    coupon_discount_by_dc_product: Dict[str, Dict[str, float]],
) -> Optional[float]:
    """S2b Suggested Discount for one product (₹/unit) -- combines this DC's own
    historical discount on that product with the block-then-node peer average discount
    on the same product (both from coupon_analysis, see _sql_coupon_discount_history).
    Simple unweighted average when both signals exist -- the confirmed methodology says
    "combine... informed by," not a documented weighting formula, so an unweighted blend
    is the honest default, not invented precision. Falls back to whichever single signal
    exists when only one does; None if neither -- same honest-degrade convention as
    every other talking point in this file. Block tried before node, same "more locally
    relevant" reasoning _peer_stats already uses for the product recommendation itself."""
    own = coupon_discount_by_dc_product.get(dc_id, {}).get(product)
    peer_amounts = [a for a in (coupon_discount_by_dc_product.get(p, {}).get(product) for p in block_ids) if a is not None]
    if not peer_amounts:
        peer_amounts = [a for a in (coupon_discount_by_dc_product.get(p, {}).get(product) for p in node_ids) if a is not None]
    peer_avg = (sum(peer_amounts) / len(peer_amounts)) if peer_amounts else None
    signals = [v for v in (own, peer_avg) if v is not None]
    return (sum(signals) / len(signals)) if signals else None


def _build_business_area_tree(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Flat product-level rows from _sql_business_area_strength_detailed -> per-DC list
    of {sub_category, total, segments: [{segment, total, share_of_subcat, products:
    [{name, brand, value}]}]}, sub-categories and segments ranked highest-value-first,
    products ranked highest-value-first within their segment. Gross values (2026-09-03
    fix -- see _sql_business_area_strength_detailed's own docstring), matching
    _sql_ytd_pl elsewhere in this file. Zero/negative rows (a sub-category that's all
    returns this window) are dropped, same convention as every other value-ranked list
    in this file."""
    tree: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        dc_id = agent.normalize_id(row.get("dc_id"))
        value = agent.parse_number(row.get("product_gross_value")) or 0.0
        if not dc_id or value <= 0:
            continue
        subcat_name = row.get("sub_category_name") or "Unclassified"
        segment_name = row.get("brand_tier") or "Branded"
        subcats = tree.setdefault(dc_id, {})
        sc = subcats.setdefault(subcat_name, {"total": 0.0, "segments": {}})
        sc["total"] += value
        seg = sc["segments"].setdefault(segment_name, {"total": 0.0, "products": []})
        seg["total"] += value
        seg["products"].append({"name": row.get("product_name"), "brand": row.get("product_brand"), "value": value})

    result: Dict[str, List[Dict[str, Any]]] = {}
    for dc_id, subcats in tree.items():
        subcat_list = []
        for name, sc in subcats.items():
            segments = []
            for seg_name, seg in sc["segments"].items():
                seg["products"].sort(key=lambda p: -p["value"])
                segments.append({
                    "segment": seg_name, "total": seg["total"],
                    "share_of_subcat": (seg["total"] / sc["total"] * 100.0) if sc["total"] else 0.0,
                    "products": seg["products"],
                })
            segments.sort(key=lambda s: -s["total"])
            subcat_list.append({"sub_category": name, "total": sc["total"], "segments": segments})
        subcat_list.sort(key=lambda s: -s["total"])
        result[dc_id] = subcat_list
    return result


# DC Card PL recommendation geo fallback (confirmed 2026-08-18): own purchases -> block
# peers -> node peers can all come up empty for a DC with no purchase history in the
# normal peer-comparison scopes. Rather than give up, widen the search geographically:
# first every OTHER DC within NEARBY_PL_RADIUS_KM (straight-line, via
# se_daily_plan_agent.haversine_km -- same distance function the Routing Agent uses),
# then, only if that radius search itself has zero purchase data among any candidate, the
# nearest NEARBY_PL_NODE_FALLBACK_COUNT Nodes by centroid distance. Both tiers' candidate
# DC_IDs are queried in ONE combined live pull, not one query per failing DC.
NEARBY_PL_RADIUS_KM = 200.0
NEARBY_PL_NODE_FALLBACK_COUNT = 10
NEARBY_PL_PRODUCT_COUNT = 5

# S1/PL_Recommendation top-N within a DC's own dominant_category (block-then-node peer
# pool) -- widened 2026-08-18 from a single top product per direct instruction. Kept as
# a separate constant from NEARBY_PL_PRODUCT_COUNT even though both are 5 today -- the
# two tiers (category-scoped vs geographic) are independent decisions that happen to
# agree on count right now, not the same knob.
RECOMMENDED_PRODUCT_COUNT = 5


def _node_centroids(dc_master: "agent.Table") -> Dict[str, Tuple[float, float]]:
    """Average Latitude/Longitude per Node, over DCs with real coordinates only --
    used only by the geo-fallback's second tier (nearby Nodes), when even a
    NEARBY_PL_RADIUS_KM-radius DC search finds no purchase data at all."""
    sums: Dict[str, List[float]] = {}
    counts: Dict[str, int] = {}
    for dc in dc_master:
        node = dc.get("Node")
        lat, lon = dc.get("Latitude"), dc.get("Longitude")
        if not node or lat is None or lon is None:
            continue
        s = sums.setdefault(node, [0.0, 0.0])
        s[0] += lat
        s[1] += lon
        counts[node] = counts.get(node, 0) + 1
    return {node: (s[0] / counts[node], s[1] / counts[node]) for node, s in sums.items()}


def _attach_nearby_product_recommendations(
    client: "agent.MetabaseClient", dc_master: "agent.Table", needs_geo_fallback: List[str],
    extra_data_by_dc: Dict[str, Dict[str, Any]], plan_date: str,
    result_key: str = "recommended_products", segment: Optional[str] = None,
) -> None:
    """Mutates extra_data_by_dc in place, adding to result_key (a list of up to
    NEARBY_PL_PRODUCT_COUNT {name, value, category, sub_category, brand,
    business_segment, scope} dicts, highest value first, scope "nearby_radius" or
    "nearby_node") for every dc_id in needs_geo_fallback that a real candidate search
    actually found something for -- same unified key/shape pitching.py already reads
    from the category-scoped (block/node) tier, so callers never need to know which
    tier a DC's recommendation actually came from. Leaves the key entirely absent for
    a DC where not even the Node-level fallback found any purchase data anywhere
    nearby -- pitching._tp_block_comparison treats that the same as every other "no
    data" case, not a fabricated empty recommendation.

    result_key/segment (added 2026-08-18, PRIVATE LABEL-only caller removed 2026-09-03
    alongside the DC Card section it fed): segment restricts candidate purchases BEFORE
    ranking, same reasoning as _peer_stats' own segment param (filtering an already-
    ranked general list would routinely return nothing, since higher-value BRANDED
    items usually crowd PL out of an unfiltered top-5) -- kept as a general capability
    even though the only current caller uses the plain recommended_products default."""
    dc_by_id = {dc["DC_ID"]: dc for dc in dc_master}

    def _own_coords(dc_id: str) -> Optional[Tuple[float, float]]:
        dc = dc_by_id.get(dc_id)
        if not dc or dc.get("Latitude") is None or dc.get("Longitude") is None:
            return None
        return dc["Latitude"], dc["Longitude"]

    # Tier 1 candidates: every other DC within the radius, per needing DC.
    radius_candidates: Dict[str, List[str]] = {}
    for dc_id in needs_geo_fallback:
        origin = _own_coords(dc_id)
        if origin is None:
            continue
        nearby = []
        for other in dc_master:
            other_id = other.get("DC_ID")
            if not other_id or other_id == dc_id or other.get("Latitude") is None or other.get("Longitude") is None:
                continue
            dist = agent.haversine_km(origin[0], origin[1], other["Latitude"], other["Longitude"])
            if dist is not None and dist <= NEARBY_PL_RADIUS_KM:
                nearby.append(other_id)
        radius_candidates[dc_id] = nearby

    # Tier 2 candidates (nearest Nodes by centroid), computed for every needing DC up
    # front too -- avoids a second live query later if tier 1 turns out empty for some
    # of them once real purchase data is checked.
    centroids = _node_centroids(dc_master)
    node_candidates: Dict[str, List[str]] = {}
    for dc_id in needs_geo_fallback:
        origin = _own_coords(dc_id)
        own_node = (dc_by_id.get(dc_id) or {}).get("Node")
        if origin is None or not centroids:
            continue
        ranked_nodes = sorted(
            (n for n in centroids if n != own_node),
            key=lambda n: agent.haversine_km(origin[0], origin[1], centroids[n][0], centroids[n][1]) or float("inf"),
        )[:NEARBY_PL_NODE_FALLBACK_COUNT]
        node_candidates[dc_id] = [
            other["DC_ID"] for other in dc_master
            if other.get("Node") in ranked_nodes and other.get("DC_ID")
        ]

    combined_ids = sorted({i for ids in radius_candidates.values() for i in ids} | {i for ids in node_candidates.values() for i in ids})
    if not combined_ids:
        return

    purchases_by_dc: Dict[str, List[Dict[str, Any]]] = {}
    for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_block_product_purchase(combined_ids, plan_date)):
        dc_id = agent.normalize_id(row.get("dc_id"))
        if dc_id:
            purchases_by_dc.setdefault(dc_id, []).append(row)

    def _top_products(candidate_ids: List[str], category: Optional[str]) -> List[Dict[str, Any]]:
        totals: Dict[str, float] = {}
        attrs: Dict[str, Dict[str, Any]] = {}
        for cid in candidate_ids:
            for row in purchases_by_dc.get(cid, []):
                if category and row.get("category_name") != category:
                    continue
                if segment and row.get("business_segment_name") != segment:
                    continue
                product = row.get("product_name")
                value = agent.parse_number(row.get("purchase_30d")) or 0.0
                if not product or value <= 0:
                    continue
                totals[product] = totals.get(product, 0.0) + value
                attrs[product] = row
        ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:NEARBY_PL_PRODUCT_COUNT]
        return [
            {
                "name": product, "value": value,
                "category": attrs[product].get("category_name"),
                "sub_category": attrs[product].get("sub_category_name"),
                "brand": attrs[product].get("product_brand"),
                "business_segment": attrs[product].get("business_segment_name") or None,
            }
            for product, value in ranked
        ]

    for dc_id in needs_geo_fallback:
        category = extra_data_by_dc.get(dc_id, {}).get("dominant_category")
        products = _top_products(radius_candidates.get(dc_id, []), category)
        is_radius = True
        if not products:
            products = _top_products(node_candidates.get(dc_id, []), category)
            is_radius = False
        # A DC with a real dominant_category but zero matching products nearby either
        # way falls back to an unrestricted (any-category) search once, rather than
        # reporting "nothing nearby" when nearby DCs simply don't sell the SAME
        # category this one happens to favor.
        if not products and category:
            radius_any = _top_products(radius_candidates.get(dc_id, []), None)
            products, is_radius = (radius_any, True) if radius_any else (_top_products(node_candidates.get(dc_id, []), None), False)
        if products:
            scope = "nearby_radius" if is_radius else "nearby_node"
            entry = extra_data_by_dc.setdefault(dc_id, {})
            entry[result_key] = [{**p, "scope": scope} for p in products]


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


def _sql_recent_punch_ins(se_user_ids: List[int], plan_date: str, days: int = 30) -> str:
    # Routing Agent R0.4 Origin_Point, REWRITTEN 2026-09-04 (explicit user request) --
    # was a single most-recent-day punch-in (_sql_prev_punch_in, removed), confirmed
    # live root cause of a real 300km+ routing anomaly (kanhaiya.raj1: one anomalous
    # day's GPS reading, zero cross-checking against his actual recent pattern). Now
    # pulls each SE's earliest check-in for EVERY one of the last `days` calendar days
    # before plan_date (N=30 per direct instruction) -- clustering (500m buffer,
    # majority/dominant cluster wins) happens in Python, see
    # se_daily_plan_agent.resolve_typical_origin(). Same rn_in_day-per-calendar-day
    # convention as _sql_punch_in above, just over a window instead of a single day.
    return f"""
    SELECT se_user_id, lat, lon, check_date FROM (
        SELECT user_id AS se_user_id, check_in_latitude AS lat, check_in_longitude AS lon,
               check_in_time::date AS check_date,
               ROW_NUMBER() OVER (PARTITION BY user_id, check_in_time::date ORDER BY check_in_time ASC) AS rn_in_day
        FROM attendance_attendance
        WHERE user_id IN ({",".join(str(u) for u in se_user_ids)})
          AND check_in_time >= DATE '{plan_date}' - INTERVAL '{days} days'
          AND check_in_time < '{plan_date}'
    ) t
    WHERE rn_in_day = 1
    ORDER BY se_user_id, check_date ASC
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


def make_routing_plan_asker(stdout, style) -> Optional[Callable[[], str]]:
    """Builds an interactive Plan A / Plan B confirmation prompt for CLI commands
    (activate_tuff/generate_se_plan) -- wired 2026-08-28 per direct instruction: when the
    Routing Agent activates, ask which plan to run rather than silently auto-selecting.
    This is a deliberate choice, not a stand-in for a real fallback rule -- the source
    doc (SE_DC_Data_Normalization_Agent_Prompt.docx, Section 3e) states explicitly that
    "the exact Plan A -> Plan B trigger condition" is "not yet confirmed," so an
    automatic trigger would be guessing at an unconfirmed rule. Asking is the honest
    interim behavior until that condition is specified. Same isatty-gated pattern as
    make_farmer_meeting_asker() immediately above -- returns None (no asking, defaults
    to Plan A) when stdin isn't a real TTY, never blocks/EOFErrors under cron/scripting."""
    if not sys.stdin.isatty():
        return None

    def ask() -> str:
        stdout.write(style.WARNING("\n  Routing Agent: which plan should generate today's routes?"))
        stdout.write("    Plan A -- Priority-Max / Distance-Min / Balanced (Models 1-3, existing default)")
        stdout.write("    Plan B -- Beat Planning / Cluster-Based Model (density clustering + BO-score maximization)")
        answer = input("  Choice [A/b]: ").strip().upper()
        return "B" if answer == "B" else "A"

    return ask


@transaction.atomic
def generate_plan_for_scope(
    scope_type: str, scope_value: str, plan_date: Optional[str] = None,
    farmer_meeting_asker: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
    farmer_meeting_confirmed_emails: Optional[set] = None,
    focus_product_material_id: Optional[str] = None,
    focus_product_node_id: Optional[str] = None,
    focus_product_years: int = 4,
    focus_product_season_weeks: Optional[Dict[str, int]] = None,
    focus_product_crop_districts: Optional[List[str]] = None,
    focus_product_related_products: Optional[List[str]] = None,
    routing_plan_asker: Optional[Callable[[], str]] = None,
    routing_plan_choice: Optional[str] = None,
    enable_rotation: bool = False,
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
    stronger signal than the pacing algorithm's opinion.

    focus_product_*: optional, wires the Focus Product Campaign Targeting agent
    (planning.product_cohort, Product _cohort/) into this same run -- product-first, not
    DC-first, so it's opt-in per call rather than automatic like Routing/Pitching (see
    FocusProductTargetRun's docstring on why no default Focus Product selection exists).
    focus_product_material_id is the only required one to trigger it at all;
    focus_product_node_id defaults to scope_value when scope_type == NODE (the natural
    case), and is otherwise required explicitly -- there's no confirmed mapping from the
    other scope types (SE/ABM/RBM/BLOCK/DISTRICT/STATE) to a single Product Cohort node.

    routing_plan_asker / routing_plan_choice: which Routing Agent mode to run for every
    SE in this scope -- see make_routing_plan_asker(). routing_plan_choice ("A" or "B")
    is an explicit override, same precedence pattern as farmer_meeting_confirmed_emails
    above -- takes priority over routing_plan_asker. When neither is supplied (the
    run_scheduled_tuff/HTTP-API case), defaults to "A" -- Plan A stays the safe,
    unattended default; Plan B only ever runs when a human chose it, explicitly or
    interactively, never silently.

    enable_rotation: opt-in, Plan B only (Beat_Planning_Routing_Agent_Cluster_Model.xlsx
    Sheet 11 Model B, "Fixed Rotation") -- see planning.routing.generate_route_plans_for_se's
    own docstring. False by default, same never-silent posture as routing_plan_choice."""
    started_at = timezone.now()
    plan_date = plan_date or timezone.now().date().isoformat()
    constants = agent.BusinessConstants()
    client = agent.get_client()
    resolved_routing_plan = routing_plan_choice or (routing_plan_asker() if routing_plan_asker else None) or "A"

    geo_mapping_cache: Dict[str, agent.Table] = {}
    dc_master = load_dc_master()
    scoped_dcs = resolve_scope_dcs(scope_type, scope_value, dc_master, client, geo_mapping_cache)
    dc_ids = [d["DC_ID"] for d in scoped_dcs]
    se_emails = sorted({d["Assigned_SE_Email"] for d in scoped_dcs if d.get("Assigned_SE_Email")})

    run_exceptions: List[Dict[str, Any]] = []

    config_drift_exc = agent.Exceptions(agent.utc_now_iso())
    agent.check_business_constants_against_config(constants, load_config_rows(), config_drift_exc)
    run_exceptions.extend({"record_id": r["Record_ID"], "source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in config_drift_exc.rows)

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
    promise_by_dc: Dict[str, Dict[str, Any]] = {}
    dc_club_by_id: Dict[str, Dict[str, Any]] = {}
    geo_by_dc: Dict[str, tuple] = {}
    ytd_pl_by_dc: Dict[str, float] = {}
    ytd_pl_last_year_by_dc: Dict[str, float] = {}
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
        run_exceptions.extend({"record_id": r["Record_ID"], "source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in fin_exc.rows)

        # Real per-DC BO3 (Outstanding) scoring -- see score_bo3_outstanding_live_proxy()
        # docstring for why this is a live-data substitute for the literal 3.1-3.6
        # formula, not that formula itself (Expected_Outstanding needs last month's
        # outstanding balance, which has no historical/time-series source in this
        # pipeline). Wired 2026-08-06 so Outstanding-qualifying DCs get a real,
        # DC-specific severity in Layer 3's ranking instead of one shared SE-level floor
        # score that made every Outstanding match lose to Visits regardless of amount.
        #
        # Removed 2026-09-04, explicit user request: this used to also apply a Tier-2
        # "adaptive weighting" multiplier (agent.completion_multiplier(), 0.7x-1.3x based
        # on the SE's own trailing-30d completion rate for this objective). Confirmed
        # live that formula appears NOWHERE in SE_DC_Data_Normalization_Agent_Prompt.docx
        # -- no completion-rate, adaptive-weighting, or Tier-2 language anywhere in the
        # source spec -- it was a system extension never validated against the actual
        # business requirements, quietly shrinking/inflating every DC's Outstanding/PL
        # score by up to 30% based on something that isn't the DC's own data at all.
        # weight_multiplier now always defaults to 1.0 (no-op) -- scores reflect each
        # DC's own real numbers only. ObjectiveCompletionStats/compute_completion_stats
        # deliberately left in place (real tracked data, harmless once unread) rather
        # than dropped, in case this is ever reintroduced with a confirmed formula.
        dc_bo_scores = {
            dc_id: {"Outstanding": agent.score_bo3_outstanding_live_proxy(
                fin.get("Current_Outstanding"), fin.get("Current_Overdue"), fin.get("OS_90_Plus"), constants,
            )}
            for dc_id, fin in dc_financials.items()
        }

        try:
            fy_start = _fiscal_year_start(plan_date)
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_ytd_pl(dc_ids, fy_start, plan_date)):
                dc_id = agent.normalize_id(row.get("dc_id"))
                if dc_id:
                    ytd_pl_by_dc[dc_id] = agent.parse_number(row.get("ytd_pl"))
        except Exception as e:
            run_exceptions.append({"source": "sale_orderrequestline", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        try:
            # YoY PL comparison (confirmed 2026-08-18) -- same query, same window shape,
            # shifted back exactly one fiscal year via _prior_fy_window so it's a
            # like-for-like comparison (same days-elapsed-into-the-year), not a full
            # prior-year total. Feeds both DC Card's Turnover-wise Standing and BO1's
            # yoy_growth_multiplier below -- moved ahead of the BO1 scoring block (was
            # after it) so the multiplier is actually available when scoring runs.
            prior_fy_start, prior_plan_date = _prior_fy_window(plan_date)
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_ytd_pl(dc_ids, prior_fy_start, prior_plan_date)):
                dc_id = agent.normalize_id(row.get("dc_id"))
                if dc_id:
                    ytd_pl_last_year_by_dc[dc_id] = agent.parse_number(row.get("ytd_pl"))
        except Exception as e:
            run_exceptions.append({"source": "sale_orderrequestline", "reason_code": "Live_Pull_Failed", "detail": f"YoY PL comparison: {type(e).__name__}: {e}"})

        def _yoy_pl_growth_multiplier(dc_id: str) -> Tuple[float, Optional[float]]:
            """Returns (multiplier, growth_pct). A provisional business default, NOT a
            confirmed Source 5 formula (unlike weight_multiplier's completion-rate
            weighting, which IS confirmed) -- a gentle +/-10% nudge to BO1's score from
            YoY PL growth, clamped at +/-30% growth so one outlier DC can't swing it
            further. Neutral (1.0, None) only when there's no real LAST-YEAR baseline to
            compare against (never divides by zero or a negative/zero prior figure).

            Bug fixed 2026-08-18: a missing key in ytd_pl_by_dc means zero PL orders in
            that window, not "unknown" -- _sql_ytd_pl only returns a row for a DC with
            >=1 PL order (same convention as _sql_business_area_strength_detailed). Treating a
            missing THIS-year figure as "no data" (instead of a real 0) previously
            suppressed the single most important case entirely: a DC with real PL sales
            last year and NONE this year. Confirmed live: Maa Laxmi Khad Beej Bhandar
            went Rs56,900 (last year, same window) -> Rs0 (this year) -- a full PL
            collapse that showed up as nothing at all in the DC Card or BO1 reasoning."""
            this_year = ytd_pl_by_dc.get(dc_id) or 0.0
            last_year = ytd_pl_last_year_by_dc.get(dc_id)
            if not last_year or last_year <= 0:
                return 1.0, None
            growth_pct = (this_year - last_year) / last_year
            clamped = max(-0.30, min(0.30, growth_pct))
            multiplier = 1.0 + clamped * (0.10 / 0.30)
            return multiplier, growth_pct

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
                leg_trailing = (pl_sum_90d / 3.0 * PL_TRAILING_LEG_GROWTH_MULTIPLIER) if pl_sum_90d else None

                leg_aop = None
                node = dc_node_by_id.get(dc_id, "")
                node_target = node_pl_aop_targets.get(node)
                node_total = node_trailing_pl_total.get(node)
                if node_target and node_total and pl_sum_90d and pl_sum_90d > 0:
                    leg_aop = node_target * (pl_sum_90d / node_total)
                    leg_aop_by_dc[dc_id] = leg_aop

                legs = [v for v in (leg_trailing, leg_aop) if v is not None]
                pl_expected = (sum(legs) / len(legs)) if legs else None

                # weight_multiplier removed 2026-09-04 (see score_bo3_outstanding_live_proxy
                # call site above for why) -- yoy_growth_multiplier is unaffected, that's a
                # separate, independently-confirmed adjustment.
                yoy_multiplier, yoy_growth_pct = _yoy_pl_growth_multiplier(dc_id)
                result = agent.score_bo1_private_label(
                    pl_actual_30d_by_dc.get(dc_id, 0.0), pl_expected, constants,
                    yoy_growth_multiplier=yoy_multiplier,
                )
                if leg_trailing is not None:
                    result["reason"] += f"; trailing-90d leg carries a {PL_TRAILING_LEG_GROWTH_MULTIPLIER:.1f}x growth expectation (provisional, see PL_TRAILING_LEG_GROWTH_MULTIPLIER)"
                if leg_aop is not None:
                    result["reason"] += "; AOP-allocated leg blended in (Node target x trailing-PL share, estimate, not a confirmed per-DC AOP figure)"
                if yoy_growth_pct is not None:
                    result["reason"] += f"; YoY PL {yoy_growth_pct:+.0%} vs same period last FY"
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
                # Dominant category = highest combined this+same-period-last-year sales --
                # a DC selling across multiple categories is graded on its largest one
                # (documented simplification, same single-tag-per-DC pattern DC_RAnk's
                # Cohort uses).
                dominant = max(
                    rows,
                    key=lambda r: (agent.parse_number(r.get("sum_this_30d")) or 0.0) + (agent.parse_number(r.get("sum_last_year_30d")) or 0.0),
                )
                momentum_this = (agent.parse_number(dominant.get("sum_this_30d")) or 0.0) / constants.bo4_momentum_period_days
                momentum_last_year = (agent.parse_number(dominant.get("sum_last_year_30d")) or 0.0) / constants.bo4_momentum_period_days
                sales_result = agent.score_bo4_sales_momentum(
                    momentum_this, momentum_last_year, dominant.get("business_category"), constants,
                )
                # mom_trend_pct (2026-09-04, explicit user request) -- informational
                # only, never fed into score_pct/grade above. sum_this_30d ÷ sum_prior_30d
                # (last MONTH, not last year) -- same category the DC was actually graded
                # on, no growth multiplier applied (this is a plain trend read, not a
                # target comparison). None (not 0%) when there's no real prior-month sales
                # to compare against, same never-fabricate convention as every other gap.
                sum_prior_30d = agent.parse_number(dominant.get("sum_prior_30d"))
                sales_result["mom_trend_pct"] = (
                    (agent.parse_number(dominant.get("sum_this_30d")) or 0.0) / sum_prior_30d
                    if sum_prior_30d else None
                )
                dc_bo_scores.setdefault(dc_id, {})["Sales"] = sales_result
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
        run_exceptions.extend({"record_id": r["Record_ID"], "source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in pay_exc.rows)

        promise_exc = agent.Exceptions(agent.utc_now_iso())
        try:
            promise_raw = client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_promise_to_pay(dc_ids))
            promise_by_dc = agent.normalize_promise_to_pay(promise_raw, promise_exc)
        except Exception as e:
            run_exceptions.append({"source": "task_management_visitpurposedetails", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})
        run_exceptions.extend({"record_id": r["Record_ID"], "source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in promise_exc.rows)

        club_exc = agent.Exceptions(agent.utc_now_iso())
        try:
            club_raw = client.execute_sql(agent.REDSHIFT_DB_ID, _sql_club_mapping(dc_ids))
            turnover_raw = client.execute_sql(agent.REDSHIFT_DB_ID, _sql_club_qualifying_turnover(dc_ids))
            club_rows = agent.normalize_dc_club(club_raw, [], turnover_raw, dc_financials, club_exc)
            dc_club_by_id = {row["DC_ID"]: row for row in club_rows}
        except Exception as e:
            run_exceptions.append({"source": "dc_mapping_club_scheme", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})
        run_exceptions.extend({"record_id": r["Record_ID"], "source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in club_exc.rows)

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
            recent_points_by_se: Dict[int, List[Tuple[float, float, str]]] = {}
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_recent_punch_ins(uids, plan_date)):
                uid = row["se_user_id"]
                lat, lon = agent.parse_number(row.get("lat")), agent.parse_number(row.get("lon"))
                if lat is not None and lon is not None:
                    recent_points_by_se.setdefault(uid, []).append((lat, lon, str(row.get("check_date"))))
            for uid, points in recent_points_by_se.items():
                resolved = agent.resolve_typical_origin(points)
                if resolved is None:
                    continue
                prev_punch_in_by_se[uid] = (resolved["lat"], resolved["lon"])
                if not resolved["most_recent_point_in_dominant_cluster"]:
                    # Exactly the case that produced the kanhaiya.raj1 300km+ anomaly --
                    # the SE's single most recent punch-in doesn't match where they've
                    # actually been starting their day over the last 30d. Overridden
                    # with the majority location instead of trusting the outlier, but
                    # flagged, not silently swapped.
                    run_exceptions.append({
                        "source": "attendance_attendance", "reason_code": "Origin_Point_Outlier_Overridden",
                        "detail": (
                            f"SE user_id={uid}: most recent punch-in ({points[-1][2]}) does not match the "
                            f"{resolved['days_in_cluster']}/{resolved['days_total']}-day majority location "
                            f"(last matching {resolved['most_recent_date_in_cluster']}) -- used the majority "
                            f"location instead of the most recent day's outlier reading"
                        ),
                    })
        except Exception as e:
            run_exceptions.append({"source": "attendance_attendance", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e} -- Routing Agent Origin_Point (R0.4) falls back to today's punch-in or defers"})
    else:
        run_exceptions.append({
            "source": "Source1/3/4", "reason_code": "Metabase_Not_Configured",
            "detail": "METABASE_URL/METABASE_API_KEY not set -- plan generated from DC_Master_Normalized.json only; "
                      "no live visit history, geo, financials, payments, or club data. Every DC treated as eligible "
                      "(In_Scope_Flag not re-checked against 6.2 recency), and every task is Provisional.",
        })

    top_dc_allowlist, top_dc_exc = agent.load_top_dc_allowlist()
    run_exceptions.extend({"record_id": r["Record_ID"], "source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in top_dc_exc.rows)
    excl_exc = agent.Exceptions(agent.utc_now_iso())
    agent.apply_dc_exclusion_rules(scoped_dcs, excl_exc, constants, last_visit_by_dc, plan_date, top_dc_allowlist=top_dc_allowlist)
    run_exceptions.extend({"record_id": r["Record_ID"], "source": r["Source"], "reason_code": r["Reason_Code"], "detail": r["Detail"]} for r in excl_exc.rows)
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

    # Confirmed 2026-08-18 -- DCVisitStreak.consecutive_misses (only ever written by
    # `manage.py reconcile_outcomes`, on PAST plan_dates) drives generate_se_daily_plan's
    # Critical flag for chronic non-execution. One query for every (SE, DC) pair in this
    # scope, not one per SE -- same batching reasoning as everywhere else in this file.
    all_se_uids = sorted({str(se_user_ids.get(e, e)) for e in se_emails})
    consecutive_misses_by_se_dc: Dict[Tuple[str, str], int] = {
        (s.se_id, s.dc_id): s.consecutive_misses
        for s in DCVisitStreak.objects.filter(se_id__in=all_se_uids, dc_id__in=dc_ids)
    }

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
                origin, origin_basis = prev, "prev_30d_punch_in"
            elif punch_in_coords is not None:
                origin, origin_basis = punch_in_coords, "today_punch_in"
            else:
                origin, origin_basis = None, "waiting_for_today"
            result = routing.generate_route_plans_for_se(
                plan_run, str(_uid), _email, plan_date_, candidates, origin, origin_basis, constants_,
                plan_choice=resolved_routing_plan, enable_rotation=enable_rotation,
            )
            run_exceptions.extend(result["exceptions"])
            return result

        consecutive_misses_by_dc = {
            dc_id: misses for (se_uid, dc_id), misses in consecutive_misses_by_se_dc.items() if se_uid == str(uid)
        }
        plan = agent.generate_se_daily_plan(
            str(uid), email, plan_date, in_scope, bo_scores, dynamic_params, constants,
            attendance_gate_ok=attendance_gate_ok, recent_attempts_by_dc=recent_attempts_by_se_dc.get(uid, {}),
            dc_financials=dc_financials, last_payment_by_dc=last_payment_by_dc, dc_club_by_id=dc_club_by_id,
            ytd_pl_by_dc=ytd_pl_by_dc, punch_in_coords=punch_in_by_se.get(uid), dc_bo_scores=dc_bo_scores,
            farmer_meeting_scheduled_today=farmer_meeting_confirmed_by_se.get(email, False),
            route_selector=_route_selector, consecutive_misses_by_dc=consecutive_misses_by_dc,
            promise_by_dc=promise_by_dc,
        )
        tasks = plan.get("Tasks", [])
        if not tasks:
            # Full root-cause breakdown, not just the generic reason string --
            # not_in_scope covers DCs excluded before ever reaching generate_se_daily_
            # plan() at all (Section 6: Legal_Hold/recency/Rank<=6000, computed here
            # since only services.py has se_dcs, the pre-scope-filter full assigned
            # list); in_scope_no_objective_match is generate_se_daily_plan()'s own
            # Skipped_Qualification_Detail for DCs that passed scope but failed every
            # Visits/Outstanding/PL qualifier. Together these are the exact two tiers a
            # manual investigation would otherwise have to reconstruct by hand.
            not_in_scope_detail = []
            for dc in se_dcs:
                if dc.get("In_Scope_Flag"):
                    continue
                reasons = []
                if dc.get("DC_Status") == "Legal_Hold":
                    reasons.append("Legal_Hold")
                # Visited_Too_Recently branch removed 2026-09-04 -- the rule it explained
                # no longer exists (see apply_dc_exclusion_rules docstring), so a DC can
                # never legitimately be out-of-scope for only this reason anymore.
                rank = dc.get("Rank")
                if not (isinstance(rank, (int, float)) and rank <= constants.max_eligible_rank):
                    reasons.append(f"DC_Rank_Ineligible (Rank={rank!r})")
                not_in_scope_detail.append({
                    "DC_ID": dc["DC_ID"], "DC_Name": dc.get("DC_Name"),
                    "Reason": "; ".join(reasons) if reasons else "unknown",
                })
            skipped_ses.append({
                "se_id": str(uid), "se_email": email,
                "reason": plan.get("Skipped_Reason") or "No in-scope DC qualified for any objective this run",
                "dc_breakdown": {
                    "total_assigned_dcs": len(se_dcs),
                    "not_in_scope": not_in_scope_detail,
                    "in_scope_no_objective_match": plan.get("Skipped_Qualification_Detail") or [],
                },
            })
        for t in tasks:
            pending_tasks.append(DailyTask(
                plan_run=plan_run, se_id=str(uid), se_name=email, plan_date=plan_date,
                sr_no=t["Sr_No"], dc_name=t["DC_Name"], dc_id=t["DC_ID"], distance_km=t["Distance_Km"],
                recommended_task_type=t["Recommended_Task_Type"], purpose_of_visit=t["Purpose_Of_Visit"],
                reason_of_visit=t["Reason_Of_Visit"], last_visit_date=t["Last_Visit_Date"],
                days_since_last_visit=t["Days_Since_Last_Visit"], present_outstanding=t["Present_Outstanding"],
                present_overdue=t["Present_Overdue"], overdue_aging_bucket=t.get("Overdue_Aging_Bucket"),
                avg_repayment_days=t.get("Avg_Repayment_Days"), last_order_date=t["Last_Order_Date"],
                last_order_value=t["Last_Order_Value"], last_payment_date=t["Last_Payment_Date"],
                last_payment_join_key_unconfirmed=t["Last_Payment_Join_Key_Unconfirmed"],
                ytd_private_label=t["YTD_Private_Label"], dc_club_participation=t["DC_Club_Participation"],
                club_detail=t.get("Club_Detail") or {},
                objective=t["Objective"], no_new_orders=t["No_New_Orders"], credit_on_hold=t["Credit_On_Hold"],
                credit_on_hold_reason=t["Credit_On_Hold_Reason"], estimated_duration=t["Estimated_Duration"],
                priority_multiplier=t["Priority_Multiplier"],
                finance_status=t.get("Finance_Status"),
                promise_to_pay_date=t.get("Promise_To_Pay_Date"), promise_to_pay_amount=t.get("Promise_To_Pay_Amount"),
                promise_status=t.get("Promise_Status"),
                bo_scores=t.get("BO_Scores") or {},
                bo_composite_score=t.get("BO_Composite_Score"), bo_rank=t.get("BO_Rank"),
                critical=t.get("Critical", False), critical_reasons=t.get("Critical_Reasons", ""),
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
                # Node-level peer pool, pulled alongside the block-level one -- fallback
                # for S1/PL_Recommendation when a DC's own block has too few peers (or
                # none) with trailing-30d purchase data to rank anything from (a real,
                # common gap for small/single-DC blocks, not an edge case). See the
                # per-DC entry-building loop below for where block is tried first and
                # node is only used if block yields nothing -- never the reverse, since
                # block is the more locally-relevant comparison when it has data.
                # dc_id-gated (not just node-gated) -- confirmed live 2026-08-17: unlike
                # block, some geo_mapping rows carry a real node value with no dc_id at
                # all (a node-level rollup row, not tied to one DC), which crashed
                # sorted() below on None-vs-str comparison the first time this ran
                # against Bihar's full geo_mapping. Filtered out here rather than loosened
                # into the block line above, which has no such rows in practice.
                node_by_dc = {row["dc_id"]: row["node"] for row in geo_mapping if row.get("node") and row.get("dc_id")}
                task_nodes = {node_by_dc[d] for d in task_dc_ids if d in node_by_dc}
                node_peer_dc_ids = (
                    sorted({row["dc_id"] for row in geo_mapping if row.get("dc_id") and row.get("node") in task_nodes})
                    if task_nodes else []
                )
                pull_dc_ids = sorted(set(task_dc_ids) | set(peer_dc_ids) | set(node_peer_dc_ids))

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

                # S2b Suggested Discount raw input (see _sql_coupon_discount_history
                # docstring) -- pulled once for pull_dc_ids (task DCs + block + node
                # peers), looked up per (dc_id, product_name) below rather than queried
                # per DC.
                coupon_discount_by_dc_product: Dict[str, Dict[str, float]] = {}
                for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_coupon_discount_history(pull_dc_ids, plan_date)):
                    dc_id, product = agent.normalize_id(row.get("dc_id")), row.get("product_name")
                    avg_discount = agent.parse_number(row.get("avg_discount_per_unit"))
                    if dc_id and product and avg_discount is not None:
                        coupon_discount_by_dc_product.setdefault(dc_id, {})[product] = avg_discount

                per_dc_category: Dict[str, Dict[str, float]] = {}
                for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_block_category_purchase(pull_dc_ids, plan_date)):
                    dc_id, cat = agent.normalize_id(row.get("dc_id")), row.get("category_name")
                    if dc_id and cat:
                        per_dc_category.setdefault(dc_id, {})[cat] = agent.parse_number(row.get("purchase_30d")) or 0.0

                # DC Card "Recommended Product & Brief" + Pitching Agent S1 -- product-
                # name granularity (_sql_block_product_purchase), wired 2026-08-14,
                # S1b enrichment (sub-category/brand/business segment) added 2026-08-15.
                # dc_id -> category -> product -> {value, sub_category, brand,
                # business_segment}, so the caller can find the single top-selling
                # PRODUCT (not just category) among a DC's block peers, with its full
                # S1b context attached. Attributes are template-level, not per-order, so
                # last-row-wins on duplicates is fine -- they don't vary within a product.
                per_dc_category_product: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = {}
                for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_block_product_purchase(pull_dc_ids, plan_date)):
                    dc_id, cat, product = agent.normalize_id(row.get("dc_id")), row.get("category_name"), row.get("product_name")
                    if dc_id and cat and product:
                        per_dc_category_product.setdefault(dc_id, {}).setdefault(cat, {})[product] = {
                            "value": agent.parse_number(row.get("purchase_30d")) or 0.0,
                            "sub_category": row.get("sub_category_name"),
                            "brand": row.get("product_brand"),
                            "business_segment": row.get("business_segment_name") or None,
                        }

                # DC Card / "Dehaat Center Ko Jaano" Section 1 "कौन" -- Business Area
                # Strength (Source 3h), wired 2026-08-14 alongside the DC Card feature.
                # Rebuilt 2026-08-22 (see _sql_business_area_strength_detailed docstring):
                # ALL sub-categories (not top-5), current-FY YTD window, each split
                # Branded/Private Label with a share%, product-wise within each segment
                # -- paired with the same structure over the prior FY's YTD window
                # ("Historical Performance") for sub-category-level trend. Only
                # task_dc_ids, not the wider pull_dc_ids -- unlike S1's block comparison,
                # this is never compared against peers, so there's no reason to pull it
                # for DCs never actually on a task this run.
                fy_start = _fiscal_year_start(plan_date)
                business_area_current_by_dc = _build_business_area_tree(
                    list(client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_business_area_strength_detailed(task_dc_ids, fy_start, plan_date)))
                )
                prior_fy_start, prior_plan_date = _prior_fy_window(plan_date)
                business_area_prior_by_dc = _build_business_area_tree(
                    list(client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_business_area_strength_detailed(task_dc_ids, prior_fy_start, prior_plan_date)))
                )

                extra_data_by_dc: Dict[str, Dict[str, Any]] = {}
                needs_geo_fallback: List[str] = []
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
                    block, node = block_by_dc.get(dc_id), node_by_dc.get(dc_id)

                    def _peer_stats(candidate_ids: List[str], segment: Optional[str] = None) -> Optional[Dict[str, Any]]:
                        """Category-average purchase + up to RECOMMENDED_PRODUCT_COUNT
                        (5) top-selling PRODUCTS (not just 1) among candidate_ids, within
                        dominant_category, ranked by peer-summed value, each with S1b
                        enrichment (sub-category/brand/business segment) attached --
                        widened 2026-08-18 from a single top product per direct
                        instruction. Value summed across peers first (a product 3 peers
                        each bought a little of should still outrank one only 1 peer
                        bought a lot of -- "peer trend," not "single biggest peer").
                        None if candidate_ids is empty; avg is None if none of them have
                        any purchase in dominant_category at all (vs. a real ₹0 average,
                        which the Hindi builders already treat the same as None -- see
                        their own `if not block_avg` gate). top_products is never padded
                        -- a DC with only 2 real peer products in this category just gets
                        2, not 5.

                        segment, when given, restricts totals/ranking to only that
                        business_segment BEFORE ranking (not a post-hoc filter of the
                        general top-5) -- added 2026-08-18 for dc_card.py's Private Label
                        section, which must only ever recommend a PRIVATE LABEL product.
                        Filtering the already-ranked general list instead would routinely
                        return nothing, since higher-value BRANDED bulk items (fertilizer
                        etc.) usually crowd PL products out of an unfiltered top 5 even
                        when real PL peer-purchase data exists further down."""
                        if not candidate_ids:
                            return None
                        amounts = [per_dc_category.get(p, {}).get(dominant_category, 0.0) for p in candidate_ids]
                        totals: Dict[str, float] = {}
                        attrs: Dict[str, Dict[str, Any]] = {}
                        for p in candidate_ids:
                            for product, info in per_dc_category_product.get(p, {}).get(dominant_category, {}).items():
                                if segment and info.get("business_segment") != segment:
                                    continue
                                totals[product] = totals.get(product, 0.0) + info["value"]
                                attrs[product] = info
                        ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:RECOMMENDED_PRODUCT_COUNT]
                        top_products = [
                            {
                                "name": name, "value": value, "category": dominant_category,
                                "sub_category": attrs[name].get("sub_category"),
                                "brand": attrs[name].get("brand"),
                                "business_segment": attrs[name].get("business_segment"),
                            }
                            for name, value in ranked
                        ]
                        return {
                            # avg stays the whole-category average regardless of segment
                            # -- it's never shown for a segment-filtered list (dc_card.py
                            # only ever reports products_pl's product names/values, not a
                            # PL-only average that doesn't exist as a real pulled figure).
                            "avg": (sum(amounts) / len(amounts)) if amounts else None,
                            "top_products": top_products,
                        }

                    block_ids = [p for p in peer_dc_ids if block and block_by_dc.get(p) == block]
                    node_ids = [p for p in node_peer_dc_ids if node and node_by_dc.get(p) == node]

                    if dominant_category:
                        stats, scope = _peer_stats(block_ids), "block"
                        # Block yielded nothing usable (no peers, or peers with zero
                        # purchase in this category) -- widen to node-level peers. Only
                        # this direction: block is the more locally-relevant comparison
                        # when it has real data, so it's never overridden by node.
                        if not stats or not stats["avg"]:
                            node_stats = _peer_stats(node_ids)
                            if node_stats and node_stats["avg"]:
                                stats, scope = node_stats, "node"
                        if stats and stats["avg"]:
                            entry["block_category_avg"] = stats["avg"]
                            entry["peer_comparison_scope"] = scope
                            if stats["top_products"]:
                                entry["recommended_products"] = [{**p, "scope": scope} for p in stats["top_products"]]
                                # S2b Suggested Discount -- for the #1 recommended
                                # product only (the methodology's own wording is "the
                                # recommended product + discount combination," singular).
                                top_product_name = stats["top_products"][0]["name"]
                                entry["suggested_discount"] = _suggested_discount(
                                    dc_id, top_product_name, block_ids, node_ids, coupon_discount_by_dc_product,
                                )

                    # DC Card-only additions -- read by planning/dc_card.py, ignored by
                    # planning/pitching.py's builders (they only ever ctx.get() the keys
                    # they know about).
                    entry["business_area_strength"] = business_area_current_by_dc.get(dc_id)
                    entry["business_area_strength_prior_year"] = business_area_prior_by_dc.get(dc_id)
                    entry["club"] = dc_club_by_id.get(dc_id)
                    # YoY PL comparison (confirmed 2026-08-18) -- PL-specific, distinct
                    # from purchase_last_fy/purchase_ytd above (those are overall
                    # purchase, not PL-tagged). ytd_pl itself is already in DailyTaskRow
                    # (YTD_Private_Label); last year's figure and the growth % are new.
                    entry["ytd_pl_last_year"] = ytd_pl_last_year_by_dc.get(dc_id)
                    _, entry["yoy_pl_growth_pct"] = _yoy_pl_growth_multiplier(dc_id)
                    extra_data_by_dc[dc_id] = entry
                    # Own purchases + block peers + node peers all came up empty (no
                    # recommended_products set -- either no dominant_category at all, or
                    # peers had a category average but no product-level breakdown) --
                    # flagged for the geographic fallback below (confirmed 2026-08-18:
                    # 200km radius first, then nearest Nodes by centroid distance if
                    # even that finds nothing).
                    if not entry.get("recommended_products"):
                        needs_geo_fallback.append(dc_id)

                if needs_geo_fallback:
                    _attach_nearby_product_recommendations(
                        client, dc_master, needs_geo_fallback, extra_data_by_dc, plan_date, result_key="recommended_products",
                    )

                from .pitching import generate_pitches_for_plan_run
                _, pitch_failures = generate_pitches_for_plan_run(plan_run, extra_data_by_dc)
                run_exceptions.extend({
                    "record_id": f["dc_id"], "source": "PitchingAgent", "reason_code": "Pitch_Generation_Failed",
                    "detail": f"DC {f['dc_id']}: {f['detail']}",
                } for f in pitch_failures)

                # DC Card (Preface) / "Dehaat Center Ko Jaano", wired 2026-08-14 --
                # separate try/except (own exception source) so a DC Card-specific
                # failure is never mislabeled as PitchingAgent, and vice versa; reuses
                # the exact same extra_data_by_dc Pitching just used, no re-fetch.
                try:
                    from .dc_card import generate_dc_cards_for_plan_run
                    _, card_failures = generate_dc_cards_for_plan_run(plan_run, extra_data_by_dc)
                    run_exceptions.extend({
                        "record_id": f["dc_id"], "source": "DCCardAgent", "reason_code": "DC_Card_Generation_Failed",
                        "detail": f"DC {f['dc_id']}: {f['detail']}",
                    } for f in card_failures)
                except Exception as e:
                    run_exceptions.append({"source": "DCCardAgent", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})
        except Exception as e:
            run_exceptions.append({"source": "PitchingAgent", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

    if focus_product_material_id:
        node_id = focus_product_node_id or (scope_value if scope_type == PlanRun.ScopeType.NODE else None)
        if node_id is None:
            run_exceptions.append({
                "source": "ProductCohort", "reason_code": "Focus_Product_Node_Not_Resolved",
                "detail": f"focus_product_material_id={focus_product_material_id!r} given but no focus_product_node_id, and scope_type={scope_type!r} isn't NODE -- no confirmed mapping from this scope type to a single Product Cohort node, so Focus Product Targeting was skipped this run",
            })
        else:
            fp_result = product_cohort.get_focus_product_campaign_targets(
                material_id=focus_product_material_id, node_id=node_id, years=focus_product_years,
                season_weeks=focus_product_season_weeks, crop_districts=focus_product_crop_districts,
                related_product_names=focus_product_related_products,
            )
            step_3 = fp_result["Step_3"]
            if step_3 and isinstance(step_3.get("results"), dict) and isinstance(step_3["results"].get("dcs"), list):
                # Section 6's Rank<=6000 eligibility gate (confirmed 2026-08-13) applies
                # network-wide to every agent's DC selection, but the live Product Cohort
                # API has no awareness of it -- its raw cohort can include ineligible DCs,
                # so the persisted record is tagged here for any future direct consumer.
                # Additive only: the raw API response is annotated, never filtered/mutated.
                eligible_dc_ids = {
                    dc["DC_ID"] for dc in dc_master
                    if isinstance(dc.get("Rank"), (int, float)) and dc["Rank"] <= constants.max_eligible_rank
                }
                for dc_entry in step_3["results"]["dcs"]:
                    dc_entry["rank_eligible"] = str(dc_entry.get("partnerId") or "") in eligible_dc_ids
            FocusProductTargetRun.objects.create(
                plan_run=plan_run, material_id=focus_product_material_id, node_id=node_id,
                step_2a=fp_result["Step_2A"], step_2b=fp_result["Step_2B"], step_3=step_3,
            )
            run_exceptions.extend(fp_result["exceptions"])

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
    focus_product_material_id: Optional[str] = None,
    focus_product_node_id: Optional[str] = None,
    focus_product_years: int = 4,
    focus_product_season_weeks: Optional[Dict[str, int]] = None,
    focus_product_crop_districts: Optional[List[str]] = None,
    focus_product_related_products: Optional[List[str]] = None,
    routing_plan_asker: Optional[Callable[[], str]] = None,
    routing_plan_choice: Optional[str] = None,
    enable_rotation: bool = False,
) -> Tuple[PlanRun, Dict[str, Any]]:
    """Agent TUFF's full two-step flow as a single reusable call -- Step 1 (Data
    Normalization, once-per-day, see run_normalization_step) then Step 2
    (generate_plan_for_scope, which auto-triggers Pitching + Routing, and optionally
    Focus Product Campaign Targeting -- see that function's focus_product_* docstring).
    Shared by `manage.py activate_tuff` and GET /api/planning/tuff/<scope_type>/<scope_value>/,
    so the two can never drift on the once-per-day/abort-on-empty rules. Returns
    (plan_run, normalization_info) -- normalization_info is run_normalization_step()'s
    return value, for callers that want to report Step 1's own outcome alongside the
    PlanRun."""
    normalization_info = run_normalization_step(date=plan_date, force=force_normalization, skip=skip_normalization)
    _abort_if_empty_dc_master(scope_type, scope_value, normalization_info.get("Row_Counts", {}))

    plan_run = generate_plan_for_scope(
        scope_type, scope_value, plan_date,
        farmer_meeting_asker=farmer_meeting_asker,
        farmer_meeting_confirmed_emails=farmer_meeting_confirmed_emails,
        focus_product_material_id=focus_product_material_id, focus_product_node_id=focus_product_node_id,
        focus_product_years=focus_product_years, focus_product_season_weeks=focus_product_season_weeks,
        focus_product_crop_districts=focus_product_crop_districts, focus_product_related_products=focus_product_related_products,
        routing_plan_asker=routing_plan_asker, routing_plan_choice=routing_plan_choice, enable_rotation=enable_rotation,
    )
    return plan_run, normalization_info
