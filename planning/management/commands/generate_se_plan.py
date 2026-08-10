import json

from django.core.management.base import BaseCommand, CommandError

from planning.models import PlanRun
from planning.reporting import summary_lines, table_lines
from planning.services import PlanningError, generate_plan_for_scope, make_farmer_meeting_asker
from planning.views import _serialize_plan_run


class Command(BaseCommand):
    """SE Daily Task Agent, as a standalone command -- no dev server needed. Mirrors
    `python se_daily_plan_agent.py` (the Data Normalization Agent's own CLI) so both
    agents can be run the same simple way. Wraps planning.services.generate_plan_for_scope()
    directly; see AGENT_OPERATING_PROMPTS.md Prompt 2/3 for what this does and doesn't
    guarantee. Also used as the second half of `activate_tuff` -- see that command for
    the combined normalize-then-plan flow."""

    help = "Run the SE Daily Task Agent for one scope (SE/ABM/Node/Block/District/State) and save the result."

    def add_arguments(self, parser):
        parser.add_argument("scope_type", choices=[c.value for c in PlanRun.ScopeType], help="SE, ABM, NODE, BLOCK, DISTRICT, or STATE")
        parser.add_argument("scope_value", help="e.g. an SE email, a node name, a state name, an ABM employee code, ...")
        parser.add_argument("--date", default=None, help="Plan date YYYY-MM-DD (default: today)")
        parser.add_argument("--json", action="store_true", help="Print the full plan as JSON instead of a summary")
        parser.add_argument("--table", action="store_true", help="Print the full outcome as one fixed-width plain-text table (one command, one shot -- the default reporting format)")
        parser.add_argument(
            "--confirm-farmer-meeting", action="append", default=[], metavar="EMAIL",
            help="Explicitly confirm a Farmer Meeting for this SE email today, no interactive terminal needed "
                 "(repeatable for multiple SEs). Applies even if the SE isn't FM_Urgency-flagged this run.",
        )

    def handle(self, *args, **options):
        try:
            plan_run = generate_plan_for_scope(
                options["scope_type"], options["scope_value"], options["date"],
                farmer_meeting_asker=make_farmer_meeting_asker(self.stdout, self.style),
                farmer_meeting_confirmed_emails=set(options["confirm_farmer_meeting"]),
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

        if options["table"]:
            self.stdout.write("")
            for line in table_lines(plan_run):
                self.stdout.write(line)
