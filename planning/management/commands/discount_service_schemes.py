import json

from django.core.management.base import BaseCommand

from planning.discount_service import get_active_discount_schemes


class Command(BaseCommand):
    """Standalone CLI to fetch and preview live Discount Service (coupon-service.api.
    agrevolution.in) active schemes -- NOT wired into plan generation. See
    planning.discount_service's own docstring: this is a "supplement, don't replace"
    building block for the existing Redshift-based _sql_scheme_description_cards()
    (which already reads the same live coupon_service database plus richer joins this
    REST API doesn't expose). Useful to sanity-check the live API, preview
    generated_description output, or cross-check active scheme counts against the SQL
    version -- not to change what the pitch/DC card pipeline sees."""

    help = "Fetch active schemes from the live Discount Service API (coupon-service.api.agrevolution.in) -- standalone preview, not wired into plan generation."

    def add_arguments(self, parser):
        parser.add_argument("--resolve-names", action="store_true", help="Also resolve node/state IDs to names via a live Redshift lookup (slower)")
        parser.add_argument("--json", action="store_true", help="Print the raw result dict as JSON instead of a summary")
        parser.add_argument("--limit", type=int, default=10, help="How many schemes to print in the summary view (default 10)")

    def handle(self, *args, **options):
        result = get_active_discount_schemes(resolve_names=options["resolve_names"])

        if options["json"]:
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        if result["exceptions"]:
            self.stdout.write(self.style.WARNING(f"Exceptions ({len(result['exceptions'])}):"))
            for e in result["exceptions"]:
                self.stdout.write(f"  - [{e['source']}] {e['reason_code']}: {e['detail']}")

        schemes = result["schemes"]
        self.stdout.write(self.style.SUCCESS(f"{len(schemes)} active schemes fetched from the live Discount Service API"))
        for s in schemes[: options["limit"]]:
            self.stdout.write(f"\n[{s['scheme_id']}] {s['scheme_name']} ({s['scheme_type']})")
            self.stdout.write(f"  {s['generated_description']}")
            if s["node_names_raw"] or s["state_names_raw"]:
                self.stdout.write(f"  Location: nodes={s['node_names_raw']} states={s['state_names_raw']}")
