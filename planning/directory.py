"""Read-only directory/lookup endpoints for the org hierarchy (State/District/Block/
Node/ZBM/RBM/ABM/SE) and the DC master -- for populating a frontend's dropdowns/search.
Nothing before this let a caller discover what values exist; every other endpoint
requires already knowing a specific scope value to query (e.g. GET /state/<name>/ needs
you to already know "Bihar" is a real state).

Two source tables, same reasoning as planning.headcount: DC_Master_Normalized.json
(local + live-supplemented, broadest DC/Node/State coverage) for States/Nodes/SEs/DCs,
and Geo_Mapping_Normalized.json (live-only, Source 1c) for Districts/Blocks/ZBM/RBM/ABM
-- DC_Master never carries Block/District/ZBM/RBM/ABM at all."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .data_cache import load_output_json as _load


def _norm(v: Optional[str]) -> str:
    return (v or "").strip()


def _eq_ci(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def list_states(output_dir: Path) -> List[Dict[str, Any]]:
    dc_master = _load(output_dir, "DC_Master_Normalized.json")
    by_state: Dict[str, Dict[str, Any]] = {}
    for r in dc_master:
        state = _norm(r.get("State"))
        if not state:
            continue
        bucket = by_state.setdefault(state, {"nodes": set(), "ses": set(), "dc_count": 0})
        if r.get("Node"):
            bucket["nodes"].add(r["Node"])
        if r.get("Assigned_SE_Email"):
            bucket["ses"].add(r["Assigned_SE_Email"])
        bucket["dc_count"] += 1
    return sorted(
        [{"state": s, "node_count": len(b["nodes"]), "se_count": len(b["ses"]), "dc_count": b["dc_count"]} for s, b in by_state.items()],
        key=lambda x: x["state"],
    )


def list_nodes(output_dir: Path, state: Optional[str] = None) -> List[Dict[str, Any]]:
    dc_master = _load(output_dir, "DC_Master_Normalized.json")
    by_node: Dict[tuple, Dict[str, Any]] = {}
    for r in dc_master:
        node, st = _norm(r.get("Node")), _norm(r.get("State"))
        if not node or (state and not _eq_ci(st, state)):
            continue
        bucket = by_node.setdefault((node, st), {"ses": set(), "dc_count": 0})
        if r.get("Assigned_SE_Email"):
            bucket["ses"].add(r["Assigned_SE_Email"])
        bucket["dc_count"] += 1
    return sorted(
        [{"node": k[0], "state": k[1], "se_count": len(b["ses"]), "dc_count": b["dc_count"]} for k, b in by_node.items()],
        key=lambda x: (x["state"], x["node"]),
    )


def list_districts(output_dir: Path, state: Optional[str] = None) -> List[Dict[str, Any]]:
    geo = _load(output_dir, "Geo_Mapping_Normalized.json")
    by_district: Dict[tuple, Dict[str, Any]] = {}
    for r in geo:
        district, st = _norm(r.get("district")), _norm(r.get("state"))
        if not district or (state and not _eq_ci(st, state)):
            continue
        bucket = by_district.setdefault((district, st), {"blocks": set(), "dc_count": 0})
        if r.get("block"):
            bucket["blocks"].add(r["block"])
        bucket["dc_count"] += 1
    return sorted(
        [{"district": k[0], "state": k[1], "block_count": len(b["blocks"]), "dc_count": b["dc_count"]} for k, b in by_district.items()],
        key=lambda x: (x["state"], x["district"]),
    )


def list_blocks(output_dir: Path, state: Optional[str] = None, district: Optional[str] = None) -> List[Dict[str, Any]]:
    geo = _load(output_dir, "Geo_Mapping_Normalized.json")
    by_block: Dict[tuple, Dict[str, Any]] = {}
    for r in geo:
        block, dist, st = _norm(r.get("block")), _norm(r.get("district")), _norm(r.get("state"))
        if not block or (state and not _eq_ci(st, state)) or (district and not _eq_ci(dist, district)):
            continue
        bucket = by_block.setdefault((block, dist, st), {"dc_count": 0})
        bucket["dc_count"] += 1
    return sorted(
        [{"block": k[0], "district": k[1], "state": k[2], "dc_count": b["dc_count"]} for k, b in by_block.items()],
        key=lambda x: (x["state"], x["district"], x["block"]),
    )


def _list_role(output_dir: Path, field_code: str, field_name: str, field_email: str) -> List[Dict[str, Any]]:
    """Shared shape for ZBM/RBM/ABM -- each is a (code, name, email) triple on every
    Geo_Mapping row, so one grouping function serves all three."""
    geo = _load(output_dir, "Geo_Mapping_Normalized.json")
    by_code: Dict[str, Dict[str, Any]] = {}
    for r in geo:
        code = _norm(r.get(field_code))
        if not code:
            continue
        bucket = by_code.setdefault(code, {
            "name": _norm(r.get(field_name)) or None, "email": _norm(r.get(field_email)) or None,
            "states": set(), "nodes": set(), "dc_count": 0,
        })
        if r.get("state"):
            bucket["states"].add(r["state"])
        if r.get("node"):
            bucket["nodes"].add(r["node"])
        bucket["dc_count"] += 1
    return sorted(
        [
            {
                "code": code, "name": b["name"], "email": b["email"],
                "states": sorted(b["states"]), "node_count": len(b["nodes"]), "dc_count": b["dc_count"],
            }
            for code, b in by_code.items()
        ],
        key=lambda x: x["code"],
    )


def list_zbms(output_dir: Path) -> List[Dict[str, Any]]:
    """"State Head" -- ZBM (Zonal Business Manager). No standalone "State Head" field
    exists anywhere in this data model; ZBM is the closest real role, since a Zone
    typically spans one or more States (confirmed by usage, not a documented 1:1)."""
    return _list_role(output_dir, "zbm_e_code", "zbm", "zbm_email")


def list_rbms(output_dir: Path) -> List[Dict[str, Any]]:
    return _list_role(output_dir, "rbm_e_code", "rbm", "rbm_email")


def list_abms(output_dir: Path) -> List[Dict[str, Any]]:
    return _list_role(output_dir, "abm_e_code", "abm", "abm_email")


def list_ses(output_dir: Path, state: Optional[str] = None, node: Optional[str] = None) -> List[Dict[str, Any]]:
    """DC-assigned SEs (DC_Master.Assigned_SE_Email) -- deliberately narrower than
    planning.headcount's "active SE" definition (task/plan activity in the last 90d).
    This answers "which SE values can I pass to GET /se/<email>/", which is scope
    resolution's own definition of an SE (resolve_scope_dcs), not an activity signal."""
    dc_master = _load(output_dir, "DC_Master_Normalized.json")
    by_se: Dict[str, Dict[str, Any]] = {}
    for r in dc_master:
        email = _norm(r.get("Assigned_SE_Email"))
        if not email:
            continue
        st, nd = _norm(r.get("State")), _norm(r.get("Node"))
        if (state and not _eq_ci(st, state)) or (node and not _eq_ci(nd, node)):
            continue
        bucket = by_se.setdefault(email, {"states": set(), "nodes": set(), "dc_count": 0})
        if st:
            bucket["states"].add(st)
        if nd:
            bucket["nodes"].add(nd)
        bucket["dc_count"] += 1
    return sorted(
        [{"se_email": e, "states": sorted(b["states"]), "nodes": sorted(b["nodes"]), "dc_count": b["dc_count"]} for e, b in by_se.items()],
        key=lambda x: x["se_email"],
    )


def list_dcs(
    output_dir: Path, state: Optional[str] = None, node: Optional[str] = None,
    se: Optional[str] = None, limit: int = 200, offset: int = 0,
) -> Dict[str, Any]:
    """Paginated -- DC_Master has 19k+ rows network-wide (post 2026-08-12 supplement
    fix), never returned unfiltered/unpaginated by default. limit capped at 1000."""
    dc_master = _load(output_dir, "DC_Master_Normalized.json")
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

    def matches(r: Dict[str, Any]) -> bool:
        if state and not _eq_ci(_norm(r.get("State")), state):
            return False
        if node and not _eq_ci(_norm(r.get("Node")), node):
            return False
        if se and not _eq_ci(_norm(r.get("Assigned_SE_Email")), se):
            return False
        return True

    filtered = [r for r in dc_master if matches(r)]
    page = filtered[offset : offset + limit]
    return {
        "total": len(filtered), "limit": limit, "offset": offset, "returned": len(page),
        "dcs": [
            {
                "dc_id": r.get("DC_ID"), "dc_name": r.get("DC_Name"), "node": r.get("Node"),
                "state": r.get("State"), "assigned_se_email": r.get("Assigned_SE_Email"),
                "rank": r.get("Rank"), "cohort": r.get("Cohort"), "total_score": r.get("Total_Score"),
                "total_score_unscored": r.get("Total_Score_Unscored"), "latitude": r.get("Latitude"),
                "longitude": r.get("Longitude"), "dc_status": r.get("DC_Status"),
                "in_scope_flag": r.get("In_Scope_Flag"),
            }
            for r in page
        ],
    }
