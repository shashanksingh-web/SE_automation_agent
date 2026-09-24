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
import functools
import json
import sys
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import agent  # moved from a sys.path-inserted top-level script to planning/agent.py 2026-09-18

from . import data_cache, dc_selection, discount_service, product_cohort, routing
from .admin_config import load_business_constants
from .locking import LockContendedError, scope_lock
from .models import DailyTask, DCVisitStreak, ExceptionRecord, FocusProductTargetRun, PlanRun, RoutingScopeOverride
from .pitch_context import ExtraDcContext
from .notify import send_alert


class PlanningError(RuntimeError):
    """Raised for scope-resolution failures the caller should see as a 4xx, not a 500 --
    e.g. DC_Master_Normalized.json missing, or an ABM/Block/District scope requested
    without live Metabase access."""


# Reason codes this pipeline writes to run_exceptions/ExceptionRecord as ROUTINE,
# BY-DESIGN bookkeeping for a completely normal, expected outcome -- never a live-data
# failure or a data-quality problem. Added 2026-09-23 (see generate_plan_for_scope's own
# 10%-threshold alert comment for the full investigation) to stop these from being
# counted toward that alert, which they had been inflating to 100%+ on every state,
# every day, since the alert was first written -- rendering it permanently meaningless.
# Each entry below is the reason_code exactly as written at its own exc.flag()/
# run_exceptions.append() call site in planning.agent/planning.routing; see that call
# site's own comment for why it's written on every occurrence, not just on failure.
_ROUTINE_EXCEPTION_REASON_CODES = frozenset({
    # DC selection/eligibility -- written for EVERY DC an admin rule or dc_datamart's own
    # is_active flag routinely excludes, not a failure of anything.
    "DC_Not_In_Program_Selection",       # excluded by the Admin Control Panel's Program DC Selection rule
    "DC_Not_Active",                     # dc_datamart query succeeded; this DC is genuinely is_active=false
    "DC_Datamart_Inactive_Outstanding_Unavailable",  # same is_active=false signal, financials leg
    # GR-28 / Health Score overrides -- explicitly flagged so a deliberate override is
    # visible in the audit trail, per direct instruction ("flag that 60 day eligibility
    # condition") -- the override itself is the intended behavior, not a problem.
    "GR28_Bypassed_60Day_Eligibility",
    # Provisional/estimate markers -- informational caveats on an otherwise-real value,
    # not something that failed to compute.
    "FM_Urgency_Provisional",
    "SE_AOP_PL_Target_Estimate",
    # Routing Agent outcomes -- real, working decisions the algorithm makes under its own
    # documented constraints (a route hit the travel/distance ceiling, 3 independently-
    # generated plans converged to the same stops, diversity/outlier rules kicked in) --
    # not evidence anything is broken.
    "Travel_Ceiling_Exceeded", "Distance_Ceiling_Exceeded", "Exceptional_DC_Single_DC_Fallback",
    "Plans_Converged", "Route_Diversity_Enforced", "Origin_Point_Outlier_Overridden",
    "Insufficient_Candidates_For_3_Plans",
    # Documented, permanent data-model limitations flagged on every DC they apply to (see
    # each field's own docstring for why no better source exists) -- not a live failure.
    "Club_Enrollment_Flag_Unconfirmed", "Club_Turnover_Partial_Exclusion",
})


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
    # NOTE 2026-09-13: a fix was attempted here to also count self-logged Liquidation
    # visits (visit_type_id=3 "External Meeting" + visit_purpose_name mentioning
    # "liquidation") toward Days_Since_Last_Visit/Last_Visit_Date -- explicit user
    # request "add the visit purpose if se add the liqudation by himself so it reflect
    # in the plan". REVERTED after live verification: every one of the confirmed 879
    # "done" Liquidation tasks has partner_id/block_id/district_id ALL NULL and
    # type='unplanned' -- there is no field anywhere in task_management_task (or
    # task_management_visitpurposedetails, checked directly for these exact task IDs)
    # that attributes these tasks to a specific DC. A join on cc.id = t.partner_id
    # (the only DC-linking key this query has) eliminates every single one of them
    # before the WHERE clause is even reached, making that fix a silent no-op -- kept
    # reverted rather than leaving code in that implies a capability that doesn't
    # exist. Confirmed genuine data gap, not a query bug: these tasks would need a real
    # DC attribution added at the source (the app SEs log them in) before this query
    # could ever recognize them.
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
    # Latest order per DC, in TWO independent rankings -- covers both Last_Order_*
    # (filtered to 'processed' inside normalize_sales_transactions) and Credit_On_Hold
    # (any status) in one pull. Uses ROW_NUMBER() rather than Postgres's DISTINCT ON --
    # confirmed live 2026-08-04 that Redshift (this cluster) does not support DISTINCT ON
    # at all ("FeatureNotSupported").
    #
    # FIXED 2026-09-15 (found live: a DC showing a real ₹24,800 overdue balance but
    # Last_Order_Date/Value both blank, which should be structurally impossible if any
    # order had ever gone through) -- this used to rank ALL statuses together and keep
    # only the single overall-latest row (rn=1), so a DC whose most recent order attempt
    # happened to be 'failed' (or any non-'processed' status) never surfaced its real,
    # older 'processed' order at all: normalize_sales_transactions' Python-side status
    # filter had nothing to filter FROM, since the SQL itself had already discarded every
    # row except that one non-processed one. Confirmed live for DC 1000043083: latest
    # order overall was 'failed' (2026-07-28), masking a real 'processed' order twelve
    # weeks earlier (2026-05-19, Rs.25,340) that this query never even fetched. Now ranks
    # "latest of any status" and "latest of status='processed' specifically" independently
    # -- returns 1 row per DC if they're the same order, 2 if they differ (or the DC has
    # no processed order at all, in which case only the any-status row's rn_processed
    # never reaches 1 since CASE...END is NULL for every row - correctly leaves Last_
    # Order_Date/Value blank for a DC with a real order history but no processed order,
    # rather than fabricating one).
    return f"""
    SELECT dc_id, amount_total, created_at, status, credit_on_hold, credit_on_hold_reason, partner_finance_status
    FROM (
        SELECT cc.partner_id AS dc_id, o.amount_total, o.created_at, o.status,
               o.credit_on_hold, o.credit_on_hold_reason, o.partner_finance_status,
               ROW_NUMBER() OVER (PARTITION BY cc.partner_id ORDER BY o.created_at DESC) AS rn_any,
               ROW_NUMBER() OVER (
                   PARTITION BY cc.partner_id
                   ORDER BY CASE WHEN o.status = 'processed' THEN o.created_at END DESC NULLS LAST
               ) AS rn_processed
        FROM sale_orderrequest o
        JOIN customer_management_customer cc ON cc.id = o.partner_id
        WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
    ) ranked
    WHERE rn_any = 1 OR (status = 'processed' AND rn_processed = 1)
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


def _sql_promise_to_pay(dc_ids: List[str], plan_date: str) -> str:
    """Scoped counterpart of se_daily_plan_agent.SQL_PROMISE_TO_PAY_3J -- see that
    query's own docstring for the dialect trap (JSON_EXTRACT_PATH_TEXT, not ->>) and
    the "most recent promise only" / "any qualifying payment, not full amount" rules.
    No lookback window, same reasoning as _sql_payments above -- already scoped to a
    handful of dc_ids, no cost to finding each DC's true most recent promise.

    Qualifying-payment window WIDENED 2026-09-07, explicit user request: was
    [promise_created_at, promise_date] (a payment landing AFTER the committed date,
    even a day late, was never counted -- the promise was marked Broken the moment its
    date passed, regardless of whether the DC then actually paid before today). Now
    [promise_created_at, plan_date] -- any real SUCCESS payment up to and including
    today counts as Kept, even if it landed after the originally committed date. Only
    a promise with STILL no qualifying payment by today reads as Broken. The column is
    still named paid_on_time for continuity (_promise_status's own Python logic is
    unchanged -- it already only judges Kept-vs-Broken once promise_date < plan_date,
    this just widens what payment window counts as having been paid at all)."""
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
                 AND p.created_at >= lp.promise_created_at AND p.created_at <= '{plan_date}'
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


def _sql_active_schemes_for_nodes(nodes: List[str], plan_date: str) -> str:
    """Active Sales/ABS Schemes -- added 2026-09-12, explicit user request ("dc club and
    scheme are different in the system" -- a genuinely separate live system from the DC
    Club/Scheme-Tier loyalty program above, confirmed live before building this:
    scheme_details.created_at reaches 2026-09-11 (i.e. current), and every one of its
    currently-active scheme_code values (e.g. "DS Supreme Red Onion ABS MP Rabi26",
    valid 2026-09-11 to 2026-09-30) is also present in abs_scheme -- the two tables are
    in sync, not stale. abs_scheme itself carries no reliable validity_start_time/
    validity_end_time (confirmed live: NULL on every row) or Node-only scope; the actual
    current/expiry window comes from scheme_details via a scheme_code join instead.
    Node join key confirmed live: 100% of DC_Master's 93 distinct Node values match
    abs_scheme.node exactly, no fallback/fuzzy-match needed. Both tables live on "dev"
    (agent.REDSHIFT_DB_ID), same database dc_mapping_club_scheme/coupon_analysis
    already use above.

    DISTINCT since abs_scheme carries one row per (node, material) slab tier -- callers
    want one entry per scheme+product, not one per pricing bracket."""
    return f"""
    SELECT DISTINCT a.node, a.material_name, a.brand_name, a.business_category,
           a.product_sub_category, s.name AS scheme_name, s.description, s.scheme_end_date
    FROM abs_scheme a
    JOIN scheme_details s ON s.scheme_code = a.scheme_code
    WHERE a.node IN ({_sql_list(nodes)})
      AND a.active = 'true'
      AND s.is_active = 'true'
      AND s.scheme_end_date >= '{plan_date}'
    """


def _sql_scheme_description_cards(plan_date: str) -> str:
    """One row per currently-live DC scheme, with a ready-made factual English
    description (generated_description) -- added 2026-09-16, explicit user request, to
    enrich (not replace) the node-scoped abs_scheme/scheme_details pull above with a
    genuinely richer source: coupon_service.public.scheme + discounting_scheme_slab +
    scheme_rules + scheme_translations + discounting_scheme_user_status, covering ABS/
    CDS/CVR scheme types the abs_scheme join above never captured. User-provided and
    already validated live (16 Sep 2026, all 675 DC schemes, no errors). Runs on the
    same REDSHIFT_DB_ID=41 ("dev") connection as every other query in this module --
    the coupon_service.public.*/input_backend_db.public.*/dev.s3_tables.* prefixes are
    Redshift cross-database references, already proven to work on this cluster (see
    _sql_punch_in's own input_backend_db usage elsewhere), not a second connection.

    Adapted from the user's original Metabase question (its own {{scheme_id}}/
    {{scheme_name}}/{{live_on}} optional-filter blocks replaced below with a plain
    is_active + live_on=plan_date filter -- this call site always wants every
    currently-live scheme, never a single one) plus two new CTEs
    (node_ids_txt/state_ids_txt) not in the original: scheme_rules stores node/state as
    numeric ids (common_salesoffice.id/common_state.id), and the original query only
    ever surfaced those resolved into human-formatted display text (node_names,
    states -- with COALESCE fallbacks and count suffixes baked in), not something a
    caller can reliably split back apart. These two CTEs reuse the same
    common_salesoffice/common_state joins the query already performs (via node_geo/
    state_txt) to also emit a plain comma-joined list of resolved NAMES (not raw ids --
    DC_Master_Normalized only carries Node/State as names, never
    common_salesoffice.id/common_state.id, so returning ids would just push a second
    id->name lookup onto every caller for no benefit) so run_pitching_and_dc_card_agents
    can check per-node eligibility in Python instead of trusting name-string matching
    alone."""
    return f"""
    WITH s AS (
        SELECT
            sc.id, sc.name, sc.scheme_code, sc.description, sc.is_active,
            sc.scheme_type, sc.discounting_scheme_type, sc.slab_min_max_type,
            sc.discount_type, sc.booking_type, sc.benefit_channel, sc.max_discount_per_user,
            sc.scheme_start_date::date   AS booking_start,
            sc.scheme_end_date::date     AS booking_end,
            sc.discount_start_date::date AS discount_start,
            sc.discount_end_date::date   AS discount_end,
            sc.benefit_pass_date::date   AS benefit_pass_date,
            sc.created_at, sc.updated_at,
            CASE sc.unit_of_measure
                WHEN 'KILOGRAM' THEN 'kg' WHEN 'PACKET' THEN 'packet' WHEN 'LITRE' THEN 'litre' ELSE 'unit'
            END AS unit,
            CASE
                WHEN COALESCE(TRIM(sc.description), '') = '' THEN 'No - empty'
                WHEN LOWER(TRIM(sc.description)) = LOWER(TRIM(sc.name)) THEN 'No - repeats the name'
                ELSE 'Yes'
            END AS has_real_description
        FROM coupon_service.public.scheme sc
        WHERE sc.user_type = 'DC'
          AND sc.is_active = 'true'
          AND COALESCE(sc.discount_end_date, sc.scheme_end_date)::date >= '{plan_date}'
    ),

    today AS (
        SELECT CONVERT_TIMEZONE('Asia/Kolkata', GETDATE())::date AS d
    ),

    /* ---------- Slabs ---------- */
    slab_base AS (
        SELECT
            sl.scheme_id,
            sl.id AS slab_id,
            sl.expiry_date,
            s.slab_min_max_type AS basis,
            s.discount_type,
            s.unit,
            CASE WHEN TRIM(sl.slab_max) ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}$'
                 THEN TO_DATE(TRIM(sl.slab_max), 'DD-MM-YYYY') END AS max_date,
            CASE
                WHEN TRIM(sl.slab_min) ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}$'
                    THEN TO_CHAR(TO_DATE(TRIM(sl.slab_min), 'DD-MM-YYYY'), 'DD Mon YYYY')
                WHEN TRIM(sl.slab_min) ~ '^[0-9]+$'
                    THEN TO_CHAR(TRIM(sl.slab_min)::bigint, 'FM99,99,99,99,999')
                ELSE TRIM(sl.slab_min)
            END AS min_txt,
            CASE
                WHEN COALESCE(TRIM(sl.slab_max), '') = '' THEN NULL
                WHEN TRIM(sl.slab_max) ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}$'
                    THEN TO_CHAR(TO_DATE(TRIM(sl.slab_max), 'DD-MM-YYYY'), 'DD Mon YYYY')
                WHEN TRIM(sl.slab_max) ~ '^[0-9]+$'
                    THEN TO_CHAR(TRIM(sl.slab_max)::bigint, 'FM99,99,99,99,999')
                ELSE TRIM(sl.slab_max)
            END AS max_txt,
            CASE WHEN sl.discount_rate = ROUND(sl.discount_rate, 0)
                 THEN ROUND(sl.discount_rate, 0)::bigint::varchar
                 ELSE sl.discount_rate::varchar
            END AS rate_num,
            CASE WHEN sl.booking_amount_rate IS NULL THEN NULL
                 WHEN sl.booking_amount_rate = ROUND(sl.booking_amount_rate, 0)
                 THEN ROUND(sl.booking_amount_rate, 0)::bigint::varchar
                 ELSE sl.booking_amount_rate::varchar
            END AS adv_num,
            CASE
                WHEN TRIM(sl.slab_min) ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}$'
                    THEN DATEDIFF(day, '2000-01-01'::date, TO_DATE(TRIM(sl.slab_min), 'DD-MM-YYYY'))::numeric(18,4)
                WHEN TRIM(sl.slab_min) ~ '^[0-9]+([.][0-9]+)?$'
                    THEN TRIM(sl.slab_min)::numeric(18,4)
            END AS min_num,
            CASE
                WHEN TRIM(sl.slab_max) ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}$'
                    THEN DATEDIFF(day, '2000-01-01'::date, TO_DATE(TRIM(sl.slab_max), 'DD-MM-YYYY'))::numeric(18,4)
                WHEN TRIM(sl.slab_max) ~ '^[0-9]+([.][0-9]+)?$'
                    THEN TRIM(sl.slab_max)::numeric(18,4)
            END AS max_num,
            sl.discount_rate::varchar || '|' || COALESCE(sl.booking_amount_rate::varchar, '-')
                || '|' || COALESCE(TO_CHAR(sl.expiry_date, 'YYYY-MM-DD'), '-') AS rate_key
        FROM coupon_service.public.discounting_scheme_slab sl
        JOIN s ON s.id = sl.scheme_id
    ),

    slab_seq AS (
        SELECT b.*,
               COALESCE(b.min_num, b.slab_id) AS sort_key,
               LAG(b.rate_key) OVER (PARTITION BY b.scheme_id ORDER BY COALESCE(b.min_num, b.slab_id), b.slab_id) AS prev_rate_key,
               LAG(b.max_num)  OVER (PARTITION BY b.scheme_id ORDER BY COALESCE(b.min_num, b.slab_id), b.slab_id) AS prev_max_num
        FROM slab_base b
    ),

    slab_islands AS (
        SELECT q.*,
               SUM(CASE WHEN q.prev_rate_key = q.rate_key
                             AND q.min_num IS NOT NULL AND q.prev_max_num IS NOT NULL
                             AND q.min_num - q.prev_max_num BETWEEN 0 AND 1
                        THEN 0 ELSE 1 END)
                   OVER (PARTITION BY q.scheme_id ORDER BY q.sort_key, q.slab_id ROWS UNBOUNDED PRECEDING) AS island
        FROM slab_seq q
    ),

    slab_ranked AS (
        SELECT i.*,
               COUNT(*) OVER (PARTITION BY i.scheme_id, i.island) AS slabs_in_group,
               ROW_NUMBER() OVER (PARTITION BY i.scheme_id, i.island ORDER BY i.sort_key, i.slab_id) AS rn_first,
               ROW_NUMBER() OVER (PARTITION BY i.scheme_id, i.island ORDER BY i.sort_key DESC, i.slab_id DESC) AS rn_last
        FROM slab_islands i
    ),

    slab_merged AS (
        SELECT f.scheme_id, f.slab_id, f.sort_key, f.slabs_in_group, f.expiry_date,
               f.basis, f.discount_type, f.unit, f.rate_num, f.adv_num,
               f.min_txt, l.max_txt
        FROM slab_ranked f
        JOIN slab_ranked l
          ON l.scheme_id = f.scheme_id AND l.island = f.island AND l.rn_last = 1
        WHERE f.rn_first = 1
    ),

    slab_fmt AS (
        SELECT
            scheme_id, slab_id, sort_key, slabs_in_group, expiry_date,
            CASE basis
                WHEN 'DATE'  THEN CASE WHEN max_txt IS NULL THEN 'from ' || min_txt
                                       ELSE min_txt || ' to ' || max_txt END
                WHEN 'DAYS'  THEN CASE WHEN max_txt IS NULL THEN min_txt || '+ days'
                                       ELSE min_txt || '-' || max_txt || ' days' END
                WHEN 'VALUE' THEN CASE WHEN max_txt IS NULL THEN '₹' || min_txt || '+'
                                       ELSE '₹' || min_txt || ' - ₹' || max_txt END
                ELSE              CASE WHEN max_txt IS NULL THEN min_txt || '+ ' || unit
                                       ELSE min_txt || '-' || max_txt || ' ' || unit END
            END AS range_txt,
            CASE WHEN discount_type = 'PERCENT' THEN rate_num || '%'
                 ELSE '₹' || rate_num || '/' || unit
            END AS rate_txt,
            CASE WHEN adv_num IS NULL THEN NULL
                 WHEN discount_type = 'PER_UNIT' THEN '₹' || adv_num || '/' || unit
                 ELSE adv_num || ' (unit not defined)'
            END AS adv_txt
        FROM slab_merged
    ),

    slab_txt AS (
        SELECT
            scheme_id,
            SUM(slabs_in_group) AS slab_count,
            COUNT(*)            AS slab_rows,
            LISTAGG(range_txt || ' -> ' || rate_txt, ' | ')
                WITHIN GROUP (ORDER BY sort_key, slab_id) AS slabs_short,
            MIN(adv_txt)   AS adv_min,
            MAX(adv_txt)   AS adv_max,
            COUNT(adv_txt) AS adv_slabs
        FROM slab_fmt
        GROUP BY scheme_id
    ),

    date_slab_check AS (
        SELECT b.scheme_id,
               SUM(CASE WHEN b.max_date IS NULL OR b.max_date >= td.d THEN 1 ELSE 0 END) AS date_slabs_still_open
        FROM slab_base b
        CROSS JOIN today td
        WHERE b.basis = 'DATE'
        GROUP BY b.scheme_id
    ),

    /* ---------- Rules: who and what the scheme covers ---------- */
    rules AS (
        SELECT r.scheme_id, r.node_ids, r.state_ids, r.district_ids, r.block_ids, r.village_ids,
               r.partner_ids, r.dc_type, r.product_template_ids, r.sku,
               r.brand_ids, r.category_ids, r.sub_category_ids
        FROM coupon_service.public.scheme_rules r
        JOIN s ON s.id = r.scheme_id
        WHERE r.is_active = 'true'
    ),

    rule_count AS (
        SELECT scheme_id, COUNT(*) AS active_rules FROM rules GROUP BY scheme_id
    ),

    rule_dims AS (
                  SELECT scheme_id, 'node' AS dim, node_ids AS id_list FROM rules
        UNION ALL SELECT scheme_id, 'state',        state_ids            FROM rules
        UNION ALL SELECT scheme_id, 'district',     district_ids         FROM rules
        UNION ALL SELECT scheme_id, 'block',        block_ids            FROM rules
        UNION ALL SELECT scheme_id, 'village',      village_ids          FROM rules
        UNION ALL SELECT scheme_id, 'dc_list',      partner_ids          FROM rules
        UNION ALL SELECT scheme_id, 'dc_type',      dc_type              FROM rules
        UNION ALL SELECT scheme_id, 'template',     product_template_ids FROM rules
        UNION ALL SELECT scheme_id, 'sku',          sku                  FROM rules
        UNION ALL SELECT scheme_id, 'brand',        brand_ids            FROM rules
        UNION ALL SELECT scheme_id, 'category',     category_ids         FROM rules
        UNION ALL SELECT scheme_id, 'sub_category', sub_category_ids     FROM rules
    ),

    rule_arr AS (
        SELECT scheme_id, dim, SPLIT_TO_ARRAY(id_list, ',') AS arr
        FROM rule_dims
        WHERE COALESCE(TRIM(id_list), '') <> ''
    ),

    rule_vals AS (
        SELECT DISTINCT scheme_id, dim, val
        FROM (SELECT ra.scheme_id, ra.dim, TRIM(v::varchar) AS val
              FROM rule_arr ra, ra.arr AS v) x
        WHERE val <> ''
    ),

    node_geo AS (
        SELECT rv.scheme_id, rv.val AS node_id, so.name AS node_name, cst.name AS state_name
        FROM rule_vals rv
        LEFT JOIN input_backend_db.public.common_salesoffice so ON so.id::varchar = rv.val
        LEFT JOIN input_backend_db.public.common_state cst     ON cst.id = so.state_id
        WHERE rv.dim = 'node'
    ),

    node_txt AS (
        SELECT scheme_id,
               COUNT(*) AS node_count,
               LISTAGG(COALESCE(node_name, 'node ' || node_id), ', ')
                   WITHIN GROUP (ORDER BY COALESCE(node_name, node_id)) AS node_names
        FROM node_geo
        GROUP BY scheme_id
    ),

    node_state_txt AS (
        SELECT scheme_id, LISTAGG(state_name, ', ') WITHIN GROUP (ORDER BY state_name) AS node_states
        FROM (SELECT DISTINCT scheme_id, state_name FROM node_geo WHERE state_name IS NOT NULL) x
        GROUP BY scheme_id
    ),

    state_txt AS (
        SELECT rv.scheme_id,
               COUNT(*) AS state_count,
               LISTAGG(COALESCE(cst.name, 'state ' || rv.val), ', ')
                   WITHIN GROUP (ORDER BY COALESCE(cst.name, rv.val)) AS state_names
        FROM rule_vals rv
        LEFT JOIN input_backend_db.public.common_state cst ON cst.id::varchar = rv.val
        WHERE rv.dim = 'state'
        GROUP BY rv.scheme_id
    ),

    dc_type_txt AS (
        SELECT scheme_id, LISTAGG(val, ', ') WITHIN GROUP (ORDER BY val) AS dc_types
        FROM rule_vals
        WHERE dim = 'dc_type'
        GROUP BY scheme_id
    ),

    other_limits_txt AS (
        SELECT scheme_id,
               LISTAGG(dim || ' (' || n::varchar || ')', ', ') WITHIN GROUP (ORDER BY dim) AS other_limits,
               MAX(CASE WHEN dim IN ('brand', 'category', 'sub_category') THEN 1 ELSE 0 END) AS has_product_group_limit
        FROM (SELECT scheme_id, dim, COUNT(*) AS n
              FROM rule_vals
              WHERE dim IN ('district', 'block', 'village', 'dc_list', 'brand', 'category', 'sub_category')
              GROUP BY scheme_id, dim) x
        GROUP BY scheme_id
    ),

    /* Machine-parseable node/state eligibility (added 2026-09-16, not in the original
    Metabase question) -- reuses node_geo/common_state above, but as a plain
    comma-joined list of resolved names with no display formatting, so
    run_pitching_and_dc_card_agents can split() it and check membership per DC's own
    Node/State (which DC_Master_Normalized carries as names, never
    common_salesoffice.id/common_state.id). Falls back to the raw id text when a name
    can't be resolved, same as node_txt above, rather than silently dropping it. */
    node_ids_txt AS (
        SELECT scheme_id, LISTAGG(val, ',') WITHIN GROUP (ORDER BY val) AS node_names_raw
        FROM (SELECT scheme_id, COALESCE(node_name, node_id) AS val FROM node_geo) x
        GROUP BY scheme_id
    ),

    state_ids_txt AS (
        SELECT rv.scheme_id, LISTAGG(COALESCE(cst.name, rv.val), ',') WITHIN GROUP (ORDER BY COALESCE(cst.name, rv.val)) AS state_names_raw
        FROM rule_vals rv
        LEFT JOIN input_backend_db.public.common_state cst ON cst.id::varchar = rv.val
        WHERE rv.dim = 'state'
        GROUP BY rv.scheme_id
    ),

    /* ---------- Products (product templates + SAP SKUs) ---------- */
    products AS (
        SELECT rv.scheme_id, 'template' AS kind, rv.val AS product_id,
               pt.name AS product_name, pb.name AS brand, pt.business_segment_name AS segment
        FROM rule_vals rv
        LEFT JOIN input_backend_db.public.products_template pt ON pt.id::varchar = rv.val
        LEFT JOIN input_backend_db.public.products_brand pb    ON pb.id::varchar = pt.brand_id::varchar
        WHERE rv.dim = 'template'
        UNION ALL
        SELECT rv.scheme_id, 'sku', rv.val, mm.material_name, mm.brand_name, mm.business_segment
        FROM rule_vals rv
        LEFT JOIN (
            SELECT LTRIM(material_id::varchar, '0') AS material_id,
                   material_name, brand_name, business_segment,
                   ROW_NUMBER() OVER (PARTITION BY LTRIM(material_id::varchar, '0') ORDER BY material_name) AS rn
            FROM dev.s3_tables.material_master
            WHERE SPLIT_PART(domain_id, '.', 1) = '10'
        ) mm ON mm.material_id = rv.val AND mm.rn = 1
        WHERE rv.dim = 'sku'
    ),

    product_summary AS (
        SELECT scheme_id,
               COUNT(*) AS products,
               SUM(CASE WHEN product_name IS NULL THEN 1 ELSE 0 END) AS products_not_found,
               COUNT(DISTINCT brand) AS brands
        FROM products
        GROUP BY scheme_id
    ),

    segment_txt AS (
        SELECT scheme_id, LISTAGG(segment, ', ') WITHIN GROUP (ORDER BY segment) AS segments
        FROM (SELECT DISTINCT scheme_id, segment FROM products WHERE segment IS NOT NULL) x
        GROUP BY scheme_id
    ),

    /* ---------- Bookings (advance booking schemes) ---------- */
    booking_lines AS (
        SELECT bo.id, bo.scheme_id, bo.status, bo.booking_amount
        FROM input_backend_db.public.discount_schemes_bookingorder bo
        JOIN s ON s.id = bo.scheme_id
    ),

    booking_summary AS (
        SELECT scheme_id,
               SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END)                       AS active_bookings,
               SUM(CASE WHEN status = 'done' THEN booking_amount ELSE 0 END)          AS advance_collected
        FROM booking_lines
        GROUP BY scheme_id
    ),

    /* ---------- Translations / T&C ---------- */
    translation_txt AS (
        SELECT t.scheme_id,
               LISTAGG(CASE WHEN COALESCE(TRIM(t.terms_and_conditions), '') <> ''
                            THEN t.language_code || ': ' || t.terms_and_conditions END, ' | ')
                   WITHIN GROUP (ORDER BY t.language_code) AS terms_and_conditions
        FROM coupon_service.public.scheme_translations t
        JOIN s ON s.id = t.scheme_id
        GROUP BY t.scheme_id
    )

    SELECT
        s.id                                               AS scheme_id,
        s.name                                              AS scheme_name,
        CASE s.discounting_scheme_type
            WHEN 'ABS' THEN 'Advance booking scheme'
            WHEN 'CVR' THEN 'Cumulative volume rebate'
            WHEN 'CDS' THEN 'Cash discount scheme'
            ELSE COALESCE(s.discounting_scheme_type, s.scheme_type)
        END                                                 AS scheme_type,

        CASE s.discounting_scheme_type
            WHEN 'ABS' THEN 'Advance booking scheme'
            WHEN 'CVR' THEN 'Cumulative volume rebate'
            WHEN 'CDS' THEN 'Cash discount scheme'
            ELSE COALESCE(s.discounting_scheme_type, s.scheme_type)
        END
        || ' for DCs'
        || CASE
               WHEN rc.scheme_id IS NULL THEN ' (no active eligibility rule set up)'
               WHEN nt.node_count > 0
                   THEN ' in ' || nt.node_count::varchar
                        || CASE WHEN nt.node_count = 1 THEN ' node' ELSE ' nodes' END
                        || COALESCE(' (' || nst.node_states || ')', '')
               WHEN stt.state_count > 0 THEN ' in ' || stt.state_names
               ELSE ' (no location limit)'
           END
        || COALESCE(', ' || dtt.dc_types || ' DCs only', '')
        || CASE
               WHEN rc.scheme_id IS NULL THEN ''
               WHEN COALESCE(ps.products, 0) = 0
                   THEN CASE WHEN olt.has_product_group_limit = 1 THEN ', on selected brands/categories'
                             ELSE ', no product list in rules' END
               ELSE ', on ' || ps.products::varchar || ' '
                    || COALESCE(LOWER(sgt.segments) || ' ', '')
                    || CASE WHEN ps.products = 1 THEN 'product' ELSE 'products' END
                    || CASE WHEN ps.brands = 1 THEN ' (1 brand)'
                            WHEN ps.brands > 1 THEN ' (' || ps.brands::varchar || ' brands)'
                            ELSE '' END
           END
        || '. '
        || CASE WHEN s.discounting_scheme_type = 'ABS'
                THEN 'Booking window ' || COALESCE(TO_CHAR(s.booking_start, 'DD Mon YYYY'), '?')
                     || ' - ' || COALESCE(TO_CHAR(s.booking_end, 'DD Mon YYYY'), '?')
                     || CASE WHEN st.adv_slabs = st.slab_rows AND st.adv_min = st.adv_max THEN ', advance ' || st.adv_min
                             WHEN st.adv_slabs > 0 THEN ', advance varies by slab'
                             ELSE '' END
                     || '. '
                ELSE ''
           END
        || 'Discount by '
        || CASE s.slab_min_max_type
               WHEN 'DATE'  THEN CASE WHEN s.discounting_scheme_type = 'ABS' THEN 'booking date' ELSE 'date' END
               WHEN 'DAYS'  THEN 'payment days'
               WHEN 'VALUE' THEN 'value'
               ELSE CASE WHEN s.discounting_scheme_type = 'CVR' THEN 'total quantity bought' ELSE 'quantity' END
           END
        || ': ' || COALESCE(st.slabs_short, 'no slabs set')
        || COALESCE('; on purchases ' || TO_CHAR(s.discount_start, 'DD Mon YYYY')
                    || ' - ' || TO_CHAR(s.discount_end, 'DD Mon YYYY'), '')
        || COALESCE('. Paid as ' || LOWER(REPLACE(s.benefit_channel, '_', ' ')), '')
        || COALESCE(' by ' || TO_CHAR(s.benefit_pass_date, 'DD Mon YYYY'), '')
        || COALESCE('. Max ₹' || TO_CHAR(s.max_discount_per_user, 'FM99,99,99,99,999') || ' per DC', '')
        || '.'                                              AS generated_description,

        COALESCE(tr.terms_and_conditions, 'None')          AS terms_and_conditions,
        st.slabs_short,
        -- Structured benefit facts for the pitch's scheme pointer (added 2026-09-17,
        -- "add the profit of these scheme"): the same numbers generated_description
        -- folds into prose, kept separate so a Hindi pointer can be built from them.
        CASE WHEN st.adv_slabs = st.slab_rows AND st.adv_min = st.adv_max THEN st.adv_min END AS advance_per_unit,
        CASE s.slab_min_max_type
            WHEN 'DATE'  THEN 'date' WHEN 'DAYS' THEN 'days' WHEN 'VALUE' THEN 'value' ELSE 'quantity'
        END                                                 AS slab_basis,
        LOWER(REPLACE(s.benefit_channel, '_', ' '))         AS benefit_channel,
        TO_CHAR(s.booking_end, 'YYYY-MM-DD')                AS booking_end,
        s.max_discount_per_user                             AS max_discount_per_dc,
        COALESCE(rc.active_rules, 0)                        AS active_rules,
        nit.node_names_raw,
        sit.state_names_raw,
        COALESCE(ps.products, 0)                            AS products,
        COALESCE(bs.active_bookings, 0)                     AS active_bookings,
        COALESCE(bs.advance_collected, 0)                   AS advance_collected

    FROM s
    LEFT JOIN slab_txt st          ON st.scheme_id  = s.id
    LEFT JOIN rule_count rc        ON rc.scheme_id  = s.id
    LEFT JOIN node_txt nt          ON nt.scheme_id  = s.id
    LEFT JOIN node_state_txt nst   ON nst.scheme_id = s.id
    LEFT JOIN state_txt stt        ON stt.scheme_id = s.id
    LEFT JOIN dc_type_txt dtt      ON dtt.scheme_id = s.id
    LEFT JOIN other_limits_txt olt ON olt.scheme_id = s.id
    LEFT JOIN product_summary ps   ON ps.scheme_id  = s.id
    LEFT JOIN segment_txt sgt      ON sgt.scheme_id = s.id
    LEFT JOIN booking_summary bs   ON bs.scheme_id  = s.id
    LEFT JOIN translation_txt tr   ON tr.scheme_id  = s.id
    LEFT JOIN node_ids_txt nit     ON nit.scheme_id = s.id
    LEFT JOIN state_ids_txt sit    ON sit.scheme_id = s.id
    ORDER BY s.id DESC
    """


_SCHEME_DESCRIPTION_CACHE_PATH = agent.BASE_DIR / "output" / "scheme_description_cache.json"
_scheme_description_cache_store = agent.JsonFileCache(_SCHEME_DESCRIPTION_CACHE_PATH)

# Discount Service live cross-check cache -- added 2026-09-19, same once-per-day-by-
# plan_date convention as _SCHEME_DESCRIPTION_CACHE_PATH above, for the same reason
# (this is a live external HTTP call; every plan generation that day should pay for it
# once, not per generation). Used only to FLAG a name-matched scheme the live API no
# longer shows as active (see discount_service.cross_check_active_status) -- never to
# change what generated_description says, per the "supplement, don't replace" decision
# (project_discount_service_supplement_20260918 memory).
_DISCOUNT_SERVICE_LIVE_CACHE_PATH = agent.BASE_DIR / "output" / "discount_service_live_cache.json"
_discount_service_live_cache_store = agent.JsonFileCache(_DISCOUNT_SERVICE_LIVE_CACHE_PATH)


def _fetch_live_discount_schemes(plan_date: str) -> List[Dict[str, Any]]:
    """Cached wrapper around discount_service.get_active_discount_schemes() -- mirrors
    _fetch_scheme_description_cards' own caching exactly. Returns [] (not an exception)
    on any failure INCLUDING "not configured" -- this is a best-effort cross-check, not
    a required Source, so run_pipeline's caller is responsible for deciding whether an
    empty result is worth flagging (see the try/except around this call site, which
    only logs Discount_Service_Cross_Check_Failed on a genuine exception, not on a
    quiet empty/not-configured result -- an operator who never set up the live API
    credentials shouldn't see a spurious exception on every single run)."""
    cache = _discount_service_live_cache_store.load()
    if cache.get("date") == plan_date and "schemes" in cache:
        return cache["schemes"]
    result = discount_service.get_active_discount_schemes()
    cache["date"] = plan_date
    cache["schemes"] = json.loads(json.dumps(result.get("schemes", []), default=str))
    _discount_service_live_cache_store.save()
    return cache["schemes"]


def _fetch_scheme_description_cards(client: Any, plan_date: str) -> List[Dict[str, Any]]:
    """Cached wrapper around _sql_scheme_description_cards() -- added 2026-09-17 after
    live timing showed the raw query costs ~17.5s (vs ~0.6s for the node-scoped
    _sql_active_schemes_for_nodes it enriches), and it was being re-run on EVERY single
    plan generation regardless of scope -- a single SE's plan paid the same 17.5s as a
    whole STATE's, directly inflating every frontend request that triggers generation
    (diagnosed live 2026-09-17: "why loading time on frontend too much").

    Same once-per-day convention as se_daily_plan_agent's own normalization dedup
    (run_normalization_step): cached by plan_date, one Redshift round trip per day, every
    other generation that day reuses it. json.dumps(..., default=str) round-trip on
    write -- raw psycopg2 rows carry Decimal/date objects JsonFileCache's plain
    json.dumps() can't serialize on its own."""
    cache = _scheme_description_cache_store.load()
    if cache.get("date") == plan_date and "rows" in cache:
        return cache["rows"]
    rows = client.execute_sql(agent.REDSHIFT_DB_ID, _sql_scheme_description_cards(plan_date))
    cache["date"] = plan_date
    cache["rows"] = json.loads(json.dumps(rows, default=str))
    _scheme_description_cache_store.save()
    return cache["rows"]


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


# ---------------------------------------------------------------------------
# Source 3k -- DC Composite Health Score (added 2026-09-06, business-confirmed via
# BO_Configuration_Sheet_v3.xlsx's dedicated sheet). 5 of 7 sub-scores are live-computable
# today; Credit/OD stay hardcoded 0 (see agent.compute_dc_health_score docstring) pending
# a Locus-to-sap_partner_id bridge the business itself calls a data-access gap, not a
# design gap. Note on the doc's own NRV query: it filters `status NOT IN ('cancelled',
# 'rejected')` -- live-checked this round, sale_orderrequest.status only ever takes
# 'processed'/'failed'/'processing' in this DB (no 'cancelled'/'rejected' value exists at
# all), so that filter is a no-op here. Uses the same `status = 'processed'` filter this
# codebase already applies everywhere else against this table (_sql_pl_metrics above),
# for consistency, not the doc's literal (here, ineffective) clause.
# ---------------------------------------------------------------------------

def _sql_nrv_score(dc_ids: List[str], plan_date: str) -> str:
    # NRV_Score = nrv_12m / MAX(nrv_12m across the WHOLE network) -- this query returns
    # only the numerator (this scope's DC totals); the network-wide MAX is a SEPARATE,
    # unscoped query (_sql_nrv_network_max) so the denominator never drifts per-scope
    # (a State-scoped run must use the same MAX a Network-scoped run would).
    d = datetime.fromisoformat(plan_date).date()
    year_start = (d - timedelta(days=365)).isoformat()
    return f"""
    SELECT cc.partner_id AS dc_id, SUM(sor.amount_total) AS nrv_12m
    FROM sale_orderrequest sor
    JOIN customer_management_customer cc ON cc.id = sor.partner_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
      AND sor.status = 'processed'
      AND sor.request_date >= '{year_start}' AND sor.request_date <= '{plan_date}'
    GROUP BY cc.partner_id
    """


def _sql_nrv_network_max(plan_date: str) -> str:
    # Unscoped on purpose -- the true network-wide denominator, not this run's scope.
    # Real confirmed result (business-run + this session's own check): ~Rs4.23 crore.
    # Queried live every run rather than hardcoded, so it can never silently go stale.
    d = datetime.fromisoformat(plan_date).date()
    year_start = (d - timedelta(days=365)).isoformat()
    return f"""
    SELECT MAX(nrv_12m) AS network_max_nrv
    FROM (
        SELECT cc.partner_id AS dc_id, SUM(sor.amount_total) AS nrv_12m
        FROM sale_orderrequest sor
        JOIN customer_management_customer cc ON cc.id = sor.partner_id
        WHERE sor.status = 'processed'
          AND sor.request_date >= '{year_start}' AND sor.request_date <= '{plan_date}'
        GROUP BY cc.partner_id
    ) t
    """


def _peer_group_indices(geo_mapping: List[Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, List[str]], Dict[str, List[str]]]:
    """Peer benchmarking (2026-09-07, explicit user request) -- block, then node
    fallback, reusing the exact same peer-grouping shape the Pitching Agent's S1
    PL_Recommendation already established further down in generate_plan_for_scope (see
    its own block_by_dc/node_by_dc/peer_dc_ids/node_peer_dc_ids). geo_mapping is the
    full, unscoped Source 1c table -- every DC appears in its own block/node group
    (including itself), so a DC's own value is always one of the candidates its
    peer-max is taken over, keeping the resulting ratio naturally in [0,1]."""
    block_by_dc: Dict[str, str] = {}
    node_by_dc: Dict[str, str] = {}
    dc_ids_by_block: Dict[str, List[str]] = {}
    dc_ids_by_node: Dict[str, List[str]] = {}
    for row in geo_mapping:
        dc_id = row.get("dc_id")
        if not dc_id:
            continue
        block, node = row.get("block"), row.get("node")
        if block:
            block_by_dc[dc_id] = block
            dc_ids_by_block.setdefault(block, []).append(dc_id)
        if node:
            node_by_dc[dc_id] = node
            dc_ids_by_node.setdefault(node, []).append(dc_id)
    return block_by_dc, node_by_dc, dc_ids_by_block, dc_ids_by_node


def _peer_group_for(
    dc_id: str, block_by_dc: Dict[str, str], node_by_dc: Dict[str, str],
    dc_ids_by_block: Dict[str, List[str]], dc_ids_by_node: Dict[str, List[str]],
) -> List[str]:
    """Block first; node fallback only when the block has no other DC in it at all (a
    real, common gap for small/single-DC blocks -- same fallback trigger reasoning as
    Pitching Agent's own S1 peer logic: block is more locally relevant when it has
    data, node is only used when block yields nothing)."""
    block = block_by_dc.get(dc_id)
    if block:
        peers = dc_ids_by_block.get(block, [])
        if len(peers) > 1:
            return peers
    node = node_by_dc.get(dc_id)
    return dc_ids_by_node.get(node, []) if node else []


def _peer_relative_score(
    dc_id: str, raw_value_by_dc: Dict[str, Optional[float]],
    block_by_dc: Dict[str, str], node_by_dc: Dict[str, str],
    dc_ids_by_block: Dict[str, List[str]], dc_ids_by_node: Dict[str, List[str]],
    fallback_max: Optional[float] = None,
) -> Optional[float]:
    """own-value / MAX(peer-group's own values), clamped [0,1]. fallback_max (e.g. the
    old network-wide max) is used only when this DC has no peer group at all, or every
    peer (including itself) has no value in raw_value_by_dc -- never silently drops to
    a guessed number; returns None (missing-component rule) if even that fails."""
    own = raw_value_by_dc.get(dc_id)
    if own is None:
        return None
    peers = _peer_group_for(dc_id, block_by_dc, node_by_dc, dc_ids_by_block, dc_ids_by_node)
    peer_values = [raw_value_by_dc[p] for p in peers if raw_value_by_dc.get(p) is not None]
    peer_max = max(peer_values) if peer_values else None
    if not peer_max:
        peer_max = fallback_max
    if not peer_max or peer_max <= 0:
        return None
    return max(0.0, min(1.0, own / peer_max))


def _sql_pl_contribution(dc_ids: List[str], plan_date: str) -> str:
    # PL_Contribution input: PL% = SUM(price*qty WHERE PRIVATE LABEL) / SUM(price*qty),
    # trailing 365 days -- same confirmed join chain as _sql_pl_metrics above, but a
    # 365-day window (not 90d/30d) and BOTH the PL-tagged and all-segment totals in one
    # pass via CASE WHEN, since PL_Contribution's denominator is total sales, not a
    # separate baseline computation.
    d = datetime.fromisoformat(plan_date).date()
    year_start = (d - timedelta(days=365)).isoformat()
    return f"""
    SELECT cc.partner_id AS dc_id,
           SUM(CASE WHEN pt.business_segment_name = 'PRIVATE LABEL' THEN sol.price_unit * sol.quantity ELSE 0 END) AS pl_value_365d,
           SUM(sol.price_unit * sol.quantity) AS total_value_365d
    FROM sale_orderrequestline sol
    JOIN sale_orderrequest sor ON sor.id = sol.order_request_id
    JOIN customer_management_customer cc ON cc.id = sor.partner_id
    JOIN products_product pp ON pp.id = sol.product_id
    JOIN products_template pt ON pt.id = pp.template_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
      AND sor.status = 'processed'
      AND sor.created_at >= '{year_start}' AND sor.created_at <= '{plan_date}'
    GROUP BY cc.partner_id
    """


def _sql_return_score(dc_ids: List[str], plan_date: str) -> str:
    # Return_Rate = SUM(credit_note_net_value) / SUM(sale_orderrequest.amount_total),
    # trailing 365 days. b2b_sales_return lives on Redshift (db 41); sale_orderrequest
    # lives on the input-backend Postgres (db 31) -- two separate database servers, no
    # cross-DB join possible, so this query returns ONLY the b2b_sales_return
    # (Redshift) side. The sales-total denominator reuses _sql_nrv_score's own
    # nrv_12m result (== SUM(sale_orderrequest.amount_total), the identical quantity)
    # rather than re-querying it -- see the caller in generate_plan_for_scope.
    # b2b_sales_return.partner_id is ALREADY sap_partner_id format directly (confirmed
    # live this round -- 10-digit values, e.g. '1000043726') -- no bridge needed here.
    d = datetime.fromisoformat(plan_date).date()
    year_start = (d - timedelta(days=365)).isoformat()
    return f"""
    SELECT partner_id AS dc_id, SUM(credit_note_net_value) AS returns_365d
    FROM b2b_sales_return
    WHERE partner_id::text IN ({_sql_list(dc_ids)})
      AND credit_note_timestamp >= '{year_start}' AND credit_note_timestamp <= '{plan_date}'
    GROUP BY partner_id
    """


def _sql_pathik_overdue(dc_ids: List[str]) -> str:
    # GR-28's interim OD signal (business-confirmed 2026-09-06): pathik_report.overdue,
    # NOT customer_management_input_outstanding.current_od (Source 3d) as previously
    # assumed -- the doc explicitly flags this as a correction. pathik_report's grain is
    # one row per partner per SE per day (Source 1a), confirmed live this round to carry
    # many rows per sap_partner_id -- ROW_NUMBER() picks the most recent row per DC (this
    # codebase's established no-DISTINCT-ON-on-Redshift convention), not just any row.
    return f"""
    WITH ranked AS (
        SELECT sap_partner_id AS dc_id, overdue,
               ROW_NUMBER() OVER (PARTITION BY sap_partner_id ORDER BY transaction_date DESC) AS rn
        FROM pathik_report
        WHERE sap_partner_id::text IN ({_sql_list(dc_ids)})
    )
    SELECT dc_id, overdue FROM ranked WHERE rn = 1
    """


def _sql_od_bridge(dc_ids: List[str]) -> str:
    # OD_Score's real formula, wired 2026-09-07 (explicit user request, after a live
    # cross-check found LOCUS_DB_ID (27, "locus") IS reachable from this environment via
    # the same Redshift connection/credentials as everything else -- previously assumed
    # unreachable, see se_daily_plan_agent.RedshiftDirectClient's own docstring
    # correction. Two-step cross-database process: this query is Step 1 (bridge,
    # Redshift-41), _sql_od_aging below is Step 2 (aging, the separate "locus" database,
    # LOCUS_DB_ID) -- no single query can do both since they're genuinely different
    # physical database connections.
    #
    # PARTITION BY kunnr (sap_partner_id), not ledger_partner_id -- caught live: an
    # earlier version of this query partitioned by ledger_partner_id (matching GR-28's
    # own bridge query, which needs the OPPOSITE direction and is correct as written
    # there), which for THIS direction let pure noise through. Confirmed real case:
    # kunnr 1000015534 has two candidate ledger_partner_ids in the source table --
    # '378988' (16,737 occurrences) and '967629' (1 occurrence) -- partitioning by
    # ledger_partner_id ranks each of those two as "its own top row" independently
    # (trivially true for a partition of size 1), returning BOTH as if they were
    # equally valid. Partitioning by kunnr instead correctly keeps only '378988' as
    # this DC's single dominant ledger_partner_id, discarding '967629' as noise.
    return f"""
    WITH ranked AS (
        SELECT ledger_partner_id, kunnr AS sap_partner_id, COUNT(*) AS cnt,
               ROW_NUMBER() OVER (PARTITION BY kunnr ORDER BY COUNT(*) DESC) AS rn
        FROM sap_locus_document_check
        WHERE kunnr IS NOT NULL AND ledger_partner_id IS NOT NULL
          AND kunnr::text IN ({_sql_list(dc_ids)})
        GROUP BY ledger_partner_id, kunnr
    )
    SELECT ledger_partner_id, sap_partner_id FROM ranked WHERE rn = 1
    """


def _sql_od_aging(ledger_partner_ids: List[str]) -> str:
    # Step 2 (aging calculation) -- runs against LOCUS_DB_ID ("locus" database), NOT
    # Redshift-41 -- see _sql_od_bridge's docstring. Keyed by ledger_ledgerentry's own
    # partner_id (the Locus-internal ID, i.e. _sql_od_bridge's ledger_partner_id), NOT
    # sap_partner_id directly -- caller bridges the two in Python (see the composite-
    # scoring wiring below). Field names confirmed live against information_schema.
    # columns this round (partner_id, type, amount, active, visible, overdue_date,
    # is_paid all real columns) -- overdue_date specifically, NOT to_date (a real but
    # different column on this same table that silently returns zero aged debt for
    # every DC if used by mistake -- caught live before wiring this in).
    #
    # REWRITTEN 2026-09-07 -- the original approach (still visible in git history)
    # summed GROSS aged-invoice buckets, then FIFO-netted a DC's lifetime Payment/
    # credit_note total against those buckets oldest-first (standard AR-aging
    # convention). Caught live on a real, verified case (ledger_partner_id 1362680/
    # sap_partner_id 1000020693): FIFO netting scored this DC od_90plus=0 (OD_Score=1.0,
    # "Strong") because its lifetime payments summed close to its lifetime invoicing --
    # but its own per-invoice is_paid flag shows real invoices sitting UNPAID in the
    # 90-120/120-180/180-365-day buckets (Rs4,55,832 total 90+, verified: SUM(amount)
    # split by is_paid true/false reconciles to the penny with total invoiced,
    # 96,06,279.61 + 6,60,885.22 = 1,02,67,164.83) while various NEWER invoices got paid
    # instead. FIFO's oldest-first assumption is simply wrong for how this real customer
    # pays -- netting aggregates can't see which SPECIFIC invoices are unpaid, only
    # whether the totals happen to balance.
    #
    # Fixed by reading is_paid directly, per invoice, instead of any aggregate-vs-
    # aggregate netting -- no Payment/credit_note query needed anymore at all.
    # outstanding_amount/paid_amount (would have been the ideal per-invoice remaining-
    # balance columns, avoiding even the "unpaid = full amount, no partial-payment
    # visibility" caveat below) are confirmed DEAD on this table -- read exactly 0.00 on
    # every single row regardless of is_paid, including invoices flagged unpaid with a
    # real amount -- so is_paid is the most granular reliable signal actually available
    # here. Caveat: a genuinely PARTIALLY paid invoice still reads is_paid=false and
    # counts its FULL amount as unpaid (no way to see the partial-payment remainder
    # specifically, since the columns that would show it are the broken ones) -- an
    # overstatement risk in that case, but far smaller and more defensible than FIFO's
    # proven understatement.
    #
    # Conditional SUM (not a WHERE is_paid='false' filter) so a DC with real invoice
    # history that's ALL paid off still returns a row (correctly all-zero buckets, a
    # genuine Strong/1.0) -- distinct from a DC with NO invoice rows at all, which
    # returns no row here and stays a real missing-component None downstream, never
    # silently upgraded to "Strong" for having nothing to check.
    return f"""
    SELECT partner_id,
      SUM(CASE WHEN is_paid = 'false' AND CURRENT_DATE - overdue_date < 90 THEN amount ELSE 0 END) AS aged_0_90,
      SUM(CASE WHEN is_paid = 'false' AND CURRENT_DATE - overdue_date >= 90 THEN amount ELSE 0 END) AS aged_90_plus,
      SUM(CASE WHEN is_paid = 'false' THEN amount ELSE 0 END) AS overall_outstanding
    FROM ledger_ledgerentry
    WHERE type ILIKE 'Invoice' AND active = 'true'
      AND partner_id IN ({_sql_list(ledger_partner_ids)})
    GROUP BY partner_id
    """


def _sql_credit_payments(ledger_partner_ids: List[str]) -> str:
    # Credit_Score's real formula, wired 2026-09-07 (explicit user request, business-
    # confirmed formula + worked example). Runs against LOCUS_DB_ID ("locus" database,
    # Postgres) -- reuses OD_Score's own sap_partner_id -> ledger_partner_id bridge
    # (_sql_od_bridge, same GR-32 resolution) since credit_line_customer.
    # source_identifier_id IS that same ledger_partner_id value (confirmed live: querying
    # this table for source_identifier_id='378988', OD's own worked-example
    # ledger_partner_id, returns a real matching row) -- no separate bridge query needed.
    #
    # AVG((p.payment_date - l.overdue_date)::numeric) is DELIBERATELY NOT used here --
    # caught live: Redshift's AVG() over an integer/date-diff silently truncates to a
    # whole number (returned exactly -36 for the real worked example, ledger_partner_id
    # 378988) instead of the true decimal average (-36.1546, confirmed by computing
    # SUM(...)::numeric / COUNT(*) manually, which matches the business's own reported
    # figure exactly) -- the same "runs without erroring but silently wrong" class of bug
    # as OD_Score's to_date/overdue_date trap. SUM/COUNT division avoids it.
    return f"""
    SELECT
      c.source_identifier_id AS ledger_partner_id,
      COUNT(*) AS num_payments,
      SUM(p.payment_date - l.overdue_date)::numeric / COUNT(*) AS ard,
      100.0 * SUM(CASE WHEN p.payment_date <= l.overdue_date THEN 1 ELSE 0 END) / COUNT(*) AS pct_paid_in_due
    FROM payment_payment p
    JOIN loan_paymentloanmap lpm ON lpm.payment_id = p.id
    JOIN loan_loan l ON l.id = lpm.loan_id
    JOIN credit_line_customercreditline ccl ON ccl.id = l.customer_credit_line_id
    JOIN credit_line_customer c ON c.id = ccl.customer_id
    WHERE p.status = 'POSTED'
      AND p.sub_status = 'RECONCILED'
      AND l.overdue_date IS NOT NULL
      AND l.disbursement_date >= CURRENT_DATE - INTERVAL '395 days'
      AND p.payment_date >= CURRENT_DATE - INTERVAL '365 days'
      AND c.source_identifier_id IN ({_sql_list(ledger_partner_ids)})
    GROUP BY c.source_identifier_id
    HAVING COUNT(*) >= 5
    """


def _credit_score_from_payments(ard: float, pct_paid_in_due: float) -> float:
    """Business-confirmed: pct_paid_in_due x ard_factor. ard_factor is full weight (1.0)
    up to 120 days average repayment delay, decays 2%/day past that, floors at exactly 0
    at 170 days. A negative ard (pays early on average) is clamped to 0 rather than
    rewarded above the on-time baseline -- early and exactly-on-time read the same."""
    ard_clamped = max(ard, 0.0)
    ard_factor = 1.0 if ard_clamped <= 120 else max(0.0, 1.0 - 0.02 * (ard_clamped - 120))
    return (pct_paid_in_due / 100.0) * ard_factor


def _sql_credit_line_details(ledger_partner_ids: List[str]) -> str:
    # Raw credit_limit/available_credit_limit/status, added 2026-09-07 (explicit user
    # request) -- runs against LOCUS_DB_ID, reuses the same credit_line_customer ->
    # credit_line_customercreditline join as Credit_Score's own _sql_credit_payments,
    # NOT derived from Credit_Score itself (independent raw fields).
    #
    # A customer can have MULTIPLE credit_line_customercreditline rows (confirmed live:
    # up to 3 for a single customer_id) -- ROW_NUMBER() picks status='ACTIVE' first
    # (the DC's real, currently-usable credit line), then most recent effective_from,
    # then highest id as a final tiebreak, rather than picking an arbitrary/stale row.
    # "Credit is active" maps to status='ACTIVE' specifically, NOT the active column --
    # confirmed live that active is 'true' for essentially every row regardless of
    # status (ACTIVE/ONHOLD/DORMANT all show active='true'), so active alone is not a
    # useful signal for this.
    return f"""
    WITH ranked AS (
        SELECT c.source_identifier_id AS ledger_partner_id,
               cl.credit_limit, cl.available_credit_limit, cl.status,
               ROW_NUMBER() OVER (
                   PARTITION BY c.source_identifier_id
                   ORDER BY (cl.status = 'ACTIVE') DESC, cl.effective_from DESC NULLS LAST, cl.id DESC
               ) AS rn
        FROM credit_line_customercreditline cl
        JOIN credit_line_customer c ON c.id = cl.customer_id
        WHERE c.source_identifier_id IN ({_sql_list(ledger_partner_ids)})
    )
    SELECT ledger_partner_id, credit_limit, available_credit_limit, status
    FROM ranked WHERE rn = 1
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


# _sql_bo4_momentum/_sql_bo5_meetings REMOVED 2026-09-07, explicit user request ("Stop
# computing Sales & Long-Term entirely") -- these exclusively fed score_bo4_sales_
# momentum/score_bo5_long_term (both removed from se_daily_plan_agent.py), never used
# for anything else. _sql_bo5_meetings_mtd below is a SEPARATE query (calendar
# month-to-date, not a rolling 30 days) feeding FM_Urgency/compute_fm_urgency, which is
# unaffected by this removal and stays.


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


# _sql_bo5_first_orders REMOVED 2026-09-07, explicit user request ("Stop computing
# Sales & Long-Term entirely") -- exclusively fed BO5's onboarding-count input, no
# longer used anywhere.


def _sql_dc_purchase_summary(dc_ids: List[str], plan_date: str) -> str:
    # Pitching Agent (S3 purchase-half / S6 / S7), wired 2026-08-08 -- reuses the
    # customer_management_customer bridge and status='processed' rule already
    # established by _sql_orders(). Fiscal year = April-March,
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


_PRODUCT_TAXONOMY_CACHE_PATH = agent.BASE_DIR / "output" / "product_taxonomy_cache.json"
_product_taxonomy: Optional[Dict[str, Dict[str, str]]] = None


def _load_product_taxonomy() -> Dict[str, Dict[str, str]]:
    """MCP-sourced snapshot (2026-09-14) of products_category/products_subcategory/
    products_brand -- worked around here because redshift_metabase_readonly lost SELECT
    on those 3 tables (still blocked as of this writing; see the DC Card "dc ko pehchaane"
    generation-failure investigation, which traced the crash to these 3 tables on top of
    the 5 already fixed). All 3 are small, slow-changing reference tables (5/27/1208 rows
    as of the snapshot) -- a static id->name lookup is a reasonable interim stand-in
    until the GRANT lands, not a permanent replacement. The 3 SQL builders below now
    select the raw *_id columns off products_template instead of LEFT JOINing the
    blocked tables; callers resolve id->name from this cache in Python."""
    global _product_taxonomy
    if _product_taxonomy is None:
        try:
            _product_taxonomy = json.loads(_PRODUCT_TAXONOMY_CACHE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _product_taxonomy = {"category": {}, "subcategory": {}, "brand": {}}
    return _product_taxonomy


def _category_name(category_id: Optional[str]) -> Optional[str]:
    return _load_product_taxonomy()["category"].get(str(category_id)) if category_id is not None else None


def _subcategory_name(sub_category_id: Optional[str]) -> Optional[str]:
    return _load_product_taxonomy()["subcategory"].get(str(sub_category_id)) if sub_category_id is not None else None


def _brand_name(brand_id: Optional[str]) -> Optional[str]:
    return _load_product_taxonomy()["brand"].get(str(brand_id)) if brand_id is not None else None


def _sql_block_category_purchase(dc_ids: List[str], plan_date: str) -> str:
    # Pitching Agent (S1, Same-Block Purchase), wired 2026-08-08 -- trailing-30d purchase
    # summed by (dc_id, category), for the caller to aggregate into a block-level peer
    # average in Python once each DC's Block is known (from Geo_Mapping_1c, resolved
    # separately -- this query has no block column of its own, sale_orderrequest has no
    # geo data). Category granularity (not per-SKU) matches the doc's own example
    # phrasing ("PL फर्टिलाइज़र ₹15,000") reasonably well without full per-product detail.
    d = datetime.fromisoformat(plan_date).date()
    month_start = (d - timedelta(days=30)).isoformat()
    # products_category is currently blocked for this DB role (see _load_product_taxonomy
    # docstring) -- selects the raw category_id here instead of joining; the caller
    # resolves category_id -> category_name from the local MCP-sourced cache.
    return f"""
    SELECT cc.partner_id AS dc_id, tmpl.category_id::text AS category_id,
           SUM(sol.price_unit * sol.quantity) AS purchase_30d
    FROM sale_orderrequest o
    JOIN customer_management_customer cc ON cc.id = o.partner_id
    JOIN sale_orderrequestline sol ON sol.order_request_id = o.id
    JOIN products_product prod ON prod.id = sol.product_id
    JOIN products_template tmpl ON tmpl.id = prod.template_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)}) AND o.status = 'processed'
      AND o.created_at >= '{month_start}'
    GROUP BY cc.partner_id, tmpl.category_id
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
    # products_category/products_subcategory/products_brand are currently blocked for
    # this DB role (see _load_product_taxonomy docstring) -- selects the raw *_id columns
    # here instead of joining; the caller resolves id -> name from the local cache.
    return f"""
    SELECT cc.partner_id AS dc_id, tmpl.category_id::text AS category_id,
           tmpl.sub_category_id::text AS sub_category_id,
           tmpl.name AS product_name, tmpl.brand_id::text AS brand_id,
           tmpl.business_segment_name AS business_segment_name,
           SUM(sol.price_unit * sol.quantity) AS purchase_30d
    FROM sale_orderrequest o
    JOIN customer_management_customer cc ON cc.id = o.partner_id
    JOIN sale_orderrequestline sol ON sol.order_request_id = o.id
    JOIN products_product prod ON prod.id = sol.product_id
    JOIN products_template tmpl ON tmpl.id = prod.template_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)}) AND o.status = 'processed'
      AND o.created_at >= '{month_start}'
    GROUP BY cc.partner_id, tmpl.category_id, tmpl.sub_category_id, tmpl.name, tmpl.brand_id, tmpl.business_segment_name
    """


# BO1 PL_Expected's trailing-90d leg (confirmed 2026-08-18): raises the recent-
# performance baseline by 20% before averaging it with the AOP-target leg, i.e. this
# DC is expected to beat its own trailing average by this much, not just match it.
# Provisional business default, NOT the same as config item 1.3's growth-requirement
# language (that one asks for the AOP-target leg to carry a growth factor, not this
# leg) -- a distinct engineering decision, picked deliberately over multiplying the AOP
# leg or the final combined figure instead. MOVED 2026-09-07 onto BusinessConstants.
# pl_trailing_leg_growth_multiplier (was a bare module constant) so the Admin Control
# Panel can override it live -- tune there, or change the class default here.

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
    # products_category/products_subcategory are currently blocked for this DB role (see
    # _load_product_taxonomy docstring) -- selects the raw *_id columns here instead of
    # joining; the caller (_build_business_area_tree) resolves id -> name from the local
    # cache. sol.product_brand is unaffected -- it's a direct column on
    # sale_orderrequestline, never joined off products_brand.
    return f"""
    SELECT
      cc.partner_id AS dc_id, tmpl.category_id::text AS category_id,
      tmpl.sub_category_id::text AS sub_category_id,
      CASE WHEN tmpl.business_segment_name = 'PRIVATE LABEL' THEN 'Private Label' ELSE 'Branded' END AS brand_tier,
      sol.product_name, sol.product_brand,
      SUM(sol.price_unit * sol.quantity) AS product_gross_value
    FROM sale_orderrequestline sol
    JOIN sale_orderrequest sor ON sor.id = sol.order_request_id
    JOIN customer_management_customer cc ON cc.id = sor.partner_id
    JOIN products_product prod ON prod.id = sol.product_id
    JOIN products_template tmpl ON tmpl.id = prod.template_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)})
      AND sor.status = 'processed'
      AND sor.created_at >= '{window_start}' AND sor.created_at <= '{window_end}'
    GROUP BY cc.partner_id, tmpl.category_id, tmpl.sub_category_id, brand_tier, sol.product_name, sol.product_brand
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
    in this file. Resolves sub_category_id -> sub_category_name from the local taxonomy
    cache here (see _load_product_taxonomy) since the query itself no longer joins the
    currently-blocked products_subcategory table."""
    tree: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        dc_id = agent.normalize_id(row.get("dc_id"))
        value = agent.parse_number(row.get("product_gross_value")) or 0.0
        if not dc_id or value <= 0:
            continue
        subcat_name = _subcategory_name(row.get("sub_category_id")) or "Unclassified"
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
    client: "agent.RedshiftDirectClient", dc_master: "agent.Table", needs_geo_fallback: List[str],
    extra_data_by_dc: Dict[str, ExtraDcContext], plan_date: str,
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

    # Static DC_Master_Normalized.json is missing Latitude/Longitude for ~60% of the
    # whole network (confirmed live 2026-09-15: 11,532/19,330 rows) -- and a DC that
    # needs this fallback (no dominant_category, i.e. no purchase in the last 30 days)
    # is exactly the kind of DC disproportionately likely to also be missing from a
    # normalization snapshot. Root-caused a real "recommended_products empty" report:
    # 5 real DCs (same Bettiah node, all with genuine purchase history, just none in
    # the last 30d) all had Latitude/Longitude = None in dc_master, so _own_coords
    # returned None for every one of them and the geo-fallback never even ran a
    # candidate search. Same live-preferred/static-fallback pattern as routing.py's
    # _live_geo_lookup (added the same day, same root cause) -- overlay live
    # input_partner_details.lat_2/long_2 for exactly the DCs needing this fallback
    # before computing their own origin coordinates.
    live_geo: Dict[str, Tuple[float, float]] = {}
    try:
        for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_geo(needs_geo_fallback)):
            lat, lon = agent.parse_number(row.get("latitude")), agent.parse_number(row.get("longitude"))
            if lat is not None and lon is not None:
                live_geo[row["sap_partner_id"]] = (lat, lon)
    except Exception:
        pass

    def _own_coords(dc_id: str) -> Optional[Tuple[float, float]]:
        if dc_id in live_geo:
            return live_geo[dc_id]
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
            row["category_name"] = _category_name(row.get("category_id"))
            row["sub_category_name"] = _subcategory_name(row.get("sub_category_id"))
            row["product_brand"] = _brand_name(row.get("brand_id"))
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


# Product benefit text fed to the AI pitch (added 2026-09-16, explicit user request:
# "in sales - batana part product benifits will be described by SE to DC for maximise
# trust and sales by using llm"). products_template carries real per-product
# description/description_en/description_hi columns (confirmed live: 1,727 of 9,376
# templates have an English one, 1,343 a Hindi one -- composition, use stage, target
# crops, dosage, e.g. "18% Nitrogen and 46% Phosphorous", "8-12 लीटर दूध देने वाली
# गायों के लिए"), so the model can ground a benefit claim in the product's OWN text
# rather than improvise one. Looked up once per run for just the names that actually
# ended up recommended (a handful), not joined into the heavy trailing-30d GROUP BY
# queries above -- keeps those unchanged and avoids grouping on long text columns.
PRODUCT_DESCRIPTION_MAX_CHARS = 500


def _sql_product_descriptions(product_names: List[str]) -> str:
    # MAX() over NULLIF-trimmed text: a name can map to several templates (variants),
    # and an empty string must lose to a real description rather than win the group.
    return f"""
    SELECT name, MAX(NULLIF(TRIM(description_hi), '')) AS description_hi,
           MAX(NULLIF(TRIM(description_en), '')) AS description_en,
           MAX(NULLIF(TRIM(description), '')) AS description
    FROM products_template
    WHERE name IN ({_sql_list(product_names)})
    GROUP BY name
    """


def _attach_product_descriptions(client: "agent.RedshiftDirectClient", extra_data_by_dc: Dict[str, ExtraDcContext]) -> Optional[str]:
    """Mutates every recommended_products entry in place, adding "description" (Hindi
    preferred since the pitch is Hindi, else English, else the untagged column; None
    when the template has none -- the AI prompt then says so explicitly rather than
    passing an empty string the model might read as "describe it yourself").
    Whitespace-collapsed and capped at PRODUCT_DESCRIPTION_MAX_CHARS so a long
    marketing blurb can't crowd the rest of the prompt. Fail-open: a failed lookup
    leaves every product without a description and returns the failure detail for
    the caller's Exceptions_Report (Live_Pull_Failed, same as every other live pull
    here) -- never blocks the pitch. Returns None on success."""
    names = sorted({
        p["name"] for entry in extra_data_by_dc.values()
        for p in (entry.get("recommended_products") or []) if p.get("name")
    })
    if not names:
        return None
    by_name: Dict[str, str] = {}
    try:
        for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_product_descriptions(names)):
            text = row.get("description_hi") or row.get("description_en") or row.get("description")
            if row.get("name") and text:
                collapsed = " ".join(str(text).split())
                by_name[row["name"]] = collapsed[:PRODUCT_DESCRIPTION_MAX_CHARS]
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    for entry in extra_data_by_dc.values():
        for p in entry.get("recommended_products") or []:
            p["description"] = by_name.get(p.get("name"))
    return None


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
    # t.status = 'done' added 2026-09-17 (explicit user correction: "planned means from
    # se daily planning not from pathik"). Pathik's task_management_task has exactly two
    # statuses for a DC visit, confirmed live: 'submitted' is the SE's own plan in Pathik
    # -- every future-dated row is 'submitted', none 'done', and ~12% of each past day's
    # rows stay 'submitted' forever (never carried out) -- and 'done' is the visit having
    # happened. Without this filter a visit merely planned in Pathik counted as executed,
    # inflating COMPLETED. "Planned" is SE Daily Planning's DailyTask; Pathik only ever
    # answers "did it happen".
    return f"""
    SELECT cc.partner_id AS sap_partner_id, p.user_id AS se_user_id, p.plan_execution_date, t.status AS task_status
    FROM task_management_task t
    JOIN task_management_plan p ON p.id = t.plan_id
    JOIN customer_management_customer cc ON cc.id = t.partner_id
    WHERE t.visit_type_id = 1 AND t.status = 'done' AND p.user_id IN ({",".join(str(u) for u in se_user_ids)})
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


def _sql_payment_outcomes(dc_ids: List[str], plan_date: str) -> str:
    # Collection realised in the same [plan_date, plan_date+2] window the visit/order
    # outcome pulls use (added 2026-09-16 -- reconcile_outcomes recorded visits and
    # orders but never what a DC actually PAID, so actual_payment_amount stayed NULL and
    # the collection half of every Promise-To-Pay task was unmeasurable).
    # payments_paymenttransaction has no amount of its own; it lives on
    # payments_paymentreferencemap, joined via payment_reference_id = map.id -- confirmed
    # live 2026-09-16 (3,746 of 4,666 SUCCESS transactions since 2026-09-01 carry an
    # amount that way; the two other plausible keys, reference_number/
    # reference_system_identifier, match zero rows). A SUCCESS transaction with no map
    # row simply contributes nothing -- never estimated.
    return f"""
    SELECT cc.partner_id AS dc_id, SUM(m.amount) AS amount_paid, COUNT(*) AS payments
    FROM payments_paymenttransaction p
    JOIN payments_paymentreferencemap m ON m.id = p.payment_reference_id
    JOIN customer_management_customer cc ON cc.id = p.customer_id
    WHERE cc.partner_id::text IN ({_sql_list(dc_ids)}) AND p.status = 'SUCCESS'
      AND p.created_at >= DATE '{plan_date}' AND p.created_at <= DATE '{plan_date}' + INTERVAL '2 days'
    GROUP BY cc.partner_id
    """


def _resolve_geo_mapping(client: "agent.RedshiftDirectClient", geo_mapping_cache: Optional[Dict[str, agent.Table]] = None) -> agent.Table:
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
    scope_type: str, scope_value: str, dc_master: agent.Table, client: "agent.RedshiftDirectClient",
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
        stdout.write("    Plan C -- AI-Reasoned via Anthropic Claude (one route + a written reason; requires ANTHROPIC_API_KEY)")
        answer = input("  Choice [A/b/c]: ").strip().upper()
        return answer if answer in ("B", "C") else "A"

    return ask


def _exc(source: str, reason_code: str, detail: str, record_id: Optional[str] = None) -> Dict[str, str]:
    """Builds one run_exceptions entry -- the {"source", "reason_code", "detail",
    "record_id"} dict shape every live-pull/validation failure in this file (and
    planning.routing) appends to run_exceptions, eventually persisted via
    persist_exceptions() below. Extracted 2026-09-16 (architecture audit finding): this
    exact dict was previously hand-built at 47+ call sites with no shared constructor --
    a typo'd or missing key at any one of them was a silent data-quality gap, not a
    caught bug. New call sites should prefer this over a bare dict literal; existing
    ones are being migrated incrementally, not all at once in one large diff."""
    return {"source": source, "reason_code": reason_code, "detail": detail, "record_id": record_id or ""}


def persist_exceptions(plan_run: PlanRun, run_exceptions: List[Dict[str, str]]) -> None:
    """Bulk-persists run_exceptions (see _exc() above) as ExceptionRecord rows tied to
    plan_run -- the single shared "go to feedback" step both generate_plan_for_scope
    (below) and planning.routing.resync_daily_tasks_from_selected_plan call, instead of
    each independently hand-writing the same ExceptionRecord.objects.bulk_create(...)
    (architecture audit finding, 2026-09-16 -- routing.py's resync function had grown
    its own byte-for-byte copy of this exact block). No-ops on an empty list rather than
    issuing a pointless empty bulk_create."""
    if not run_exceptions:
        return
    run_ts = agent.utc_now_iso()
    ExceptionRecord.objects.bulk_create([
        ExceptionRecord(
            plan_run=plan_run, record_id=str(e.get("record_id") or e.get("dc_id") or ""),
            source=e["source"], reason_code=e["reason_code"], detail=e["detail"], run_timestamp=run_ts,
        )
        for e in run_exceptions
    ])


def run_pitching_and_dc_card_agents(
    plan_run: PlanRun, plan_date: str, client, geo_mapping_cache: Optional[dict] = None,
    dc_financials: Optional[Dict[str, Any]] = None, dc_club_by_id: Optional[Dict[str, Any]] = None,
    active_schemes_by_node: Optional[Dict[str, Any]] = None, ytd_pl_last_year_by_dc: Optional[Dict[str, Any]] = None,
    yoy_pl_growth_fn: Optional[Callable[[str], Tuple[float, Optional[float]]]] = None,
    task_ids: Optional[Iterable[int]] = None,
) -> List[dict]:
    """Runs Pitching Agent + DC Card generation for every DC currently on plan_run's
    DailyTask set. Extracted 2026-09-15 from generate_plan_for_scope's own inline block
    (unchanged body) so it can also be called by planning.routing whenever DailyTask
    rows change after initial generation -- route plan switch (select_default_route_plan/
    accept_route_plan) or SE add/remove-stop (edit_route_stops), all of which funnel
    through resync_daily_tasks_from_selected_plan. Re-derives task_dc_ids fresh from the
    DB each call rather than taking it as a parameter, so it's safe to call again any
    time: generate_pitches_for_plan_run/generate_dc_cards_for_plan_run both use
    update_or_create keyed on daily_task (OneToOne), so an existing DC's pitch/card is
    simply refreshed, never duplicated, and a genuinely new DC gets one for the first
    time. Returns exception dicts for the caller to persist via ExceptionRecord.bulk_create.

    dc_financials/dc_club_by_id/active_schemes_by_node/ytd_pl_last_year_by_dc/
    yoy_pl_growth_fn are the DC-Card-only enrichment context generate_plan_for_scope
    already has computed earlier in its own run (Sources 3d/3g/3h's own live pulls) --
    passed through unchanged for that call site, so its behavior here is a pure
    refactor. A caller that doesn't have these on hand (planning.routing, after a
    plan-switch/add-stop) omits them: they default to empty/neutral, meaning the
    PITCH SCRIPT itself (S1/S2 recommended products + discount, computed fresh from
    task_dc_ids/pull_dc_ids inside this function, no outer dependency) is always fully
    populated, but a DC newly added via route-switch/add-stop gets a DC Card with its
    Business Area Strength/Club/Active Schemes/YoY PL sections blank until the next
    full activate_tuff/generate_se_plan re-run repopulates them -- same honest-degrade
    convention as every other missing-source case in this file, not fabricated.

    task_ids (added 2026-09-24, real-time per-SE visibility): restricts BOTH total_tasks/
    task_dc_ids below AND the two sub-calls to just these DailyTask ids -- generate_plan_
    for_scope's per-SE loop passes one SE's own just-created task ids here so that SE's
    pitches/cards appear immediately, without re-processing every earlier SE's tasks too
    on each call (see generate_pitches_for_plan_run's own docstring for the full
    rationale). None (every caller before this date) means every task on plan_run,
    unchanged behavior -- including planning.routing's resync call site, which correctly
    keeps seeing the whole plan_run since a route switch/add-stop can touch any of its
    SEs' tasks, not just one."""
    dc_financials = dc_financials or {}
    dc_club_by_id = dc_club_by_id or {}
    active_schemes_by_node = active_schemes_by_node or {}
    ytd_pl_last_year_by_dc = ytd_pl_last_year_by_dc or {}
    if yoy_pl_growth_fn is None:
        def yoy_pl_growth_fn(dc_id: str) -> Tuple[float, Optional[float]]:
            return 1.0, None
    dc_master = load_dc_master()
    run_exceptions: List[dict] = []
    task_qs = plan_run.tasks.filter(dc_id__isnull=False)
    if task_ids is not None:
        task_qs = task_qs.filter(id__in=task_ids)
    total_tasks = task_qs.count()

    if client.configured and total_tasks > 0:
        try:
            task_dc_ids = list(task_qs.values_list("dc_id", flat=True).distinct())
            # Purpose_Of_Visit per dc_id (added 2026-09-20, explicit user request --
            # "for all se sale pitcg only for focused private label") -- one DailyTask
            # row per dc_id per plan_run (same assumption task_dc_ids' own .distinct()
            # already makes), so a plain dict is safe. Used below to restrict S1's
            # recommended-product ranking to PRIVATE LABEL for any task whose bundled
            # Purpose_Of_Visit includes Sale (e.g. "Promise To Pay / Collection + Sale"),
            # not just a task whose ENTIRE purpose is Sale alone.
            purpose_by_dc: Dict[str, str] = dict(
                plan_run.tasks.exclude(dc_id__isnull=True).values_list("dc_id", "purpose_of_visit")
            )
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
                    dc_id, cat = agent.normalize_id(row.get("dc_id")), _category_name(row.get("category_id"))
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
                    dc_id, cat, product = agent.normalize_id(row.get("dc_id")), _category_name(row.get("category_id")), row.get("product_name")
                    if dc_id and cat and product:
                        per_dc_category_product.setdefault(dc_id, {}).setdefault(cat, {})[product] = {
                            "value": agent.parse_number(row.get("purchase_30d")) or 0.0,
                            "sub_category": _subcategory_name(row.get("sub_category_id")),
                            "brand": _brand_name(row.get("brand_id")),
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

                extra_data_by_dc: Dict[str, ExtraDcContext] = {}
                needs_geo_fallback: List[str] = []
                for dc_id in task_dc_ids:
                    entry: ExtraDcContext = dict(purchase_by_dc.get(dc_id, {}))  # type: ignore[assignment]
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

                    def _block_then_node(segment: Optional[str]) -> Tuple[Optional[Dict[str, Any]], str]:
                        # Block yielded nothing usable (no peers, or peers with zero
                        # purchase in this category) -- widen to node-level peers. Only
                        # this direction: block is the more locally-relevant comparison
                        # when it has real data, so it's never overridden by node.
                        s, sc = _peer_stats(block_ids, segment), "block"
                        if not s or not s["avg"]:
                            node_s = _peer_stats(node_ids, segment)
                            if node_s and node_s["avg"]:
                                s, sc = node_s, "node"
                        return s, sc

                    if dominant_category:
                        # Sale-purpose Private Label preference (added 2026-09-20,
                        # explicit user request -- "for all se sale pitcg only for
                        # focused private label"): "sale" anywhere in this DC's bundled
                        # Purpose_Of_Visit (e.g. "Promise To Pay / Collection + Sale"),
                        # not only a task whose entire purpose is Sale alone.
                        want_pl_only = "sale" in (purpose_by_dc.get(dc_id) or "").lower()
                        stats, scope = _block_then_node("PRIVATE LABEL" if want_pl_only else None)
                        # Peers had real purchase data in this category (stats["avg"] is
                        # the whole-category average regardless of segment, per
                        # _peer_stats' own docstring), but none of it was Private Label --
                        # fall back to the unrestricted (any-segment) result rather than
                        # recommending nothing, per explicit user request ("fall back to
                        # Branded"). Genuinely no candidate_ids/no category purchase at
                        # all is unaffected -- stats stays None/empty either way.
                        if want_pl_only and stats and not stats["top_products"]:
                            stats, scope = _block_then_node(None)
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
                    # Active Sales/ABS Schemes (added 2026-09-12) -- distinct from
                    # entry["club"] just above (see _sql_active_schemes_for_nodes' own
                    # docstring). Node-scoped, not DC-scoped -- every DC in the same Node
                    # shares the same list, deliberately (that's the real scope these
                    # schemes are defined at).
                    entry["active_schemes"] = active_schemes_by_node.get(node_by_dc.get(dc_id), [])
                    # Carried alongside active_schemes (added 2026-09-19, "which scheme
                    # is recomending in which [node] actually he is in") so the DC Card
                    # can show which Node produced this DC's scheme list.
                    entry["node"] = node
                    # YoY PL comparison (confirmed 2026-08-18) -- PL-specific, distinct
                    # from purchase_last_fy/purchase_ytd above (those are overall
                    # purchase, not PL-tagged). ytd_pl itself is already in DailyTaskRow
                    # (YTD_Private_Label); last year's figure and the growth % are new.
                    entry["ytd_pl_last_year"] = ytd_pl_last_year_by_dc.get(dc_id)
                    _, entry["yoy_pl_growth_pct"] = yoy_pl_growth_fn(dc_id)
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
                    # Same Sale-purpose Private Label preference as the block/node tier
                    # above (added 2026-09-20), applied to the geo-radius/nearest-Node
                    # fallback tier too -- a Sale-purpose DC whose own block+node peers
                    # had zero purchase data at all still reaches this tier and should
                    # get the same PL-first treatment, not silently drop back to
                    # unrestricted just because it needed the wider fallback.
                    sale_pl_ids = [d for d in needs_geo_fallback if "sale" in (purpose_by_dc.get(d) or "").lower()]
                    other_ids = [d for d in needs_geo_fallback if d not in sale_pl_ids]
                    if sale_pl_ids:
                        _attach_nearby_product_recommendations(
                            client, dc_master, sale_pl_ids, extra_data_by_dc, plan_date,
                            result_key="recommended_products", segment="PRIVATE LABEL",
                        )
                        # No Private Label product found nearby even at this wider tier
                        # -- fall back to the unrestricted search rather than leaving
                        # these DCs with no recommendation at all.
                        still_empty = [d for d in sale_pl_ids if not extra_data_by_dc.get(d, {}).get("recommended_products")]
                        if still_empty:
                            _attach_nearby_product_recommendations(
                                client, dc_master, still_empty, extra_data_by_dc, plan_date, result_key="recommended_products",
                            )
                    if other_ids:
                        _attach_nearby_product_recommendations(
                            client, dc_master, other_ids, extra_data_by_dc, plan_date, result_key="recommended_products",
                        )
                # Benefit text for whatever ended up recommended (both tiers above),
                # for the AI pitch's per-product pointers -- see _attach_product_descriptions.
                description_failure = _attach_product_descriptions(client, extra_data_by_dc)
                if description_failure:
                    run_exceptions.append({
                        "source": "products_template", "reason_code": "Live_Pull_Failed",
                        "detail": f"Product description lookup failed, pitching without benefit text: {description_failure}",
                    })

                from .pitching import generate_pitches_for_plan_run
                _, pitch_failures = generate_pitches_for_plan_run(plan_run, extra_data_by_dc, task_ids=task_ids)
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
                    _, card_failures = generate_dc_cards_for_plan_run(plan_run, extra_data_by_dc, task_ids=task_ids)
                    run_exceptions.extend({
                        "record_id": f["dc_id"], "source": "DCCardAgent", "reason_code": "DC_Card_Generation_Failed",
                        "detail": f"DC {f['dc_id']}: {f['detail']}",
                    } for f in card_failures)
                except Exception as e:
                    run_exceptions.append({"source": "DCCardAgent", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})
        except Exception as e:
            run_exceptions.append({"source": "PitchingAgent", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})
    return run_exceptions


# The PlanRun a generate_plan_for_scope call has created and not yet finished, so
# _discard_plan_run_on_failure can remove it if the call dies part-way. A ContextVar
# rather than a module global: the dev server runs each request in its own thread and
# threads get their own context, so two concurrent generations never see each other's.
_IN_PROGRESS_PLAN_RUN: ContextVar[Optional[PlanRun]] = ContextVar("_IN_PROGRESS_PLAN_RUN", default=None)


def _discard_plan_run_on_failure(fn: Callable[..., PlanRun]) -> Callable[..., PlanRun]:
    """Replaces the @transaction.atomic that used to wrap generate_plan_for_scope
    (removed 2026-09-16, explicit user request: "fix it properly narrow the lock").

    That one atomic block spanned the ENTIRE generation -- every Redshift pull, the
    scoring, routing (Plan C's LLM calls), pitching (the AI pitch's LLM calls) -- so
    under transaction_mode=IMMEDIATE the SQLite write lock was taken at function entry
    and held for the full run, 2+ minutes under Plan C. Any other writer in that window
    (an SE switching Today -> Tomorrow while today's plan was still generating, a route
    Accept, a password reset, another scope's generation) either waited the whole time
    or died with "database is locked". The lock is now only ever held by the individual
    write statements themselves (milliseconds each; the one multi-row cluster is under
    its own small atomic() below), and every slow external call runs with no lock held.

    What the big transaction also gave us was all-or-nothing persistence: a crash
    part-way left no half-built PlanRun behind. This decorator keeps that guarantee the
    only way that's possible without holding the lock -- as a compensating delete: the
    body publishes its PlanRun into _IN_PROGRESS_PLAN_RUN right after creating it, and
    if the call then raises for any reason, that row is deleted (FK cascade takes the
    tasks, route plans, pitches, cards and exception records with it) before the
    original error propagates. Best-effort by design -- if even the delete fails, the
    original exception still wins, and the orphan is visible (finished_at NULL) rather
    than hidden.

    One consequence worth knowing: a PlanRun is now visible to other readers while it's
    still being built (finished_at NULL, task_count 0) instead of appearing fully-formed
    at commit. planning.routing.resolve_route_plan_run's newest-run fallback filters
    those out; the System Plan Runs list simply shows them as in progress."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> PlanRun:
        token = _IN_PROGRESS_PLAN_RUN.set(None)
        try:
            return fn(*args, **kwargs)
        except BaseException:
            partial = _IN_PROGRESS_PLAN_RUN.get()
            if partial is not None and partial.pk is not None:
                try:
                    with transaction.atomic():
                        PlanRun.objects.filter(pk=partial.pk).delete()
                except Exception:
                    pass  # the original failure below is the one that matters
            raise
        finally:
            _IN_PROGRESS_PLAN_RUN.reset(token)
    return wrapper


def _scope_locked(fn: Callable[..., PlanRun]) -> Callable[..., PlanRun]:
    """Acquires planning.locking.scope_lock(scope_type, scope_value) for the duration of
    one generate_plan_for_scope() call -- added 2026-09-24 after a real incident: a manual
    run_scheduled_tuff invocation and celery_worker's own scheduled copy of the same
    command ran concurrently against the same ScheduledScope rows, with nothing anywhere
    preventing it. Placed as the OUTERMOST decorator (applied first, listed above
    @_discard_plan_run_on_failure below) so a contended lock is detected and raised before
    that decorator's own ContextVar/cleanup setup ever runs -- there is nothing to clean
    up yet at that point regardless, but failing fastest costs nothing.

    scope_type/scope_value are always generate_plan_for_scope's first two positional (or
    keyword) arguments for every real caller in this codebase (confirmed: run_scheduled_
    tuff.py, run_all_states_tuff.py, generate_se_plan.py, activate_tuff.py via
    activate_tuff_scope, and every HTTP scope endpoint in planning/views.py all call it
    this way) -- read positionally-or-by-keyword here rather than requiring a caller
    change.

    Converts locking.LockContendedError into PlanningError so every existing caller's
    already-correct `except PlanningError` handling (run_scheduled_tuff.py, run_all_
    states_tuff.py, planning/views.py) treats "another process is already generating this
    exact scope" as the same kind of expected, routine failure as e.g. the weekly-off-day
    gate just below in this same function -- not an "unexpected crash" that would
    otherwise hit the generic except-Exception branch and page as a crash."""
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> PlanRun:
        scope_type = kwargs.get("scope_type", args[0] if len(args) > 0 else None)
        scope_value = kwargs.get("scope_value", args[1] if len(args) > 1 else None)
        try:
            with scope_lock(scope_type, scope_value):
                return fn(*args, **kwargs)
        except LockContendedError as e:
            raise PlanningError(str(e)) from e
    return wrapper


@_scope_locked
@_discard_plan_run_on_failure
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
    SE in this scope -- see make_routing_plan_asker(). routing_plan_choice ("A", "B", or
    "C" -- C opened up to the HTTP API 2026-09-11, see planning.views.
    _routing_plan_choice_from_get) is an explicit override, same precedence pattern as
    farmer_meeting_confirmed_emails above -- takes priority over routing_plan_asker. When
    neither is supplied (the run_scheduled_tuff/HTTP-API-with-no-param case), defaults to
    "A" -- Plan A stays the safe, unattended default; Plan B/C only ever run when a human
    chose it, explicitly or interactively, never silently.

    enable_rotation: opt-in, Plan B only (Beat_Planning_Routing_Agent_Cluster_Model.xlsx
    Sheet 11 Model B, "Fixed Rotation") -- see planning.routing.generate_route_plans_for_se's
    own docstring. False by default, same never-silent posture as routing_plan_choice."""
    started_at = timezone.now()
    plan_date = plan_date or timezone.now().date().isoformat()
    # Admin Control Panel (added 2026-09-07) -- BusinessConstants() defaults, with any
    # live admin overrides applied on top. See planning.admin_config's own docstring for
    # exactly which fields are overridable and why.
    constants = load_business_constants()

    # Weekly off-day gate (added 2026-09-13, explicit user request -- "no planing
    # creating on sunday and its setting provide in admin panel"). Checked immediately
    # after load_business_constants() (so a live admin override to this field always
    # takes effect) and before any live data pull, DC resolution, or client connection --
    # an off day means there's nothing to plan for anyone in this scope, not a per-SE
    # condition worth spending a live Metabase/Redshift round trip to discover. This is
    # entirely independent of R0.4 Origin_Point resolution below -- missing a punch-in
    # on a normal working day never blocks that day's plan when 30-day history exists
    # (prev_30d_punch_in wins regardless of today_punch_in); this gate only fires for a
    # day the admin has explicitly marked as a network-wide off day.
    if agent.PLAN_GENERATION_WEEKLY_OFF_DAY != "None" and datetime.fromisoformat(plan_date).strftime("%A") == agent.PLAN_GENERATION_WEEKLY_OFF_DAY:
        raise PlanningError(
            f"{plan_date} is {agent.PLAN_GENERATION_WEEKLY_OFF_DAY}, the configured weekly off day "
            f"(Admin Control Panel: Scheduling) -- no plan generated for {scope_type}='{scope_value}'."
        )

    client = agent.get_client()
    # Default family = the admin's "SE view's routing plan" (SE_ROUTING_PLAN, applied by
    # load_business_constants above), not a hardcoded "A" (CHANGED 2026-09-18, explicit
    # user request -- "but in back end we generate": the admin had set C, the SE views
    # asked for C, but every backend generation with no explicit choice -- the 06:15
    # Celery run_scheduled_tuff pass, "generate for all states", a bare activate_tuff --
    # still produced Plan A, so 425 of 447 SEs had no Plan C to read). An explicit
    # routing_plan_choice / interactive answer still wins, so Admin/ZBM/RBM/ABM picking
    # a family on a view is unchanged.
    resolved_routing_plan = (
        routing_plan_choice or (routing_plan_asker() if routing_plan_asker else None) or agent.SE_ROUTING_PLAN or "A"
    )

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
        unresolved_count = len([e for e in se_emails if e not in se_user_ids])
        se_emails = [e for e in se_emails if e in se_user_ids]
        if not se_emails:
            # Every SE dropped out -- in practice the users_user pull itself failed (seen
            # live 2026-09-18: a Redshift DNS failure took West Bengal's whole run down,
            # and without this guard it died much later with an UnboundLocalError on
            # dc_datamart_query_ok, hiding the real cause). Fail here, naming it.
            pull_failure = next((x["detail"] for x in run_exceptions if x.get("source") == "users_user" and x.get("reason_code") == "Live_Pull_Failed"), None)
            client.close()
            raise PlanningError(
                f"None of the {unresolved_count} SE(s) under {scope_type}='{scope_value}' could be resolved to a user_id"
                + (f" -- the users_user live pull failed: {pull_failure}" if pull_failure else "")
                + " -- nothing to plan."
            )

    # Outcome reconciliation for these SEs' past plans, before their new one is built
    # (added 2026-09-16, see planning.reconciliation's docstring for why it lives here
    # and not on a scheduler). Runs first because its result feeds this very plan:
    # DCVisitStreak.consecutive_misses drives the Critical flag below. Idempotent --
    # only still-UNKNOWN past tasks are touched, so the second generation of a day
    # finds nothing to do. Never blocks generation: a failure lands in the
    # Exceptions_Report and the plan proceeds on whatever streak data already exists.
    if client.configured:
        from .reconciliation import reconcile_past_tasks  # local import -- reconciliation imports this module
        try:
            for summary in reconcile_past_tasks(
                client=client, before_date=timezone.now().date().isoformat(),
                se_ids=[str(se_user_ids.get(e, e)) for e in se_emails],
            ):
                for failure in summary["pull_failures"]:
                    run_exceptions.append({
                        "source": "reconcile_outcomes", "reason_code": "Live_Pull_Failed",
                        "detail": f"Reconciling {summary['plan_date']}: {failure}",
                    })
        except Exception as e:
            run_exceptions.append({
                "source": "reconcile_outcomes", "reason_code": "Reconciliation_Failed",
                "detail": f"{type(e).__name__}: {e} -- past outcomes for this scope stay UNKNOWN this run",
            })

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
    fm_urgency_by_se: Dict[str, Dict[str, Any]] = {}
    farmer_meeting_confirmed_by_se: Dict[str, bool] = {}
    dc_datamart_query_ok = False  # read far below regardless of whether the live block runs

    if client.configured and se_emails:
        uids = [se_user_ids[e] for e in se_emails]
        fatigue_start = (datetime.fromisoformat(plan_date) - timedelta(days=constants.contact_fatigue_window_days)).date().isoformat()

        # Contact Attempt mode (added 2026-09-13) -- see CONTACT_ATTEMPT_MODE's own
        # comment in se_daily_plan_agent.py. contact_only/visit_plus_contact both need
        # real call-attempt data, which doesn't exist anywhere in this pipeline yet
        # (confirmed live) -- flagged once per run rather than silently treating a visit
        # as a call, or crashing. visit_only (default) is unaffected -- exactly today's
        # existing behavior.
        count_visits_as_attempts = agent.CONTACT_ATTEMPT_MODE in ("visit_only", "visit_plus_contact")
        if agent.CONTACT_ATTEMPT_MODE in ("contact_only", "visit_plus_contact"):
            run_exceptions.append({
                "source": "contact_attempt_mode", "reason_code": "Contact_Data_Not_Configured",
                "detail": (
                    f"Admin Control Panel Contact attempt mode is '{agent.CONTACT_ATTEMPT_MODE}', which needs real "
                    "call-attempt data -- no call/IVR/telecall table exists anywhere in this pipeline's reachable "
                    "databases (confirmed live). Call attempts contribute 0 this run"
                    + (", falling back to visit-only counting" if count_visits_as_attempts else " -- Contact Fatigue will never trigger (0 attempts for every DC)")
                    + "; never silently substituted."
                ),
            })

        try:
            for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_last_visit(dc_ids, uids, agent.LOOKBACK_DAYS)):
                dc_id, uid, date, status = row["sap_partner_id"], row["se_user_id"], agent.standardize_date(row["plan_execution_date"]), row["task_status"]
                if dc_id not in last_visit_by_dc or date > last_visit_by_dc[dc_id]:
                    last_visit_by_dc[dc_id] = date
                if count_visits_as_attempts and fatigue_start <= date < plan_date:
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
        dc_datamart_query_ok = False
        try:
            outstanding_raw = client.execute_sql(agent.REDSHIFT_DB_ID, _sql_outstanding(dc_ids))
            dc_datamart_query_ok = True
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

        # Source 3k -- DC Composite Health Score, wired 2026-09-06 (business request).
        # A separate, parallel scoring model from BO1-5 -- does NOT feed dc_bo_scores or
        # Section 7's Priority_Score above, only the separate Health-Focus qualification
        # track (see _qualify_health_focus below / generate_se_daily_plan's pool-merge).
        # Eligibility gate first (business-confirmed): active=TRUE AND Days_Since_Last_
        # Sale<=60. dc_financials already implies active=TRUE by construction -- a DC
        # failing dc_datamart's own is_active check never gets a dc_financials entry at
        # all (see normalize_sales_transactions' is_active filter) -- so this loop only
        # needs to additionally check the 60-day recency, using the same Outstanding_
        # Last_Invoice_Date already computed from dc_datamart.last_invoice_date (read as
        # "last sale" -- the doc names no other confirmed source for this field).
        dc_master_by_id = {d["DC_ID"]: d for d in scoped_dcs}
        plan_date_dt = datetime.fromisoformat(plan_date).date()
        active_dc_ids = list(dc_financials.keys())
        health_eligible_dc_ids = [
            dc_id for dc_id, fin in dc_financials.items()
            if fin.get("Outstanding_Last_Invoice_Date")
            and (plan_date_dt - datetime.fromisoformat(fin["Outstanding_Last_Invoice_Date"]).date()).days <= constants.health_focus_days_since_last_sale_max
        ]
        dc_health_scores: Dict[str, Dict[str, Any]] = {}
        nrv_by_dc: Dict[str, float] = {}
        pl_contribution_by_dc: Dict[str, float] = {}
        return_score_by_dc: Dict[str, float] = {}
        pathik_overdue_by_dc: Dict[str, float] = {}
        nrv_raw_by_dc: Dict[str, float] = {}

        # Peer benchmarking for NRV/GM/GM% (2026-09-07, explicit user request) -- block,
        # then node fallback (_peer_group_indices/_peer_group_for/_peer_relative_score
        # above). geo_mapping is the same full, unscoped Source 1c table the Pitching
        # Agent's own S1 peer logic uses further down in this function -- routed through
        # geo_mapping_cache so this is the SAME single live pull, not a duplicate.
        block_by_dc: Dict[str, str] = {}
        node_by_dc: Dict[str, str] = {}
        dc_ids_by_block: Dict[str, List[str]] = {}
        dc_ids_by_node: Dict[str, List[str]] = {}
        gm_score_by_dc: Dict[str, float] = {}
        gm_pct_score_by_dc: Dict[str, float] = {}
        if health_eligible_dc_ids:
            try:
                geo_mapping = _resolve_geo_mapping(client, geo_mapping_cache)
                block_by_dc, node_by_dc, dc_ids_by_block, dc_ids_by_node = _peer_group_indices(geo_mapping)
                # GM/GM% peer-relative scores computed entirely from DC_RAnk.csv's own
                # raw GM_FY2526/GM% columns (explicit user request, "use the rank
                # working sheet only for this") -- no live query, dc_master is already
                # the full, network-wide load (not scope-filtered like scoped_dcs), so
                # a DC's peers outside today's scope are still available for the max.
                #
                # GAP FIXED 2026-09-07 (caught in a self-audit): 6,537 of 19,317 DCs
                # (34%) have NO block AND no node in geo_mapping at all -- confirmed
                # live. Without a fallback, every one of those DCs' GM/GM% would
                # silently read as missing (0%, Worst bucket) purely for lacking a geo
                # mapping, not because of real margin performance -- the same
                # network-wide-max fallback NRV already gets below, so the peer-
                # relative family stays consistent (block -> node -> network), never
                # reverting to the old file-based score this change was asked to
                # replace.
                gm_raw_by_dc = {d["DC_ID"]: d.get("GM_FY2526") for d in dc_master}
                gm_pct_raw_by_dc = {d["DC_ID"]: d.get("GM_Percent") for d in dc_master}
                gm_network_max = max((v for v in gm_raw_by_dc.values() if v is not None), default=None)
                gm_pct_network_max = max((v for v in gm_pct_raw_by_dc.values() if v is not None), default=None)
                for dc_id in health_eligible_dc_ids:
                    gm_v = _peer_relative_score(
                        dc_id, gm_raw_by_dc, block_by_dc, node_by_dc, dc_ids_by_block, dc_ids_by_node,
                        fallback_max=gm_network_max,
                    )
                    if gm_v is not None:
                        gm_score_by_dc[dc_id] = gm_v
                    gm_pct_v = _peer_relative_score(
                        dc_id, gm_pct_raw_by_dc, block_by_dc, node_by_dc, dc_ids_by_block, dc_ids_by_node,
                        fallback_max=gm_pct_network_max,
                    )
                    if gm_pct_v is not None:
                        gm_pct_score_by_dc[dc_id] = gm_pct_v
            except Exception as e:
                run_exceptions.append({"source": "input_partner_details", "reason_code": "Live_Pull_Failed", "detail": f"Health Score GM/GM% peer benchmarking: {type(e).__name__}: {e}"})

        # GR-28 (DECOUPLED 2026-09-06, explicit user request): the Guardrails sheet's own
        # text is an unqualified "ANY DC with overdue > 0" -- no 60-day-recent-sale
        # precondition. Scoped to active_dc_ids (still requires active=TRUE, the one
        # eligibility condition that genuinely applies network-wide per the sheet's own
        # Row 1), NOT health_eligible_dc_ids (which additionally requires a sale within
        # 60 days -- only relevant to the FULL 7-component Health Score composite, not
        # this specific override). Queried once here, before the composite-scoring block
        # below, so a DC that fails the 60-day gate can still be found flagged further
        # down even though it never gets a full composite computed.
        if active_dc_ids:
            try:
                for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_pathik_overdue(active_dc_ids)):
                    dc_id = agent.normalize_id(row.get("dc_id"))
                    v = agent.parse_number(row.get("overdue"))
                    if dc_id and v is not None:
                        pathik_overdue_by_dc[dc_id] = v
            except Exception as e:
                run_exceptions.append({"source": "pathik_report", "reason_code": "Live_Pull_Failed", "detail": f"GR-28 interim OD signal: {type(e).__name__}: {e}"})

        if health_eligible_dc_ids:
            # NRV and PL_Contribution both read sale_orderrequest(line) -- the
            # input-backend Postgres DB (31), NOT Redshift. b2b_sales_return and
            # pathik_report ARE Redshift (41) -- two separate database servers, hence
            # the split client.execute_sql database_id below (a real bug caught live
            # this round: b2b_sales_return can't be joined to sale_orderrequest in one
            # query the way _sql_return_score originally assumed).
            try:
                # NRV_Score, CHANGED 2026-09-07 (explicit user request, "replace with
                # peer-relative") -- was this DC's nrv_12m / the single network-wide
                # MAX; now this DC's nrv_12m / MAX(nrv_12m among its own block-then-node
                # peer group, see _peer_group_indices above). network_max_nrv is kept
                # only as the final fallback for a DC with no peer group at all (never
                # silently dropped, just no longer the primary denominator for anyone
                # who has real peers). Needs nrv_12m for the PEERS too, not just
                # health_eligible_dc_ids, so the pull is widened before querying.
                peer_dc_ids: set = set()
                for dc_id in health_eligible_dc_ids:
                    peer_dc_ids.update(_peer_group_for(dc_id, block_by_dc, node_by_dc, dc_ids_by_block, dc_ids_by_node))
                nrv_pull_dc_ids = sorted(set(health_eligible_dc_ids) | peer_dc_ids)
                max_row = next(iter(client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_nrv_network_max(plan_date))), None)
                network_max_nrv = agent.parse_number(max_row.get("network_max_nrv")) if max_row else None
                for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_nrv_score(nrv_pull_dc_ids, plan_date)):
                    dc_id = agent.normalize_id(row.get("dc_id"))
                    v = agent.parse_number(row.get("nrv_12m"))
                    if dc_id and v is not None:
                        nrv_raw_by_dc[dc_id] = v
                for dc_id in health_eligible_dc_ids:
                    v = _peer_relative_score(
                        dc_id, nrv_raw_by_dc, block_by_dc, node_by_dc, dc_ids_by_block, dc_ids_by_node,
                        fallback_max=network_max_nrv,
                    )
                    if v is not None:
                        nrv_by_dc[dc_id] = v
            except Exception as e:
                run_exceptions.append({"source": "sale_orderrequest", "reason_code": "Live_Pull_Failed", "detail": f"Health Score NRV: {type(e).__name__}: {e}"})

            try:
                for row in client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_pl_contribution(health_eligible_dc_ids, plan_date)):
                    dc_id = agent.normalize_id(row.get("dc_id"))
                    if not dc_id:
                        continue
                    pl_value = agent.parse_number(row.get("pl_value_365d")) or 0.0
                    total_value = agent.parse_number(row.get("total_value_365d"))
                    # Zero-denominator rule (business-confirmed): 0, not undefined.
                    # PL_Contribution_Score = MIN(PL% x 2, 100%), PL% = pl_value/total_value.
                    pl_contribution_by_dc[dc_id] = min((pl_value / total_value) * 2, 1.0) if total_value else 0.0
            except Exception as e:
                run_exceptions.append({"source": "sale_orderrequestline", "reason_code": "Live_Pull_Failed", "detail": f"Health Score PL_Contribution: {type(e).__name__}: {e}"})

            try:
                # Return_Rate's denominator (SUM(sale_orderrequest.amount_total)) is the
                # exact same quantity as nrv_raw_by_dc -- reused rather than re-queried.
                for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_return_score(health_eligible_dc_ids, plan_date)):
                    dc_id = agent.normalize_id(row.get("dc_id"))
                    if not dc_id:
                        continue
                    returns = agent.parse_number(row.get("returns_365d")) or 0.0
                    sales = nrv_raw_by_dc.get(dc_id)
                    # Zero-denominator rule (business-confirmed): 0, not undefined.
                    return_score_by_dc[dc_id] = max(0.0, 1.0 - (returns / sales)) if sales else 0.0
                # A DC with ZERO returns never appears in b2b_sales_return at all (the
                # GROUP BY only returns rows that exist) -- business-confirmed this is a
                # perfect score (Return_Rate=0 -> Score=1), NOT a missing/undefined
                # component, so it's backfilled explicitly rather than left absent.
                for dc_id in nrv_raw_by_dc:
                    return_score_by_dc.setdefault(dc_id, 1.0)
            except Exception as e:
                run_exceptions.append({"source": "b2b_sales_return", "reason_code": "Live_Pull_Failed", "detail": f"Health Score Return: {type(e).__name__}: {e}"})

            # OD_Score, UNBLOCKED 2026-09-07 (explicit user request) -- two-step
            # cross-database process, see _sql_od_bridge/_sql_od_aging docstrings.
            # Step 1: bridge sap_partner_id -> ledger_partner_id (Redshift-41). Step 2:
            # aging calculation for those ledger_partner_ids (LOCUS_DB_ID, "locus" db).
            # Combined in Python since the two queries hit genuinely different physical
            # database connections -- no single SQL statement can join across them.
            od_score_by_dc: Dict[str, float] = {}
            try:
                ledger_partner_id_by_dc: Dict[str, str] = {}
                for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_od_bridge(health_eligible_dc_ids)):
                    dc_id = agent.normalize_id(row.get("sap_partner_id"))
                    ledger_pid = row.get("ledger_partner_id")
                    if dc_id and ledger_pid:
                        ledger_partner_id_by_dc[dc_id] = ledger_pid
                if ledger_partner_id_by_dc:
                    aging_by_ledger_pid: Dict[str, Dict[str, float]] = {}
                    for row in client.execute_sql(agent.LOCUS_DB_ID, _sql_od_aging(list(ledger_partner_id_by_dc.values()))):
                        ledger_pid = row.get("partner_id")
                        if ledger_pid:
                            aging_by_ledger_pid[ledger_pid] = {
                                "od_90plus": agent.parse_number(row.get("aged_90_plus")) or 0.0,
                                "overall_outstanding": agent.parse_number(row.get("overall_outstanding")) or 0.0,
                            }
                    for dc_id, ledger_pid in ledger_partner_id_by_dc.items():
                        aging = aging_by_ledger_pid.get(ledger_pid)
                        if not aging:
                            continue
                        od_90plus = aging["od_90plus"]
                        overall_outstanding = aging["overall_outstanding"]
                        # od_90plus<=0 (no unpaid invoice aged 90+) is a perfect 1
                        # regardless of total unpaid size. The elif below is now
                        # effectively unreachable given _sql_od_aging's own formula
                        # (overall_outstanding = od_90plus + aged_0_90, both non-negative
                        # sums, so overall_outstanding can never be < a positive
                        # od_90plus) -- kept as a defensive guard against a genuinely
                        # negative amount slipping through, not a real business case
                        # anymore (it WAS reachable under the old FIFO-netted formula,
                        # replaced 2026-09-07 -- see _sql_od_aging's docstring).
                        if od_90plus <= 0:
                            od_score_by_dc[dc_id] = 1.0
                        elif not overall_outstanding or overall_outstanding <= 0:
                            od_score_by_dc[dc_id] = 0.0
                        else:
                            od_score_by_dc[dc_id] = max(0.0, min(1.0, 1.0 - (od_90plus / overall_outstanding)))
            except Exception as e:
                run_exceptions.append({"source": "ledger_ledgerentry", "reason_code": "Live_Pull_Failed", "detail": f"Health Score OD: {type(e).__name__}: {e}"})

            # Credit_Score, UNBLOCKED 2026-09-07 (explicit user request, business-
            # confirmed formula + worked example) -- reuses the same sap_partner_id ->
            # ledger_partner_id bridge as OD (_sql_od_bridge, same GR-32 resolution) since
            # credit_line_customer.source_identifier_id turns out to be that same
            # ledger_partner_id value (confirmed live). Re-resolved independently here
            # (rather than sharing OD's own ledger_partner_id_by_dc) so a failure in
            # either component's own query can't take the other down with it -- same
            # per-source failure-isolation convention as every other Health Score
            # component in this function.
            credit_score_by_dc: Dict[str, float] = {}
            try:
                credit_ledger_partner_id_by_dc: Dict[str, str] = {}
                for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_od_bridge(health_eligible_dc_ids)):
                    dc_id = agent.normalize_id(row.get("sap_partner_id"))
                    ledger_pid = row.get("ledger_partner_id")
                    if dc_id and ledger_pid:
                        credit_ledger_partner_id_by_dc[dc_id] = ledger_pid
                if credit_ledger_partner_id_by_dc:
                    payments_by_ledger_pid: Dict[str, Dict[str, float]] = {}
                    for row in client.execute_sql(agent.LOCUS_DB_ID, _sql_credit_payments(list(credit_ledger_partner_id_by_dc.values()))):
                        ledger_pid = row.get("ledger_partner_id")
                        if ledger_pid:
                            payments_by_ledger_pid[ledger_pid] = {
                                "ard": agent.parse_number(row.get("ard")) or 0.0,
                                "pct_paid_in_due": agent.parse_number(row.get("pct_paid_in_due")) or 0.0,
                            }
                    for dc_id, ledger_pid in credit_ledger_partner_id_by_dc.items():
                        payments = payments_by_ledger_pid.get(ledger_pid)
                        if not payments:
                            continue  # <5 qualifying payments (or none) -- None, not a guessed fallback
                        credit_score_by_dc[dc_id] = _credit_score_from_payments(payments["ard"], payments["pct_paid_in_due"])
            except Exception as e:
                run_exceptions.append({"source": "payment_payment", "reason_code": "Live_Pull_Failed", "detail": f"Health Score Credit: {type(e).__name__}: {e}"})

            # Credit line detail (2026-09-07, explicit user request) -- credit_limit
            # (total sanctioned), available_credit_limit (remaining/utilizable), and
            # credit_active (status='ACTIVE'), raw from credit_line_customercreditline.
            # Reuses credit_ledger_partner_id_by_dc from the block above (same bridge,
            # already resolved) -- own try/except so a failure here can't take down
            # Credit_Score itself, same isolation convention as every other component.
            credit_details_by_dc: Dict[str, Dict[str, Any]] = {}
            try:
                if credit_ledger_partner_id_by_dc:
                    details_by_ledger_pid: Dict[str, Dict[str, Any]] = {}
                    for row in client.execute_sql(agent.LOCUS_DB_ID, _sql_credit_line_details(list(credit_ledger_partner_id_by_dc.values()))):
                        ledger_pid = row.get("ledger_partner_id")
                        if ledger_pid:
                            details_by_ledger_pid[ledger_pid] = {
                                "Credit_Limit": agent.parse_number(row.get("credit_limit")),
                                "Available_Credit_Limit": agent.parse_number(row.get("available_credit_limit")),
                                "Credit_Active": row.get("status") == "ACTIVE",
                            }
                    for dc_id, ledger_pid in credit_ledger_partner_id_by_dc.items():
                        details = details_by_ledger_pid.get(ledger_pid)
                        if details:
                            credit_details_by_dc[dc_id] = details
            except Exception as e:
                run_exceptions.append({"source": "credit_line_customercreditline", "reason_code": "Live_Pull_Failed", "detail": f"Credit line detail: {type(e).__name__}: {e}"})

            for dc_id in health_eligible_dc_ids:
                dc = dc_master_by_id.get(dc_id, {})
                gm_fy_value = dc.get("GM_FY2526")
                gm_pct = dc.get("GM_Percent")
                # Negative-GM rule (business-confirmed): a loss-making DC's GM/GM% scores
                # are NOT run through the normal formula -- flagged for manual review
                # instead, rather than producing a score that breaks the 0-1 convention.
                negative_gm_flag = (gm_fy_value is not None and gm_fy_value < 0) or (gm_pct is not None and gm_pct < 0)
                # GM/GM% peer-relative scores, CHANGED 2026-09-07 (explicit user
                # request, "replace with peer-relative", "use the rank working sheet
                # only for this") -- was DC_RAnk.csv's own precomputed GM Score/GM%
                # Score columns (an external, business-computed methodology this
                # codebase had no visibility into); now this DC's own GM_FY2526/GM%
                # (still from that same Rank Working sheet, no new query) divided by
                # its block-then-node peer group's max, see gm_score_by_dc/
                # gm_pct_score_by_dc above.
                gm_score = None if negative_gm_flag else gm_score_by_dc.get(dc_id)
                gm_pct_score = None if negative_gm_flag else gm_pct_score_by_dc.get(dc_id)
                result = agent.compute_dc_health_score(
                    nrv_by_dc.get(dc_id), gm_score, gm_pct_score,
                    pl_contribution_by_dc.get(dc_id), return_score_by_dc.get(dc_id),
                    negative_gm_flag, constants, od_score=od_score_by_dc.get(dc_id),
                    credit_score=credit_score_by_dc.get(dc_id),
                )
                # GR-28 (business-confirmed): current_od>0 always force-includes the DC
                # in the candidate pool regardless of composite score, bypassing the
                # Health-Focus bucket logic entirely -- tracked here, applied at pool-
                # merge time in generate_se_daily_plan. Threshold made Admin Control
                # Panel-overridable 2026-09-07 (explicit user request, "dc selection...
                # based on rank and condition like overdue") -- was a bare "> 0" literal;
                # constants.gr28_overdue_min_threshold defaults to 0.0 (identical
                # behavior to before) but can be raised to require a real minimum
                # overdue balance before this force-include fires.
                result["GR28_Force_Include"] = (pathik_overdue_by_dc.get(dc_id) or 0.0) > constants.gr28_overdue_min_threshold
                if result["GR28_Force_Include"]:
                    result["Health_Focus_Purposes"] = agent.health_focus_purposes({
                        **result["Sub_Scores"],
                        "OD": {"score_pct": 0.0, "bucket": "Worst", "urgency": 1.0},
                    })
                elif result["Qualify_HealthFocus"]:
                    result["Health_Focus_Purposes"] = agent.health_focus_purposes(result["Sub_Scores"])
                else:
                    result["Health_Focus_Purposes"] = []
                credit_details = credit_details_by_dc.get(dc_id, {})
                result["Credit_Limit"] = credit_details.get("Credit_Limit")
                result["Available_Credit_Limit"] = credit_details.get("Available_Credit_Limit")
                result["Credit_Active"] = credit_details.get("Credit_Active")
                dc_health_scores[dc_id] = result

        # GR-28 for DCs that are active but failed the Health Score's own 60-day-
        # recent-sale eligibility gate -- decoupled 2026-09-06, explicit user request
        # ("flag that 60 day eligibility condition"). These DCs never get a full
        # 7-component composite (NRV/PL_Contribution/Return were never queried for
        # them, scoped to health_eligible_dc_ids above) -- only the bare GR-28 override,
        # explicitly flagged via an exception record so this bypass is visible, not
        # silently folded in as if it were an ordinary Health-Focus qualification.
        for dc_id in active_dc_ids:
            # Same gr28_overdue_min_threshold as the main GR28_Force_Include check above
            # -- one shared threshold for both GR-28 code paths, not a second knob that
            # could silently drift out of sync with it.
            if dc_id in dc_health_scores or (pathik_overdue_by_dc.get(dc_id) or 0.0) <= constants.gr28_overdue_min_threshold:
                continue
            run_exceptions.append({
                "dc_id": dc_id,
                "source": "GR-28", "reason_code": "GR28_Bypassed_60Day_Eligibility",
                "detail": (
                    f"DC {dc_id}: force-included for Outstanding via GR-28 (real overdue balance, "
                    "pathik_report.overdue) despite failing the Health Score's own Days_Since_Last_"
                    "Sale<=60 eligibility gate -- no full Health Score composite computed for this "
                    "DC, only the bare GR-28 override."
                ),
            })
            dc_health_scores[dc_id] = {
                "DC_Health_Score": None, "Health_Gap": None, "Sub_Scores": {},
                "Negative_GM_Flag": False, "Qualify_HealthFocus": False,
                "Health_Focus_Urgency": None, "GR28_Force_Include": True,
                "GR28_Bypassed_60Day_Gate": True,
                "Health_Focus_Purposes": agent.health_focus_purposes(
                    {"OD": {"score_pct": 0.0, "bucket": "Worst", "urgency": 1.0}}
                ),
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
                leg_trailing = (pl_sum_90d / 3.0 * constants.pl_trailing_leg_growth_multiplier) if pl_sum_90d else None

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
                    result["reason"] += f"; trailing-90d leg carries a {constants.pl_trailing_leg_growth_multiplier:.1f}x growth expectation (provisional, see BusinessConstants.pl_trailing_leg_growth_multiplier)"
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

        # BO4 (Sales Momentum) and BO5 (Long-Term) scoring REMOVED 2026-09-07, explicit
        # user request ("Stop computing Sales & Long-Term entirely"). FM_Urgency below
        # (Farmer Meeting scheduling pacing) is a SEPARATE mechanism from BO5's own
        # grade and is unaffected -- it still governs the DC-Visit/Farmer-Meeting
        # day-type choice, just no longer via a BO5 "score".

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
            promise_raw = client.execute_sql(agent.INPUT_BACKEND_DB_ID, _sql_promise_to_pay(dc_ids, plan_date))
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

        # Active Sales/ABS Schemes (added 2026-09-12) -- see _sql_active_schemes_for_nodes'
        # own docstring for why this is a genuinely separate system from the DC Club
        # pull just above, confirmed live before building. Batched by distinct Node
        # (abs_scheme/scheme_details are Node-scoped, not DC-scoped) rather than per-DC,
        # same one-query-for-the-whole-run economy as every other Source pull here.
        active_schemes_by_node: Dict[str, List[Dict[str, Any]]] = {}
        node_by_dc: Dict[str, Optional[str]] = {d["DC_ID"]: d.get("Node") for d in scoped_dcs}
        try:
            dc_nodes = sorted({d["Node"] for d in scoped_dcs if d.get("Node")})
            if dc_nodes:
                for row in client.execute_sql(agent.REDSHIFT_DB_ID, _sql_active_schemes_for_nodes(dc_nodes, plan_date)):
                    active_schemes_by_node.setdefault(row["node"], []).append({
                        "name": row.get("scheme_name"), "description": row.get("description"),
                        "category": row.get("business_category"), "sub_category": row.get("product_sub_category"),
                        "brand": row.get("brand_name"), "material_name": row.get("material_name"),
                        "valid_until": str(row["scheme_end_date"]) if row.get("scheme_end_date") else None,
                    })
                # Live_Pull_Silently_Incomplete (added 2026-09-22, found live -- a
                # Maharashtra run gave 0/6 Pune-node DCs any scheme_context while every
                # other node in the SAME run correctly got its schemes, even though
                # Pune genuinely has one live since 2026-07-25 the same query returns
                # correctly moments later): a multi-node join can apparently come back
                # missing an entire node's rows without Redshift raising anything --
                # no exception here at all, so this gap was invisible until traced
                # DC-by-DC by hand. This can't tell a real "no active scheme anywhere"
                # empty from that failure mode, so it only flags a node that got NOTHING
                # while at least one sibling node in the SAME query DID get real rows --
                # never on a run where every node is legitimately empty (e.g. a state
                # with genuinely no live schemes right now).
                empty_nodes = [n for n in dc_nodes if not active_schemes_by_node.get(n)]
                if empty_nodes and any(active_schemes_by_node.get(n) for n in dc_nodes):
                    run_exceptions.append({
                        "source": "abs_scheme", "reason_code": "Live_Pull_Silently_Incomplete",
                        "detail": (
                            f"{len(empty_nodes)} of {len(dc_nodes)} node(s) got zero active schemes while "
                            f"sibling nodes in the same query got real ones -- possibly a genuine "
                            f"zero-schemes node, possibly the same node dropped from an incomplete "
                            f"multi-node join; re-check before trusting a DC in {', '.join(empty_nodes[:10])} "
                            f"has no scheme to offer: {empty_nodes}"
                        ),
                    })
        except Exception as e:
            run_exceptions.append({"source": "abs_scheme", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        # Scheme Description Cards (added 2026-09-16, explicit user request) -- enriches
        # the node-scoped abs_scheme/scheme_details pull just above with a genuinely
        # richer source, coupon_service.public.scheme + discounting_scheme_slab/
        # scheme_rules/scheme_translations, see _sql_scheme_description_cards' own
        # docstring. Fetched once for the whole run (not per-node -- it's already a
        # full live-scheme scan), then attached to every node this scheme's own rules
        # actually cover: scheme_rules stores node/state as ids, not names, so
        # node_names_raw/state_names_raw (also not in the user's original query -- see
        # that function's own docstring for why they were added) are what make this
        # checkable here, rather than trusting a name-string match alone regardless of
        # location.
        #
        # CHANGED 2026-09-21 (confirmed live -- "in rajasthan and uttar pradesh" the
        # scheme recommendation was completely missing): this used to only ENRICH an
        # entry abs_scheme had already created (`for node, schemes in active_schemes_
        # by_node.items()`), silently doing nothing for a state abs_scheme/scheme_
        # details has zero rows for at all. Confirmed live: Rajasthan and Uttar Pradesh
        # both have 0 abs_scheme rows for every one of their nodes, yet 18 real,
        # active_rules=1 Cash Discount Schemes on the coupon_service feed genuinely
        # cover them (Cash Discount Scheme Insecticide RJ NF H1 2026, etc.) -- abs_
        # scheme and coupon_service turn out to be two independently-populated scheme
        # catalogs, covering different states, not one superset of the other; nothing
        # here previously created a fresh entry for a node abs_scheme never mentioned,
        # so every one of those 18 schemes was silently invisible to every SE in both
        # states. Now creates one when no abs_scheme-sourced entry exists to enrich --
        # coupon_service becomes this node's ONLY source for that scheme, not merely a
        # supplement, exactly when abs_scheme has nothing to supplement.
        try:
            state_by_node: Dict[str, Optional[str]] = {}
            for d in scoped_dcs:
                if d.get("Node") and d["Node"] not in state_by_node:
                    state_by_node[d["Node"]] = d.get("State")
            for row in _fetch_scheme_description_cards(client, plan_date):
                scheme_name = row.get("scheme_name")
                if not scheme_name or not row.get("active_rules"):
                    continue  # no active eligibility rule set up -- don't attribute this description anywhere
                node_names = {n.strip() for n in (row.get("node_names_raw") or "").split(",") if n.strip()}
                state_names = {n.strip() for n in (row.get("state_names_raw") or "").split(",") if n.strip()}
                no_location_limit = not node_names and not state_names
                for node in state_by_node:  # every node actually in scope for this run
                    state = state_by_node.get(node)
                    if not (no_location_limit or node in node_names or (state and state in state_names)):
                        continue
                    schemes = active_schemes_by_node.setdefault(node, [])
                    entry = next((e for e in schemes if e.get("name") == scheme_name), None)
                    if entry is None:
                        # abs_scheme never had this scheme for this node -- coupon_
                        # service is this node's only source for it. booking_end is a
                        # different concept from abs_scheme's own scheme_end_date
                        # (booking window vs overall scheme validity), but it's the one
                        # date this source has -- an honest best-available stand-in,
                        # not a fabricated figure.
                        entry = {
                            "name": scheme_name, "description": row.get("generated_description"),
                            "category": None, "sub_category": None, "brand": None, "material_name": None,
                            "valid_until": row.get("booking_end"),
                        }
                        schemes.append(entry)
                    entry["generated_description"] = row.get("generated_description")
                    entry["benefit"] = {
                        "advance_per_unit": row.get("advance_per_unit"), "slabs": row.get("slabs_short"),
                        "slab_basis": row.get("slab_basis"), "benefit_channel": row.get("benefit_channel"),
                        "booking_end": row.get("booking_end"), "max_discount_per_dc": row.get("max_discount_per_dc"),
                    }
        except Exception as e:
            run_exceptions.append({"source": "coupon_service.scheme", "reason_code": "Live_Pull_Failed", "detail": f"{type(e).__name__}: {e}"})

        # Scheme recommendation (rank by value) + live-status cross-check -- added
        # 2026-09-19, on top of the Discount Service REST API client (a "supplement,
        # don't replace" building block -- see project_discount_service_supplement_
        # 20260918 memory). Best-effort: a live-API outage/misconfiguration must never
        # block plan generation, so this is wrapped the same way every optional live
        # source in this pipeline is -- an exception here becomes a logged entry, never
        # a raise. Ranking always runs (pure, local, no I/O); the live cross-check is
        # additionally gated on discount_service.agent.DiscountServiceClient().configured
        # so an operator who never set DISCOUNT_SERVICE_CLIENT_ID/SECRET doesn't pay for
        # a doomed network call (or see a spurious exception) on every single run.
        try:
            for node, schemes in active_schemes_by_node.items():
                active_schemes_by_node[node] = discount_service.rank_schemes_by_value(schemes)
            if discount_service.agent.DiscountServiceClient().configured:
                live_schemes = _fetch_live_discount_schemes(plan_date)
                for node, schemes in active_schemes_by_node.items():
                    active_schemes_by_node[node] = discount_service.cross_check_active_status(schemes, live_schemes)
        except Exception as e:
            run_exceptions.append({"source": "discount_service_live", "reason_code": "Discount_Service_Cross_Check_Failed", "detail": f"{type(e).__name__}: {e}"})

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

    # dc_active_by_id/dc_overdue_by_id -- built from outstanding_raw (already fetched
    # above, dc_datamart's own is_active/total_overdue columns) rather than a fresh
    # query -- same raw rows normalize_sales_transactions() already consumed, just kept
    # here before that function drops them once it's done filtering.
    #
    # BUG FIXED 2026-09-07 (caught in a self-audit): None (not {}) when the dc_datamart
    # QUERY ITSELF failed above -- apply_dc_exclusion_rules()/evaluate_dc_selection_rule
    # treat None as "skip the Active/overdue check entirely" vs. a real (possibly empty)
    # dict as "enforce it strictly". Collapsing a query failure into {} would fail-closed
    # EVERY DC network-wide on a single transient dc_datamart hiccup, not just withhold
    # Outstanding-financial data the way every other consumer of this same query already
    # degrades.
    dc_active_by_id: Optional[Dict[str, bool]] = {} if dc_datamart_query_ok else None
    dc_overdue_by_id: Optional[Dict[str, float]] = {} if dc_datamart_query_ok else None
    if dc_active_by_id is not None:
        for row in outstanding_raw:
            row_dc_id = agent.normalize_id(row.get("dc_id"))
            if row_dc_id:
                dc_active_by_id[row_dc_id] = str(row.get("is_active")).lower() == "true"
                overdue = agent.parse_number(row.get("total_overdue"))
                if overdue is not None:
                    dc_overdue_by_id[row_dc_id] = overdue

    # Program DC Selection (2026-09-08, explicit user request -- "in admin control panel
    # we have select the dcs for this whole program") is the ONE DC-selection allowlist
    # mechanism now (the Excel-based Top DC list fallback was removed 2026-09-18 -- see
    # se_daily_plan_agent.apply_dc_exclusion_rules' own docstring for why). No rule
    # configured means no restriction applies here at all -- every DC passes this gate,
    # not a fallback to a different mechanism. get_selection_config() is a plain DB
    # read (no live query); evaluate_dc_selection_rule reuses the scope-filtered
    # dc_active_by_id/dc_overdue_by_id above rather than a second, unscoped
    # dc_datamart pull.
    selection_config = dc_selection.get_selection_config()
    top_dc_allowlist = agent.evaluate_dc_selection_rule(
        selection_config["rules"], scoped_dcs, dc_active_by_id, dc_overdue_by_id,
        selection_config["manual_includes"], selection_config["manual_excludes"],
    )
    program_dc_gate_active = top_dc_allowlist is not None

    excl_exc = agent.Exceptions(agent.utc_now_iso())
    agent.apply_dc_exclusion_rules(
        scoped_dcs, excl_exc, constants, last_visit_by_dc, plan_date,
        top_dc_allowlist=top_dc_allowlist, dc_active_by_id=dc_active_by_id,
        program_dc_gate_active=program_dc_gate_active,
    )
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
    # From here on a crash must take this row with it -- see _discard_plan_run_on_failure.
    _IN_PROGRESS_PLAN_RUN.set(plan_run)

    # Confirmed 2026-08-18 -- DCVisitStreak.consecutive_misses (only ever written by
    # `manage.py reconcile_outcomes`, on PAST plan_dates) drives generate_se_daily_plan's
    # Critical flag for chronic non-execution. One query for every (SE, DC) pair in this
    # scope, not one per SE -- same batching reasoning as everywhere else in this file.
    all_se_uids = sorted({str(se_user_ids.get(e, e)) for e in se_emails})
    consecutive_misses_by_se_dc: Dict[Tuple[str, str], int] = {
        (s.se_id, s.dc_id): s.consecutive_misses
        for s in DCVisitStreak.objects.filter(se_id__in=all_se_uids, dc_id__in=dc_ids)
    }

    # Per-scope Routing ceiling overrides (added 2026-09-11, explicit user request --
    # "in routing parameter rule may be different for node, district, state or
    # overall") -- fetched ONCE for the whole scope, not per SE, then resolved per-SE
    # inside routing.generate_route_plans_for_se (each SE's own Node/State already
    # known there for free). {} for any level just means "no override configured
    # at that level" -- resolve_routing_ceilings falls through to the next-less-specific
    # level then the global default in that case, same as if this dict were never
    # built at all.
    def _override_rows(scope_type: str) -> Dict[str, Dict[str, Optional[float]]]:
        return {
            o.scope_value: {
                "R1_2_MAX_TRAVEL_MINUTES": o.r1_2_max_travel_minutes,
                "PLAN_A_MAX_ROUND_TRIP_DISTANCE_KM": o.plan_a_max_round_trip_distance_km,
                "PLAN_B_MAX_DAILY_DISTANCE_KM": o.plan_b_max_daily_distance_km,
                "PLAN_B_MAX_DAILY_TRAVEL_MINUTES": o.plan_b_max_daily_travel_minutes,
            }
            for o in RoutingScopeOverride.objects.filter(scope_type=scope_type)
        }

    routing_overrides = {
        "node": _override_rows("NODE"),
        "district": _override_rows("DISTRICT"),
        "state": _override_rows("STATE"),
    }

    # District isn't itself a DC_Master field the way Node/State are, so a given SE's
    # District has to be joined in via dc_id -> district off Geo_Mapping_Normalized.json
    # (added 2026-09-11 alongside DISTRICT-level overrides above -- see
    # planning.routing.generate_route_plans_for_se, which looks a DC candidate's District
    # up in this dict before calling agent.resolve_routing_ceilings). Only worth building
    # when at least one DISTRICT override actually exists -- otherwise it would never be
    # consulted (resolve_routing_ceilings only reads district_overrides.get(district),
    # which is {} either way).
    dc_district_lookup: Dict[str, str] = {}
    if routing_overrides["district"]:
        try:
            geo_mapping = data_cache.load_output_json(_output_dir(), "Geo_Mapping_Normalized.json")
        except FileNotFoundError:
            geo_mapping = []
        for row in geo_mapping:
            dc_id, district = row.get("dc_id"), row.get("district")
            if dc_id and district:
                dc_district_lookup[str(dc_id)] = district

    total_tasks = 0
    skipped_ses: List[Dict[str, Any]] = []
    # CHANGED 2026-09-24 (real-time per-SE frontend visibility, explicit user request):
    # each SE's own DailyTask rows are now bulk_create()'d and pitched/carded right after
    # that SE's own generate_se_daily_plan() call, instead of every SE's tasks being
    # accumulated into one list and bulk_create()'d once after the whole loop finishes.
    # Confirmed via a full read of this loop before making this change: no iteration
    # reads another iteration's pending_tasks/skipped_ses -- every input (constants,
    # dc_financials, top_dc_allowlist, etc.) is computed once, above this loop, from
    # scope-wide (not cross-SE) data, so this is safe, not just possible. A reader
    # polling this PlanRun now sees an already-processed SE's real tasks + pitches + DC
    # Cards while later SEs in the same scope are still being computed, instead of
    # waiting for the whole scope (previously: one bulk_create for every SE's tasks,
    # after this entire loop, see the removed comment this replaces for that rationale --
    # still true for the per-SE batch size, just no longer batched across SEs too).
    for email in se_emails:
        uid = se_user_ids.get(email, email)
        se_dcs = [dc for dc in scoped_dcs if dc.get("Assigned_SE_Email") == email]
        in_scope = [dc for dc in se_dcs if dc.get("In_Scope_Flag")]
        bo_scores = {
            "Visits": agent.score_bo2_visits(len(visits_last30_by_se.get(uid, set())), len(se_dcs), constants),
            "PL": {"score_pct": None, "grade": None, "reason": "PL_Value/PL_Expected not wired into this endpoint"},
            "Outstanding": {"ratio": None, "grade": None, "reason": "BO3 ratio needs last-month-OS/growth% -- not wired"},
            "Liquidation": {"score_pct": None, "grade": None, "reason": "no confirmed scoring formula exists (Source 3d Provisional)"},
        }
        attendance_gate_ok = None if attendance_unknowable else attendance_ok_by_se.get(uid, False)

        # Routing Agent hookup (R0.4 Origin_Point): prefer the previous working day's
        # punch-in; fall back to plan_date's own punch-in (punch_in_coords, already
        # fetched above) if no prior-day one exists; fall back further to the centroid
        # of this SE's own in-scope DC candidates (added 2026-09-21, explicit user
        # request -- SE Daily Plans must not depend on same-day punch-in) when NEITHER
        # real punch-in source exists; only defers entirely (None) in the genuine
        # can't-route case (no punch-in history AND no DC coordinates) --
        # planning.routing.generate_route_plans_for_se() honors that last case by
        # producing no RoutePlan/DailyTask rows for this SE this run.
        def _route_selector(candidates, se_id_str, plan_date_, punch_in_coords, constants_, dc_by_id_, _uid=uid, _email=email):
            prev = prev_punch_in_by_se.get(_uid)
            if prev is not None:
                origin, origin_basis = prev, "prev_30d_punch_in"
            elif punch_in_coords is not None:
                origin, origin_basis = punch_in_coords, "today_punch_in"
            else:
                centroid = agent.dc_portfolio_centroid(candidates)
                origin, origin_basis = (centroid, "dc_portfolio_centroid") if centroid else (None, "waiting_for_today")
            result = routing.generate_route_plans_for_se(
                plan_run, str(_uid), _email, plan_date_, candidates, origin, origin_basis, constants_,
                plan_choice=resolved_routing_plan, enable_rotation=enable_rotation,
                routing_overrides=routing_overrides, dc_district_lookup=dc_district_lookup,
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
            promise_by_dc=promise_by_dc, dc_health_scores=dc_health_scores,
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
                days = dc.get("Days_Since_Last_Visit")
                if days is not None and days < constants.min_days_since_last_visit:
                    reasons.append(f"Visited_Too_Recently ({days}d < {constants.min_days_since_last_visit}d)")
                if not dc.get("Has_Assigned_SE"):
                    reasons.append("No_Assigned_SE")
                # Rank<=6000 fully disabled for eligibility 2026-09-04 (see apply_dc_
                # exclusion_rules docstring) -- Top-DC-list is now fail-closed, no Rank
                # fallback, so a load failure gets its own explicit reason here too.
                if top_dc_allowlist is None:
                    reasons.append("Top_DC_List_Unavailable")
                elif dc["DC_ID"] not in top_dc_allowlist:
                    reasons.append("DC_Not_In_Top_List")
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
        se_pending_tasks: List[DailyTask] = [
            DailyTask(
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
                dc_health_score=t.get("DC_Health_Score"), health_gap=t.get("Health_Gap"),
                health_sub_scores=t.get("Health_Sub_Scores") or {},
                negative_gm_flag=t.get("Negative_GM_Flag", False),
                health_focus_track=t.get("Health_Focus_Track", False),
                health_focus_purposes=t.get("Health_Focus_Purposes", ""),
                credit_limit=t.get("Credit_Limit"), available_credit_limit=t.get("Available_Credit_Limit"),
                credit_active=t.get("Credit_Active"),
            )
            for t in tasks
        ]

        if se_pending_tasks:
            # Small, per-SE write cluster (was: one cluster for the WHOLE scope, after
            # this entire loop) -- confirmed on this SQLite/Django 6.0 setup that
            # bulk_create() populates real .id values on the input objects, so se_task_ids
            # below is immediately usable, no second query needed. Holding the write lock
            # for one SE's ~5 tasks instead of an entire scope's SEs is strictly friendlier
            # to the existing WAL + IMMEDIATE + 300s-timeout concurrency tuning (shorter
            # lock hold per transaction), not less safe -- run_all_states_tuff already
            # proves 3 concurrent generate_plan_for_scope calls are safe under this same
            # config, and per-SE batches only shrinks each individual transaction further.
            with transaction.atomic():
                DailyTask.objects.bulk_create(se_pending_tasks)
            total_tasks += len(se_pending_tasks)
            plan_run.task_count = total_tasks
            plan_run.save(update_fields=["task_count"])

            # Pitching Agent + DC Card, wired 2026-08-08/2026-08-14, made real-time-per-SE
            # 2026-09-24 -- previously ran once for the whole scope's tasks, after this
            # entire loop; now runs right after each SE's own tasks are written, scoped to
            # just this SE's new task ids (task_ids=, added to run_pitching_and_dc_card_
            # agents/generate_pitches_for_plan_run/generate_dc_cards_for_plan_run
            # specifically for this) so a reader sees this SE's real pitches/cards
            # immediately, and so this call does NOT silently re-process every earlier SE's
            # tasks too on every iteration (see run_pitching_and_dc_card_agents' own
            # docstring for why that would otherwise be an O(N^2) cost blow-up). Passes
            # every enrichment dict this function has already computed above (dc_financials/
            # dc_club_by_id/active_schemes_by_node/ytd_pl_last_year_by_dc/
            # _yoy_pl_growth_multiplier), unchanged from the old single end-of-scope call.
            se_task_ids = [t.id for t in se_pending_tasks]
            run_exceptions.extend(run_pitching_and_dc_card_agents(
                plan_run, plan_date, client, geo_mapping_cache,
                dc_financials=dc_financials, dc_club_by_id=dc_club_by_id,
                active_schemes_by_node=active_schemes_by_node, ytd_pl_last_year_by_dc=ytd_pl_last_year_by_dc,
                yoy_pl_growth_fn=_yoy_pl_growth_multiplier, task_ids=se_task_ids,
            ))

    # finished_at/skipped_ses are set exactly once, here, after every SE is done --
    # finished_at is a deliberate "whole scope done" semantic marker (unlike task_count,
    # which now updates live per-SE above), and skipped_ses is only meaningful once
    # complete (a reader polling mid-run sees the real, growing task_count/DailyTask rows
    # instead, per the change above -- skipped_ses finalizing at the end is a minor
    # readability gap for a live-polling reader, not a correctness issue, since nothing
    # else in this codebase reads it mid-run).
    plan_run.finished_at = timezone.now()
    plan_run.skipped_ses = skipped_ses
    plan_run.save(update_fields=["finished_at", "skipped_ses"])

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
                # Section 6's eligibility gate applies network-wide to every agent's DC
                # selection, but the live Product Cohort API has no awareness of it -- its
                # raw cohort can include ineligible DCs, so the persisted record is tagged
                # here for any future direct consumer. Additive only: the raw API response
                # is annotated, never filtered/mutated. Field kept as "rank_eligible" for
                # external-consumer stability, but as of 2026-09-04 (see apply_dc_exclusion_
                # rules docstring) it's Top-DC-list membership, fail-closed -- Rank<=6000
                # plays no role at all anymore, not even as a fallback, so an unavailable
                # list means nothing is eligible here.
                eligible_dc_ids = top_dc_allowlist or set()
                for dc_entry in step_3["results"]["dcs"]:
                    dc_entry["rank_eligible"] = str(dc_entry.get("partnerId") or "") in eligible_dc_ids
            FocusProductTargetRun.objects.create(
                plan_run=plan_run, material_id=focus_product_material_id, node_id=node_id,
                step_2a=fp_result["Step_2A"], step_2b=fp_result["Step_2B"], step_3=step_3,
            )
            run_exceptions.extend(fp_result["exceptions"])

    persist_exceptions(plan_run, run_exceptions)

    # GR-17-style escalation alert -- more than 10% of in-scope DCs producing a REAL
    # exception (live-pull failures, referential-integrity issues, etc.) is a signal the
    # underlying data/connection quality degraded for this run, not just isolated
    # one-off records; surface it rather than letting it sit unnoticed in ExceptionRecord.
    #
    # FIXED 2026-09-23 (root-caused after this alert fired on all 11 states 2 days
    # running, at 114-192% -- investigated rather than just re-flagged): the ratio used
    # to be len(run_exceptions)/len(scoped_dcs) with NO filtering at all, so it counted
    # every entry in run_exceptions, including the routine, BY-DESIGN, one-or-more-per-
    # excluded-or-flagged-DC bookkeeping records this pipeline deliberately writes for
    # completely normal, expected outcomes (a DC the admin's Program DC Selection rule
    # excluded, a DC dc_datamart itself marks inactive, a GR-28 override, a routing
    # constraint like Travel_Ceiling_Exceeded or Route_Diversity_Enforced, etc.) -- see
    # each reason_code's own exc.flag()/run_exceptions.append() call site for why it's
    # written on every occurrence, not just failures. Verified live across 4 real
    # PlanRuns (2026-09-22/23, #2191/#2192/#2193/#2208): genuine failure-type exceptions
    # (Live_Pull_Failed, *_Unresolved, *_Query_Failed) were consistently under 1% of DC
    # count in every one, while the routine bookkeeping alone was 114-192% -- meaning
    # this alert had never once reflected an actual data-quality problem since it was
    # added; it was mathematically guaranteed to fire on any state with enough excluded/
    # inactive DCs, which is every state, every day. _ROUTINE_EXCEPTION_REASON_CODES
    # below is deliberately a DENYLIST, not an allowlist of "real" codes -- a new
    # exception type nobody remembers to add here just causes an occasional false-
    # positive alert (annoying, not dangerous), whereas an allowlist missing a genuinely
    # new failure type would mean that failure class silently NEVER alerts, the far
    # worse failure mode for anything alert-shaped.
    real_exceptions = [e for e in run_exceptions if e.get("reason_code") not in _ROUTINE_EXCEPTION_REASON_CODES]
    if scoped_dcs and (len(real_exceptions) / len(scoped_dcs)) > 0.10:
        send_alert(
            f"PlanRun #{plan_run.id} ({scope_type}={scope_value} @ {plan_date}): "
            f"{len(real_exceptions)} real exceptions across {len(scoped_dcs)} DCs "
            f"({len(real_exceptions) / len(scoped_dcs):.0%}) -- exceeds 10% threshold "
            f"({len(run_exceptions)} total exception records, {len(run_exceptions) - len(real_exceptions)} routine/informational).",
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
