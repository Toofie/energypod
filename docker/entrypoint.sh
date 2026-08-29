#!/bin/sh
# The container's entrypoint: source the operator's credential env files
# (var/solcast.env and var/pvoutput.env carry `export KEY=...` lines —
# sourcing them is the exact mechanism the standing launch command uses on
# the Windows deployment), then exec the controller.
#
# Absent files are skipped with a note: the boot-survives-absence doctrine
# (the Solcast provider and the PVOutput client compose out with visible
# notes when their references don't resolve — never a boot failure).
set -e

for env_file in /app/var/solcast.env /app/var/pvoutput.env; do
    if [ -f "$env_file" ]; then
        # shellcheck disable=SC1090
        . "$env_file"
        echo "[entrypoint] sourced $env_file"
    else
        echo "[entrypoint] NOTE: $env_file not present — the matching provider composes out with a note"
    fi
done

exec python -m energypod.main run config/config.live-write-example.yaml
