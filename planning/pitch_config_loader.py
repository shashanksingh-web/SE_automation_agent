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
code no longer appears anywhere in these files.

Re-exported again 2026-08-14 -- filenames shifted TWICE within the same session (first
to "(1)"/"(2)" suffixes, then back to bare names minutes later, while 2 of the other 5
files in the folder kept their "(1)" suffix) -- confirmed live to be an active external
sync process (this folder behaves like a Google Drive/Sheets-synced directory: repeat
exports append a "(N)" suffix, and something in this pipeline resolves/renames duplicates
over time) touching pitch_config/ during normal use, not a one-time rename to react to.
A hardcoded exact filename broke within minutes of being fixed to match a since-
superseded state -- see _resolve_csv() below, which matches by stable name PREFIX
instead, tolerant of whatever suffix churn happens next.

This export also added a real new column to the Scripts CSV: "Dehaat Center Ko Jaano
(Heading)", inserted between Win Condition and What to Ask -- confirmed live 2026-08-14
by reading the file's actual header row, not assumed from the filename change alone (a
naive re-point of the path constants without checking column count would have silently
fed the file's "What to Wish" column into what load_single_purpose_scripts() treats as
Applicable Sources, since the old unpacking took a fixed r[:6] slice --
_applicable_sources()'s S1-S8 selection runs on that field, so this would have quietly
broken which data feeds every pitch, not just crashed loudly like the missing file did).
Captured as "heading" in the returned dict below; not yet consumed by
planning/pitching.py's _compose() (same "confirmed source, not yet wired" pattern as
S2b/S4 -- see that module's docstring)."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet

from django.conf import settings

PITCH_CONFIG_DIR = Path(getattr(settings, "SE_DAILY_PLAN_AGENT_PATH", ".")) / "pitch_config"

# Stable name PREFIXES, not exact filenames -- see module docstring on why an exact
# filename here is fragile against this folder's own live sync churn. Resolved fresh on
# every PitchConfig() construction (once per process -- see get_pitch_config()'s cache),
# not once at import time, so a fresh management command invocation always sees whatever
# pitch_config/ currently holds.
_SCRIPTS_PREFIX = "SE_DC_Visit_Pitch_Playbook - DC Visit Pitch Scripts"
_COMBO_PREFIX = "SE_DC_Visit_Pitch_Playbook - DC Visit Pitch (Multi-Purpose)"
_SOURCES_PREFIX = "SE_DC_Visit_Pitch_Playbook - Required Data Sources"

# S1-S8 canonical labels, keyed by the short code used everywhere else in this module --
# read from Required Data Sources.csv at load time (see load_data_source_labels()) rather
# than hardcoded here, so a pitch_config edit to that file's wording is picked up without
# a code change.


_SUFFIX_RE = re.compile(r"\((\d+)\)(?=\.csv$)")


def _resolve_csv(prefix: str) -> Path:
    """pitch_config/<prefix>*.csv -> a single Path, tolerant of the "(1)"/"(2)"-style
    suffixes this folder's own sync process adds/strips over time (see module docstring).
    Picks the most recently modified match when more than one exists (e.g. a stale
    duplicate mid-sync), never silently whichever glob() happens to yield first. Raises
    with the full actual directory listing on zero matches, so a real missing-file
    problem still fails loud rather than silently picking the wrong CSV.

    mtime is the primary sort key, but a fresh git checkout gives every file in the repo
    the same mtime -- with only mtime, a real ambiguity (two files matching one prefix at
    once, as happened live on 2026-08-14) would resolve to "whichever sorted() happened to
    keep," a silent non-determinism the sort() call looked deliberate but wasn't. On a
    mtime tie, prefer the bare filename (no "(N)" suffix) -- per the module docstring,
    this sync process's settled end-state is the bare name, with "(N)" suffixes as its
    in-flight/transient form -- then the highest suffix number, so the result is fully
    deterministic even when mtime alone can't disambiguate."""
    matches = list(PITCH_CONFIG_DIR.glob(f"{prefix}*.csv"))
    if not matches:
        actual = sorted(p.name for p in PITCH_CONFIG_DIR.glob("*.csv"))
        raise FileNotFoundError(f"No file matching {prefix!r}*.csv in {PITCH_CONFIG_DIR} -- actual contents: {actual}")

    def _sort_key(p: Path):
        m = _SUFFIX_RE.search(p.name)
        suffix_num = int(m.group(1)) if m else -1
        return (p.stat().st_mtime, suffix_num == -1, suffix_num)

    matches.sort(key=_sort_key, reverse=True)
    return matches[0]


def _read_data_rows(prefix: str) -> list:
    """Rows 1-2 are title/note, row 3 (index 2) is the real header, data follows."""
    with open(_resolve_csv(prefix), newline="", encoding="utf-8") as f:
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
    _, rows = _read_data_rows(_SOURCES_PREFIX)
    return {r[0]: r[1] for r in rows if r and _SOURCE_ID_RE.match(r[0])}


def _parse_sources_cell(cell: str) -> FrozenSet[str]:
    """'S1 Block Purchase · S2 Discount · S3 Historical' -> frozenset({'S1','S2','S3'})."""
    return frozenset(part.strip().split()[0] for part in cell.split("·") if part.strip())


def load_single_purpose_scripts() -> Dict[str, Dict[str, Any]]:
    """{'Sale': {'win_condition': ..., 'heading': ..., 'ask': ..., 'tell': ..., 'wish': ...,
    'sources': frozenset({...})}, ...} -- 7 columns as of the 2026-08-14 re-export (see
    module docstring): Visit Purpose, Win Condition, Dehaat Center Ko Jaano (Heading),
    What to Ask, What to Tell, What to Wish, Applicable Sources (curated)."""
    _, rows = _read_data_rows(_SCRIPTS_PREFIX)
    out = {}
    for r in rows:
        if not r or not r[0]:
            continue
        purpose, win_condition, heading, ask, tell, wish, sources = r[:7]
        out[purpose] = {
            "win_condition": win_condition,
            "heading": heading,
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
    _, rows = _read_data_rows(_COMBO_PREFIX)
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
_cached_config_mtimes: tuple | None = None


def _current_source_mtimes() -> tuple:
    """mtimes of the 3 resolved CSVs -- cheap stat() calls, not a re-parse -- used to
    detect when pitch_config/'s live sync has written new content under us."""
    return tuple(_resolve_csv(p).stat().st_mtime for p in (_SCRIPTS_PREFIX, _COMBO_PREFIX, _SOURCES_PREFIX))


def get_pitch_config() -> PitchConfig:
    """Was a true once-per-process singleton: correct for a short-lived CLI command, but
    given the module docstring's own documented active external sync churning
    pitch_config/ live, a long-running worker (gunicorn/uwsgi) that never restarts would
    keep serving whatever content it saw on its FIRST pitch generation, even after a real
    re-export changed win conditions/scripts. Re-checks the 3 source files' mtimes on
    every call (cheap) and only re-parses (not cheap) when one of them actually changed,
    same mtime-cache shape as planning.data_cache's output/*.json cache."""
    global _cached_config, _cached_config_mtimes
    mtimes = _current_source_mtimes()
    if _cached_config is None or mtimes != _cached_config_mtimes:
        _cached_config = PitchConfig()
        _cached_config_mtimes = mtimes
    return _cached_config
