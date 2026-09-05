#!/bin/bash
# Runs the API and the arq worker in one container.
#
# This exists because Render's free tier has no background worker service, no
# cron service and no shell. Everything the deployment needs — migrations, the
# job queue, the cron schedule — has to happen inside the single web container
# or not at all.
#
# Not how you would run this in production. Two processes in one container
# means they share a memory limit and a restart, and if the service ever scales
# past one instance every cron fires once per instance. See the note at the
# bottom for what to do when a worker service becomes available.

set -euo pipefail

# Idempotent: a no-op once the version table is current, so it is safe on the
# cold starts the free tier forces every time the service wakes from idle.
echo "==> running migrations"
alembic upgrade head

echo "==> starting arq worker"
arq app.workers.settings.WorkerSettings &
WORKER_PID=$!

echo "==> starting api on port ${PORT:-8000}"
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
API_PID=$!

# Neither process is optional, so the container should die if either does and
# let Render restart the whole thing. Without this the API would keep serving
# while the queue silently stopped draining — the failure mode that is hardest
# to notice, because the storefront looks healthy.
terminate() {
    trap - TERM INT
    kill "$WORKER_PID" "$API_PID" 2>/dev/null || true
    wait "$WORKER_PID" "$API_PID" 2>/dev/null || true
}
trap terminate TERM INT

# Not a bare `wait -n`: under `set -e` a non-zero return would kill the script
# before the cleanup below could run, leaving the surviving process orphaned.
wait -n && EXIT_CODE=0 || EXIT_CODE=$?
echo "==> one process exited (status ${EXIT_CODE}); shutting the container down"
terminate
exit "${EXIT_CODE}"

# When you move to a paid plan: delete this file, restore the plain uvicorn CMD
# in the Dockerfile, move `alembic upgrade head` to the pre-deploy command, and
# add a Background Worker service running
#   arq app.workers.settings.WorkerSettings
# against the same DATABASE_URL and REDIS_URL.
