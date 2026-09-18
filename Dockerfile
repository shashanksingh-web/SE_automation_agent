# Production image for this Django + Celery app. One image, three roles (web/
# celery worker/celery beat) -- docker-compose.yml picks the role per service via
# `command:`, docker-entrypoint.sh runs migrations once before any of them starts.
#
# Python 3.12, not the 3.14 this project has been developed against locally --
# deliberate choice for a production image (a well-established, widely-deployed
# version), and it sidesteps a real, confirmed bug hit during local Celery setup:
# Celery's default prefork pool (billiard, spawn-based multiprocessing) raised
# `ValueError: not enough values to unpack (expected 3, got 0)` on every task under
# Python 3.14 + macOS specifically. --pool=solo (see docker-compose.yml's celery_worker
# command) is kept as the safe default here regardless, since Linux containers default
# to fork-based multiprocessing (a different code path than macOS's spawn default) and
# this hasn't been re-tested under 3.12+Linux+prefork -- solo costs nothing real for
# this app's two low-frequency, fire-and-forget scheduled tasks, so there's no reason
# to gamble on prefork working differently here without evidence either way.

FROM python:3.12-slim AS builder

WORKDIR /app

# build-essential: defensive, not confirmed required -- ortools/psycopg2-binary both
# ship manylinux wheels for the common case, but a transitive dependency needing a
# source build on an unexpected platform/arch should not silently break the image
# build. Removed entirely from the final runtime stage below either way.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


FROM python:3.12-slim AS runtime

# libpq5: psycopg2-binary bundles its own libpq internally, so this is usually not
# required -- kept anyway since it's a tiny, well-known runtime dependency for any
# psycopg2 install and the cost of being wrong about "usually" is a container that
# crashes on first Redshift query, not a build-time failure that's easy to catch.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --chown=appuser:appuser . .

# Mutable/operator-managed data lives on volumes (see docker-compose.yml), not in the
# image -- these are just the mount points, created here so the volume mounts (and the
# app writing into them before a mount is attached, e.g. during a `docker build`-only
# smoke test) don't fail on a missing directory.
#
# chown /app itself (not just its contents): WORKDIR created /app as root before
# anything else ran, and COPY --chown only sets ownership on the copied entries, not
# retroactively on the pre-existing directory they landed in -- confirmed live, this
# left appuser able to read/modify existing files but NOT create new ones directly in
# /app, which broke celery beat (it writes its celerybeat-schedule dbm file to the
# current working directory): `PermissionError: [Errno 13] Permission denied:
# 'celerybeat-schedule'`.
RUN mkdir -p /app/output /app/logs && \
    chown appuser:appuser /app && \
    chown -R appuser:appuser /app/output /app/logs && \
    chmod +x docker-entrypoint.sh

USER appuser

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
# --timeout 900: the single-scope plan-generation endpoints (se_plan/state_plan/tuff/
# normalize) are still fully synchronous by design (only admin_generate_all_states was
# moved to Celery) -- a real STATE-scope generation has been observed taking up to
# ~750s end to end (live Redshift pulls + LLM routing/pitch calls per DC), so
# gunicorn's default 30s worker timeout would kill the request mid-generation. 900s
# gives real headroom above the largest observed run. This is a known, accepted
# limitation of dockerizing the existing synchronous architecture as-is, not something
# this Docker setup itself was asked to redesign.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "900", "--access-logfile", "-", "--error-logfile", "-"]
