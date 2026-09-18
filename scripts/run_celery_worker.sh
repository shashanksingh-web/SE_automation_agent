#!/bin/bash
# Celery worker -- added 2026-09-18, replacing the macOS-specific scheduling this
# project used to depend on (launchd plists, then plain crontab hardcoding an absolute
# venv path). Portable: this script and celery itself have no macOS/launchd
# dependency, run identically on any OS.
#
# --pool=solo (not the default prefork pool): this environment (Celery 5.6.3 +
# billiard + Python 3.14's spawn-based multiprocessing) hits a real, reproducible
# `ValueError: not enough values to unpack (expected 3, got 0)` in billiard's
# fast_trace_task path on every task under the default prefork pool -- confirmed live,
# not a code bug in this project. solo runs everything in the single main process; the
# two scheduled tasks here never run concurrently with each other in practice (they're
# fire-and-forget daily jobs, not a high-throughput queue), so solo's lack of
# parallelism costs nothing real. Revisit this flag if celery/billiard/Python versions
# change and the prefork pool becomes viable again.
set -euo pipefail
cd "$(dirname "$0")/.."
exec venv/bin/celery -A config worker --loglevel=info --pool=solo
