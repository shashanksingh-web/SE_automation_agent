from django.core.management.base import BaseCommand, CommandError

from planning.models import RoutePlan
from planning.routing import RoutingError, list_route_plans, select_default_route_plan


class Command(BaseCommand):
    """Ops-CLI stand-in for the Routing Agent's R5.3 ("the SE selects the final plan")
    -- there's no SE-facing mobile app in this repo to make that choice from yet, so
    this is the trust-equivalent of activate_tuff: whoever runs it is acting on the SE's
    behalf. With no --select, lists the >=3 synced RoutePlans for an SE/day (R5.2's
    presentation fields); --select flips is_default_selected to the chosen one and
    re-syncs DailyTask rows from its stops, so Pitching Agent / reporting / the API
    immediately reflect the new choice without needing to know a selection happened (see
    planning.routing's resync_daily_tasks_from_selected_plan docstring for a known
    limitation on re-synced row richness).

    All lookup/selection logic lives in planning.routing (list_route_plans /
    select_default_route_plan) -- this command is a thin wrapper so
    GET /api/planning/routes/<se>/<plan_date>/[select/<plan_type>/] (same underlying
    calls) can never drift from what this CLI does."""

    help = "List or select among an SE's synced route plans for a given day (Routing Agent R5.3)."

    def add_arguments(self, parser):
        parser.add_argument("--se", required=True, help="SE email or SE_ID, matching RoutePlan.se_id/se_name")
        parser.add_argument("--date", required=True, help="Plan date YYYY-MM-DD")
        parser.add_argument("--plan-run", type=int, default=None, help="Disambiguate by PlanRun id (default: most recent PlanRun with routes for this SE/date)")
        parser.add_argument("--select", default=None, choices=[c[0] for c in RoutePlan.PlanType.choices], help="Plan type to select as the default (e.g. DISTANCE_MIN)")

    def handle(self, *args, **options):
        try:
            if options["select"]:
                result = select_default_route_plan(options["se"], options["date"], options["select"], options["plan_run"])
                self.stdout.write(self.style.SUCCESS(
                    f"Selected {result['selected']} for {options['se']} @ {options['date']} (PlanRun #{result['plan_run_id']}) "
                    f"-- {result['daily_tasks_resynced']} DailyTask row(s) re-synced."
                ))
                return

            result = list_route_plans(options["se"], options["date"], options["plan_run"])
        except RoutingError as e:
            raise CommandError(str(e))

        self.stdout.write(f"Route plans for {options['se']} @ {options['date']} (PlanRun #{result['plan_run_id']}):")
        for p in result["plans"]:
            marker = " *SELECTED*" if p["is_default_selected"] else ""
            self.stdout.write(
                f"  {p['plan_type']}{marker} -- {p['stop_count']} stops, "
                f"{p['total_distance_km']}km, {p['total_travel_minutes']}min travel + {p['total_visit_minutes']}min visit, "
                f"priority captured {p['priority_score_captured']}, feasible={p['feasible']}"
                + (f" ({p['infeasibility_reason']})" if p["infeasibility_reason"] else "")
            )
            for s in p["stops"]:
                self.stdout.write(f"      {s['sequence_no']}. {s['dc_id']} -- {s['purposes']} (+{s['distance_from_prev_km']}km, +{s['travel_time_from_prev_min']}min)")
            if p["dropped_dcs"]:
                dropped_str = ", ".join(f"{d['dc_id']} ({d['reason']})" for d in p["dropped_dcs"])
                self.stdout.write(f"      dropped: {dropped_str}")
