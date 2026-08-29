#!/bin/sh
# Build the two EnergyPod images ON the deployment host (the Synology) and
# save the loadable archive.
#
# Why build here: the image must match this machine's processor, and the
# build bakes var/ (credentials + database snapshot) and config/ — so the
# repo folder must be transferred to this machine COMPLETE first (see
# docs/DEPLOY_DOCKER.md §2).
#
# Usage (SSH on the Synology, from the repo root):
#   sh scripts/build-images.sh
#
# Output: energypod-images.tar.gz — load it via Container Manager
# (Image > Import) or `docker load < energypod-images.tar.gz`.
set -e

cd "$(dirname "$0")/.."
echo "== EnergyPod image build (repo root: $(pwd)) =="

# The build bakes var/ and config/ — they must be present.
if [ ! -f var/live-write.sqlite3 ] || [ ! -f var/live-credentials.json ]; then
    echo "ERROR: var/live-write.sqlite3 or var/live-credentials.json is missing."
    echo "Transfer the COMPLETE repo folder (including var/ and config/) first,"
    echo "and stop the Windows controller before the final copy (a live database"
    echo "must not be snapshotted mid-write)."
    exit 1
fi
if [ ! -f config/config.live-write-example.yaml ]; then
    echo "ERROR: config/config.live-write-example.yaml is missing."
    exit 1
fi

TAG="$(date +%Y%m%d)"
echo "== building the controller (target: runtime) =="
docker build --target runtime -t "energypod-controller:${TAG}" -t energypod-controller:latest .

echo "== building the console (target: console-nginx) =="
docker build --target console-nginx -t "energypod-console:${TAG}" -t energypod-console:latest .

echo "== saving the loadable archive =="
docker save energypod-controller:latest energypod-console:latest | gzip > energypod-images.tar.gz

echo ""
echo "== DONE =="
ls -lh energypod-images.tar.gz
echo ""
echo "Next steps:"
echo "  1. Container Manager > Image > Import > Add from file:"
echo "     energypod-images.tar.gz   (loads BOTH images)"
echo "  2. Create the containers (Project > Create > use compose.yaml),"
echo "     or over SSH:  docker compose up -d"
echo "  3. Verify from the repo root:  sh scripts/docker-verify.sh"
