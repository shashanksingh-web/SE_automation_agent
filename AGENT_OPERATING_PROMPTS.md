# Operating Prompts — SE/DC Data Normalization Agent & SE Daily Task Agent

These are reusable prompts for activating the two agents implemented in
`se_daily_plan_agent.py`. Paste the relevant one into a new session (with this
repo open and Metabase MCP access available) to reproduce the same behavior
without re-deriving context from scratch.

There are two agents in one file, with a hard boundary between them:

- **Data Normalization Agent** (Sections 1–9 of the file) — ingests the 6
  sources, standardizes/cleans, cross-checks, quarantines. Never scores or
  plans. This is what `SE_DC_Data_Normalization_Agent_Prompt.docx` specifies.
- **SE Daily Task Agent** (Section 11, `generate_se_daily_plan()`) — an
  explicit extension beyond the doc's own scope, built on top of the
  normalized tables, matching the output shape in the doc's Section 10
  ("Downstream Outcome").

**Doc updated 2026-08-04 — schema and output shape both changed materially.**
Before that update, `.docx` re-reads had only added prose/examples; this one
changed real structure, so always re-diff the doc against what the code
implements rather than assuming it's still just commentary. What changed:
- Source 3d (Outstanding, Orders) went from 3/2-column partial views to full
  17/23-column schemas — new fields include `current_od` (real overdue
  amount), `credit_on_hold`/`credit_on_hold_reason` (now the confirmed 6.4
  data source), and order `status` (filter to `'processed'`).
- Two brand-new sources: **3f Payments** (`payments_paymenttransaction`, db
  31 — `customer_id` join key to `partner_id` NOT proven, flag
  `Join_Key_Unconfirmed` on everything derived from it) and **3g DC Club
  Scheme** (`dc_mapping_club_scheme` + `dc_club_slabs`, db 41 — presence in
  the mapping table is a plausible, unconfirmed enrollment proxy; tier
  computation needs turnover vs. `dc_club_slabs`, done downstream not here).
- **The SE Daily Task output shape changed from one-row-per-(DC,objective)
  task to ONE ROW PER DC PER DAY**, 15 fixed columns (Sr.No, DC Name, DC ID,
  Distance, Task Type, Purpose, Reason, Last Visit date+days, Present
  Outstanding, Present Overdue, Last Order date+value, Last Payment date, YTD
  PL, Club participation) — `Win_Definition` is gone from the new spec
  entirely, don't reintroduce it. `DailyTaskRow` (was `PlanTask`) implements
  this; a DC selected under more than one objective is deduplicated to a
  single row tagged with its highest-ranked objective.
- Distance is now **sequential** (punch-in → DC1 → DC2 → …), not per-DC
  straight-line. `sequence_with_distance()` implements this via haversine;
  the doc explicitly leaves a second option undecided —
  `attendance_attendance`'s own `google_distance`/OSRM-matched fields might
  already give real road distance and could replace haversine if confirmed
  better. Re-check this before treating haversine as final.

---

## Prompt 0 — RETIRED 2026-08-06 (was: mandatory, before every SE Daily Task Agent activation)

```
Historical: this gate required asking the user for real values behind 4 unresolved
"custom X requested" config rows before every SE Daily Task Agent activation.

Status as of 2026-08-06 -- ALL FOUR RESOLVED, gate retired:
  - 7.3 default tie-break order — RESOLVED: Outstanding, PL, Visits, Long-Term, Sales,
    Liquidation (changed 2026-08-06 when the source sheet was re-synced; BO4/Overall
    Sales was left unplaced by the sheet itself, user confirmed it goes last). Wired
    into BusinessConstants.default_objective_priority. Liquidation (user-added 6th
    objective) still has no confirmed scoring formula anywhere in Source 5 -- treat
    that specific gap as still open, don't invent one.
  - 4.4 BO4 growth target multiplier — RESOLVED: 1.05 (fixed, not AOP-tied). Wired into
    BusinessConstants.bo4_growth_multiplier.
  - 8.5 qualification thresholds — RESOLVED 2026-08-06: Source 5 now shows Status
    "Overridden" with real fixed numbers, not a live-computed default -- Visits: not
    visited >14 days; Outstanding: balance >=Rs20,000 AND overdue >=15 days; PL: <3
    orders in 30 days; Long-Term: had PL sales in last 90 days. Already exactly what
    constants.qualify_outstanding_balance/qualify_outstanding_days_overdue/
    qualify_visits_days_since encode in code.
  - 4.5 BO4 grade cutoffs — RESOLVED 2026-08-06: Source 5 shows Status "Confirmed
    Default" -- "keep current cut-offs," no custom thresholds requested. Has no
    practical effect on task generation today since BO4/Overall Sales isn't part of
    the Daily Task Assignment Formula's candidate pool (8.12 excludes it) -- nothing to
    wire.

Do NOT ask about any of these 4 before future activations. If the source config sheet
changes again (it has twice before), re-check Config_Normalized for the "custom ... not
specified/left blank/requested" pattern before assuming this retirement still holds --
the gate mechanism itself isn't dead, it would reactivate for a genuinely new
unresolved parameter.

The Data Normalization Agent (Prompt 1) never needed this gate — none of these 4
params are consumed by normalization, only by BO scoring / the SE Daily Task Agent.
```

---

## Prompt 1 — Activate the Data Normalization Agent (full pipeline run)

```
Run the SE/DC Data Normalization Agent (se_daily_plan_agent.py) end to end.

- Sources 2/5/6 (DC Master, Config, AOP) are local files in this repo — always available.
- Sources 1/3/4 (SE attendance, tasks, geo mapping, active roster) are pulled live via
  Metabase REST API. Confirmed database_ids against this Metabase instance:
    - Redshift: 41 (pathik_report, input_partner_details, input_se_node_mapping, coupon_analysis)
    - input-backend (Postgres): 31 (task_management_*, users_user, customer_management_*)
    - kheti (Postgres): 4 (hyperlocal_order)
- If METABASE_URL / METABASE_API_KEY aren't set as env vars, the run still completes
  using Sources 2/5/6 only — Sources 1/3/4 get skipped with an explicit
  Metabase_Not_Configured entry in the Exceptions_Report, never silently.

Command:
  cd /Users/dehaat/Desktop/SE_automation_server
  source venv/bin/activate
  python se_daily_plan_agent.py --output-dir ./output --date YYYY-MM-DD -v

Output: normalized tables + Exceptions_Report.json + Run_Summary.json in ./output/.
Read Run_Summary.json first — it states plainly what ran live vs. what was skipped.

Do not treat a clean run as proof of completeness if Metabase_Configured is false —
say explicitly which sources were missing.
```

---

## Prompt 2 — Activate the SE Daily Task Agent (per node / per SE)

```
Generate the SE Daily Task list for [NODE NAME] node, for [DATE].

STOP — run Prompt 0 first. Do not proceed past this line until the user has answered
the 7.3 / 8.5 / 4.4 / 4.5 custom-value questions (or explicitly told you to use the
live-computed defaults for this run). This applies every time, not just the first time.

Use se_daily_plan_agent.py's generate_se_daily_plan() function (Section 11) — do not
hand-roll the ranking/capacity/sequencing logic separately; call the real function so
bug fixes and rule changes stay in one place.

Steps:
1. Load output/DC_Master_Normalized.json (from a prior Data Normalization Agent run),
   filter to Node == [NODE NAME] and In_Scope_Flag == true.
2. Group by Assigned_SE_Email to get the SE list for this node; resolve each email to a
   users_user.id (input-backend db_id=31).
3. Pull live via Metabase:
   - Last visit date per DC (task_management_task -> task_management_plan ->
     customer_management_customer, visit_type_id=1, db_id=31) — apply the 5-day
     min-days-since-last-visit exclusion (6.2) before treating a DC as In_Scope for today.
     Set both Last_Visit_Date and Days_Since_Last_Visit on the dc dict.
   - Geo lat_2/long_2 per DC (Redshift input_partner_details, is_dc=true, db_id=41) for
     sequential distance.
   - Recent (3-day) attempt counts per (SE, DC) for the 8.7 contact-fatigue penalty.
   - dc_financials: build from dc_datamart (dev, Redshift -- REPLACES
     customer_management_input_outstanding, confirmed absent from this cluster entirely,
     see Prompt 4) joined with sale_orderrequest (full 23-col schema, input_backend_db,
     status='processed' for last-order, any status for credit_on_hold). dc_datamart is
     one row per sap_partner_id already -- no dedup-by-latest needed, no bridge through
     customer_management_customer needed. Confirmed genuinely fresh (MAX(last_invoice_date)
     within days of today, not the ~2-year-stale snapshots the old table had).
   - last_payment_by_dc: payments_paymenttransaction (db_id=31), MAX(created_at) WHERE
     status='SUCCESS' per customer_id -- flag Join_Key_Unconfirmed, this ID space is not
     proven to match partner_id.
   - dc_club_by_id: dc_mapping_club_scheme + dc_club_slabs (Redshift, db_id=41) --
     presence-in-table is a plausible, unconfirmed enrollment proxy.
   - punch_in_coords: attendance_attendance check_in_lat/long for the SE, same date, if
     this is a same-day (not forward-looking) plan.
4. Build bo_scores_by_objective per SE, now including "Liquidation" alongside the
   original 5. Only score what live data actually supports (e.g. Visits coverage from
   real visit counts) — leave everything else as
   {"score_pct": None, "grade": None, "reason": "<why>"} rather than fabricating a number.
5. Call generate_se_daily_plan(se_id, se_name, plan_date, in_scope_dcs, bo_scores,
   dynamic_params, constants, attendance_gate_ok, recent_attempts_by_dc=...,
   dc_financials=..., last_payment_by_dc=..., dc_club_by_id=..., ytd_pl_by_dc=...,
   punch_in_coords=...) per SE. Pass attendance_gate_ok=None for a forward-looking
   ("tomorrow"/future-date) plan, since punch-in can't be known yet.
6. Present output as ONE ROW PER DC (not per objective-task) — Sr.No, DC Name, DC ID,
   Distance, Task Type, Purpose, Reason, Last Visit date+days, Present Outstanding,
   Present Overdue, Last Order date+value, Last Payment date, YTD PL, Club participation.
   State the Capacity_Check and Travel.Cap_Exceeded per SE plainly, don't bury them.

If asked to cap the list (e.g. "only 5 tasks per SE"), truncate the already-sequenced
Tasks list to the first N — it's pre-ranked by priority, so this is a safe truncation,
not a re-ranking.

Flag explicitly, every time:
- Which objectives are real-data-scored vs. unscored (don't let unscored objectives'
  DC selections look equivalent in confidence to scored ones).
- Last_Payment_Join_Key_Unconfirmed on every row that has a Last_Payment_Date.
- DC_Club_Participation is a presence-based read, not a confirmed enrollment flag.
- Credit_On_Hold rows are surfaced, not auto-excluded — say so explicitly, don't let it
  read as a silent block.
- Data_Confidence field and Travel.Cap_Exceeded on every plan.
```

---

## Prompt 3 — Django API (`planning` app) — same engine, HTTP-accessible

```
se_daily_plan_agent.py is wired into the Django project (config/) as the `planning` app,
imported as a library (not duplicated) by planning/services.generate_plan_for_scope().
Every scoring/ranking/capacity/sequencing decision still comes from
se_daily_plan_agent.generate_se_daily_plan() -- the Django layer only resolves scope
(SE/ABM/Node/Block/District/State -> a DC/SE list), pulls Sources 1/3/4 live SCOPED to
just those DCs/SEs (not a full pipeline run), and persists the result.

Endpoints (all GET, all accept an optional ?date=YYYY-MM-DD, default today):
  /api/planning/se/<se_email>/
  /api/planning/abm/<abm_code>/        -- requires live Metabase (needs Source 1c)
  /api/planning/node/<node_name>/
  /api/planning/block/<block_name>/    -- requires live Metabase (needs Source 1c)
  /api/planning/district/<district_name>/  -- requires live Metabase (needs Source 1c)
  /api/planning/state/<state_name>/
  /api/planning/runs/                  -- list past PlanRuns (?scope_type=&scope_value=)
  /api/planning/runs/<id>/             -- re-fetch a persisted PlanRun without regenerating

Models (planning/models.py): PlanRun (one per activation, with Run_Summary-style
metadata + dynamic_parameters JSON), DailyTask (the 15-column shape, one row per DC per
SE per day -- 1:1 with se_daily_plan_agent.DailyTaskRow), ExceptionRecord (tied to a
PlanRun, same guardrail as the CLI: an exceptions report ships every run, including
clean ones).

Requires DC_Master_Normalized.json to exist first (run Prompt 1 / the CLI at least once)
-- SE/NODE/STATE scopes resolve against it directly; ABM/BLOCK/DISTRICT additionally
need live Metabase since Source 1c (the canonical geo hierarchy) is never cached to a
local file. Without METABASE_URL/METABASE_API_KEY, SE/NODE/STATE endpoints still work
but return Provisional plans (no live visit history/geo/financials) with a single
Metabase_Not_Configured exception -- ABM/BLOCK/DISTRICT return a 422 instead of a
silently-empty or wrong plan.

Known simplification, not yet wired: attendance gating and punch-in-based route
sequencing (every plan uses attendance_gate_ok=None and sequences from the first DC, not
a real punch-in point) -- noted in every PlanRun.note field, not hidden.

STANDALONE COMMAND (no dev server needed) -- planning/management/commands/generate_se_plan.py
wraps generate_plan_for_scope() as a Django management command, symmetric with the CLI
Data Normalization Agent (`python se_daily_plan_agent.py`):

  python manage.py generate_se_plan NODE Jaipur --date 2026-08-05
  python manage.py generate_se_plan SE mewa.garhwal@agrevolution.in
  python manage.py generate_se_plan ABM E02213137 --json   # full JSON instead of a summary

Prints a summary (SE/DC/task counts, note, first 10 exceptions) by default, or the full
serialized plan with --json. Same PlanRun/DailyTask/ExceptionRecord persistence as the
HTTP endpoints -- both are just different front doors onto the same
generate_plan_for_scope() call.
```

---

## Prompt 4 — Live data access: direct Redshift, not the Metabase API

```
As of 2026-08-04, the real live-data path is a direct psycopg2 connection to the Redshift
cluster (RedshiftDirectClient / get_client() in se_daily_plan_agent.py), NOT the Metabase
REST API (MetabaseClient) and NOT the Metabase MCP endpoint. Both alternatives were tried
and ruled out first -- keep that reasoning in mind before re-attempting them:

- The "Metabase MCP" connector this session's Claude Code had access to
  (https://metabase.agrevolution.in/api/metabase-mcp) is a genuine OAuth-protected
  resource (probed live: 401 + WWW-Authenticate: Bearer realm="mcp"). Its authorization
  server supports ONLY authorization_code + refresh_token grants -- no client_credentials
  -- so there is no way to get a headless service-account token for it. It's built for
  interactive human-in-browser clients (like Claude Code's own one-time OAuth connect),
  not for an unattended backend service. Don't re-attempt an automated MCP OAuth
  integration without accepting that a one-time human browser login is unavoidable.
- The Metabase REST API path (MetabaseClient, METABASE_URL/METABASE_API_KEY) still works
  as a fallback if someone provides a real Metabase-issued API key, but nobody has yet.

Credentials (.env, gitignored, chmod 600 -- never in a committed file or printed in full
again): REDSHIFT_HOST, REDSHIFT_PORT, REDSHIFT_USER, REDSHIFT_PASSWORD,
REDSHIFT_DB_INPUT_BACKEND, REDSHIFT_DB_LOCUS, REDSHIFT_DB_DEV. Loaded via python-dotenv in
both config/settings.py (Django) and se_daily_plan_agent.py's own top-of-file import (so
the CLI works standalone too). get_client() in se_daily_plan_agent.py prefers
RedshiftDirectClient over MetabaseClient automatically whenever REDSHIFT_* are set.

This ONE Redshift cluster hosts 3 databases -- confirmed live via information_schema:
  "dev"              -- pathik_report, input_partner_details, input_se_node_mapping,
                         coupon_analysis, dc_mapping_club_scheme, dc_club_slabs,
                         hyperlocal_order (everything under Metabase's old db_id 41)
  "input_backend_db" -- task_management_*, users_user, sale_orderrequest,
                         customer_management_customer, attendance_attendance,
                         payments_paymenttransaction (Metabase's old db_id 31's tables)
  "locus"            -- unrelated (just users_user), not used

Confirmed real limitations of this specific credential/cluster (not fixable by SQL changes):
  - customer_management_input_outstanding does NOT exist ANYWHERE reachable by this
    credential -- confirmed by sweeping all 32 databases on the cluster via
    information_schema, not just the 3 originally named ones. It's genuinely
    Postgres-only, and no separate input-backend Postgres credential was ever obtained
    (that path was tried and abandoned -- see the RESOLVED note below). This is now
    moot for Present_Outstanding/Present_Overdue specifically since dc_datamart replaced
    it, but keep this finding in mind if some OTHER Postgres-only table is needed later --
    the fix will not be "try a different database on this same Redshift cluster."
  - RESOLVED 2026-08-04: dc_datamart (dev, Redshift, same cluster already fully
    accessible) is a denormalized per-DC datamart that supersedes
    customer_management_input_outstanding entirely -- total_outstanding/total_overdue
    map directly to the old current_os/current_od, plus a richer aging split
    (current_month_os/os_1_to_90/os_90_plus) and genuinely fresh data (confirmed
    MAX(last_invoice_date) within days of today). No Postgres credential needed at all.
    SQL_OUTSTANDING_3D in se_daily_plan_agent.py and _sql_outstanding() in
    planning/services.py both query it now; PostgresDirectClient (a short-lived class
    built for a Postgres-based fix) was removed once this made it unnecessary.
  - coupon_analysis (Sales_Transactions_3d, GMV/PL data) returns
    "InsufficientPrivilege: permission denied for schema s3_tables" -- it's a Redshift
    Spectrum external table over S3, and this readonly user lacks access to the
    underlying s3_tables schema. Needs a credential/grant change, not a query fix.
  - Redshift does NOT support Postgres's `DISTINCT ON` (raises FeatureNotSupported) --
    use `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)` + filter rn=1 instead,
    everywhere, not just in the one query already fixed this way.

Real column-name corrections discovered live (the doc's claims / earlier-session guesses
were wrong in these specific spots -- already fixed in se_daily_plan_agent.py's SQL
constants, don't revert):
  - pathik_report has NO plan_execution_date column -- it's transaction_date.
  - attendance_attendance's geo columns are check_in_latitude/check_in_longitude and
    check_out_latitude/check_out_longitude (not _lat/_long) -- it also carries
    total_distance_travelled, osrm_match_coordinates, osrm_trip_coordinates, and
    google_polyline: real road-distance data that could replace the haversine
    sequencing in sequence_with_distance() once wired in (still not done).
  - task_management_taskdetails has NO check_in_time column -- the per-visit timestamp
    is created_at.
  - input_se_node_mapping carries NO node/block/district columns at all (only zone,
    state, "p&l node") -- it exists purely to add employee CODES ("emp id se",
    "abm e code", "rbm e code", "zbm e code", "growth manager e code") keyed by
    sales_rep_email. Node/Block/District/State AND the ABM/RBM/ZBM/Growth-Manager
    NAMES and EMAILS all live directly on input_partner_details (node_name, block_name,
    district_name, state_name, abm, "abm email id", rbm, "rbm email id", zbm,
    "zbm email id", "growth manager name", "growth manager email id") -- the original
    SQL_GEO_MAPPING_1C query design (joining input_se_node_mapping for these) was
    structurally wrong and has been rewritten to source them from input_partner_details
    directly, joining input_se_node_mapping only for the employee codes.
  - SQL_ACTIVE_ROSTER_4 had a real bug (not a doc/guess error, a bug introduced while
    writing the query): it selected `cc.id AS dc_id` (customer_management_customer's
    internal PK) instead of `cc.partner_id AS dc_id` (sap_partner_id) -- this silently
    produced ~92,000 false Referential_Integrity exceptions (DC_ID not found in DC
    Master) on the first full live run, which dropped to ~12,600 genuine ones once fixed.
    If a future change to this query reintroduces `cc.id` instead of `cc.partner_id`,
    expect the same symptom: a sudden huge spike in Referential_Integrity exceptions
    where the Record_ID values look like small internal integers, not 10-digit
    sap_partner_id-format strings.

Confirmed live end-to-end with this access (2026-08-04): full CLI pipeline run produced
511 real SE_Daily_Plan rows, 111k+ Visits_Normalized, 31.9k Attendance_Normalized, 12,880
Geo_Mapping_Normalized rows, 48k+ Payments_Normalized, 9,801 DC_Club_Normalized. All 6
Django planning endpoints (SE/ABM/Node/Block/District/State) tested over real HTTP and
returned real, non-empty plans -- ABM/Block/District specifically had been un-testable
before this (they need Source 1c, which only ever existed live).
```

---

## 2026-08-06 — Daily Task Assignment Formula + Guardrails sheets added, engine rewritten

The user added 3 new config sheets alongside re-exported updates to the 2
existing ones: `BO_Configuration_Sheet_v3 - Daily Task Assignment Formula.csv`,
`- Guardrails.csv`, `- Open Questions (Sec 9).csv`. The 2 existing files were
re-exported with a `(1)` suffix in their filenames — this broke
`se_daily_plan_agent.py`'s default config paths (pointed at the old,
now-deleted filenames) until fixed; `CONFIG_ALL_PARAMS_CSV`/
`CONFIG_SE_INCENTIVE_CSV` now point at the `(1)` filenames, plus 3 new path
constants (`CONFIG_OPEN_QUESTIONS_CSV`/`CONFIG_TASK_FORMULA_CSV`/
`CONFIG_GUARDRAILS_CSV`) for the new files, though nothing parses those 3 as
data yet — they're specs to hand-implement, not lookup tables.

**`generate_se_daily_plan()`'s task-selection core was rewritten** to match
the new Daily Task Assignment Formula (Layers 0-3 + Final, params 8.9-8.12),
replacing the old per-objective-cap loop (Visits≤6/day, Outstanding≤4/day,
PL≤5/day, time-budget-limited — this is what produced the 9-15 tasks/day
seen in earlier runs):
- **8.10: max 5 tasks/day**, hard cap (`constants.max_daily_tasks`).
- **8.11: farmer-meeting exclusivity** — `farmer_meeting_scheduled_today`
  param (new, default `False`). No live Farmer_Meetings data source exists
  anywhere in this pipeline, so Layer 0's `FM_Urgency` can only honestly
  default to `False`, never guessed — this gate is real code but currently
  dormant until a real scheduling source exists or a caller passes `True`.
- **8.12: visit bundling** — a DC qualifying on multiple objectives
  ({Visits, Outstanding, PL, Long-Term} — NOT Sales/Liquidation, which
  8.12's own definition excludes from `Candidate_DCs` entirely) now gets
  ONE task with `Objective="Visits,Long-Term"`-style comma-joined tags and a
  combined `Purpose_Of_Visit`, confirmed live (e.g. a DC matched Visits+
  Long-Term got exactly 1 row, not 2).
- **Layer 3 Priority_Score(DC)** ranks DCs (not objectives) via
  0.40/0.35/0.25 of that DC's own top-3 matched-objective grades.

**Real, load-bearing gaps hit while building this — degraded honestly, not
guessed:**
- `Qualify_Outstanding`'s "overdue ≥15 days" leg has no confirmed per-DC
  days-overdue field in `dc_datamart` — only the balance≥₹20,000 leg is
  enforced; flagged in `Safety_Flags.Qualify_Outstanding_Days_Overdue_Leg`.
- `Qualify_PL`'s "<3 orders in 30 days" needs a per-DC PL order COUNT;
  `coupon_analysis` has no confirmed DC-level join key (same gap that
  blocked Sales_Transactions_Normalized's DC grain originally) — `_qualify_pl`
  always returns `False` until that's resolved. No DC currently reaches the
  candidate pool via PL.
- `Qualify_LongTerm`'s "had PL sales in last 90 days" uses `YTD_Private_Label
  > 0` as a proxy (fiscal-year-to-date, not a strict rolling 90-day window)
  since that's the only live per-DC PL figure available.
- **Priority_Score(DC) substitutes the SE-level objective score
  (`_objective_gap`) as each DC's proxy grade per matched objective**, since
  true per-DC BO1/BO3 grading needs data this pipeline doesn't have (3.1's
  `Expected_Outstanding` needs last-month's-outstanding history — no
  time-series source exists, `dc_datamart` is current-snapshot only; 1.2's
  `PL_Expected` combination method is itself still TBD in Source 5).

**Consequential finding from live testing (PlanRun #19, Jaipur, 2026-08-05):**
because only `Visits` has real SE-level scoring wired in
`planning/services.py` (Outstanding/PL/Long-Term are all `None`-scored →
floor gap of -1.0 in the ranking formula), **every Visits-only-matched DC
outranks every Outstanding-matched DC, regardless of overdue amount** — a
DC with ₹694,026 overdue and a DC with ₹0 overdue get the identical -0.4
Outstanding-weighted priority contribution. Confirmed live: 30/30 tasks
across all 6 SEs came back tagged `Visits` or `Visits,Long-Term` or
`Long-Term` — zero `Outstanding` matches survived into any top-5, even
though Outstanding is 9.1's declared #1 business priority this quarter.
Wiring real BO3 (Outstanding) scoring into `planning/services.py`'s
`bo_scores` dict is the highest-leverage next fix for this — not yet done,
needs the user's input on how to approximate `Expected_Outstanding` without
historical data.

**7.3 tie-break order changed and was re-confirmed by the user**: was
`PL, Outstanding, Sales, Liquidation, Visits, Long-Term`; now
`Outstanding, PL, Visits, Long-Term, Sales, Liquidation` — the source
sheet itself left BO4 (Overall Sales)'s position unplaced ("TBD"); user
confirmed it goes last, matching the existing 7.4(b) all-D pattern.
Liquidation (user-added 6th, still no confirmed formula) stays last of all.
`BusinessConstants.default_objective_priority` updated accordingly.

**22 new Guardrails (GR-01–GR-22) — only partially implemented.** Already
matched by existing code: GR-01 (3.7 order-block), GR-03 (Legal_Hold
exclusion, upstream in `apply_dc_exclusion_rules`). Newly added this round:
GR-11 (5-task truncation safety net), GR-12 (FM-exclusivity sole-item
return path), GR-14 (independent second-pass Inactive/Legal_Hold check,
`Safety_Flags.GR_14_Second_Pass_Violations`). NOT implemented yet: GR-04
through GR-10 (bounds-clamping on the 7 agent-computed dynamic values —
currently unclamped), GR-13 as a hard reject-and-regenerate (currently only
reported, not enforced), GR-16 (full audit-log format with
snapshot/timestamp/bound-hit per value), GR-17 (OD>10% escalation alert),
GR-18 (HR compliance-hold suppression — no HR data source exists), GR-19
(BO4 Field Crop provisional labelling), GR-20/21/22 (fail-safe fallback
behaviors for stale/missing data, framed generically but not coded per-case).

## 2026-08-06 (later same day) — 3 real N/A-column bugs found and fixed

Investigated why Distance/Last_Payment/Present_Outstanding were showing N/A
so often. Two were genuine bugs (fixed), one is a real data-coverage gap
(not fixable by query changes):

1. **Payments join key CONFIRMED, not unconfirmed** — live-tested:
   `payments_paymenttransaction.customer_id` matches
   `customer_management_customer.id` (internal PK) on 521,345 of 538,039
   rows (96.9%), 0 rows match `partner_id` directly. Same bridging pattern
   as Orders. Fixed `SQL_PAYMENTS_3F` (se_daily_plan_agent.py) and
   `_sql_payments()` (planning/services.py) to bridge through
   `customer_management_customer`; `Last_Payment_Join_Key_Unconfirmed` now
   defaults `False`, `Join_Key_Unconfirmed` exception retired. **The Django
   version of this query had a separate, worse bug on top**: it filtered
   `customer_id::text IN (dc_ids)` directly — comparing an internal PK
   against `sap_partner_id` strings — which returned zero rows on every
   single call. That's the actual reason Last_Payment was N/A on literally
   every Django-generated plan throughout this project until now.
2. **`_sql_geo()`'s `is_dc = true` filter was silently dropping real geo
   data.** Confirmed live: several DCs already scoped via `dc_ids` (a
   confirmed real DC per DC_Master/Source 2) carry real `lat_2`/`long_2` in
   `input_partner_details` but `is_dc = false` — `dc_ids` is already the
   authoritative filter, so re-filtering by `is_dc` there was redundant and
   actively wrong. Removed the filter. Distance coverage on a Jaipur test
   went from 0/30 to 25/30 tasks immediately after this one fix.
3. **`sequence_with_distance()` degraded all-or-nothing** — one
   coordinate-less DC in an otherwise-fully-geocoded list blanked
   `Distance_Km` for every DC in that SE's list, not just the one missing
   it. Rewrote to sequence the geocoded subset via haversine (real
   distances), append coordinate-less DCs at the end with `Distance_Km =
   None` individually, and note the partial-degrade in
   `Travel.Sequencing_Basis` (e.g. `..._partial_1_of_5_dcs_missing_geo`).
   Confirmed live: went from 29/30 (this specific DC really has no
   `lat_2`/`long_2` anywhere in `input_partner_details` — genuine data gap,
   not a bug) up from 0 for that SE's whole list.
4. **Present_Outstanding/Overdue N/A — investigated, genuine data gap, not
   a bug.** Spot-checked 4 DCs showing N/A outstanding (e.g. `Jivan Jyoti
   Khad Beej Bhandar Bhojmed`, `1000019305`) directly against `dc_datamart`
   — they simply don't have a row there at all (14,862 total rows / 14,762
   distinct DCs in `dc_datamart`, vs 10,195 in DC_Master — different
   coverage, not a subset). No join/query fix can produce data that isn't
   in the source table; this stays N/A honestly until `dc_datamart`'s own
   population covers these DCs.

## 2026-08-06 (later still) — real per-DC BO3 (Outstanding) scoring wired

Fixes the crowding-out problem flagged right above: added
`score_bo3_outstanding_live_proxy()` (se_daily_plan_agent.py) -- NOT the
literal 3.1-3.6 formula (blocked on missing historical Expected_Outstanding,
same as before), but a real, live, per-DC substitute: `Outstanding_Health_Pct
= 1 - (Current_Overdue / Current_Outstanding)`, graded against the doc's own
3.6 cutoffs (A>=100%, B>=75%, C>=50%, D<50%) for directional consistency. A
DC with nothing outstanding is the best case (health=1.0, Grade A) rather
than "unscored." `os_90_plus` is surfaced in the `reason` text when present
but doesn't change the grade (no business-approved downgrade rule for that
exists). The doc-faithful `score_bo3_outstanding()` (needs `last_month_os`)
is left untouched and dormant for whenever a real historical source exists.

`generate_se_daily_plan()` gained a new optional `dc_bo_scores` param
(`Dict[dc_id -> Dict[objective -> score_dict]]`) -- Layer 3's per-DC ranking
now prefers a real per-DC grade over the SE-level `bo_scores_by_objective`
proxy when one is supplied for that objective, falling back to the old
proxy otherwise. `planning/services.py` builds this per-DC dict from
`dc_financials` (already populated from `dc_datamart`) right after it's
built, and passes it through. `Reason_Of_Visit` now also surfaces the real
grade and basis text (e.g. "Outstanding Grade D (100% of outstanding is
overdue; ₹487,978 is 90+ days overdue)") when a per-DC score exists.

**Confirmed live, Jaipur node, 2026-08-05**: Outstanding went from 0/30
tasks to 13/30 (objective-tag counts: `Outstanding` alone x3,
`Outstanding,Visits` x4, `Outstanding,Visits,Long-Term` x4,
`Outstanding,Long-Term` x1). Genuinely severe, previously-invisible accounts
now surface -- e.g. `bunty.suwalka`'s M/s R.K Khad Beej Bhandar (₹487,978,
100% overdue, Grade D) and `prahlad.choudhary`'s Shree Shyam Agro Tech
(₹412,061, 100% overdue, Grade D) both now lead their SE's list, where
previously they never appeared at all. PL and Long-Term (via the
`ytd_pl_by_dc>0` qualify-proxy) still use the SE-level score, so they're
still subject to the same crowding-out dynamic until similarly wired.

## 2026-08-06 (config files moved + re-synced) — Day_Type exclusivity fix + real Purpose taxonomy

The 5 `BO_Configuration_Sheet_v3` CSVs moved into a new subfolder,
`config and parameter ` (trailing space in the name is literal, confirmed
via `ls` -- not a typo). `DC_RAnk.csv` (Source 2) and the AOP dashboard
(Source 6) stayed at `BASE_DIR`. Added `CONFIG_DIR` in
`se_daily_plan_agent.py` and repointed all 5 config path constants, plus 2
new ones for files added this round (`CONFIG_AGENT_DETERMINED_CSV`,
`CONFIG_VISIT_PURPOSE_MAPPING_CSV`/`CONFIG_VISIT_PURPOSE_SYSTEM_CSV`).
"Open Questions (Sec 9).csv" was retired (superseded by "Agent-Determined
Parameters.csv", same 7-row dynamic-parameter content, restructured) --
`CONFIG_OPEN_QUESTIONS_CSV` kept for backward compat, will honestly report
missing if ever loaded.

**Real finding, not just a path fix: 8.11 and GR-12 were REWRITTEN, and my
existing bundling logic violated the new rule.** 8.11 now explicitly says
DC Visit and Farmer Meeting are mutually exclusive BOTH directions --
"if the system proposes a DC Visit day, no Farmer Meeting exists in that
day's plan" -- and GR-12 was rewritten to match: "Daily_Task_List may never
mix Day_Types." My 2026-08-06-earlier rewrite had been bundling `Long-Term`
into the same task as `Visits`/`Outstanding` whenever a DC qualified for
both (e.g. `Objective: "Visits,Long-Term"`) -- a direct violation once this
rule landed. Fixed: removed `_qualify_longterm()` from the DC Visit
`QUALIFIERS` dict entirely -- Long-Term/BO5 now only ever appears via the
standalone `Farmer_Meeting_Task` exclusivity-gate path (Layer 0/1), never
bundled into a DC Visit. Confirmed live: 0/29 tasks now carry `Long-Term`
in a mixed objective tag (was present in ~5/30 before).

Note: the Daily Task Assignment Formula sheet's own Layer 2 formula text
still lists `Qualify_LongTerm` in the `Candidate_DCs` union, and the FINAL
row still says `Matched_Objectives` where Layer 2 renamed it to
`Matched_Purposes` -- read as an incomplete edit in the source sheet (it
updated 8.11/8.12's prose but not every formula line to match), not a
deliberate instruction to violate the GR-12 exclusivity the same sheet just
added. Worth a quick confirm with whoever owns the sheet if this reading is
wrong.

**Also fixed: real system Visit Type / Purpose taxonomy, confirmed via the
new "Visit Type & Purpose Mapping" / "Visit Type to Purpose (System)"
sheets.** Neither "Visit" nor "Call" are real Visit Types in
`task_management` -- the real ones are DC Visit, Demo Visit, External
Meeting, Farmer Meeting, Lead Generation, Node/Warehouse Visit (4 of these
6 have zero configured Purposes and can't be BO-tagged at all, per new
GR-15). `TASK_TYPE_BY_OBJECTIVE` now maps Visits/Outstanding/PL/Sales/
Liquidation to `"DC Visit"` (was `"Visit"`/`"Call"`). `PURPOSE_BY_OBJECTIVE`
fixed two real mismatches: Outstanding's purpose is `"Promise To Pay /
Collection"` (exact system spacing, was missing the spaces around `/`);
PL's purpose is `"Sale"` (was `"Private Label Product Promotion"` -- that
purpose only exists under the Farmer Meeting visit type per the bridge
table, not DC Visit; PL is distinguished by product tag on a Sale-purpose
order line instead). Confirmed live: all 29 tasks now show `Recommended_
Task_Type = "DC Visit"` and correctly-spaced Purposes.

## 2026-08-06 (latest) — attendance gating (Section 3a) wired for real

`planning/services.py` had `attendance_gate_ok=None` hardcoded on every
call, for the whole project's lifetime -- Django-generated plans were
always `Data_Confidence: Provisional_No_Attendance_Gate` regardless of
whether the SE had actually punched in. Fixed: real attendance is checked
via the existing punch-in query's rows (any check-in row on `plan_date` =
gate passes), tracked in a new `attendance_ok_by_se` dict.

The gate is conditional on plan_date, not blanket-on: a **forward-dated**
plan (`plan_date` in the future -- the common "plan for tomorrow" case used
throughout this project) can never have real attendance yet, so
`attendance_gate_ok` stays `None` (Provisional, unchanged behavior) rather
than being wrongly evaluated as `False`. Same for when the live client
isn't configured at all -- `attendance_unknowable = plan_date > today OR
not client.configured`, both cases keep the gate `None`, not `False`
(defaulting to `False` there would have wrongly zeroed out every plan
whenever Metabase wasn't configured, not just when attendance is genuinely
missing).

For today-or-earlier with a configured client, the gate is real: an SE with
no `attendance_attendance` row on `plan_date` now gets `Tasks: []` and a
`Skipped_Reason`, per `generate_se_daily_plan()`'s own pre-existing (but
previously unreachable in Django) early-return logic. **This is a real
behavior change worth knowing**: running a same-day plan before an SE's
morning punch-in will now legitimately return zero tasks for them, not a
bug -- that's Section 3a's actual design ("attendance gates whether a plan
is generated at all").

**Confirmed live, 3 cases, Jaipur node:**
- Forward-dated (2026-08-07): 29 tasks, unaffected -- gate stayed `None` as before.
- Today at run-time, before anyone's morning punch-in (2026-08-06): 0 tasks
  across all 6 SEs -- correct, nobody had checked in yet at that moment.
- A past date with mixed real attendance (2026-08-05): 24 tasks (was 29) --
  `prahlad.choudhary` (confirmed earlier in this project to have no
  attendance row that day) now correctly excluded entirely; the other 5 SEs,
  who did have real check-ins, got their normal plans unaffected.

## 2026-08-06 (newest) — real per-DC BO1 (PL) scoring wired

Same pattern as BO3: 1.2's `PL_Expected` combination method (90-day-average
vs AOP target) is itself still TBD in Source 5, and the AOP-target leg
needs a per-DC AOP join that isn't confirmed either -- so
`_sql_pl_metrics()` (`planning/services.py`) uses ONLY the 90-day-average
leg from `pathik_report.pl_billed_amount` (same live, DC-scoped source as
YTD PL), scaled to a 30-day-equivalent baseline (`pl_sum_90d / 3`), compared
against the real trailing-30-day `pl_actual_30d`. Reuses the existing
`score_bo1_private_label()` unchanged (1.5's confirmed A>=80%/B>=60%/
C>=40%/D<40% cutoffs) -- just needed a live `pl_value`/`pl_expected` pair
supplied, which nothing did before. Added a `reason` string to that
function's success path too (it previously only set `reason` on the
`PL_Expected undefined` failure path, leaving `Reason_Of_Visit` showing an
empty `PL Grade D ()` when it succeeded).

`_qualify_pl()` (`se_daily_plan_agent.py`) also switched from a permanent
`return False` stub to a live proxy: a DC qualifies for PL if its live
PL_Ratio grades C or D. 8.5's literal "<3 PL orders in 30 days" is still
not computable (`coupon_analysis` has no confirmed DC-level join key) --
this substitutes a more direct read of the same business question ("does
this DC need a PL push") from the one live source that does have DC grain.

**Confirmed live, Jaipur node, 2026-08-05, and a genuinely important
finding, not a bug**: PL now dominates the candidate pool -- 23 of 25 tasks
carry a `PL` tag, nearly all Grade D. Traced this to a **real, verified,
massive negative net-PL swing across the ENTIRE `pathik_report` dataset on
2026-08-02 through 2026-08-04** (e.g. -₹3.78M network-wide total PL on
Aug 3 alone, confirmed by aggregating ALL DCs' `pl_billed_amount` per day,
not just one) -- returns/credits during that window exceeded new PL billing
almost everywhere, which depresses every DC's trailing-30-day PL number at
once since that window is inside it. This is NOT a data-lag artifact
(checked: row counts per day stay ~23,000 straight through, data isn't
missing) and NOT a code bug (checked: the negative sum traces to real
negative `pl_billed_amount` rows, correctly kept per the doc's own "never
net off negative billed amounts" guardrail). Worth surfacing to whoever
owns sales data -- a swing this large and this synchronized across the
whole network in a 3-day window looks like either a real business event
(bulk return/correction batch) or worth a data-quality gut-check, and it
will keep dominating PL-driven planning until it rolls out of the trailing
30-day window naturally (around 2026-09-02).

## 2026-08-06 (newest) — `generate_se_plan --table` fixed to show all 15 columns

The `--table` flag added earlier the same day (`planning/management/commands/
generate_se_plan.py`) only printed 11 columns -- silently dropping Task
Type, Reason, Last Visit (date + days), Last Order (date + value), and Club
Participation. Fixed: `_print_table()` now prints all 15 of the doc's
Section 10 columns, plus a leading SE column (needed since one `PlanRun`
can span many SEs, unlike the doc's own per-SE table spec). Confirmed live
on a fresh Kota run -- all 15 fields populated correctly, including
previously-hidden real data (`Last_Visit_Date`, `Last_Order_Date`/`Value`).

## 2026-08-06 (newest) — Agent TUFF is now real infrastructure: `activate_tuff`

Replaces the manual "background-run the normalization CLI as a subprocess,
poll until it exits, ask 2 gate questions, then run `generate_se_plan`"
sequencing with one Django management command, run in-process:

```
python manage.py activate_tuff <SCOPE_TYPE> <SCOPE_VALUE> [--date YYYY-MM-DD] [--skip-normalization]
```

- **Step 1** calls `se_daily_plan_agent.run_pipeline()` directly (function
  call, not `subprocess`) -- no poll loop needed. Aborts with a
  `CommandError` before Step 2 if `DC_Master_Normalized` comes back with 0
  rows, rather than silently letting Step 2 plan against empty/failed
  normalization output. `--skip-normalization` reuses the existing
  `output/` for back-to-back runs on the same fresh data.
- **Step 2** calls `planning.services.generate_plan_for_scope()` -- same as
  `generate_se_plan` always has. No gate questions anymore (see Prompt 0,
  retired).
- **Outcome** is rendered by a new shared module, `planning/reporting.py`
  (`summary_lines()` / `table_lines()`), extracted out of
  `generate_se_plan`'s command so both `activate_tuff` and
  `generate_se_plan --table` print identically -- they can't drift apart on
  format now.

Confirmed live: a full Kota node run (Step 1 + Step 2 + outcome, no
`--skip-normalization`) and a Jaipur node run with `--skip-normalization`
both produced correct `PlanRun`s end-to-end in one command each.

## 2026-08-09 — DC Master supplemented from live roster (20 canonical nodes were fully missing)

User reported Metabase showing 113 nodes/11 states vs 93/12 from `DC_Master_Normalized.json`.
Root cause: DC_Master's Node/State come entirely from local `DC_RAnk.csv` (Source 2),
never synced to live data. The canonical node-state master is `input_node_mapping`
(Redshift `dev` db) -- `SELECT DISTINCT state, node FROM input_node_mapping WHERE
state <> 'State' AND state IS NOT NULL AND state <> ''` reproduces the user's Metabase
numbers exactly (113/11). DC_RAnk.csv is missing 20 whole canonical nodes across 9
states -- including `Agra` (explaining an earlier `activate_tuff NODE Agra` "no DCs
found" failure that wasn't a typo). Separately and much larger: 9,096 of 19,069 active
live DCs (48%) are missing even within nodes DC_RAnk.csv already covers -- user
explicitly scoped the fix to ONLY the 20 fully-missing nodes, not that full 48% gap
(would nearly double DC_Master's size on every run).

New `supplement_dc_master_from_live()` (`se_daily_plan_agent.py`), wired into
`run_pipeline()` right after Sources 1/3/4 load (needs the live Redshift client),
before cross-checks/`apply_dc_exclusion_rules()`:
- Queries `input_node_mapping` for the canonical node list, diffs against DC_Master's
  existing nodes to get the true missing set.
- Queries `input_partner_details` (`active='true'`) for the live DC roster, appends any
  DC whose `node_name` (case-normalized) is in that missing set and whose `DC_ID` isn't
  already present.
- Appended DCs get real Node/State/Assigned_SE_Email/geo but Rank/Cohort/Total_Score/
  NRV/GM/PL%/Avg_Repayment_Days/Credit_Score all stay `None`
  (`Total_Score_Unscored=True`) -- `DC_RAnk.csv` is the only source for those, so this
  is an honest degrade, not a fabricated rank. Every addition is individually flagged
  `DC_Supplemented_From_Live` in the exceptions report.

**Caught and fixed a real bug before calling this done**: a first version judged
"missing node" as "not already in `DC_Master`" -- `input_partner_details.node_name`
carries a lot of non-canonical junk (`AHMEDNAGAR_VIRTUAL`, `Do not Use`, `Frontier
market_Varanasi_Pindra`, etc.) that was never a real node, and that filter let all of
it through. A live test added 322 DCs across 63 "nodes," most junk. Fixed by
cross-referencing `input_node_mapping` as the truth for "real missing node" instead of
just "not in DC_Master" -- re-verified live: 187 DCs added, 0 junk labels, node count
93→100 (the remaining ~13 canonical nodes, e.g. `Sikar`/`Rajkot`, currently have zero
active live DCs, so correctly stay empty rather than being force-added). DC_Master row
count went 10,195→10,382, matching exactly.

## 2026-08-09 — PL_Expected's AOP-target leg wired via Node-level allocation (real code, not just docs)

Confirmed live investigation first: the AOP source (`Niyojan Q2-FY_26_27 Dashboard -
Planning.csv`, 106 columns) has **no DC/SE dimension anywhere** -- finest grain is
Node x Material/SKU x Month. The doc's own hoped-for schema (`'SE_ID/DC_ID, Month,
Metric, AOP_Target [FILL IN exact structure]'`) never matched reality. `load_aop_targets()`
also didn't capture any real numeric value before this fix -- only Node/Metric/Status
metadata, no GMV/target number at all, making `AOP_Target_Normalized` an unused stub
despite being loaded every run.

Fixed both problems:
1. `load_aop_targets()` now captures `Segment` (needed to isolate PRIVATE LABEL rows)
   and each row's real `GMV [Month] (AOP)` value (July/Aug/Sept 26 -- the only months
   this export covers), header-matched by normalized name (collapsed whitespace) not
   fixed index, same defensive style as the existing "Node Key" row-locator.
2. New `aop_pl_target_by_node(aop_targets, plan_date)` sums PRIVATE LABEL-segment GMV
   per Node for plan_date's month (falls back to quarterly total /3 outside Jul-Sep
   2026). Node names normalized (stripped+uppercased) before summing -- the raw export
   carries the same real node under inconsistent casing (both 'Alwar' and 'ALWAR' rows
   exist).
3. `planning/services.py`'s PL scoring block (the "wired 2026-08-06" per-DC BO1 flow)
   now blends TWO legs into `PL_Expected`: the original 90-day trailing average, and a
   new AOP-allocated leg = the DC's Node's PL AOP target x (DC's trailing-90d PL /
   Node's total trailing-90d PL across in-scope DCs). Combination is a simple average
   of whichever legs exist -- an engineering default, since 1.2's own combination
   method is still TBD in Source 5, unconfirmed either way. Every DC that gets the AOP
   leg has it flagged directly in `Reason_Of_Visit` ("AOP-allocated leg blended in...
   estimate, not a confirmed per-DC AOP figure") -- never blended in silently.

**Known, documented limitation, not fixed**: the allocation-share denominator (Node's
total trailing PL) is summed only across DCs in the current request's scope. Accurate
for NODE/STATE-scoped plans (already cover every DC under their nodes); an SE/BLOCK-
scoped plan doesn't see sibling DCs elsewhere in the same node, so its allocated leg
runs a bit high. Would need a node-wide DC query regardless of request scope to fix --
not done, since NODE/STATE is the common case this session.

Confirmed live 2026-08-09: forced a full normalization re-run (`--force-normalization`)
to regenerate `AOP_Target_Normalized.json` with the new fields, then a Jaipur NODE run
(`PlanRun #121`) -- 28 of 30 tasks got the AOP leg blended in; the 2 that didn't
degraded honestly (no PL AOP data for their Node, or zero trailing PL to allocate a
share from). All 30 still graded PL D -- expected, both legs draw from the same Aug 2-4
anomaly window, so blending adds a second real signal rather than rescuing the grade.

## 2026-08-09 — RedshiftDirectClient.execute_sql() now retries with backoff, not once unretried

Following the same-day incident where 2 manual normalization runs died repeatedly with
`SSL connection has been closed unexpectedly` (a NAT-idle-timeout-adjacent drop, fixed
at the connection level the same day via TCP keepalives + `statement_timeout`,
see the comments on `RedshiftDirectClient._connect`), `execute_sql()` itself still only
gave a dying query exactly one immediate, no-delay reconnect-and-retry -- adequate for a
one-off blip, not for the condition recurring across consecutive queries the way it did
that morning (log showed `died mid-run, reconnecting once` / `FAILED after Xs` cycling
across several different source queries in the same run).

Fixed: `execute_sql()` now shares the same `[0, 2, 6]`s backoff schedule as `_connect()`
(new module-level `REDSHIFT_RETRY_BACKOFF_SECONDS` constant, both methods reference it
instead of each hardcoding their own) -- up to 3 attempts per query, dropping the dead
connection and reconnecting before each retry, same as before but with 2 extra
backed-off attempts instead of 1 immediate one. Still only catches `psycopg2.
OperationalError` (connection-level) -- a bad query (`ProgrammingError` etc.) still
propagates immediately, unretried, same as always. Confirmed live 2026-08-09: a forced
full normalization re-run (`--force-normalization`) completed all 12 live queries
cleanly in ~32s with zero retries needed, confirming the change doesn't slow down or
break the already-working happy path.

## 2026-08-09 — `activate_tuff` now runs Step 1 (Data Normalization) at most once per UTC calendar day by default

```
python manage.py activate_tuff <SCOPE_TYPE> <SCOPE_VALUE> [--date YYYY-MM-DD]
                                [--force-normalization | --skip-normalization]
```

Previously every `activate_tuff` call re-ran the full live Step 1 pull
regardless of scope, even for back-to-back activations across different
nodes on the same day (e.g. Jaipur then Kota, minutes apart) -- wasteful
Redshift/Metabase load for identical data, and a real risk given the same-day
Redshift SSL instability seen this morning (see
[[tuff-normalization-hang-20260809]]). New default: `_todays_run_summary()`
reads `output/Run_Summary.json`'s `Run_Timestamp` and, if it's already from
today (UTC date match, same clock convention as `Run_Timestamp` itself and
Django's `TIME_ZONE='UTC'`), Step 1 is skipped automatically and the existing
`output/` is reused -- no flag needed. First activation of the day still runs
Step 1 for real and saves fresh data automatically, same as before.

Two explicit overrides for the two different reasons a caller might not want
the auto-skip:
- `--force-normalization` -- re-pull live data anyway even though today's
  run exists (e.g. known intraday upstream change). Mutually exclusive with
  `--skip-normalization`.
- `--skip-normalization` -- unconditionally skip Step 1 even if today's data
  is missing or stale, for a caller who's already verified freshness itself
  (unchanged from before, just no longer the only way to skip).

Confirmed live 2026-08-09: Kota activated twice minutes apart (`PlanRun
#113` with a real Step 1 run, `PlanRun #114` right after) -- the second call
printed `Step 1 skipped -- today's normalization already ran at
<timestamp>` and returned Step 2 + Pitching output in seconds instead of
re-running the ~15s-to-20min live pull.

Also confirmed: the Pitching Agent (`planning/pitching.py`,
`generate_pitches_for_plan_run()`) already auto-triggers inside Step 2 right
after `DailyTask` rows are created (`planning/services.py` ~line 1004) -- so
the "Normalization → SE Daily Task → Pitching" 3-agent sequence the user
asked for was already the real behavior of Step 2, nothing new needed there.

## 2026-08-06 (newest) — Last Payment Date's 90-day lookback removed, was hiding real data

Investigated why a Kota node run showed `Last Payment: N/A` on 4 of 5
tasks. Spot-checked all 4 DCs directly -- every one had real `SUCCESS`
payment history, just older than the 90-day window
`planning/services.py`'s `_sql_payments()` was filtering on (e.g. most
recent payment 100-300 days ago). Confirmed this was inconsistent with
`_sql_orders()`, which has never had a lookback filter and has always
correctly shown `Last_Order_Date` values from many months back. Fixed:
removed the `lookback_days` filter from `_sql_payments()` -- it's already
scoped to a handful of specific `dc_ids` (unlike the CLI's full-network
`SQL_PAYMENTS_3F`/`Payments_Normalized`, which still needs the window for
performance/scope reasons and was NOT changed), so there's no cost to
finding the true most recent payment. Confirmed live: all 4 previously-N/A
rows now show their real dates (2026-01-15, 2026-04-13, 2026-04-28,
2025-10-16), matching the DB exactly.

## 2026-08-06 (newest) — Overdue aging bucket added (NOT an exact overdue-since date)

User asked for "overdue date and days" alongside the Overdue amount.
Checked `dc_datamart`'s full live schema (49 columns) before building
anything -- confirmed there is NO due-date, overdue-since-date, or exact
days-overdue column anywhere in it. What IS real: `os_1_to_90` and
`os_90_plus`, two aggregate amount buckets (part of the same aging split
3.2/3.3 describe, just collapsed to what's actually available). Rather than
inventing a fake exact date/day-count, added a new field,
`DailyTaskRow.Overdue_Aging_Bucket` / `DailyTask.overdue_aging_bucket`
(new migration `0002_dailytask_overdue_aging_bucket`), derived as: `os_90_plus
> 0` -> "90+ days"; `os_1_to_90 > 0` -> "1-90 days"; `Current_Overdue > 0`
(neither bucket populated) -> "Current month"; else `None`. Shown in the
outcome table as `Overdue (Aging)`, e.g. `60,312 (90+ days)`.

**If a literal overdue-since date or exact day count is genuinely needed
later**, that requires a due-date field this pipeline doesn't have access
to yet -- would need someone who knows the schema to point at a real
source, not a follow-up code change.

## Known issues to carry forward (as of this repo's last check, 2026-08-04)

- PL, Sales, Liquidation, and full Long-Term (BO1/BO4/BO5) scoring aren't
  wired to live data yet — they need Sales_Transactions and AOP targets
  joined per DC, which wasn't validated end-to-end. Liquidation additionally
  has no confirmed scoring formula anywhere in Source 5 at all (user-added
  6th objective, see [[ask-custom-config-before-planning]]).
- 6.4 Credit_Blocked is now implemented (flagged via `sale_orderrequest.
  credit_on_hold`/`credit_on_hold_reason`, confirmed queryable) — but only the
  automatic Legal_Hold exclusion is a hard block; Credit_Blocked is surfaced
  as a flag on the DC row (`Credit_On_Hold`/`Credit_On_Hold_Reason`), not
  auto-excluded, matching the doc's "case-by-case, not auto-blocked" wording.
  Blacklisted status specifically still has no identified data source.
- `payments_paymenttransaction.customer_id` → `partner_id` join key is NOT
  proven — every `Last_Payment_Date` carries `Last_Payment_Join_Key_Unconfirmed:
  true` until someone directly validates this join.
- **NEW TRAP, confirmed live 2026-08-04, not in the doc at all**:
  `sale_orderrequest.partner_id` is the INTERNAL `customer_management_customer.id`,
  NOT `sap_partner_id` directly — same indirection the doc already documents
  for `task_management_task.partner_id`. A direct `sap_partner_id` filter on
  `sale_orderrequest` silently returns zero rows (confirmed: 106-DC query
  returned 0 rows filtered directly, then real orders appeared once bridged
  through `customer_management_customer`). `SQL_ORDERS_3D` in the code now
  joins through `customer_management_customer` — do not revert this to a
  direct filter.
- DC Club enrollment is presence-in-table, not an explicit status flag —
  `dc_mapping_club_scheme` has no `is_member`/status column; treat
  `DC_Club_Participation` as a plausible-not-confirmed read. Related tables
  (`dc_club_fy26_27_rbm_wise`, `dc_club_fy26_27_state_wise`,
  `dc_club_priority_delivery`, `club_scheme_eligible_products`) haven't been
  checked for a more direct per-DC status field.
- Distance sequencing is haversine from the SE's punch-in point (Section
  3a), not a real routing engine — the doc leaves open whether
  `attendance_attendance`'s own `google_distance`/OSRM-matched fields should
  be used instead (may reflect real road distance already). Treat
  `Travel.Cap_Exceeded` as a flag to review, not a hard stop, either way.
- YTD Private Label (column 14): `generate_se_daily_plan()` itself still
  won't silently compute it — `ytd_pl_by_dc` must be supplied by the caller.
  As of 2026-08-06 the Django endpoint (`planning/services.py`) DOES supply
  it live: `_sql_ytd_pl()` sums `pathik_report.pl_billed_amount` (confirmed
  columns: `sap_partner_id`, `transaction_date`, `pl_billed_amount`) from
  Indian-FY start (Apr 1) to `plan_date`, per DC, scoped to the run's DC
  list. Confirmed live: 54/54 tasks populated on a Jaipur node test run. Note
  `coupon_analysis` (Sales_Transactions_3d) is NOT the source for this column
  despite also carrying PL-tagged rows — its confirmed schema has no DC-level
  join key. The standalone CLI (`se_daily_plan_agent.py run_pipeline()`)
  still does NOT pass `ytd_pl_by_dc` — this fix is Django-endpoint-only so
  far.
- Distance sequencing: `planning/services.py` now also pulls real punch-in
  coordinates (`_sql_punch_in()`, `attendance_attendance.check_in_latitude/
  longitude`, earliest check-in of `plan_date` per SE) and passes them as
  `punch_in_coords`, same as the CLI already did. Caveat confirmed live: for
  a forward-dated plan (planning for a date with no attendance rows yet —
  the most recent check-in the SE has on record is `plan_date - 1` or
  earlier), this resolves to nothing and silently falls back to
  haversine-from-first-DC, same as before the fix — this is correct
  behavior, not a bug, and will self-resolve once the SE actually punches in
  that day. Separately, some DCs show `Distance: N/A` end-to-end regardless
  of punch-in — that's a distinct, still-open gap: those DCs are missing
  `lat_2`/`long_2` in `input_partner_details`, so no haversine leg can be
  computed at all, not even DC-to-DC.
