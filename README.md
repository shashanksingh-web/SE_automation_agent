# SE Automation Server (Agent TUFF)

Django project (`planning` app) plus a standalone core engine (`se_daily_plan_agent.py`)
that together implement the SE/DC Data Normalization Agent and SE Daily Task Agent
pipeline described in `SE_DC_Data_Normalization_Agent_Prompt.docx`.

**Agent TUFF** = Data Normalization Agent (Step 1) + SE Daily Task Agent (Step 2).

## Agents — count & entry points

7 core agents, 1 orchestration/coordination tier, 4 supporting ops utilities.

### 1. Data Normalization Agent

Raw feeds -> normalized `output/*.json` + `Exceptions_Report.json`.

- **Standalone CLI (primary entry point):**
  `python se_daily_plan_agent.py [--output-dir OUTPUT_DIR] [--date YYYY-MM-DD] [-v]`
- **Internal function:** the Section 1-8 pipeline in `se_daily_plan_agent.py`
- **Also invoked as Step 1 of:** `activate_tuff`, `run_scheduled_tuff` (see Orchestration below)
- **HTTP:** `GET /normalize/` -> `views.normalize` (planning/urls.py)
- Never depends on Django -- this is the one piece of the system that must run bare.
- Runs once per day; `activate_tuff` auto-skips Step 1 if already run today
  (`--force-normalization` to override).

### 2. SE Daily Task Agent

BO1-BO5 scoring -> `SE_Daily_Plan.json` / `DailyTask` rows for one scope.

- **Django command:** `python manage.py generate_se_plan {SE,ABM,RBM,NODE,BLOCK,DISTRICT,STATE} scope_value [--date] [--confirm-farmer-meeting EMAIL] [--focus-product ...]`
- **Internal function:** `planning.services.generate_plan_for_scope()`, wrapping
  `se_daily_plan_agent.generate_se_daily_plan()`
- **HTTP:** `GET /se/<scope_value>/`, `/abm/<scope_value>/`, `/rbm/<scope_value>/`,
  `/node/<scope_value>/`, `/block/<scope_value>/`, `/district/<scope_value>/`,
  `/state/<scope_value>/`
- **Also invoked as Step 2 of:** `activate_tuff`, `run_scheduled_tuff`
- Auto-triggers agents 3, 4, and 5 (Pitching, DC Card, Routing) inline once tasks are
  created.

### 3. Pitching Agent

Generates the DC visit pitch script per DailyTask.

- **Internal function:** `planning.pitching.generate_pitches_for_plan_run()` -- called
  from `generate_plan_for_scope()`, fault-isolated per task (`(created, failures)` tuple)
- **HTTP:** `GET /pitch/<daily_task_id>/` -> `views.pitch_script`
- **Config source:** `planning/pitch_config_loader.py` reading `pitch_config/*.csv`
  (glob-resolved by name prefix, mtime-cached)
- Not independently CLI-runnable -- always rides along with Agent 2.

### 4. DC Card Agent ("Dehaat Center Ko Jaano")

Generates the DC profile/preface card per DailyTask.

- **Internal function:** `planning.dc_card.generate_dc_cards_for_plan_run()` -- called
  from `generate_plan_for_scope()`, same fault-isolation pattern as Pitching
- **HTTP:** `GET /dc-card/<daily_task_id>/` -> `views.dc_card`
- Rides along with Agent 2, never run standalone.

### 5. Routing Agent

Builds route plans (Model 1/2/3, OR-Tools) for an SE's assigned DCs.

- **Internal function:** `planning.routing.generate_route_plans_for_se()` -- called from
  `generate_plan_for_scope()` for SE-scoped runs
- **HTTP:** `GET /routes/<se>/<plan_date>/` -> `views.route_plans`; select final plan:
  `GET /routes/<se>/<plan_date>/select/<plan_type>/` -> `views.select_route_plan_view`
- **Ops-CLI stand-in for the SE's own plan selection (R5.3):**
  `python manage.py select_route_plan` -- "trust-equivalent" of an SE picking a route in
  a mobile app that doesn't exist yet.

### 6. Product Cohort / Focus Product Campaign Targeting Agent

Pulls focus-product demand cohorts from an external SaaS API.

- **Standalone/ad-hoc CLI (nothing persisted):**
  `python manage.py focus_product_targets --focus-product MATERIAL_ID --focus-node NODE [--focus-product-years ...] [--focus-product-buildup-weeks ...] ...`
- **Persisted (tied to a real PlanRun):** `--focus-product` flag on `activate_tuff` /
  `generate_se_plan` -> writes `FocusProductTargetRun`
- **Internal client:** `se_daily_plan_agent.ProductCohortClient` ->
  `https://saas-platform-service.api.dehaat.net`
- **Requires:** `PRODUCT_COHORT_SESSION` + `PRODUCT_COHORT_GO_ADMIN_SESSION` (manually
  obtained, agent never signs itself in -- see `Product _cohort/PRODUCT_COHORT_AUTH.md`)

### 7. Outcome Reconciliation / Feedback-Loop Agent

Two tiers, closing the loop from planned tasks back into scoring.

- **Tier 1 -- outcome marking:** `python manage.py reconcile_outcomes` -- sets
  `DailyTask.outcome_status` from live visit/order data, bumps
  `DCVisitStreak.consecutive_misses`, escalates at `ESCALATION_THRESHOLD=3`, batched
  `bulk_create`/`bulk_update` (batch_size=500)
- **Tier 2 -- adaptive weighting rollup:** `python manage.py compute_completion_stats`
  -- trailing-30d completion rate per (SE, objective), feeds BO1/BO3's adaptive
  weighting multiplier
- **HTTP (read-only):** `GET /streaks/` -> `views.visit_streaks`,
  `GET /completion-stats/` -> `views.completion_stats`

## Orchestration tier (coordinates agents 1-5, not a peer agent itself)

- **`python manage.py activate_tuff {SCOPE} scope_value [--date] [--skip-normalization | --force-normalization] [--confirm-farmer-meeting EMAIL] [--focus-product ...]`**
  -- the single-scope, in-process, no-subprocess "Agent TUFF": Step 1 (Data
  Normalization, auto-skipped if already run today) -> Step 2 (SE Daily Task, which
  cascades into Pitching/DC Card/Routing).
  **HTTP equivalent:** `GET /tuff/<scope_type>/<scope_value>/` -> `views.tuff`

- **`python manage.py run_scheduled_tuff`** -- cron entry point: Step 1 once, then Step 2
  for every active `ScheduledScope` row, isolating failures per scope so one bad scope
  doesn't kill the rest. This is what `launchd`/cron calls in production.
  **HTTP (read/manage scopes):** `GET /scheduled-scopes/` -> `views.scheduled_scopes`

- **`python manage.py check_tuff_heartbeat`** -- monitoring only: alerts if Step 1 or any
  active `ScheduledScope` hasn't run today.

## Supporting ops utilities (inspection/audit only, not generative agents)

| Command | Purpose |
|---|---|
| `approve_plan_run` | Records human approve/reject on a `PlanRun` -- audit trail only, doesn't gate anything |
| `show_plan_run` | Read-only: which SEs got 0 tasks, what pitches came out of a run |
| `show_headcount_bifurcation` | Read-only headcount report over the last normalization output |
| `GET /runs/`, `/runs/<id>/`, `/headcount/`, `/directory/*` | Read-only HTTP mirrors of the above / roster lookups |

## Data sources (Data Normalization Agent)

| # | Source | Access mode | Location |
|---|---|---|---|
| 1 | SE/ABM Master + Tasks | Live | Redshift (Metabase db_id 41) |
| 2 | DC Master + Rank | Local file | `DC_RAnk.csv` |
| 3 | Transactional/Field (sales, outstanding, orders, liquidation, payments) | Live | Redshift (db_id 41) + input-backend Postgres (db_id 31) |
| 4 | Active Roster/History (attendance) | Live | input-backend Postgres (db_id 31) |
| 5 | Config Master (BO formulas, guardrails, thresholds) | Local files | 12 CSVs under `config and parameter /` |
| 6 | AOP & Target Data | Local file | `Niyojan Q2-FY_26_27 Dashboard - Planning.csv` |

Sources 2/5/6 are local CSVs, so the pipeline always runs end-to-end on them even with
zero live connectivity. Sources 1/3/4 are live-only -- if unconfigured, the run summary
explicitly says so rather than silently shipping partial data as complete.

### Live infra

Two possible clients, auto-selected by `get_live_client()`:

1. **`RedshiftDirectClient`** (psycopg2, preferred when configured) -- direct connection,
   bypasses Metabase's REST/MCP layer entirely.
2. **`MetabaseClient`** -- fallback, hits Metabase's REST API (`METABASE_URL` +
   `METABASE_API_KEY`, a Metabase Admin API key).

Direct Redshift is what's wired up in this deployment. Both `db_id=41` (Redshift proper)
and `db_id=31` (input-backend Postgres, database name `input_backend_db`) live on the
same physical cluster/host, just different database names on that connection.

- **Statement timeout:** 120,000ms per query (`REDSHIFT_STATEMENT_TIMEOUT_MS`),
  server-enforced.
- **Retry backoff:** `[2, 6]` seconds, shared between connect and query-execute failures.
- **Per-query fault isolation:** each live query quarantines independently
  (`Live_Pull_Failed`) rather than crashing the whole run.

### The 13 live SQL pulls

| Query | db_id | Source table(s) |
|---|---|---|
| Live DC Roster | 41 | `input_partner_details` |
| Canonical Node Mapping | 41 | `input_node_mapping` |
| Task Nodes (1b) | 31 | `task_management_task` join `task_management_plan` join `users_user` |
| Geo Mapping (1c) | 41 | `input_partner_details` join `input_se_node_mapping` |
| Attendance (3a) | 31 | `attendance_attendance` |
| Active Roster (4) | 31 | `attendance_attendance` join `task_management_taskdetails` join `task_management_task` |
| Sales Transactions (3d) | 41 | `coupon_analysis` |
| Outstanding (3d) | 41 | `dc_datamart` |
| Orders (3d) | 31 | `sale_orderrequest` join `customer_management_customer` |
| Liquidation (3d) | 41 | `invoice_liquidation_with_pog` |
| Payments (3f) | 31 | `payments_paymenttransaction` join `customer_management_customer` |
| DC Club Mapping (3g) | 41 | `dc_mapping_club_scheme` |
| DC Club Slabs (3g) | 41 | `dc_club_slabs` |
| DC Club Qualifying Turnover (3g) | 41 | `coupon_analysis` |

`KHETI_DB_ID=4` also exists for the `hyperlocal_order` proxy but isn't in this active
query list.

### Third external API -- Product Cohort (separate, opt-in only)

`ProductCohortClient` -> `https://saas-platform-service.api.dehaat.net`, session-based
(`PRODUCT_COHORT_SESSION` + `PRODUCT_COHORT_GO_ADMIN_SESSION` cookies, obtained manually
per `Product _cohort/PRODUCT_COHORT_AUTH.md` -- this agent never signs itself in). Only
invoked when `--focus-product` is passed; not part of a normal Step 1 run.

### Env vars (`.env`, gitignored)

```
REDSHIFT_HOST / REDSHIFT_PORT / REDSHIFT_USER / REDSHIFT_PASSWORD
REDSHIFT_DB_INPUT_BACKEND / REDSHIFT_DB_LOCUS / REDSHIFT_DB_DEV
PRODUCT_COHORT_URL / PRODUCT_COHORT_SESSION / PRODUCT_COHORT_GO_ADMIN_SESSION
SECRET_KEY / DEBUG   (Django, unrelated to normalization)
```

Plus ~15 `SE_AGENT_*` overrides for every local CSV path and the three `db_id`s, letting
any of it be repointed without code changes.

### Output artifacts

Everything lands in `output/*.json` -- 9 normalized tables + `Exceptions_Report.json` +
`SE_Daily_Plan.json` + `Run_Summary.json`, read/cached by the rest of the app through
`planning/data_cache.py` (mtime-keyed, shallow-cloned per read).
