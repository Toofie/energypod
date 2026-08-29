# Deploying the controller in Docker — the operator's runbook

The permanent home: the always-on Docker host that ran the old
battery-manager container.  The controller runs as two containers —
`energypod` (the controller: Modbus to the three gateways, the sqlite
store, the guarded API) and `console` (nginx: the console's static files,
proxying `/api/v1` — WebSocket included — and `/healthz` to the
controller).  `restart: unless-stopped` is the point: crashes and host
reboots self-heal, with no Windows session involved.

## 1. Prerequisites (on the Docker host)

- Docker Engine with the compose plugin (`docker compose version`).
- Network reach from the host to the three gateways:
  `192.168.1.11`, `192.168.1.12`, `192.168.1.13` — port `4196` each.
  The old battery-manager container proved this path.
- Outbound internet for the build (dependency wheels, node packages) —
  one-time per build.

## 2. Transfer (Windows machine → Docker host)

From the repo root, transfer these paths, preserving layout:

| Path | What | Destination on host |
|---|---|---|
| `Dockerfile`, `compose.yaml`, `.dockerignore`, `docker/`, `scripts/`, `src/`, `web/`, `pyproject.toml`, `README.md` | the build context | the repo root on the host (e.g. `~/energypod/`) |
| `config/` | the commissioned configuration (revision 11) | `config/` |
| `var/live-write.sqlite3` | **the durable store** — historian, leases, PVOutput + calibration state | `var/` |
| `var/live-credentials.json` | the bearer-token store (the console's logins) | `var/` |
| `var/solcast.env`, `var/pvoutput.env` | the API-key env files (sourced by the entrypoint) | `var/` |

Easiest transfer: copy the whole repo folder, then delete the host copies
of `.git`, `.venv`, `web/node_modules`, `web/.design-shots` (the
`.dockerignore` keeps them out of the image regardless).

## 3. One-time host preparation

The container runs as uid 1000 and must own the database:

```sh
cd ~/energypod
sudo chown -R 1000:1000 var/
chmod +x scripts/docker-verify.sh
```

## 4. Cutover sequence (the ordered dance)

1. **Stop the Windows controller first** — a live sqlite file must not be
   copied mid-write.  Stop the harness task (or close its window).
2. Copy `var/live-write.sqlite3` (and the rest of §2, if not already done).
3. Build and start:
   ```sh
   docker compose up -d --build
   ```
4. Verify (§5).  Do not proceed until it passes.
5. Retire the Windows instance — do not restart it; its replacement is live.
   (Keep the Windows copy of the repo for rollback, §7.)

## 5. Verification

```sh
./scripts/docker-verify.sh          # the console port, default 8080
docker compose logs --tail=50 energypod
```

Expected: `ALL CHECKS PASS`; the controller log shows the schema at the
current version, the config at revision 11, and no credential notes.
Then the browser: `http://<host>:8080` — log in as before (the same
bearer tokens came across in `var/live-credentials.json`), confirm the
three pods answer and the Home cards render.

Optional deeper check with the operator token:

```sh
ENERGYPOD_TOKEN=<token> ./scripts/docker-verify.sh
```

## 6. Day-to-day

| Task | Command |
|---|---|
| Logs | `docker compose logs -f energypod` |
| Restart | `docker compose restart energypod` |
| Upgrade (new code) | `git pull && docker compose up -d --build` — the database persists in `var/` |
| Stop everything | `docker compose down` (data is untouched — it lives in `var/`) |

**The arm caveat, prominently:** a controller restart starts the fleet
**DISARMED** (boot never arms — the doctrine that makes restarts safe).
Armed state does not survive ANY restart, on any host.  After an upgrade
or a crash-restart, arm the fleet from the console before any program that
dispatches (night charge, the health-watch probe, the calibration
traverse).  Parked units and the PVOutput toggle DO survive restarts
(durable store) — only the armed/disarmed switch does not.

## 7. Rollback

```sh
docker compose down          # on the host
```
…then restart the Windows instance (the harness task).  The database is
the single durable fact: whichever controller starts with the newest
`var/live-write.sqlite3` continues the history.  Do not run BOTH
controllers against the pods at once — two Modbus writers interleave.

## 8. Notes

- **Timezone**: both containers set `TZ=Australia/Brisbane`; the
  civil-time windows also read `site.timezone` from the config — the two
  agree.
- **The gateways**: the controller dials OUT to `192.168.1.11/12/13:4196`
  on the default bridge network — no host networking or extra ports needed.
- **Ports**: the console publishes `8080` (browser habit unchanged); the
  controller's raw API publishes on `127.0.0.1:8081` — host-local only,
  for scripts and verification, never the LAN.
- **Upgrades**: image rebuilds are stateless; everything durable is in the
  `var/` mount.  Config changes follow the usual config-revision discipline
  (edit `config/`, restart `energypod`).
