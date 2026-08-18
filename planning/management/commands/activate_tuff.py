from django.core.management.base import BaseCommand, CommandError

from planning.models import PlanRun
from planning.product_cohort import ProductCohortError, build_season_weeks, split_csv
from planning.reporting import summary_lines, table_lines
from planning.services import PlanningError, activate_tuff_scope, make_farmer_meeting_asker


class Command(BaseCommand):
    """Agent TUFF -- the full two-agent flow in one command, in-process (no subprocess,
    no poll loop): Step 1 runs the Data Normalization Agent, Step 2 runs the SE Daily
    Task Agent for the requested scope (which itself auto-triggers the Pitching Agent
    right after DailyTask rows are created -- see planning.services.generate_plan_for_scope
    and planning/pitching.py), Step 3 prints the outcome table. Approved design
    2026-08-06, replacing the previous manual 2-command + background-process + wait-loop
    sequencing. See AGENT_OPERATING_PROMPTS.md and the reference_tuff_agent_name /
    ask-custom-config-before-planning memories for the full history -- the custom-config
    gate that used to sit between steps 1 and 2 is retired (all 4 values are fixed in
    Source 5 now), so this is a straight 2-step pipeline, not 3.

    Step 1 default behavior (2026-08-09): Data Normalization only actually re-runs once
    per UTC calendar day -- if output/Run_Summary.json already has a Run_Timestamp from
    today, it's reused automatically and Step 1 is skipped, since re-pulling the same
    live sources repeatedly in one day wastes Redshift/Metabase load for no new data.
    Use --force-normalization to override this and pull fresh live data anyway (e.g. if
    you know upstream data changed intraday); --skip-normalization still unconditionally
    skips Step 1 even on stale/missing data, for callers who've already checked.

    All real orchestration logic (once-per-day dedup, abort-on-empty-DC_Master guard,
    Step 1 + Step 2 sequencing) lives in planning.services.activate_tuff_scope() -- this
    command is a thin wrapper so GET /api/planning/tuff/<scope_type>/<scope_value>/ (same
    underlying call) can never drift from what this CLI does."""

    help = "Activate Agent TUFF: run the Data Normalization Agent (once per day, auto-reused after) then the SE Daily Task + Pitching Agents for one scope."

    def add_arguments(self, parser):
        parser.add_argument("scope_type", choices=[c.value for c in PlanRun.ScopeType], help="SE, ABM, RBM, NODE, BLOCK, DISTRICT, or STATE")
        parser.add_argument("scope_value", help="e.g. an SE email, a node name, a state name, an ABM employee code, ...")
        parser.add_argument("--date", default=None, help="Plan date YYYY-MM-DD, used for BOTH steps (default: today)")
        parser.add_argument(
            "--skip-normalization", action="store_true",
            help="Unconditionally skip Step 1 and reuse whatever is in output/ now, even if stale -- for callers who've already verified freshness themselves",
        )
        parser.add_argument(
            "--force-normalization", action="store_true",
            help="Force Step 1 to re-run live even if today's normalization already ran earlier -- use when you know upstream data changed intraday",
        )
        parser.add_argument(
            "--confirm-farmer-meeting", action="append", default=[], metavar="EMAIL",
            help="Explicitly confirm a Farmer Meeting for this SE email today, no interactive terminal needed "
                 "(repeatable for multiple SEs). Applies even if the SE isn't FM_Urgency-flagged this run.",
        )
        parser.add_argument(
            "--focus-product", metavar="MATERIAL_ID", default=None,
            help="Also run Focus Product Campaign Targeting (Product Cohort API) for this materialId, "
                 "persisted against this same PlanRun. Requires PRODUCT_COHORT_SESSION/PRODUCT_COHORT_GO_ADMIN_SESSION "
                 "in the environment -- see Product _cohort/PRODUCT_COHORT_AUTH.md.",
        )
        parser.add_argument("--focus-node", default=None, help="Product Cohort node -- defaults to scope_value when scope_type is NODE, required otherwise")
        parser.add_argument("--focus-product-years", type=int, default=4)
        parser.add_argument("--focus-product-buildup-weeks", help="e.g. 14-20 -- Step 2B seed, omit to skip Step 2B")
        parser.add_argument("--focus-product-peak-week", type=int, help="Step 2B seed, omit to skip Step 2B")
        parser.add_argument("--focus-product-closure-weeks", help="e.g. 40-48 -- Step 2B seed, omit to skip Step 2B")
        parser.add_argument("--focus-product-outer-weeks", default="1-52", help="Step 2B seed window, default 1-52")
        parser.add_argument("--focus-product-crop-districts", help="comma-separated -- Step 3 input, omit to skip Step 3")
        parser.add_argument("--focus-product-related-products", help="comma-separated product names -- Step 3 input, omit to skip Step 3")

    def handle(self, *args, **options):
        try:
            season_weeks = build_season_weeks(
                options["focus_product_outer_weeks"], options["focus_product_buildup_weeks"],
                options["focus_product_peak_week"], options["focus_product_closure_weeks"],
            )
        except ProductCohortError as e:
            raise CommandError(str(e))

        try:
            plan_run, normalization_info = activate_tuff_scope(
                options["scope_type"], options["scope_value"], options["date"],
                force_normalization=options["force_normalization"],
                skip_normalization=options["skip_normalization"],
                farmer_meeting_asker=make_farmer_meeting_asker(self.stdout, self.style),
                farmer_meeting_confirmed_emails=set(options["confirm_farmer_meeting"]),
                focus_product_material_id=options["focus_product"], focus_product_node_id=options["focus_node"],
                focus_product_years=options["focus_product_years"], focus_product_season_weeks=season_weeks,
                focus_product_crop_districts=split_csv(options["focus_product_crop_districts"]),
                focus_product_related_products=split_csv(options["focus_product_related_products"]),
            )
        except PlanningError as e:
            raise CommandError(str(e))

        if normalization_info["Skipped"]:
            self.stdout.write(self.style.WARNING("=== TUFF Step 1 skipped (--skip-normalization) -- reusing existing output/ as-is ==="))
        elif normalization_info["Reused"]:
            self.stdout.write(self.style.WARNING(
                f"=== TUFF Step 1 skipped -- today's normalization already ran at "
                f"{normalization_info['Run_Timestamp']}, reusing output/ (pass --force-normalization to override) ==="
            ))
            self.stdout.write(f"  Row_Counts: {normalization_info['Row_Counts']}")
        else:
            self.stdout.write(self.style.SUCCESS("=== TUFF Step 1: Data Normalization Agent ==="))
            self.stdout.write(f"  {normalization_info['Note']}")
            self.stdout.write(f"  Row_Counts: {normalization_info['Row_Counts']}")
            fails = {k: v for k, v in normalization_info["Check_Summary"].items() if v.get("fail")}
            if fails:
                self.stdout.write(f"  Exceptions (fail counts): {fails}")
        self.stdout.write("")

        self.stdout.write(self.style.SUCCESS("=== TUFF Step 2: SE Daily Task Agent ==="))
        lines = summary_lines(plan_run)
        self.stdout.write(self.style.SUCCESS(lines[0]))
        for line in lines[1:]:
            self.stdout.write(line)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== TUFF Outcome ==="))
        for line in table_lines(plan_run):
            self.stdout.write(line)

        for fp in plan_run.focus_product_targets.all():
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(f"=== Focus Product Campaign Targeting -- {fp.material_id} @ {fp.node_id} ==="))
            for step_name, val in (("Step 2A", fp.step_2a), ("Step 2B", fp.step_2b), ("Step 3", fp.step_3)):
                self.stdout.write(f"  {step_name}: {'(skipped/failed -- see Exceptions above)' if val is None else 'received, see FocusProductTargetRun #' + str(fp.id)}")
