from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planning.headcount import compute_active_headcount_bifurcation


class Command(BaseCommand):
    """Read-only report over whatever output/*_Normalized.json the last normalization
    run produced -- see planning/headcount.py's module docstring for the exact
    resolution logic and its honest limitations (no standalone SE/ABM/RBM roster
    query exists in this pipeline, role membership isn't mutually exclusive, and
    Node/Block/District/State counts can double-count an SE whose DCs span more than
    one geo value)."""

    help = "Bifurcate active headcount by SE/ABM/RBM role and by Node/Block/District/State, plus an overall total."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="Also print each bucket's individual emails, not just counts")

    def handle(self, *args, **options):
        output_dir = Path(settings.SE_DAILY_PLAN_AGENT_PATH) / "output"
        try:
            result = compute_active_headcount_bifurcation(output_dir)
        except FileNotFoundError as e:
            raise CommandError(str(e))

        self.stdout.write(self.style.SUCCESS(f"=== Overall: {result['overall_total']} active accounts ==="))
        self.stdout.write("")

        self.stdout.write(self.style.SUCCESS("=== Role ==="))
        role_rows = [
            ("SE", result["se_role"]),
            ("ABM", result["abm_role"]),
            ("RBM", result["rbm_role"]),
            ("No role determined", result["no_role"]),
        ]
        self._table("Role", role_rows, options["list"])

        for label, key in [("Node", "by_node"), ("Block", "by_block"), ("District", "by_district"), ("State", "by_state")]:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS(f"=== {label} ==="))
            rows = sorted(result[key].items(), key=lambda kv: (-len(kv[1]), kv[0]))
            self._table(label, rows, options["list"])

    def _table(self, label_header: str, rows, show_list: bool):
        width = max([len(label_header)] + [len(str(r[0])) for r in rows]) if rows else len(label_header)
        self.stdout.write(f"  {label_header.ljust(width)}  Count")
        self.stdout.write(f"  {'-' * width}  -----")
        for label, emails in rows:
            self.stdout.write(f"  {str(label).ljust(width)}  {len(emails)}")
            if show_list:
                for e in emails:
                    self.stdout.write(f"      {e}")
