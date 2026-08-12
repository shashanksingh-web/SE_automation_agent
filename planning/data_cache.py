"""In-process cache for output/*.json reads, keyed by (path, mtime) -- a normalization
re-run changes every file's mtime, so a stale read is never served, but repeated calls
within the same process skip re-parsing large files from disk entirely.

Confirmed real cost, not a hypothetical: parsing DC_Master_Normalized.json alone is
~84ms, AOP_Target_Normalized.json ~74ms, Task_Nodes/Visits_Normalized.json ~400ms each
(measured 2026-08-12, ~12-47MB files). Two concrete places this was previously paid
redundantly, both fixed by routing through this module:
  - `run_scheduled_tuff` calls generate_plan_for_scope() once per active ScheduledScope
    (93 in this deployment) in a sequential loop; each call re-parsed DC_Master + AOP
    fresh even though neither changes between scopes in the same cron run -- ~15s of
    pure redundant I/O eliminated.
  - GET /headcount/ and every GET /directory/* endpoint are meant to be hit repeatedly
    by a frontend (dropdowns, pickers); each call previously re-parsed 1-4 of the
    largest output files from scratch every time.

Thread-safety: a plain dict + lock, not Django's cache framework -- this is a single
long-lived process's read-through cache over local files, not something that needs to
survive a restart or be shared across processes/machines."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_cache: dict = {}
_lock = threading.Lock()


def load_output_json(output_dir: Path, filename: str) -> Any:
    path = output_dir / filename
    if not path.exists():
        raise FileNotFoundError(f"{path} not found -- run se_daily_plan_agent.py (Data Normalization Agent) first.")
    mtime = path.stat().st_mtime
    key = str(path)
    with _lock:
        cached = _cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    data = json.loads(path.read_text())
    with _lock:
        _cache[key] = (mtime, data)
    return data
