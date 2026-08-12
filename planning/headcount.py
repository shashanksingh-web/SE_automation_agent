"""Active-headcount bifurcation: how many active accounts hold each org role (SE/ABM/
RBM) and how they're distributed across the Node/Block/District/State geo hierarchy.

"Active" = is_active=True in users_user, seen via a task_management_task/plan row in
the last LOOKBACK_DAYS days (se_daily_plan_agent.LOOKBACK_DAYS, Config 0.2) -- there is
no standalone SE/ABM/RBM roster query in this pipeline (Source 1a's own roster query
was removed 2026-08-12 as confirmed zero-value), so this is the best available proxy,
not a direct users_user role/designation count.

Role membership is NOT mutually exclusive -- an account can be SE, ABM, and RBM at
once (e.g. a senior SE also covering ABM duties). "SE role" means the account has at
least one DC genuinely linked to it, resolved from three sources rather than one,
because any single source under-covers on its own (confirmed live 2026-08-12 on a
Rajasthan run):
  1. Geo_Mapping_Normalized.sales_rep_email (live, input_partner_details)
  2. DC_Master_Normalized.Assigned_SE_Email (local, DC_RAnk.csv + live-supplement)
  3. Visits_Normalized's SE_ID -> real DC_ID (bridged through customer_management_
     customer, unlike Task_Nodes_Normalized.dc_id which is a raw, unbridged internal
     PK -- see se_daily_plan_agent.SQL_TASK_NODES_1B's docstring) -- catches accounts
     genuinely doing DC-visit work whose roster assignment is stale/reassigned.
"ABM role" / "RBM role" mean the account appears as a DC's abm_email / rbm_email in
Geo_Mapping (live only -- Geo_Mapping_Normalized.json requires METABASE configured on
the run that produced output/).

Node/Block/District/State bifurcation counts each SE-role account once per distinct
geo value among the DCs resolved above -- an SE whose DCs span two Nodes appears under
both, so per-dimension counts need not sum to the SE-role total. Node/State come from
DC_Master (broadest coverage, local + supplemented); Block/District only exist in
Geo_Mapping (live-only, Source 1c never normalizes into DC_Master)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Set


def _norm(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def _load(output_dir: Path, filename: str) -> list:
    path = output_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run se_daily_plan_agent.py (Data Normalization Agent) first.")
    with open(path) as f:
        return json.load(f)


def compute_active_headcount_bifurcation(output_dir: Path) -> Dict[str, Any]:
    task_nodes = _load(output_dir, "Task_Nodes_Normalized.json")
    geo_mapping = _load(output_dir, "Geo_Mapping_Normalized.json")
    dc_master = _load(output_dir, "DC_Master_Normalized.json")
    visits = _load(output_dir, "Visits_Normalized.json")

    active_se: Dict[str, str] = {}  # lower email -> original casing (first seen)
    se_user_to_email: Dict[Any, str] = {}
    for r in task_nodes:
        email = _norm(r.get("se_email"))
        if not email:
            continue
        se_user_to_email[r.get("se_user_id")] = email
        if r.get("se_is_active"):
            active_se.setdefault(email, r["se_email"].strip())

    abm_emails = {_norm(r.get("abm_email")) for r in geo_mapping} - {""}
    rbm_emails = {_norm(r.get("rbm_email")) for r in geo_mapping} - {""}

    dc_geo: Dict[str, Dict[str, Optional[str]]] = {}
    for r in dc_master:
        dc_id = r.get("DC_ID")
        if not dc_id:
            continue
        dc_geo.setdefault(dc_id, {})
        dc_geo[dc_id]["Node"] = r.get("Node")
        dc_geo[dc_id]["State"] = r.get("State")
    for r in geo_mapping:
        dc_id = r.get("dc_id")
        if not dc_id:
            continue
        dc_geo.setdefault(dc_id, {})
        dc_geo[dc_id].setdefault("Node", None)
        dc_geo[dc_id].setdefault("State", None)
        dc_geo[dc_id]["Block"] = r.get("block")
        dc_geo[dc_id]["District"] = r.get("district")
        if not dc_geo[dc_id].get("State"):
            dc_geo[dc_id]["State"] = r.get("state")

    se_dc_ids: Dict[str, Set[str]] = {email: set() for email in active_se}
    for r in geo_mapping:
        email = _norm(r.get("sales_rep_email"))
        dc_id = r.get("dc_id")
        if email in se_dc_ids and dc_id:
            se_dc_ids[email].add(dc_id)
    for r in dc_master:
        email = _norm(r.get("Assigned_SE_Email"))
        dc_id = r.get("DC_ID")
        if email in se_dc_ids and dc_id:
            se_dc_ids[email].add(dc_id)
    for r in visits:
        email = se_user_to_email.get(r.get("SE_ID"))
        dc_id = r.get("DC_ID")
        if email in se_dc_ids and dc_id:
            se_dc_ids[email].add(dc_id)

    se_role = {email for email, dcs in se_dc_ids.items() if dcs}
    abm_role = active_se.keys() & abm_emails
    rbm_role = active_se.keys() & rbm_emails
    no_role = active_se.keys() - se_role - abm_role - rbm_role

    def bucket_counts(field: str) -> Dict[str, Set[str]]:
        buckets: Dict[str, Set[str]] = {}
        for email in se_role:
            values = {dc_geo.get(dc_id, {}).get(field) for dc_id in se_dc_ids[email]}
            for v in values:
                if v:
                    buckets.setdefault(v, set()).add(email)
        return buckets

    return {
        "overall_total": len(active_se),
        "se_role": sorted(active_se[e] for e in se_role),
        "abm_role": sorted(active_se[e] for e in abm_role),
        "rbm_role": sorted(active_se[e] for e in rbm_role),
        "no_role": sorted(active_se[e] for e in no_role),
        "by_node": {k: sorted(active_se[e] for e in v) for k, v in bucket_counts("Node").items()},
        "by_block": {k: sorted(active_se[e] for e in v) for k, v in bucket_counts("Block").items()},
        "by_district": {k: sorted(active_se[e] for e in v) for k, v in bucket_counts("District").items()},
        "by_state": {k: sorted(active_se[e] for e in v) for k, v in bucket_counts("State").items()},
    }
