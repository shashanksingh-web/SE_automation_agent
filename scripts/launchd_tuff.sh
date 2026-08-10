#!/bin/bash
# launchd entrypoint for the 6:15 AM run_scheduled_tuff job -- see
# ~/Library/LaunchAgents/com.dehaat.se-automation.tuff.plist and
# AGENT_OPERATING_PROMPTS.md's launchd section for why this replaced cron.
set -euo pipefail
cd "$(dirname "$0")/.."
exec venv/bin/python manage.py run_scheduled_tuff
