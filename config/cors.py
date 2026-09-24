"""Minimal CORS middleware (added 2026-09-24, real incident -- "not vvisible on
frontend", root-caused to this backend never sending Access-Control-Allow-Origin at
all, confirmed live via `curl -H "Origin: http://localhost:5173" .../directory/states/`
returning 200 with no CORS headers). The frontend (a separate repo, src/features/...)
runs on Vite's dev server (localhost:5173 by default) while this backend serves on
:8000 -- a real cross-origin browser request, which a missing Access-Control-Allow-
Origin header makes the browser silently block reading, even though the server itself
returns 200. django-cors-headers isn't a dependency of this project (checked
requirements.txt) -- a small hand-rolled middleware avoids adding a new package for
what's a handful of lines' worth of behavior.

Deliberately an explicit ALLOWLIST (CORS_ALLOWED_ORIGINS, env-configurable via
CORS_ALLOWED_ORIGINS_EXTRA, same convention as ALLOWED_HOSTS_EXTRA in settings.py),
never `Access-Control-Allow-Origin: *` -- a wildcard origin cannot be combined with
Access-Control-Allow-Credentials: true (the browser rejects that combination outright),
and this app will need credentialed (cookie-carrying) cross-origin requests once the
frontend's real login (planning.auth_views.auth_login, session-cookie-based) is wired
up -- a bare wildcard would have to be redone anyway at that point, so there's no
reason to start with one now.
"""
from __future__ import annotations

import os
from typing import Callable

from django.http import HttpRequest, HttpResponse

# The 2 local Vite dev-server origins (localhost and 127.0.0.1, Vite's default port
# 5173) stay unconditionally present so local frontend dev keeps working with no .env
# change required -- same reasoning ALLOWED_HOSTS' own 3 unconditional local entries
# use. CORS_ALLOWED_ORIGINS_EXTRA adds a real deployed frontend's origin(s),
# comma-separated, e.g. "https://planning.example.com".
CORS_ALLOWED_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"} | {
    o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS_EXTRA", "").split(",") if o.strip()
}


class CorsMiddleware:
    """Adds CORS headers only when the request's Origin header is in
    CORS_ALLOWED_ORIGINS -- every other origin gets no CORS headers at all (the
    browser's own same-origin policy still blocks those, exactly as before this
    middleware existed; this only ever widens access to explicitly allowed origins,
    never narrows it). Handles CORS preflight (OPTIONS) requests directly, short-
    circuiting before they'd otherwise hit a view's own @require_http_methods/
    @require_GET decorator and get rejected with 405 -- a real browser sends a
    preflight OPTIONS ahead of any POST/PUT/DELETE fetch with a JSON body or custom
    header, which none of this app's views are written to expect."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        origin = request.META.get("HTTP_ORIGIN")
        allowed = origin in CORS_ALLOWED_ORIGINS

        if allowed and request.method == "OPTIONS":
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        if allowed:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Credentials"] = "true"
            response["Vary"] = "Origin"
            if request.method == "OPTIONS":
                response["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
                requested_headers = request.META.get("HTTP_ACCESS_CONTROL_REQUEST_HEADERS")
                response["Access-Control-Allow-Headers"] = requested_headers or "Content-Type, X-CSRFToken"
                response["Access-Control-Max-Age"] = "86400"

        return response
