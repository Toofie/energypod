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
loadable archive (which is also exactly what was asked for).

**The host runs DSM 7.1.1 — the old Docker package, NOT Container Manager**
(confirmed 2026-09-04; Container Manager needs DSM 7.2+, which is why there
is no Projects tab).  Two ways to run the pair:

**(a) GUI (no SSH)** — the goal is: both containers on ONE user-defined
bridge network, with the controller NAMED `energypod` (on a user-defined
network the container name IS the DNS name the console's nginx resolves;
the default bridge has no DNS — hand-made containers on it crash-loop with
`host not found in upstream "energypod"`):

1. Image tab: confirm BOTH `energypod-controller` and `energypod-console`
   are listed (import the archive: Action > Import > Add from file — or
   `docker load` over SSH).
2. Network tab: Add → name `energypod-net`; IPv4 "Get network
   configuration automatically"; leave "Disable IP masquerade" UNCHECKED
   (the controller dials OUT to the gateways through it) and IPv6 off →
   OK.  The DSM wizard never asks for a driver — what it creates IS a
   user-defined bridge (Synology KB: user-defined bridges are what enable
   container-name DNS).
3. Delete any hand-made containers from earlier attempts.
4. Image tab → `energypod-controller` → Launch:
   container name **`energypod`** (exactly), Advanced → Network =
   `energypod-net`, NO port mapping needed, enable auto-restart.
   Wait until it is Running.
5. Image tab → `energypod-console` → Launch: name `energypod-console`,
   Network = `energypod-net`, port local **8080** → container **80**,
   enable auto-restart.
6. Browser: `http://<nas-ip>:8080`.  After a NAS reboot the console may
   loop briefly until the controller is up — auto-restart settles it (or
   restart the console once by hand).

**(b) SSH with compose** — `docker load` the archive, install the compose
CLI by hand (the 7.1 package does not ship it), then `docker-compose up -d`
with the repo's compose.yaml.

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
./scripts/docker-verify.sh          # console port 8080 (default)
./scripts/docker-verify.sh 8082     # the operator's NAS maps the console to 8082
```

**Console login:** the login field takes the credential KEY (the `live-…`
string naming the entry in `var/live-credentials.json`, not its `subject`).
The events WebSocket stays in a retry loop until login — by design it buys
a single-use ticket with the bearer key first, so a fresh browser origin
errors until the key is entered (observed on the NAS, 2026-09-04).

Expected: `ALL CHECKS PASS`; the controller log shows the current schema
version, config revision 11, no credential notes, and the boot-arm audit
rows.  Then the browser: `http://<host>:8080` — same bearer tokens (baked),
same screens, the three pods answering.

Optional deeper check with the operator token:

```sh
ENERGYPOD_TOKEN=<token> ./scripts/docker-verify.sh
```

### 4b. If the console crash-loops with `host not found in upstream "energypod"`

The console's nginx resolves the controller by the compose SERVICE name
(`energypod`) on the project network.  That name only exists when both
containers are created by the SAME compose project — a console container
created by hand (`docker run`, or a one-off UI container) lands on the
default bridge, can't resolve `energypod`, and exits at startup in a
restart loop.  (The give-away is a container name like
`energypod-console1`.)

Fix: delete the hand-made containers, and bring the pair up as ONE project —
`docker compose up -d`, or Container Manager → Project → Create with the
compose.yaml.  Both images are already loaded; compose uses them and does
not rebuild.

### 4c. If the event stream shows "connection loss, reconnecting" (REST fine)

The controller rejects a WebSocket whose Origin netloc does not equal the
Host header it receives (`_validate_websocket_origin`, rest.py).  The
console's nginx used to forward `Host $host` — port stripped — so a browser
on any non-standard port (`box:8082`) failed that check with an opaque
empty 403 while every REST call kept working.  Fixed 2026-09-04 by
forwarding `Host $http_host` (docker/nginx.conf).  Redeploy just the
CONSOLE image to pick it up — `docker compose build console` — the
controller and its data are untouched.  (Proven by handshake probe:
Origin `box:8082` + Host `box` → 403; Host `box:8082` → 101.)

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
- **Serve bind**: the controller defaults to loopback; the entrypoint moves
  it to `0.0.0.0` (`ENERGYPOD_SERVE_HOST`, main.py) because the console's
  nginx proxies cross-container.  Nothing else moves the bind.
- **Config changes**: edit `config/`, rebuild, redeploy — the config is
  baked (config-revision discipline still applies).
