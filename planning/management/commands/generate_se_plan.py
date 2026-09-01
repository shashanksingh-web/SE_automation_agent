import json

from django.core.management.base import BaseCommand, CommandError

from planning.models import PlanRun
from planning.product_cohort import ProductCohortError, build_season_weeks, split_csv
from planning.reporting import summary_lines, table_lines
from planning.services import PlanningError, generate_plan_for_scope, make_farmer_meeting_asker, make_routing_plan_asker
from planning.views import _serialize_plan_run


class Command(BaseCommand):
    """SE Daily Task Agent, as a standalone command -- no dev server needed. Mirrors
    `python se_daily_plan_agent.py` (the Data Normalization Agent's own CLI) so both
    agents can be run the same simple way. Wraps planning.services.generate_plan_for_scope()
    directly; see AGENT_OPERATING_PROMPTS.md Prompt 2/3 for what this does and doesn't
    guarantee. Also used as the second half of `activate_tuff` -- see that command for
    the combined normalize-then-plan flow."""

    help = "Run the SE Daily Task Agent for one scope (SE/ABM/RBM/Node/Block/District/State) and save the result."

    def add_arguments(self, parser):
        parser.add_argument("scope_type", choices=[c.value for c in PlanRun.ScopeType], help="SE, ABM, RBM, NODE, BLOCK, DISTRICT, or STATE")
        parser.add_argument("scope_value", help="e.g. an SE email, a node name, a state name, an ABM employee code, ...")
        parser.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today)")
        parser.add_argument("--json", action="store_true", help="Print the full plan as JSON instead of a summary")
        parser.add_argument("--table", action="store_true", help="Deprecated, no-op -- the outcome table now always prints (see reporting.table_lines()); kept only so existing callers passing this flag don't break")
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
        parser.add_argument(
            "--routing-plan", choices=["A", "B"], default=None,
            help="Which Routing Agent mode to run: A = Priority-Max/Distance-Min/Balanced (Models 1-3, default), "
                 "B = Beat Planning / Cluster-Based Model (Plan B). Omit in an interactive terminal to be asked; "
                 "omit under cron/scripting to default to Plan A (no auto-fallback -- see make_routing_plan_asker).",
        )
        parser.add_argument(
            "--enable-rotation", action="store_true",
            help="Plan B only -- Fixed Rotation (Beat_Planning_Routing_Agent_Cluster_Model.xlsx Sheet 11 Model B). "
                 "Restricts each SE to whichever persisted beat-zone is on for this date before the usual "
                 "ranking/budget logic runs. Off by default (no effect on Plan A or on Plan B without this flag).",
        )

    def handle(self, *args, **options):
        try:
            season_weeks = build_season_weeks(
                options["focus_product_outer_weeks"], options["focus_product_buildup_weeks"],
                options["focus_product_peak_week"], options["focus_product_closure_weeks"],
            )
        except ProductCohortError as e:
            raise CommandError(str(e))

        try:
            plan_run = generate_plan_for_scope(
                options["scope_type"], options["scope_value"], options["date"],
                farmer_meeting_asker=make_farmer_meeting_asker(self.stdout, self.style),
                farmer_meeting_confirmed_emails=set(options["confirm_farmer_meeting"]),
                focus_product_material_id=options["focus_product"], focus_product_node_id=options["focus_node"],
                focus_product_years=options["focus_product_years"], focus_product_season_weeks=season_weeks,
                focus_product_crop_districts=split_csv(options["focus_product_crop_districts"]),
                focus_product_related_products=split_csv(options["focus_product_related_products"]),
                routing_plan_asker=make_routing_plan_asker(self.stdout, self.style),
                routing_plan_choice=options["routing_plan"],
                enable_rotation=options["enable_rotation"],
            )
        except PlanningError as e:
            raise CommandError(str(e))

        if options["json"]:
            self.stdout.write(json.dumps(_serialize_plan_run(plan_run), indent=2, default=str))
            return

        lines = summary_lines(plan_run)
        self.stdout.write(self.style.SUCCESS(lines[0]))
        for line in lines[1:]:
            self.stdout.write(line)

        # Always shown, matching activate_tuff.py's Step 2 output -- the 15-column
        # outcome table is the project's default reporting format, not opt-in.
        self.stdout.write("")
        for line in table_lines(plan_run):
            self.stdout.write(line)
