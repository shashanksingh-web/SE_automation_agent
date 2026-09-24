"""Shared bounded-concurrency runner for the two "generate for every scope in a list"
commands (run_all_states_tuff.py, run_scheduled_tuff.py) -- extracted 2026-09-24 when
run_scheduled_tuff.py was parallelized to match run_all_states_tuff.py's existing
ThreadPoolExecutor pattern (previously a plain sequential `for scope in scopes:` loop,
~93 scopes at ~1.5min each = ~80min for a full pass), instead of duplicating the exact
same ThreadPoolExecutor/as_completed boilerplate a second time.

Deliberately shares ONLY the concurrency mechanics (bounded worker pool, yield results as
they complete), not any domain vocabulary -- the two callers have genuinely different
outcome handling (run_all_states_tuff's own "STATE=X: PlanRun #Y, N SEs, M tasks" stdout
format vs. run_scheduled_tuff's "{scope_type}={scope_value}: PlanRun #Y, N tasks" plus its
own FM_Urgency detail lines, plus a per-scope `scope.last_run_at` update that only
run_scheduled_tuff needs), so this helper stays generic rather than forcing a shared
outcome/payload shape neither caller actually wants."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterator, List, Tuple, TypeVar

T = TypeVar("T")


def run_scopes_concurrently(
    items: List[T], worker_fn: Callable[[T], Tuple[str, Any]], max_workers: int,
) -> Iterator[Tuple[T, str, Any]]:
    """Runs worker_fn(item) for every item under a bounded ThreadPoolExecutor, yielding
    (item, outcome, payload) as each completes -- in COMPLETION order (as_completed), not
    input order, same as both callers' existing behavior already was.

    worker_fn is responsible for its own try/except -- this helper does not catch
    anything itself, so an exception worker_fn lets escape propagates out of the
    corresponding future.result() call here and kills the whole pool (fail-loud; both
    current callers' own worker_fn closures already catch PlanningError/Exception
    themselves and return a ("planning_error"/"crashed", exception) tuple instead of
    raising, exactly so this doesn't happen for an expected per-scope failure -- see
    each command's own _generate_one_scope-style closure).

    worker_fn is also responsible for its own Django DB-connection cleanup
    (django.db.close_old_connections() in its own finally) -- each worker thread gets its
    own thread-local DB connection automatically, and nothing outside a request cycle
    closes those otherwise in this long-lived process."""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(worker_fn, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            outcome, payload = future.result()
            yield item, outcome, payload
