#!/bin/bash
# launchd entrypoint for the 6:00 AM reconcile_outcomes job -- see
# ~/Library/LaunchAgents/com.dehaat.se-automation.reconcile.plist and
# AGENT_OPERATING_PROMPTS.md's launchd section for why this replaced cron.
set -euo pipefail
cd "$(dirname "$0")/.."
YESTERDAY=$(date -v-1d +%Y-%m-%d)
exec venv/bin/python manage.py reconcile_outcomes --date "$YESTERDAY"
