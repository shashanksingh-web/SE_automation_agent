from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from planning.models import PlanRun


class Command(BaseCommand):
    """Soft-gate approval action for a PlanRun -- audit trail only. GET endpoints keep
    returning runs regardless of status; this just records that a human looked at one
    and marked it approved/rejected, per the orchestration-layer design (see
    AGENT_OPERATING_PROMPTS.md / the orchestration+feedback-loop plan)."""

    help = "Mark a PlanRun as approved (or rejected with --reject)."

    def add_arguments(self, parser):
        parser.add_argument("plan_run_id", type=int)
        parser.add_argument("--reject", action="store_true", help="Mark as REJECTED instead of APPROVED")
        parser.add_argument("--by", default="", help="Reviewer name/identifier, for the audit trail")

    def handle(self, *args, **options):
        try:
            plan_run = PlanRun.objects.get(id=options["plan_run_id"])
        except PlanRun.DoesNotExist:
            raise CommandError(f"PlanRun #{options['plan_run_id']} not found.")

        plan_run.status = PlanRun.Status.REJECTED if options["reject"] else PlanRun.Status.APPROVED
        plan_run.reviewed_by = options["by"]
        plan_run.reviewed_at = timezone.now()
        plan_run.save(update_fields=["status", "reviewed_by", "reviewed_at"])

        self.stdout.write(self.style.SUCCESS(f"PlanRun #{plan_run.id} -> {plan_run.status} (by {options['by'] or 'unspecified'})"))
