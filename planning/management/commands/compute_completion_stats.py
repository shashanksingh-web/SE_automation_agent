from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from planning.models import DailyTask, ObjectiveCompletionStats

MIN_SAMPLE_SIZE = 5  # must match se_daily_plan_agent.completion_multiplier's default


class Command(BaseCommand):
    """Feedback-loop Tier 2: rolls up a trailing-30d completion rate per (SE, objective)
    from DailyTask.outcome_status (populated by reconcile_outcomes), for BO1/BO3 scoring's
    adaptive weighting multiplier. COMPLETED counts as a full success, PARTIAL as half
    (a real business outcome happened -- an order -- without a confirmed field visit),
    MISSED as zero. Only rows already reconciled (outcome_status != UNKNOWN) count --
    never treated as MISSED just because reconciliation hasn't run yet for that day."""

    help = "Roll up trailing-30d (SE, objective) completion rates for adaptive BO1/BO3 weighting."

    def handle(self, *args, **options):
        today = timezone.now().date()
        window_start = (today - timedelta(days=30)).isoformat()

        rows = (
            DailyTask.objects
            .filter(plan_date__gte=window_start, plan_date__lt=today.isoformat())
            .exclude(outcome_status=DailyTask.OutcomeStatus.UNKNOWN)
            .values("se_id", "objective", "outcome_status")
        )

        # DailyTask.objective is a comma-joined bundle for visit-bundled tasks (e.g.
        # "Outstanding,Visits" -- one DC matching multiple objectives is one task, per
        # the Daily Task Assignment Formula's bundling rule). Split it so a completed
        # bundled visit counts toward EACH objective it was tagged with -- BO1/BO3
        # scoring looks up a single objective key ("Outstanding", "PL"), so grouping by
        # the raw combined string would starve that lookup of almost all real data.
        groups: dict = {}
        for r in rows:
            for objective in (r["objective"] or "").split(","):
                objective = objective.strip()
                if not objective:
                    continue
                key = (r["se_id"], objective)
                g = groups.setdefault(key, {"total": 0, "weighted": 0.0})
                g["total"] += 1
                if r["outcome_status"] == DailyTask.OutcomeStatus.COMPLETED:
                    g["weighted"] += 1.0
                elif r["outcome_status"] == DailyTask.OutcomeStatus.PARTIAL:
                    g["weighted"] += 0.5

        # Batched instead of one update_or_create() (SELECT+INSERT/UPDATE) per (SE,
        # objective) pair -- fetch every existing row for the SEs involved once, then
        # split into a single bulk_create() for new pairs and a single bulk_update() for
        # existing ones. bulk_update() doesn't apply auto_now, so computed_at is set
        # explicitly on every touched row.
        se_ids = sorted({se_id for se_id, _ in groups.keys()})
        existing = {(s.se_id, s.objective): s for s in ObjectiveCompletionStats.objects.filter(se_id__in=se_ids)}

        now = timezone.now()
        to_create = []
        to_update = []
        for (se_id, objective), g in groups.items():
            if not objective:
                continue
            rate = g["weighted"] / g["total"]
            row = existing.get((se_id, objective))
            if row is not None:
                row.completion_rate_30d = rate
                row.sample_size = g["total"]
                row.computed_at = now
                to_update.append(row)
            else:
                to_create.append(ObjectiveCompletionStats(
                    se_id=se_id, objective=objective, completion_rate_30d=rate, sample_size=g["total"], computed_at=now,
                ))

        if to_create:
            ObjectiveCompletionStats.objects.bulk_create(to_create, batch_size=500)
        if to_update:
            ObjectiveCompletionStats.objects.bulk_update(to_update, ["completion_rate_30d", "sample_size", "computed_at"], batch_size=500)
        updated = len(to_create) + len(to_update)

        below_min = sum(1 for g in groups.values() if g["total"] < MIN_SAMPLE_SIZE)
        self.stdout.write(self.style.SUCCESS(
            f"Updated {updated} (SE, objective) completion-stat row(s) from {sum(g['total'] for g in groups.values())} "
            f"reconciled tasks in the last 30 days ({below_min} below the min sample size of {MIN_SAMPLE_SIZE} -- "
            f"those stay neutral 1.0x per completion_multiplier)."
        ))
