---
name: tuff-agent
description: Orient on the SE Automation Server / Agent TUFF codebase before making changes - architecture, the 7 agents, how to run things, and the hard-won conventions this project's history has repeatedly re-learned. Use when starting work in this repo, when asked to "activate TUFF" or "activate agent TUFF", or for anything touching the Data Normalization Agent, SE Daily Task Agent, Routing Agent (Plan A/B, Beat Planning), Pitching, DC Card, Product Cohort, or Outcome Reconciliation.
---

# Agent TUFF / SE Automation Server

Built from `git log --reverse` (31 commits, 2026-08-10 -> present) plus the two
canonical docs below. Read this first; go to the canonical docs for depth, not
back through git history.

**Canonical docs, in order of authority:**
1. `SE_DC_Data_Normalization_Agent_Prompt.docx` — the actual business spec. Gets
   re-exported mid-project with real structural changes more than once — treat
   "the doc updated" as a signal to re-diff, not assume it's cosmetic.
2. `Routing_agent/Routing_Agent_Configuration_Sheet_v8.xlsx` and
   `Routing_agent/Beat_Planning_Routing_Agent_Cluster_Model.xlsx` — Plan A and
   Plan B's own specs respectively. Same re-export risk as above.
3. `AGENT_OPERATING_PROMPTS.md` — deep, dated, prompt-level session log (927+
   lines). The "how we got here and why" record.
4. `README.md` — agent inventory, entry points, live data source map. Current
   as of 2026-08-19; **the Routing Agent section predates Plan A/B and is
   stale** — trust this skill + the code over that one section specifically.

## What this is

**Agent TUFF** = Data Normalization Agent (Step 1) + SE Daily Task Agent (Step
2), the two-step core. 7 agents total, 1 orchestration tier, 4 ops utilities —
see README.md's "Agents — count & entry points" for the full inventory and
exact CLI/HTTP entry points per agent. Don't re-derive that table here; it's
already accurate and current.

- `se_daily_plan_agent.py` — the standalone core engine (Sections 1-11 of the
  spec doc). Never depends on Django. This is what must run bare.
- `planning/` — the Django app wrapping it: `services.py` (orchestration),
  `routing.py` (Routing Agent), `pitching.py`, `dc_card.py`,
  `product_cohort.py`, `models.py`, `views.py`.
- **Activation:** `python manage.py activate_tuff {SE,ABM,RBM,NODE,BLOCK,DISTRICT,STATE} <value> [flags]`
  or `GET /api/planning/tuff/<scope_type>/<scope_value>/` — Step 1 auto-skips
  if already run today (`--force-normalization` to override, never manually
  pass `--skip-normalization` unless you've independently verified freshness).

## The Routing Agent: Plan A vs Plan B

- **Plan A** (default, unattended-safe): Models 1-3 (Priority-Max/Distance-Min/
  Balanced), OR-Tools + Clarke-Wright. `--routing-plan A` / `?routing_plan=A`.
- **Plan B** (`Beat_Planning_Routing_Agent_Cluster_Model.xlsx`): density
  clustering -> Cumulative BO Score -> greedy score-per-km selection under an
  80km/180min budget. `--routing-plan B` / `?routing_plan=B`. Never
  auto-selected — the source doc itself says the Plan A->B trigger condition
  is "not yet confirmed."
  - **Conditional Ceiling Model**: a cluster exceeding 80km/180min isn't
    dropped — it's reclassified **Exceptional** and routed via the **BO Rule**
    (strict tier-priority sequencing) instead of efficiency ranking.
  - **3 routes per call**, not 1: Efficiency-Balanced / Score-Maximizing /
    Distance-Minimizing, mirroring Plan A's 3 models (`RoutePlan.PlanType`:
    `CLUSTER_BASED` / `CLUSTER_SCOREMAX` / `CLUSTER_DISTMIN`).
  - **max_daily_tasks (5) cap** applies here too, same as the legacy path.
  - **GR-R10 (plan distinctness)**: if all 3 routes (either plan family)
    produce the identical stop-set *and* sequence despite the pool having room
    to differ, that's `Plans_Converged`, distinct from `Insufficient_
    Candidates_For_3_Plans` (pool genuinely too thin). Don't conflate these —
    a real bug shipped once from not distinguishing "1 model succeeded, 2
    failed" from "all 3 genuinely agreed."
  - **Beat Cycle Rule** (`Sheet 11`, opt-in, Plan B only):
    - **Repeat-Avoidance** (`--enable-rotation` not needed, always on for Plan
      B): a DC visited in the last `PLAN_B_COOLDOWN_DAYS` (2) days is
      excluded, with an escape hatch if that would empty the route.
    - **Fixed Rotation** (`--enable-rotation` / `?rotation=true`): persisted
      `BeatZoneAssignment` (se_id, dc_id) -> zone, bootstrapped once from
      geography/density, never reclustered after — only extended for new DCs
      (nearest existing zone). Structural coverage guarantee the cool-down
      alone can't give (a DC that never wins the daily ranking has no
      cooldown history to block it, so it can starve forever under cool-down
      alone — confirmed live before this was built).

## Conventions this project has re-learned the hard way

Read these before touching anything — each one cost a real debugging session
at least once.

1. **Never resolve ambiguity silently.** The dominant bug class across this
   whole history is "resolved genuine ambiguity with zero signal" — a
   collision in `normalize_id`, a falsy-vs-missing bug in `photo_logged_from_
   task_details`, a source silently dropped from a pitch's data-sources list.
   Every one of these got fixed the same way: add an `Exceptions_Report`
   entry / a flagged reason code, never just quietly do the reasonable thing.
   When in doubt, flag it — don't guess.
2. **File re-export churn is routine, not an anomaly.** `pitch_config/*.csv`,
   `config and parameter/*.csv`, `Routing_agent/*`, and the root spec `.docx`
   all get silently re-exported by an external sync mid-project — old tracked
   filenames deleted, new ones land under `(1)`/`(2)` suffixes or entirely
   different names. The loaders resolve by glob/prefix match, not hardcoded
   exact filenames, specifically because of this. On any re-export: check
   column counts and diff content before assuming "just a rename" — twice
   this hid a real schema change (S2 -> S2a/S2b split; a join-key change).
3. **"Verified live" is not optional before calling something done.** Every
   substantive commit in this history ends with a real Redshift-backed
   confirmation (a specific PlanRun ID, a real DC, a before/after count) —
   not just "tests pass" or "looks right." Type checking / unit tests verify
   correctness of code, not correctness of the *feature* against real data.
4. **UAT synthetic scenarios for anything touching route/task selection
   logic**, run through the real orchestration path (`generate_route_plans_
   for_se`, not the algorithm function in isolation) so persistence/exception-
   reporting bugs surface too, not just algorithmic ones. Roll back the DB
   transaction after (`transaction.atomic()` + `set_rollback(True)`) so
   nothing pollutes real data. This caught multiple real bugs in this
   project (a false `Plans_Converged` positive, a `Travel_Floor_Not_Met`
   mislabel) that a purely algorithmic test would have missed.
5. **DC Rank<=6000 eligibility is an intentional policy, not a gap.**
   ~47-85% of the network (Long Tail cohort + unscored DCs) is deliberately
   excluded from all agents' selection. Don't "fix" this without checking —
   it's confirmed, cascades from one normalization-time flag, and is by
   design.
6. **Once-per-day normalization is load-bearing.** `activate_tuff` auto-skips
   Step 1 if it already ran today. Don't manually pass `--skip-normalization`
   as a shortcut — use `--force-normalization` if you genuinely need a fresh
   pull, and only skip if you've independently verified today's output is
   still fresh.
7. **Commit `output/*.json` + `logs/*.log` snapshots periodically**, matching
   the existing convention (several commits in this history do exactly this,
   e.g. after a full network run or a fix that changes normalization output)
   — but only when the user asks or it's clearly part of the requested work,
   not as a reflex on every session.
8. **Real per-DC live scoring exists for BO1 (PL), BO3 (Outstanding), BO4
   (Sales)** — don't assume these are still SE-level proxies; that was true
   early on and got fixed. BO5 (Long-Term) is correctly SE-level by design
   (routes through FM_Urgency, not `dc_bo_scores`), not a gap.
9. **The Pitching/DC Card agents fault-isolate per task** — one bad DC must
   never blank out every other task's pitch/card in the same run. If you
   touch either generator loop, preserve that isolation.
10. **Business config values (thresholds, alpha bounds, growth multipliers)
    that look like magic numbers are usually confirmed, not guessed** — check
    `BusinessConstants` field comments and the Decision Log sheets before
    assuming a number needs a source. Where something genuinely is a
    placeholder default, the code says so explicitly (e.g. `Avg_Speed_Kmph`,
    flagged as the single most important still-undefined number in the
    Routing Agent sheet).

## Quick reference

```
# Normalize + generate a plan for one scope (the standard entry point)
python manage.py activate_tuff SE <email> [--routing-plan A|B] [--enable-rotation] [-v 1]

# Same, via HTTP
GET /api/planning/tuff/SE/<email>/?routing_plan=B&rotation=true

# Standalone normalization only (no Django, Step 1 alone)
python se_daily_plan_agent.py [--date YYYY-MM-DD]

# Cron entry point (every active ScheduledScope)
python manage.py run_scheduled_tuff
```
