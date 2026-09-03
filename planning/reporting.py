"""Shared plain-text reporting for PlanRun outcomes -- used by both
generate_se_plan --table and activate_tuff, so the two commands can never drift
apart on what the "outcome" actually looks like.
"""

from __future__ import annotations

from typing import List

from .models import PlanRun


def summary_lines(plan_run: PlanRun) -> List[str]:
    """The header block: PlanRun identity, counts, note, exceptions (first 10)."""
    lines = [
        f"PlanRun #{plan_run.id}: {plan_run.scope_type}={plan_run.scope_value!r} on {plan_run.plan_date}",
        f"  Metabase_Configured: {plan_run.metabase_configured}",
        f"  SEs: {plan_run.se_count}   DCs: {plan_run.dc_count}   Tasks: {plan_run.task_count}",
    ]
    if plan_run.note:
        lines.append(f"  Note: {plan_run.note}")
    if plan_run.skipped_ses:
        lines.append(f"  Skipped SEs ({len(plan_run.skipped_ses)}, 0 tasks each):")
        for s in plan_run.skipped_ses:
            lines.append(f"    - {s.get('se_email') or s.get('se_id')}: {s.get('reason')}")
            breakdown = s.get("dc_breakdown")
            if breakdown:
                not_in_scope = breakdown.get("not_in_scope") or []
                no_match = breakdown.get("in_scope_no_objective_match") or []
                lines.append(
                    f"        {breakdown.get('total_assigned_dcs', 0)} DC(s) assigned -- "
                    f"{len(not_in_scope)} not in scope, {len(no_match)} in-scope but matched no objective"
                )
                # The in-scope-but-unmatched ones are the actionable ones -- a DC one
                # condition away from qualifying, not a structural Rank/scope problem.
                for dc in no_match[:3]:
                    lines.append(f"          closest: {dc['DC_Name']} ({dc['DC_ID']}) -- {dc['Visits']} | {dc['Outstanding']} | {dc['PL']}")
                if len(no_match) > 3:
                    lines.append(f"          ... and {len(no_match) - 3} more (use --json or /api/planning/runs/{plan_run.id}/ to see all)")
    exc_count = plan_run.exceptions.count()
    lines.append(f"  Exceptions: {exc_count}")
    for e in plan_run.exceptions.all()[:10]:
        lines.append(f"    - [{e.source}] {e.reason_code}: {e.detail}")
    if exc_count > 10:
        lines.append(f"    ... and {exc_count - 10} more (use --json or /api/planning/runs/{plan_run.id}/ to see all)")
    return lines


def table_lines(plan_run: PlanRun) -> List[str]:
    """14 of the doc's Section 10 columns (Sr, DC Name, Distance, Task Type, Purpose,
    Reason, Last Visit+days, Outstanding, Overdue, Last Order date+value, Last Payment,
    YTD PL, Club Participation), plus a leading SE column since one PlanRun spans
    multiple SEs. DC ID (doc column 3) is deliberately omitted per explicit request,
    dropped 2026-08-07 to narrow the table -- still stored on DailyTask.dc_id, just not
    shown here. Club Participation was ALSO dropped 2026-08-07 (same narrowing request)
    but re-added 2026-08-13 once normalize_dc_club() started computing a real Club_Tier/
    Zone/TOD_Percent instead of just an enrollment-proxy string -- worth a column now
    that it's not just "Presence_In_Mapping_Table_Unconfirmed" on every row. This is the
    single source of truth for the outcome table -- generate_se_plan --table and
    activate_tuff both render through this function.

    Critical column added 2026-08-18, placed right after Sr so it's visible without
    scrolling right -- non-empty (the specific reason(s): chronic miss escalation, 90+
    day aged overdue, or credit-on-hold) marks a task as "cover this one first," per
    DailyTaskRow.Critical. Empty for every non-critical task, not a fixed-width flag
    column, so it doesn't demand attention when there's nothing to flag.

    Finance column added 2026-09-03 (DailyTaskRow.Finance_Status, sale_orderrequest.
    partner_finance_status from the DC's most recent order) -- shows "Financed" /
    "Non-Financed" / "Unknown" (no order with this field populated, not assumed
    Non-Financed). Confirmed live this genuinely changes per DC over time, not a static
    attribute, so it's shown per-task/per-run rather than treated as a fixed label.

    BO Scores column added 2026-09-03 (DailyTaskRow.BO_Scores) -- compact grade-only
    summary ("PL:C Outstanding:A") of every objective actually scored for this DC, not
    just the ones that matched/qualified it (e.g. Sales/BO4 is scored but deliberately
    excluded from selection per 8.12/GR-25 -- its grade is still shown here if computed).
    A grade of None (score genuinely undefined, e.g. Config_Ambiguous PL_Expected) is
    skipped rather than shown as a blank/fake grade. Empty when bo_scores is {} (no BO
    scores were supplied for this DC this run) -- the full reason/basis text per
    objective is available via the API's BO_Scores field, not repeated here to keep the
    table width sane.

    BO Rank column added 2026-09-03 (explicit user request: "numerical ranking on the
    basis of BO scoring") -- DailyTaskRow.BO_Rank, 1 = lowest BO_Composite_Score (worst-
    performing/most in need of attention) among THIS SE's own task list that day, dense-
    ranked (ties share a rank). Not a network-wide rank, not the same thing as Sr_No
    (which is still Cohort/Total_Score/tie-break-order -- the actual selection ranking).
    "N/A" when no BO scores exist to rank by.

    Plain fixed-width columns, one line per task, single '-'-rule under the header --
    reverted 2026-08-07 back to this (the format used throughout this project's history)
    after a bordered/wrapped grid variant wasn't clearer. DC Name/Reason are truncated
    to a fixed length rather than wrapped, same as originally."""
    tasks = plan_run.tasks.order_by("se_id", "sr_no")
    if not tasks:
        return ["  (no tasks -- every SE was gated out or no candidates qualified)"]

    rows = []
    for t in tasks:
        last_visit = f"{t.last_visit_date} ({t.days_since_last_visit}d)" if t.last_visit_date else "Never"
        if t.present_overdue is not None:
            extras = []
            if t.overdue_aging_bucket:
                extras.append(t.overdue_aging_bucket)
            if t.avg_repayment_days is not None:
                extras.append(f"avg repayment {t.avg_repayment_days:.0f}d")
            overdue = f"{t.present_overdue:,.0f}" + (f" ({', '.join(extras)})" if extras else "")
        else:
            overdue = "N/A"
        rows.append([
            (t.se_name or t.se_id).split("@")[0],
            str(t.sr_no),
            t.critical_reasons if t.critical else "",
            (t.dc_name or "N/A")[:26],
            f"{t.distance_km:.1f}" if t.distance_km is not None else "N/A",
            t.recommended_task_type,
            t.purpose_of_visit,
            (t.reason_of_visit or "")[:45],
            last_visit,
            f"{t.present_outstanding:,.0f}" if t.present_outstanding is not None else "N/A",
            overdue,
            str(t.last_order_date) if t.last_order_date else "None",
            f"{t.last_order_value:,.0f}" if t.last_order_value is not None else "N/A",
            str(t.last_payment_date) if t.last_payment_date else "N/A",
            f"{t.ytd_private_label:,.0f}" if t.ytd_private_label is not None else "N/A",
            t.dc_club_participation or "N/A",
            {"financed": "Financed", "non_financed": "Non-Financed"}.get(t.finance_status, "Unknown"),
            " ".join(f"{obj}:{data['grade']}" for obj, data in (t.bo_scores or {}).items() if data.get("grade")) or "N/A",
            str(t.bo_rank) if t.bo_rank is not None else "N/A",
        ])
    headers = [
        "SE", "Sr", "Critical", "DC Name", "Km", "Task Type", "Purpose", "Reason",
        "Last Visit", "Outstanding", "Overdue (Aging)", "Last Order", "Order Value",
        "Last Payment", "YTD PL", "Club", "Finance", "BO Scores", "BO Rank",
    ]
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]

    def fmt(row):
        return "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(row))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(r) for r in rows)
    return lines
