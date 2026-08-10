"""Minimal alerting -- posts to a Slack incoming webhook when ALERT_WEBHOOK_URL is set.
No-op (log-only) when it isn't, so alerting never becomes a hard dependency for anyone
running TUFF without a webhook configured. Uses `requests`, already a dependency via
se_daily_plan_agent.py -- no new package needed.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

logger = logging.getLogger("planning.notify")


def send_alert(message: str, severity: str = "error") -> None:
    """Fire-and-forget -- a failed alert must never itself crash the run it's reporting
    on, so any exception here is caught and logged, not raised."""
    webhook_url = getattr(settings, "ALERT_WEBHOOK_URL", "") or ""
    if not webhook_url:
        logger.warning("[no ALERT_WEBHOOK_URL configured] %s: %s", severity.upper(), message)
        return
    try:
        requests.post(webhook_url, json={"text": f"[{severity.upper()}] TUFF: {message}"}, timeout=10)
    except Exception:
        logger.exception("Failed to deliver alert (severity=%s): %s", severity, message)
