"""DC Selection (added 2026-09-08, explicit user request -- "in admin control panel we
have select the dcs for this whole program"). Replaces the Excel-based 'updated TOP DC
list.xlsx' as the DC eligibility gate's master source, per direct instruction:

    1) dc_datamart (live DB) is the master DC universe
    2) an "uploader" refreshes DC_RAnk.csv-based Rank/Cohort (this overwrites
       se_daily_plan_agent.DC_MASTER_CSV in place -- the exact same file
       se_daily_plan_agent.load_dc_master() already reads for Step 5's Cohort/
       Total_Score ordering, so there is one parser, not two drifting ones)
    3) a per-criterion AND/OR filter over: rank range, cohort, active/inactive, overdue
       -- see se_daily_plan_agent.evaluate_dc_selection_rule for the actual AND/OR
       engine (a pure function, shared by this module's admin-preview path and by
       planning.services.generate_plan_for_scope's plan-generation path)

Plus three ways for an admin to hand-adjust the computed set on top of the rule (per
direct instruction: "uploader mention in last question, Search & toggle (Recommended),
Bulk paste of DC IDs") -- manual_includes/manual_excludes on ProgramDCSelection cover
both the toggle and the paste UX, this module just applies them uniformly.

This module owns the ADMIN-FACING side only (state/search/upload against the FULL DC
universe, for configuring the rule) -- it does its own live, unscoped dc_datamart query
for that, since an admin browsing/configuring needs the whole ~10k-DC universe, not one
plan's scope. The PLAN-GENERATION side (planning.services.generate_plan_for_scope) does
NOT import this module: it already has a scope-filtered dc_datamart pull in hand (the
same one used for Outstanding financials) and calls se_daily_plan_agent.
evaluate_dc_selection_rule directly against that, reading only get_selection_config()
below to avoid a second live DB round trip on every plan generation."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from django.utils import timezone

import se_daily_plan_agent as agent

from .models import ProgramDCSelection

DEFAULT_RULES: Dict[str, Dict[str, Any]] = {
    "rank_range": {"enabled": False, "combine": "AND", "min": None, "max": None},
    "cohort": {"enabled": False, "combine": "AND", "values": []},
    "active_status": {"enabled": False, "combine": "AND", "value": "active"},
    # CHANGED 2026-09-08, explicit user request ("overdue yes or no"): was a numeric
    # min_amount threshold -- now a simple has-overdue-or-not toggle, "yes" (has any
    # real overdue balance) or "no" (confirmed zero/no overdue). See
    # se_daily_plan_agent._dc_selection_criterion_matches' own "overdue" branch.
    "overdue": {"enabled": False, "combine": "OR", "value": "yes"},
}

# DC_RAnk.csv columns required for a file to be accepted by upload_rank_csv() -- the same
# two columns evaluate_dc_selection_rule's rank_range/cohort criteria actually read
# (se_daily_plan_agent.load_dc_master already tolerates the rest of the sheet drifting).
_REQUIRED_RANK_CSV_COLUMNS = {"Partner Id", "Rank", "Cohort"}


def _effective_rules(row: "ProgramDCSelection") -> Dict[str, Any]:
    """Upload-mode application (added 2026-09-08, explicit user request): "uploaded_only"
    means manual_includes IS the selection outright, per direct instruction ("uploaded
    list is the filter") -- the AND/OR rule is suppressed to {} here (not passed into
    evaluate_dc_selection_rule at all) rather than adding a mode branch inside that
    function itself, so every caller (this module's own preview paths and
    generate_plan_for_scope) gets consistent behavior for free just by reading through
    get_selection_config()/this helper. "uploaded_plus_filter" (default) is a no-op --
    returns the rule exactly as stored, same union-with-manual-includes behavior this
    feature has always had."""
    if row.upload_mode == ProgramDCSelection.UploadMode.UPLOADED_ONLY:
        return {}
    return row.rules or {}


def get_selection_config() -> Dict[str, Any]:
    """Cheap, DB-only read used by planning.services.generate_plan_for_scope -- no live
    dc_datamart query. Returns rules already resolved for the current upload_mode (see
    _effective_rules) plus the manual lists exactly as stored; the caller passes them
    straight into se_daily_plan_agent.evaluate_dc_selection_rule."""
    row = ProgramDCSelection.get_singleton()
    return {
        "rules": _effective_rules(row),
        "manual_includes": row.manual_includes or [],
        "manual_excludes": row.manual_excludes or [],
        "upload_mode": row.upload_mode,
    }


def _dc_master() -> Tuple[List[Dict[str, Any]], List[str]]:
    dc_master, exc = agent.load_dc_master()
    errors = [f"{r['Reason_Code']}: {r['Detail']}" for r in exc.rows if r["Reason_Code"] not in (
        "Duplicate_DC_ID", "Cohort_Rank_Consistency", "Unassigned_DC_Check",
    ) or r.get("Detail")]
    return dc_master, errors[:20]


def _fetch_live_dc_datamart() -> Tuple[Dict[str, bool], Dict[str, float], bool]:
    """Unscoped -- the whole dc_datamart table, no WHERE clause. Only called for the
    Admin Control Panel's own preview/search (an infrequent, human-triggered action),
    NEVER on the plan-generation hot path -- that path reuses the scope-filtered pull it
    already has (see planning.services.generate_plan_for_scope). Known tradeoff, not
    cached: every DC Selection panel view/search re-runs this live query -- acceptable
    for an admin-only feature, would need revisiting if this page saw real traffic."""
    active_by_id: Dict[str, bool] = {}
    overdue_by_id: Dict[str, float] = {}
    try:
        client = agent.get_client()
        rows = client.execute_sql(
            agent.REDSHIFT_DB_ID,
            "SELECT sap_partner_id AS dc_id, total_overdue, is_active FROM dc_datamart",
        )
        for row in rows:
            dc_id = agent.normalize_id(row.get("dc_id"))
            if not dc_id:
                continue
            active_by_id[dc_id] = str(row.get("is_active")).lower() == "true"
            overdue = agent.parse_number(row.get("total_overdue"))
            if overdue is not None:
                overdue_by_id[dc_id] = overdue
        return active_by_id, overdue_by_id, True
    except Exception:
        return active_by_id, overdue_by_id, False


def get_state() -> Dict[str, Any]:
    """GET /api/planning/admin/dc-selection/ -- current rule + manual lists + a live
    preview computed the same way the plan-generation gate will evaluate it.

    "Rules" always shows the raw stored rule (what the admin is editing), regardless of
    upload_mode -- the mode only controls whether it's actually APPLIED (see
    _effective_rules), so switching to "uploaded_only" doesn't erase a configured rule
    the admin might switch back to later."""
    row = ProgramDCSelection.get_singleton()
    rules = row.rules or {}
    manual_includes = row.manual_includes or []
    manual_excludes = row.manual_excludes or []
    dc_master, dc_master_errors = _dc_master()
    active_by_id, overdue_by_id, query_ok = _fetch_live_dc_datamart()
    selected = agent.evaluate_dc_selection_rule(
        _effective_rules(row), dc_master, active_by_id if query_ok else None, overdue_by_id if query_ok else None,
        manual_includes, manual_excludes,
    )
    return {
        "Rules": {**DEFAULT_RULES, **rules},
        "Upload_Mode": row.upload_mode,
        "Manual_Includes": manual_includes,
        "Manual_Excludes": manual_excludes,
        "Configured": selected is not None,
        "Universe_Size": len(dc_master),
        "Selected_Count": len(selected) if selected is not None else None,
        "Live_Query_Ok": query_ok,
        "Rank_Csv_Uploaded_At": row.rank_csv_uploaded_at,
        "Rank_Csv_Uploaded_By": row.rank_csv_uploaded_by,
        "Rank_Csv_Row_Count": row.rank_csv_row_count,
        "Updated_At": row.updated_at,
        "Updated_By": row.updated_by,
        "Dc_Master_Errors": dc_master_errors,
    }


def update_selection(
    rules: Optional[Dict[str, Any]], manual_includes: Optional[List[str]],
    manual_excludes: Optional[List[str]], actor: str = "",
    upload_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """POST body may include any subset of rules/manual_includes/manual_excludes/
    upload_mode -- only the provided keys are touched, same partial-update convention as
    admin_config.apply_overrides. `rules` replaces the whole dict (not merged
    per-criterion) since the frontend always sends its complete, currently-edited rule
    set -- a partial per-criterion merge here would silently resurrect a criterion the
    admin just turned off in the same request.

    Rank-range validation (added 2026-09-08, explicit user request, "rank range (manual
    if not matched error)"): if the incoming rules enable rank_range, and that criterion
    alone (evaluated against the full DC_RAnk.csv universe, independent of cohort/
    active/overdue/manual lists) matches zero DCs, the whole update is REJECTED (raises
    ValueError, same fail-loud convention as upload_rank_csv/upload_selected_dcs) rather
    than silently saved -- a manually-entered range that matches nothing is almost
    always a typo, not a deliberate "select nobody" choice. Only rank_range gets this
    check, per direct instruction ("rank range error only") -- cohort/active_status/
    overdue can validly match zero DCs (e.g. a genuinely empty cohort) without it being
    a mistake."""
    row = ProgramDCSelection.get_singleton()
    if rules is not None:
        new_rules = {k: v for k, v in rules.items() if k in DEFAULT_RULES}
        rank_rule = new_rules.get("rank_range")
        if rank_rule and rank_rule.get("enabled"):
            dc_master, _ = _dc_master()
            matches = sum(
                1 for dc in dc_master
                if agent._dc_selection_criterion_matches("rank_range", rank_rule, dc.get("Rank"), None, None, None)
            )
            if matches == 0:
                raise ValueError(
                    f"Rank range {rank_rule.get('min')!r}-{rank_rule.get('max')!r} matches zero DCs in the "
                    "current Rank & Cohort file -- check for a typo before saving."
                )
        row.rules = new_rules
    if manual_includes is not None:
        row.manual_includes = sorted({agent.normalize_id(x) for x in manual_includes if agent.normalize_id(x)})
    if manual_excludes is not None:
        row.manual_excludes = sorted({agent.normalize_id(x) for x in manual_excludes if agent.normalize_id(x)})
    if upload_mode is not None:
        if upload_mode not in ProgramDCSelection.UploadMode.values:
            raise ValueError(f"upload_mode must be one of {ProgramDCSelection.UploadMode.values}, got {upload_mode!r}")
        row.upload_mode = upload_mode
    row.updated_by = actor
    row.save()
    return get_state()


def search_dcs(query: str = "", limit: int = 50, offset: int = 0, filter_mode: str = "all") -> Dict[str, Any]:
    """Search & toggle UX -- searches the full DC_RAnk.csv universe by DC_ID/name
    substring, returns each match's Rank/Cohort/is_active/overdue plus whether it's
    currently in the computed selection and/or manually included/excluded, so the panel
    can render a toggle per row without a second round trip.

    filter_mode: "all" (default), "selected", or "excluded" -- narrows to DCs currently
    in (or manually excluded from) the computed selection, for browsing a large result
    rather than only ever searching by ID."""
    row = ProgramDCSelection.get_singleton()
    rules = row.rules or {}
    manual_includes = set(row.manual_includes or [])
    manual_excludes = set(row.manual_excludes or [])
    dc_master, _ = _dc_master()
    active_by_id, overdue_by_id, query_ok = _fetch_live_dc_datamart()
    selected = agent.evaluate_dc_selection_rule(
        _effective_rules(row), dc_master, active_by_id if query_ok else None, overdue_by_id if query_ok else None,
        manual_includes, manual_excludes,
    ) or set()

    q = (query or "").strip().lower()
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def matches_query(dc: Dict[str, Any]) -> bool:
        if not q:
            return True
        return q in (dc["DC_ID"] or "").lower() or q in (dc.get("DC_Name") or "").lower()

    def matches_mode(dc_id: str) -> bool:
        if filter_mode == "selected":
            return dc_id in selected
        if filter_mode == "excluded":
            return dc_id in manual_excludes or (rules and dc_id not in selected)
        return True

    filtered = [dc for dc in dc_master if matches_query(dc) and matches_mode(dc["DC_ID"])]
    page = filtered[offset : offset + limit]
    return {
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "returned": len(page),
        "dcs": [
            {
                "dc_id": dc["DC_ID"],
                "dc_name": dc.get("DC_Name"),
                "node": dc.get("Node"),
                "state": dc.get("State"),
                "rank": dc.get("Rank"),
                "cohort": dc.get("Cohort"),
                "is_active": active_by_id.get(dc["DC_ID"]) if query_ok else None,
                "overdue": overdue_by_id.get(dc["DC_ID"]) if query_ok else None,
                "in_selection": dc["DC_ID"] in selected,
                "manually_included": dc["DC_ID"] in manual_includes,
                "manually_excluded": dc["DC_ID"] in manual_excludes,
            }
            for dc in page
        ],
    }


def upload_rank_csv(file_bytes: bytes, filename: str, actor: str = "") -> Dict[str, Any]:
    """Uploader (point 2 of the feature request) -- validates the uploaded file parses
    with the required columns and at least one usable row via the SAME parser Step 5
    already relies on (se_daily_plan_agent.load_dc_master), then atomically replaces
    DC_MASTER_CSV (DC_RAnk.csv) in place. Rejects (raises ValueError, caller returns
    400) rather than partially applying -- a bad upload must never leave the file that
    both this feature and Step 5's Cohort/Total_Score ordering depend on truncated or
    unparsable."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"File is not valid UTF-8 text: {e}") from e
    reader = csv.DictReader(io.StringIO(text))
    header = set(reader.fieldnames or [])
    missing = _REQUIRED_RANK_CSV_COLUMNS - header
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}")

    target = agent.DC_MASTER_CSV
    tmp_path = Path(str(target) + ".upload.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    try:
        parsed, exc = agent.load_dc_master(path=tmp_path)
        fatal = [r for r in exc.rows if r["Reason_Code"] in ("DC_Master_Missing",)]
        if fatal or not parsed:
            raise ValueError("File parsed to zero usable DC rows")
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(target)

    row = ProgramDCSelection.get_singleton()
    row.rank_csv_uploaded_at = timezone.now()
    row.rank_csv_uploaded_by = actor
    row.rank_csv_row_count = len(parsed)
    row.save()
    return get_state()


def sample_rank_csv() -> str:
    """Sample file for the Rank & Cohort uploader above -- shown/downloadable from the
    Admin Control Panel so an admin knows the exact shape upload_rank_csv() requires
    (only Partner Id/Rank/Cohort are validated; every other column DC_RAnk.csv normally
    carries is accepted but ignored by this feature)."""
    return (
        "Partner Id,Rank,Cohort\n"
        "1000041207,1,Strategic\n"
        "1000031612,2,Strategic\n"
        "1000025034,3001,Opportunity\n"
        "1000000719,,Long Tail\n"
    )


def sample_selected_dcs_csv() -> str:
    """Sample file for the Selected DC List uploader below."""
    return "Partner Id\n1000041207\n1000031612\n1000025034\n"


def upload_selected_dcs(
    file_bytes: bytes, filename: str, actor: str = "", upload_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Selected DC List uploader (added 2026-09-08, explicit user request -- "add one
    more uploader for selected dc") -- a file-based alternative to the Bulk Paste tab's
    textarea, for handing this feature a list of DC IDs to select in one upload instead
    of copy-pasting them.

    REWRITTEN 2026-09-08, explicit user request ("rule is very simple here when user
    upload the file of dc - it goes to dc data mart table exist than filter out with
    reasoning matched dc goes to dcrank csv for rank and cohort"). Order and semantics,
    per direct instruction:
      1. dc_datamart FIRST -- the gate. An uploaded ID genuinely absent from a
         SUCCESSFULLY returned dc_datamart pull is REJECTED outright (not added to
         Manual Includes at all), with a `reason` explaining why. If the live query
         itself fails (not "this ID is missing" but "we couldn't check at all"), no ID
         is rejected on that basis -- same fail-open-on-infra-failure treatment already
         established elsewhere in this module/apply_dc_exclusion_rules, `dc_datamart_
         unverified` flags this instead so the admin isn't left guessing why nothing was
         checked.
      2. DC_RAnk.csv SECOND, only for IDs that passed step 1 -- Rank/Cohort/Name lookup.
         Per direct follow-up confirmation ("if dc pass the dc datamart rank not found it
         will consider in the pool"), a DC missing from DC_RAnk.csv is still ACCEPTED
         into the pool (not rejected) -- only dc_datamart absence is a rejection reason;
         a missing Rank/Cohort here just means it can never match the Rank range/Cohort
         criteria later, same soft-degrade this module gives every other missing-data
         case.
    Accepted IDs are ADDED to manual_includes (merged with whatever's already there) and
    removed from manual_excludes if present. Rejected IDs are NOT added anywhere.

    upload_mode: optional, forwarded straight to update_selection's own field (per direct
    instruction, the mode -- "uploaded list only" vs "uploaded list + optional filter" --
    is chosen at upload time, alongside the file itself, not as a separate save step).
    None leaves whatever mode is already stored untouched.

    Format: one DC ID per row. Tolerant of either a bare list (no header) or a CSV with
    a header naming the ID column (Partner Id/DC_ID/DC Id, case-insensitive) -- only the
    first column of each row is read, so an admin can paste an export with extra columns
    (name, node, etc.) without stripping them first. Rejects (400) if zero valid DC IDs
    are found, same fail-loud convention as upload_rank_csv."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"File is not valid UTF-8 text: {e}") from e

    rows = [r for r in csv.reader(io.StringIO(text)) if r and r[0].strip()]
    if rows:
        first_cell = rows[0][0].strip().lower()
        if first_cell in ("partner id", "dc_id", "dc id", "id"):
            rows = rows[1:]

    ids = {agent.normalize_id(r[0]) for r in rows if agent.normalize_id(r[0])}
    if not ids:
        raise ValueError("File parsed to zero valid DC IDs")

    # Step 1: dc_datamart -- the gate. Rejects an ID only when the query itself
    # succeeded AND the ID is genuinely absent from it (never on a query failure).
    active_by_id, overdue_by_id, query_ok = _fetch_live_dc_datamart()
    if query_ok:
        accepted_ids = {dc_id for dc_id in ids if dc_id in active_by_id}
        rejected_ids = ids - accepted_ids
    else:
        accepted_ids, rejected_ids = set(ids), set()

    # Step 2: DC_RAnk.csv -- Rank/Cohort/Name, only for accepted IDs. Missing here is
    # soft (still accepted), unlike step 1's dc_datamart absence.
    dc_master, _ = _dc_master()
    dc_by_id = {dc["DC_ID"]: dc for dc in dc_master}

    enriched = [
        {
            "dc_id": dc_id,
            "dc_name": dc_by_id[dc_id].get("DC_Name") if dc_id in dc_by_id else None,
            "rank": dc_by_id[dc_id].get("Rank") if dc_id in dc_by_id else None,
            "cohort": dc_by_id[dc_id].get("Cohort") if dc_id in dc_by_id else None,
            "is_active": active_by_id.get(dc_id) if query_ok else None,
            "overdue": overdue_by_id.get(dc_id) if query_ok else None,
            "found": dc_id in accepted_ids,
            "in_rank_csv": dc_id in dc_by_id,
            "dc_datamart_unverified": not query_ok,
            "reason": (
                None if dc_id in accepted_ids else
                f"{dc_id} was not found in dc_datamart (the live DC master) -- rejected, not added to "
                "Manual Includes. Check for a typo, or confirm this DC exists in dc_datamart before "
                "re-uploading."
            ),
        }
        for dc_id in sorted(ids)
    ]

    row = ProgramDCSelection.get_singleton()
    includes = set(row.manual_includes or []) | accepted_ids
    excludes = set(row.manual_excludes or []) - accepted_ids
    row.manual_includes = sorted(includes)
    row.manual_excludes = sorted(excludes)
    if upload_mode is not None:
        if upload_mode not in ProgramDCSelection.UploadMode.values:
            raise ValueError(f"upload_mode must be one of {ProgramDCSelection.UploadMode.values}, got {upload_mode!r}")
        row.upload_mode = upload_mode
    row.updated_by = actor
    row.save()
    state = get_state()
    state["Uploaded_Dc_Count"] = len(ids)
    state["Uploaded_Accepted_Count"] = len(accepted_ids)
    state["Uploaded_Dcs"] = enriched
    state["Uploaded_Not_Found_Count"] = len(rejected_ids)
    state["Uploaded_Dc_Datamart_Unverified"] = not query_ok
    return state
