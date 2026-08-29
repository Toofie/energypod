#!/bin/sh
# The post-deploy verification (docs/DEPLOY_DOCKER.md §5) — run on the Docker
# host after `docker compose up -d --build`.  Every check prints its own
# pass/fail line; the script exits non-zero if any fail.
#
# Usage: ./scripts/docker-verify.sh [console-port]   (default 8080)

set -e
PORT="${1:-8080}"
BASE="http://127.0.0.1:${PORT}"
FAILURES=0

check() {
    name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "  ok    $name"
    else
        echo "  FAIL  $name"
        FAILURES=$((FAILURES + 1))
    fi
}

echo "verifying the EnergyPod deployment at ${BASE} ..."

# 1. The controller is alive (unauthenticated liveness, via the console proxy).
check "controller health (/healthz)" \
    curl -sf -m 5 "${BASE}/healthz"

# 2. The console is served (the SPA's index).
check "console served (index.html)" \
    curl -sf -m 5 "${BASE}/" | grep -q "<!doctype html>\|<!DOCTYPE html>"

# 3. The guarded API answers through the proxy — an unauthenticated snapshot
#    must be REFUSED (401/403), which proves the API and the proxy are up
#    without needing a bearer token here.
code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "${BASE}/api/v1/snapshot" || echo 000)
if [ "$code" = "401" ] || [ "$code" = "403" ]; then
    echo "  ok    guarded API refuses anonymous reads (HTTP ${code})"
else
    echo "  FAIL  guarded API check (expected 401/403, got ${code})"
    FAILURES=$((FAILURES + 1))
fi

# 4. The controller sees the three batteries — needs the operator bearer
#    token (ENERGYPOD_TOKEN env var, the value from var/live-credentials.json).
if [ -n "${ENERGYPOD_TOKEN:-}" ]; then
    check "snapshot carries the fleet" \
        sh -c "curl -sf -m 5 -H 'Authorization: Bearer ${ENERGYPOD_TOKEN}' '${BASE}/api/v1/snapshot' | grep -q '\"units\"'"
else
    echo "  SKIP  fleet snapshot (set ENERGYPOD_TOKEN to check)"
fi

if [ "$FAILURES" -eq 0 ]; then
    echo "ALL CHECKS PASS"
else
    echo "${FAILURES} CHECK(S) FAILED — see docker compose logs energypod"
    exit 1
fi
