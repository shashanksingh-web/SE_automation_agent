from django.core.management.base import BaseCommand
from django.utils import timezone

from planning.models import ScheduledScope
from planning.notify import send_alert
from planning.services import _output_dir, _todays_run_summary


class Command(BaseCommand):
    """Heartbeat check for TUFF's daily cron. Nothing in this pipeline previously noticed
    when the cron chain failed silently before its first Python line ran -- the
    documented 2026-08-17 case was a launchd-vs-crontab conflict under macOS TCC that
    made `cd` itself fail, so no log line and no send_alert call were ever reached; the
    only way anyone found out was by reading tuff.log by hand.

    This command has no dependency on that chain -- it only reads already-persisted
    state (Run_Summary.json, ScheduledScope.last_run_at), so a broken upstream cron
    invocation can't take it down too. It must be scheduled SEPARATELY from
    run_scheduled_tuff's own cron slot (e.g. 07:00, after that slot's 06:15) for the
    check to mean anything -- this command does not touch crontab/launchd itself."""

    help = "Alert if today's Data Normalization run or any active ScheduledScope's TUFF run is missing. Schedule separately from run_scheduled_tuff's own cron slot."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print findings without calling send_alert")

    def handle(self, *args, **options):
        problems = []

        summary = _todays_run_summary(_output_dir())
        if summary is None:
            problems.append("Data Normalization Agent (Step 1) has not run today -- no Run_Summary.json with today's Run_Timestamp.")

        today = timezone.now().date()
        stale_scopes = [
            s for s in ScheduledScope.objects.filter(active=True).order_by("scope_type", "scope_value")
            if s.last_run_at is None or s.last_run_at.date() != today
        ]
        if stale_scopes:
            names = ", ".join(f"{s.scope_type}={s.scope_value}" for s in stale_scopes[:10])
            more = f" (+{len(stale_scopes) - 10} more)" if len(stale_scopes) > 10 else ""
            problems.append(f"{len(stale_scopes)} active ScheduledScope(s) have not run today: {names}{more}")

        if not problems:
            self.stdout.write(self.style.SUCCESS("TUFF heartbeat OK -- Step 1 ran today and every active ScheduledScope has run today."))
            return

        message = "TUFF heartbeat check failed:\n" + "\n".join(f"- {p}" for p in problems)
        self.stdout.write(self.style.ERROR(message))
        if not options["dry_run"]:
            send_alert(message, severity="critical")
