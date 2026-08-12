"""Parses pitch_config/'s 5 CSVs into in-memory lookup structures for the Pitching
Agent (planning/pitching.py). Read once per process (module-level cache) -- these are
config files, not something re-read per pitch. All 5 share the same shape: row 1 = title,
row 2 = explanatory note, row 3 = real header, data rows follow.

Verified live against the actual files (2026-08-08): 5 single-Purpose rows (matches
se_daily_plan_agent.py's real Visit-Purpose taxonomy exactly -- Promise To Bill (P2B),
Promise To Pay / Collection, Query Resolution, Sale, Stock at DC) and 7 combo rows.
"Strategic Preface Mapping.csv" is NOT about DC Cohort despite its name -- it's a
Purpose x S1-S8 applicability matrix, redundant with (confirms) each single-Purpose
row's own "Applicable Sources" column, so only one of the two is treated as authoritative
here (the Scripts file) to avoid maintaining two sources of truth for the same fact.

Re-exported 2026-08-12 with a real content change, not just a re-download: Required
Data Sources.csv splits S2 into S2a (Last Discount) and S2b (Suggested Discount, now
with a confirmed computation methodology -- see planning/pitching.py's module docstring
for what's actually wired vs. still config-only) and adds S1b (Product & Business
Category enrichment, informational -- never appears in any Applicable Sources column,
so it needs no builder). Scripts/Multi-Purpose CSVs updated to match, referencing
specific recommended products instead of a bare peer-purchase amount. The old bare "S2"
code no longer appears anywhere in these files."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet

from django.conf import settings

PITCH_CONFIG_DIR = Path(getattr(settings, "SE_DAILY_PLAN_AGENT_PATH", ".")) / "pitch_config"

_SCRIPTS_CSV = PITCH_CONFIG_DIR / "SE_DC_Visit_Pitch_Playbook - DC Visit Pitch Scripts.csv"
_COMBO_CSV = PITCH_CONFIG_DIR / "SE_DC_Visit_Pitch_Playbook - DC Visit Pitch (Multi-Purpose).csv"
_SOURCES_CSV = PITCH_CONFIG_DIR / "SE_DC_Visit_Pitch_Playbook - Required Data Sources.csv"

# S1-S8 canonical labels, keyed by the short code used everywhere else in this module --
# read from Required Data Sources.csv at load time (see load_data_source_labels()) rather
# than hardcoded here, so a pitch_config edit to that file's wording is picked up without
# a code change.


def _read_data_rows(path: Path) -> list:
    """Rows 1-2 are title/note, row 3 (index 2) is the real header, data follows."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[2]
    return header, rows[3:]


_SOURCE_ID_RE = re.compile(r"^S\d+[a-z]?$")  # S1..S8, plus 2026-08-12's split/enrichment sub-IDs (S1b, S2a, S2b)


def load_data_source_labels() -> Dict[str, str]:
    """{'S1': 'Same-Block Purchase (Product-wise)', ...} from Required Data Sources.csv.
    Only real S<N>[letter] rows -- the file has a trailing footnote row (a "User model"
    note in column 1, no real ID) that isn't a data source. Matches S1b/S2a/S2b (the
    2026-08-12 re-export's split of S2 into Last/Suggested Discount and new S1b
    Product/Category enrichment row), not just bare S<N> -- a plain r[0][1:].isdigit()
    check would silently exclude all three from data_source_labels, degrading their
    display label to the raw code with no crash to notice by."""
    _, rows = _read_data_rows(_SOURCES_CSV)
    return {r[0]: r[1] for r in rows if r and _SOURCE_ID_RE.match(r[0])}


def _parse_sources_cell(cell: str) -> FrozenSet[str]:
    """'S1 Block Purchase · S2 Discount · S3 Historical' -> frozenset({'S1','S2','S3'})."""
    return frozenset(part.strip().split()[0] for part in cell.split("·") if part.strip())


def load_single_purpose_scripts() -> Dict[str, Dict[str, Any]]:
    """{'Sale': {'win_condition': ..., 'ask': ..., 'tell': ..., 'wish': ..., 'sources': frozenset({...})}, ...}"""
    _, rows = _read_data_rows(_SCRIPTS_CSV)
    out = {}
    for r in rows:
        if not r or not r[0]:
            continue
        purpose, win_condition, ask, tell, wish, sources = r[:6]
        out[purpose] = {
            "win_condition": win_condition,
            "ask": ask,
            "tell": tell,
            "wish": wish,
            "sources": _parse_sources_cell(sources),
        }
    return out


def load_combo_scripts() -> Dict[FrozenSet[str], Dict[str, Any]]:
    """{frozenset({'Sale','Promise To Pay / Collection'}): {'win_conditions': ..., 'sequence_rationale': ...}, ...}
    Keyed by frozenset, NOT the raw 'Purposes Combined' string -- the task engine's own
    combo ordering (always Sale before Promise To Pay / Collection, since QUALIFIERS
    iterates Visits/PL before Outstanding) doesn't match this file's own key ordering for
    the same combo ('Promise To Pay / Collection + Sale') -- confirmed live, exact-string
    lookup would silently miss every real combo task this engine ever produces."""
    _, rows = _read_data_rows(_COMBO_CSV)
    out = {}
    for r in rows:
        if not r or not r[0]:
            continue
        purposes_combined, win_conditions, sequence_rationale, sample_and_script = r[:4]
        # "Full Visit — Query + Stock + Sale + Collection + P2B" uses short labels, not
        # full Purpose strings -- skip it from the frozenset index (no real task can ever
        # combine all 5 anyway, since the engine only ever produces Sale/Collection) but
        # keep it loaded under its literal string key for completeness/future lookup.
        if " + " in purposes_combined and "Full Visit" not in purposes_combined:
            key = frozenset(p.strip() for p in purposes_combined.split(" + "))
        else:
            key = purposes_combined
        out[key] = {
            "purposes_combined_label": purposes_combined,
            "win_conditions": win_conditions,
            "sequence_rationale": sequence_rationale,
        }
    return out


class PitchConfig:
    """Loaded once, reused for every pitch this process generates."""

    def __init__(self):
        self.data_source_labels = load_data_source_labels()
        self.single_purpose = load_single_purpose_scripts()
        self.combo = load_combo_scripts()


_cached_config: "PitchConfig | None" = None


def get_pitch_config() -> PitchConfig:
    global _cached_config
    if _cached_config is None:
        _cached_config = PitchConfig()
    return _cached_config
