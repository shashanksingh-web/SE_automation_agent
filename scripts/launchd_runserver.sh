#!/bin/bash
# launchd entrypoint for a persistent `manage.py runserver` -- see
# ~/Library/LaunchAgents/com.dehaat.se-automation.runserver.plist. Unlike
# launchd_tuff.sh/launchd_reconcile.sh (one-shot, StartCalendarInterval), this plist
# uses KeepAlive so launchd restarts this script automatically if runserver ever exits
# (crash, OOM, `kill`, etc.) -- built 2026-08-25 after a stale/dead runserver process
# silently broke the frontend (localhost:5173 proxying to :8000 with nobody listening)
# with no signal until someone happened to check.
set -euo pipefail
cd "$(dirname "$0")/.."
exec venv/bin/python manage.py runserver 8000
