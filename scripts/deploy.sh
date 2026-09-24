#!/bin/sh
# Rebuilds and restarts web, celery_worker, and celery_beat TOGETHER, as one command --
# added 2026-09-24 after a real incident: web, celery_worker, and celery_beat share one
# Dockerfile/build context (docker-compose.yml's `build: .` on all three service
# definitions) but get three SEPARATELY-TAGGED images since none declares an explicit
# `image:` key (Compose defaults each to `<project>-<service>:latest`) -- rebuilding one
# has zero effect on the other two's already-running containers, since `docker exec`
# operates against a container's already-started process/filesystem, frozen at whatever
# image it was created from.
#
# Confirmed live, same day: celery_worker ran a container built from days-stale source
# for an extended period after a real, already-merged bug fix (Club/Scheme pitch
# segmentation, merged 2026-09-21) had already landed in git -- because only `web` had
# been rebuilt+restarted, twice, by mistake. This script exists so there is exactly ONE
# correct command to run after any code change, instead of three easy-to-forget ones.
#
# Usage: ./scripts/deploy.sh   (run from anywhere -- cd's to this script's own directory
# first, so docker-compose.yml is always found regardless of the caller's cwd)
set -e

cd "$(dirname "$0")/.."

SERVICES="web celery_worker celery_beat"

echo "=== Building $SERVICES ==="
docker-compose build $SERVICES

echo ""
echo "=== Verifying all three images actually match (they share one Dockerfile/build context) ==="
IMAGE_IDS=$(docker-compose images $SERVICES | tail -n +2 | awk '{print $5}' | sort -u)
IMAGE_ID_COUNT=$(echo "$IMAGE_IDS" | grep -c .)
if [ "$IMAGE_ID_COUNT" -ne 1 ]; then
    echo "ABORTING: $SERVICES did not all build to the same image ID -- this should be"
    echo "structurally impossible (same Dockerfile, same build context, same command run"
    echo "just above) unless something unusual happened (a build cache inconsistency, a"
    echo "partial/interrupted build). Re-run this script; if it persists, investigate"
    echo "before restarting anything -- restarting with mismatched images is exactly the"
    echo "bug this script exists to prevent."
    docker-compose images $SERVICES
    exit 1
fi
echo "OK -- all three services share image ID: $IMAGE_IDS"

echo ""
echo "=== Restarting $SERVICES together ==="
docker-compose up -d $SERVICES

echo ""
echo "=== Done -- $SERVICES are now all running the same, just-built code ==="
docker-compose images $SERVICES
