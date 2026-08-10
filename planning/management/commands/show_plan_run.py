from django.core.management.base import BaseCommand, CommandError

from planning.models import PlanRun
from planning.reporting import summary_lines, table_lines


class Command(BaseCommand):
    """Read-only PlanRun inspection -- built to answer the two questions that otherwise
    needed an ad-hoc `python3 -c` ORM script every time: which SEs in this run got 0
    tasks, and what pitch scripts came out of it. Deliberately scoped narrow (one
    PlanRun, no mutation) so it's safe to allowlist as its own Bash command, unlike a
    general-purpose Python/Django shell escape hatch.

    PlanRun.skipped_ses (wired 2026-08-08, migration 0006) persists why each 0-task SE
    was skipped -- before that, an SE gated to 0 tasks left no DailyTask row and no
    other trace, so Skipped_Reason was computed in-memory at generation time and lost
    the moment the request finished. PlanRuns created before that migration will show
    an empty skipped_ses regardless of whether anyone was actually skipped -- for those,
    only the se_count-vs-tasks gap is knowable, not who or why."""

    help = "Show a PlanRun's summary, outcome table, and (optionally) pitch scripts -- read-only."

    def add_arguments(self, parser):
        parser.add_argument("plan_run_id", type=int)
        parser.add_argument("--pitches", action="store_true", help="Also print each task's pitch script (Hindi)")

    def handle(self, *args, **options):
        try:
            plan_run = PlanRun.objects.get(id=options["plan_run_id"])
        except PlanRun.DoesNotExist:
            raise CommandError(f"PlanRun #{options['plan_run_id']} not found.")

        for line in summary_lines(plan_run):
            self.stdout.write(line)

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== Outcome ==="))
        for line in table_lines(plan_run):
            self.stdout.write(line)

        se_task_counts = {}
        for t in plan_run.tasks.all():
            se_task_counts[t.se_name or t.se_id] = se_task_counts.get(t.se_name or t.se_id, 0) + 1
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=== SEs ==="))
        self.stdout.write(f"  {plan_run.se_count} SE(s) in scope, {len(se_task_counts)} got at least 1 task")
        for se, count in sorted(se_task_counts.items()):
            self.stdout.write(f"    {se}: {count} task(s)")
        if plan_run.skipped_ses:
            for s in plan_run.skipped_ses:
                self.stdout.write(self.style.WARNING(f"    {s.get('se_email') or s.get('se_id')}: 0 tasks -- {s.get('reason')}"))
        else:
            missing = plan_run.se_count - len(se_task_counts)
            if missing > 0:
                self.stdout.write(self.style.WARNING(
                    f"    {missing} SE(s) in scope got 0 tasks -- not individually listed, since this "
                    "PlanRun predates skipped_ses (migration 0006). Re-run to capture who/why."
                ))

        if options["pitches"]:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("=== Pitch Scripts ==="))
            tasks = plan_run.tasks.select_related("pitch").order_by("se_id", "sr_no")
            any_pitch = False
            for t in tasks:
                pitch = getattr(t, "pitch", None)
                if not pitch:
                    continue
                any_pitch = True
                self.stdout.write("-" * 70)
                self.stdout.write(f"{(t.se_name or t.se_id).split('@')[0]} Sr {t.sr_no}: {t.dc_name} -- {t.purpose_of_visit}")
                self.stdout.write(f"used: {pitch.data_sources_used}")
                self.stdout.write(f"skipped: {pitch.data_sources_skipped}")
                self.stdout.write("")
                self.stdout.write(pitch.script_hindi)
            if not any_pitch:
                self.stdout.write("  (no pitch scripts on this run -- only DC-tied tasks get one; Farmer Meeting tasks don't)")
