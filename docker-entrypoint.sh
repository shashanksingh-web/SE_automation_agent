#!/bin/sh
# Runs once, in every container regardless of role (web/celery_worker/celery_beat) --
# `manage.py migrate` is idempotent, so running it redundantly in all three is safe
# and avoids a race between them (whichever container starts first just does the real
# work, the others' migrate calls are near-instant no-ops). Then execs whatever CMD
# docker-compose specified for that service's actual role.
set -e

echo "[entrypoint] running migrations..."
python manage.py migrate --noinput

echo "[entrypoint] starting: $*"
exec "$@"
