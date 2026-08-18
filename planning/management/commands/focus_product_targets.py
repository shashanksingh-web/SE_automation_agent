import json

from django.core.management.base import BaseCommand, CommandError

from planning.product_cohort import (
    ProductCohortError, build_season_weeks, get_focus_product_campaign_targets, split_csv,
)


class Command(BaseCommand):
    """CLI entry point for Focus Product Campaign Targeting (Product _cohort/), run
    standalone/ad-hoc -- no PlanRun involved, nothing persisted. For a Focus Product
    lookup tied to (and persisted against) a real PlanRun, use `activate_tuff`/
    `generate_se_plan`'s own --focus-product flag instead (see planning.services.
    generate_plan_for_scope), which stores the result as a FocusProductTargetRun.

    Requires PRODUCT_COHORT_SESSION / PRODUCT_COHORT_GO_ADMIN_SESSION to already be set
    in the environment -- this command never signs in to obtain them. See
    Product _cohort/PRODUCT_COHORT_AUTH.md for how to get a session via Postman."""

    help = "Look up Focus Product Campaign Targeting data (Product Cohort API) for one material at one node."

    def add_arguments(self, parser):
        parser.add_argument("--material-id", required=True)
        parser.add_argument("--node-id", required=True)
        parser.add_argument("--years", type=int, default=4)
        parser.add_argument("--buildup-weeks", help="e.g. 14-20 -- Step 2B seed, omit to skip Step 2B")
        parser.add_argument("--peak-week", type=int, help="Step 2B seed, omit to skip Step 2B")
        parser.add_argument("--closure-weeks", help="e.g. 40-48 -- Step 2B seed, omit to skip Step 2B")
        parser.add_argument("--outer-weeks", default="1-52", help="Step 2B seed window, default 1-52")
        parser.add_argument("--crop-districts", help="comma-separated, e.g. Khargone,Barwani -- Step 3 input, omit to skip Step 3")
        parser.add_argument("--related-products", help="comma-separated product names -- Step 3 input, omit to skip Step 3")
        parser.add_argument("--json", action="store_true", help="Print the raw result dict as JSON instead of a summary")

    def handle(self, *args, **options):
        try:
            season_weeks = build_season_weeks(options["outer_weeks"], options["buildup_weeks"], options["peak_week"], options["closure_weeks"])
            result = get_focus_product_campaign_targets(
                material_id=options["material_id"], node_id=options["node_id"], years=options["years"],
                season_weeks=season_weeks, crop_districts=split_csv(options["crop_districts"]),
                related_product_names=split_csv(options["related_products"]),
            )
        except ProductCohortError as e:
            raise CommandError(str(e))

        if options["json"]:
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        self.stdout.write(f"Focus Product Campaign Targeting -- material_id={result['Material_ID']} node_id={result['Node_ID']}")
        for step in ("Step_2A", "Step_2B", "Step_3"):
            val = result[step]
            self.stdout.write(f"  {step}: {'(skipped/failed -- see exceptions)' if val is None else 'received'}")
        if result["exceptions"]:
            self.stdout.write(self.style.WARNING(f"  Exceptions ({len(result['exceptions'])}):"))
            for e in result["exceptions"]:
                self.stdout.write(f"    - [{e['source']}] {e['reason_code']}: {e['detail']}")
        if result["Step_2A"] is not None:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Step 2A (historical purchase pattern):"))
            self.stdout.write(f"  ib_weekly_sales rows: {len(result['Step_2A'].get('ib_weekly_sales') or [])}")
            self.stdout.write(f"  ib_raw_records rows: {len(result['Step_2A'].get('ib_raw_records') or [])}")
        if result["Step_2B"] is not None:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Step 2B (raw, schema unconfirmed):"))
            self.stdout.write(f"  {json.dumps(result['Step_2B'], default=str)[:500]}")
        if result["Step_3"] is not None:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("Step 3 (raw, schema unconfirmed):"))
            self.stdout.write(f"  {json.dumps(result['Step_3'], default=str)[:500]}")
