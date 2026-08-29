# Deploying the controller in Docker — the operator's runbook

The permanent home: the always-on Docker host that ran the old
battery-manager container.  The deployment is ALL-IN-ONE: one build bakes
the controller, the console, the commissioned config, the credentials, and
the database snapshot into two images.  No volumes, no mounts, no separate
file staging on the host: build where the repo (with `var/` and
`config/`) lives, then deploy the images.

**The image carries secrets** (the operator accepted this for a local-only
deployment, 2026-08-29): never push it to a registry, and treat the saved
image archive like a credential file.

## 1. Prerequisites

- On the BUILD machine (where this repo lives, with `var/` and `config/`):
  Docker with the compose plugin — OR build on the host (then the whole
  repo incl. `var/` and `config/` transfers there first).
- On the HOST: Docker Engine with compose, and network reach to the three
  gateways `192.168.1.11/12/13` port `4196` (the old battery-manager
  container proved this path).

## 2. Build (where the repo + var/ + config/ live)

```sh
docker compose build
```

The build bakes: `config/` (revision 11 — the commissioned configuration),
`var/live-credentials.json` (the console's bearer tokens),
`var/solcast.env` + `var/pvoutput.env` (the API keys, sourced by the
entrypoint), and `var/live-write.sqlite3` (**the build-day snapshot of the
durable store** — historian, leases, PVOutput + calibration state).

## 2b. Synology route — build ON the host (recommended)

The image must match the Synology's processor, and this Windows machine
has no Docker — so the build runs ON the Synology:

1. **Enable SSH** on the Synology: DSM → Control Panel → Terminal & SNMP
   → Enable SSH service.
2. **Transfer the COMPLETE repo folder** to a shared folder (File Station
   handles a zip upload; unzip on the host over SSH).  It must include
   `var/` (database + credentials) and `config/` — the build bakes them.
3. **Stop the Windows controller** before the final copy of
   `var/live-write.sqlite3` (a live database must not be snapshotted
   mid-write), then SSH in and build:

```sh
cd /volume1/homes/<you>/energypod        # the transferred repo root
sh scripts/build-images.sh
```

The script builds both images and writes `energypod-images.tar.gz` — the
loadable archive (which is also exactly what was asked for).  Load it via
Container Manager → Image → Import → Add from file, then create the
containers from `compose.yaml` (Project → Create), or simply
`docker compose up -d` in the same SSH session — compose ships with
DSM 7.2's Container Manager.

The rest of this runbook (verification, day-to-day, trade-offs) applies
unchanged.

## 3. Deploy

```sh
# If the images were built away from the host:
docker save energypod-controller energypod-console | gzip > energypod-images.tar.gz
# ...transfer + load on the host:  docker load < energypod-images.tar.gz

# On the host:
docker compose up -d
```

`restart: unless-stopped` is set on both services: crashes and host
reboots self-heal.  The operator's nightly restart (`docker restart
energypod-controller`) is also fine — and with `site.boot_armed: true`
(the embedded config), **the fleet re-arms itself at every boot**: no
manual re-arm after restarts.

## 4. Verification

```sh
./scripts/docker-verify.sh          # console port 8080
docker compose logs --tail=50 energypod
```

Expected: `ALL CHECKS PASS`; the controller log shows the current schema
version, config revision 11, no credential notes, and the boot-arm audit
rows.  Then the browser: `http://<host>:8080` — same bearer tokens (baked),
same screens, the three pods answering.

Optional deeper check with the operator token:

```sh
ENERGYPOD_TOKEN=<token> ./scripts/docker-verify.sh
```

## 5. Cutover from the Windows deployment (the ordered dance)

1. Build (§2) — note the DB snapshot is taken from `var/` **at build
   time**, so do this AFTER the Windows controller has been stopped (a
   live sqlite file must not be snapshotted mid-write), or stop → build.
2. Deploy on the host (§3) and verify (§4).
3. Retire the Windows instance — do not restart it.  (Keep the Windows
   copy of the repo for rollback, §7.)

**Never run both controllers against the pods at once** — two Modbus
writers interleave.

## 6. Day-to-day — and the one trade-off

| Task | Command |
|---|---|
| Logs | `docker compose logs -f energypod` |
| Nightly restart | `docker restart energypod-controller` — data persists; the fleet re-arms itself |
| **Upgrade / rebuild** | `docker compose build && docker compose up -d` |

**The rebuild trade-off, stated plainly:** the database inside the
container persists across RESTARTS (the container's writable layer), but a
REBUILD re-bakes `var/live-write.sqlite3` as it stood at build time — so a
rebuild resets history to the build-day snapshot.  If the historian data
matters at upgrade time, copy the current database out of the container
first and back in after:

```sh
docker cp energypod-controller:/app/var/live-write.sqlite3 ./live-write.sqlite3
# ...rebuild...
docker cp ./live-write.sqlite3 energypod-controller:/app/var/live-write.sqlite3
docker compose restart energypod
```

**Arming:** the embedded config sets `site.boot_armed: true` — every boot
arms all commissioned units automatically (audited under the boot
principal; a latched emergency stop still suppresses it, and a unit that
fails identity or is parked stays out).  Manual disarm from the console
still works and lasts until the next restart, which re-arms per the
config.  Boot-arming deliberately revises the old "boot never arms"
posture for this deployment — the operator accepted it for unattended
appliance operation.

## 7. Rollback

```sh
docker compose down          # on the host
```
…then restart the Windows instance (its `var/live-write.sqlite3` continues
the history as of the build-day snapshot — anything the container wrote
since the build lives only in the container layer; copy it out per §6
first if it matters).

## 8. Notes

- **Timezone**: both containers set `TZ=Australia/Brisbane`; the
  civil-time windows read `site.timezone` from the baked config — the two
  agree.
- **Gateways**: the controller dials OUT to `192.168.1.11/12/13:4196` on
  the default bridge network — no host networking needed.
- **Ports**: the console publishes `8080` (browser habit unchanged); the
  controller's raw API publishes on `127.0.0.1:8081` — host-local only.
- **Config changes**: edit `config/`, rebuild, redeploy — the config is
  baked (config-revision discipline still applies).
