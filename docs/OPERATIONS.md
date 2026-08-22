# EnergyPod controller — operations guide

Last updated: 2026-08-22 (Australia/Brisbane)

This is the operator-facing runbook for the EnergyPod controller. The
normative contracts live in `docs/API_CONTRACTS.md` (especially "Operations
surface (Milestone C)") and the safety invariants in `docs/CONTINUITY.md`;
when this guide and those documents disagree, those documents win.

Safety posture that shapes every procedure here:

- The controller is fail-closed: missing, stale, or contradictory safety
  inputs resolve to zero power, never to a guess.
- Boot and restart are observe-only and disarmed. Nothing — arming,
  authorization, active commands — is ever restored from persistence.
- One process, one event loop, one actor per unit socket. Never scale the
  controller to replicas while control is enabled (ADR-0002).
- Advisory systems may propose intents; only the deterministic safety
  kernel grants authority.
- No live hardware interaction is authorized by the build. Any commissioning
  is a separate, explicitly authorized, observe-only activity (below).

---

## 1. Quickstart

### 1.1 Prerequisites

- Python 3.12 (sole backend runtime, ADR-0002) with the pinned dependencies
  from `pyproject.toml`, installed from the repository root:

  ```powershell
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
  ```

- Node 24.19.0 with corepack providing pnpm 11.22.0 — only needed to build,
  test, or visually inspect the operator console (`web/`).
- Docker — **not required for anything in this repository today**: no image
  build has been performed because the authoring environment has no Docker
  daemon (see §6).

### 1.2 Validate a configuration (zero side effects)

```powershell
.\.venv\Scripts\energypod.exe check-config .\config\controller.yaml
```

Prints the effective configuration after strict validation and changes
nothing: no database is created, no socket opens. Always run this after
editing a configuration and before deploying it. Sample configurations ship
in `config/`:

| File | Purpose |
| --- | --- |
| `config/controller.yaml` | `run` mode, observe-only, three placeholder units, durable store on the data volume |
| `config/controller.simulate.yaml` | simulator deployment (compose `simulate` profile): observe-only site intent, the training policy, and deliberately no credential store so the development principal is minted |

### 1.3 Run (serve the guarded API against configured gateways)

```powershell
.\.venv\Scripts\energypod.exe run .\config\controller.yaml
```

`run` composes the runtime from the configuration and serves the versioned
API at `/api/v1` (default listener `127.0.0.1:8080`; the listener contract is
still pending — `docs/DEFERRED_FINDINGS.md` item 16). Two hard rules of this
mode:

- **No credential store is configured, so every bearer token is refused.**
  The authenticator is fail-closed by design; the REST boundary answers its
  structured `401 authentication_required` envelope. Until a credential store
  is commissioned, `run` mode is a telemetry/audit service you can only
  observe from the host itself.
- Units whose telemetry cannot be decoded fail closed: the unit reports no
  observations, never qualifies, and can never be armed. Run-mode telemetry
  decode is deliberately unwired until identity evidence is commissioned
  (observe-only).

Restart behavior: the process always boots disarmed regardless of what the
durable audit trail remembers. A supervisor or task failure fences every
generation, runs actor shutdown with the bounded-zero contract, and exits
nonzero rather than serving a guarded API with control authority dead.

### 1.4 Simulate (local operation, no hardware)

```powershell
.\.venv\Scripts\energypod.exe simulate .\config\controller.simulate.yaml
```

`simulate` composes the same runtime with deterministic in-process simulator
transports and in-memory persistence **regardless of the configured
endpoints and database path** — nothing opens a socket or touches hardware.
This is the supported way to exercise the whole product: arming, dispatch,
emergency stop, acknowledgement, audit, and the operator console.

**The development token.** In simulate mode — and only in simulate mode —
the process mints one deterministic development principal with full scopes
(`observe`, `audit:read`, `dispatch`, `arm`, `stop`, `stop:acknowledge`) and
interactive status, and prints its bearer token **exactly once, to stdout,
at startup**:

```
development principal token: <token>    (spelling illustrative)
```

Treat that token as the login for everything below:

- copy it from the startup output (under Docker: `docker compose logs
  simulate` — the token is printed once, so scroll to the first boot lines);
- use it as `Authorization: Bearer <token>` against `/api/v1/*`;
- it never works in `run` mode, and `run` mode never prints a token.

Because the principal is interactive with full scopes, the token can arm and
dispatch. It is a simulator-only credential: never paste it into anything
that talks to hardware, and never ship a simulate configuration to a site.

The grant is exactly "no credential store configured": a simulate
configuration that names an `authentication` credential reference stays
fail-closed (every bearer refused, no development principal minted), because
a configuration that promises operator credentials must never silently fall
back to a development one. That is why `config/controller.simulate.yaml`
declares an observe-only site, carries the training policy the safety chain
needs for realistic numbers, and has **no** `authentication` block.

### 1.5 Operator console

The console is the React build in `web/` (Vite; built artifacts land in
`web/dist`, which is git-ignored — build it in place):

```powershell
cd web
corepack pnpm install --frozen-lockfile
corepack pnpm build
```

Serve `web/dist` behind the shipped `web/nginx-spa.conf`, which serves the
SPA and proxies `/api/v1/*` (including the WebSocket event stream) and
`/healthz` to the controller — so the browser talks to one origin. The
console performs bearer authentication against the same API: in simulate
mode, paste the development token where the console asks for credentials.

**Deferred visual inspection.** The automated console coverage (states,
accessibility, wiring) lives in the vitest suites under `web/src/**/*.test.tsx`.
A human visual pass over every console state and responsive layout has not
been performed in this environment, because it needs a display. To do it:
run `energypod simulate` in one terminal, then `corepack pnpm dev` in `web/`,
and review every view (fleet, batteries, activity/control) in both desktop
and narrow layouts. Until that pass happens, console polish is verified by
the automated suites only (`docs/DEFERRED_FINDINGS.md`, Milestone B residual
queue item 6).

---

## 2. Configuration

The configuration file is a strict, immutable startup document: unknown keys
are rejected at every level, values are never silently coerced, and the
cross-validated timing budget is enforced at validation time. Both
`--config PATH` and a positional `PATH` spellings are accepted; exactly one.

Structure (see `src/energypod/runtime/config.py` for the authority):

- `site` — `site_id`, IANA `timezone`, and `expected_unit_count`, which must
  equal the number of `units`.
- `units` — one block per unit: `unit_id`, `display_name`, gateway
  `endpoint` (`host`, `port`), `transport_profile`
  (`waveshare_rtu_over_tcp`), `protocol_profile` (`iot` or `legacy`),
  `device_id` (Modbus slave id, evidence says 4), `expected_identity`, and
  `expected_cell_count`. Unit ids, identities, and endpoints must be unique.
- `timing` — the commissioned control budget. The complete operation budget
  (read + kernel + audit + write + acknowledgement + jitter + renewal
  margin) and the renewal cadence (control period + jitter + renewal margin)
  must each fit inside `device_command_expiry_s`; the write timeout must fit
  strictly inside the control period. `device_command_expiry_s` requires an
  evidence reference — a watchdog number without provenance is rejected.
- `policy` — required for `write_enabled`, forbidden-grade optional for
  observe-only: static/fleet limits, SOC/cell/temperature/imbalance bounds,
  authorization lifetime, ramp limit, blocking fault codes, and
  `debug_modes_enabled`, which must be `false`.
- `authentication` — a secret **reference** (`secret://...`), never a secret
  value; write-enabled mode requires enabled authentication. The referenced
  credential store is what the fail-closed authenticator will resolve; no
  store is composed yet.
- `storage` — `database_path` (the durable SQLite file; omit for entirely
  in-memory persistence) and `busy_timeout_ms`.

Rules of thumb:

- Under Docker the database path must stay on the `/var/lib/energypod` data
  volume so backup/restore see one durable store.
- The heartbeat cadence is not independently configurable:
  `ControlPolicy.heartbeat_interval_s` is derived from
  `timing.control_period_s`, so the kernel cadence, actor heartbeats, and
  the commissioned timing budget can never disagree.

---

## 3. Durable store lifecycle (migrate / backup / restore)

All subcommands refuse to guess and refuse to lose data (API_CONTRACTS
"Operations surface"):

- `energypod db migrate --config PATH` — applies pending schema migrations
  **transactionally**; refuses unknown or newer `schema_version` values
  rather than touching them.
- `energypod db backup --config PATH --out FILE` — produces a consistent
  snapshot through the SQLite backup API (never a mid-write file copy) and
  **refuses to overwrite an existing file**.
- `energypod db restore --config PATH --in FILE` — validates the snapshot's
  schema version and integrity, then swaps it in atomically (temp file +
  rename), and **refuses to run while a server holds the database open**
  (stop the controller first; the restore fails with a clear error instead
  of racing a live writer).

Recommended cadence:

1. Back up before every configuration change, every migration, and every
   controller upgrade: `energypod db backup --config ... --out
   backups/controller-$(date +%F-%H%M).sqlite3`.
2. Migrate deliberately: `check-config`, back up, `db migrate`, then start
   the controller and confirm `/api/v1/health` (authenticated) shows the
   repositories ready.
3. Restore as the last resort: stop the controller, restore, restart. The
   process boots observe-only and disarmed no matter what the restored trail
   contains — restored history is evidence, never authority.

In-memory deployments (no `storage` block, or simulate mode) have no
durable surface: nothing to migrate, back up, or restore.

---

## 4. Commissioning path (hardware)

No live hardware interaction is authorized by the build. Commissioning is a
separate, explicitly authorized activity that **starts observe-only**. The
sequence below is the contracted path; do not reorder it.

1. **Authorization.** Obtain explicit, separate authorization for
   observe-only operation against the specific units. Observe-only is the
   only mode that has ever been authorized.
2. **Per-unit topology.** Confirm each unit's register layout (IoT vs
   legacy), BIC/cell packing, and `expected_cell_count` against the served
   bank — the IoT packing is only safe for six BICs / 60 cells, and the
   corroborated count is 59 cells. A unit whose served bank contradicts its
   configured profile never qualifies (fail closed), so a qualified unit is
   positive evidence the topology matches.
3. **Scaling and direction.** Verify per-field scaling and the power sign
   convention against `docs/PROTOCOL_EVIDENCE.md` (never a global endian or
   sign rule): PCS/DCDC currents x0.01 A, system/BMS currents x0.1 A,
   positive discharge / negative charge on the `0x0200` objective. Public
   APIs use direction plus unsigned watts; signed conversion exists only in
   the protocol adapter.
4. **Watchdog timing.** Capture the device's actual command-expiry behavior
   per unit (the 2 s renewal is an operational observation, not a proved
   firmware constant). Set `timing.device_command_expiry_s` from that
   capture, with its evidence reference, and re-run `check-config` until the
   whole budget fits.
5. **Identity binding.** Bind `expected_identity` per unit from observed
   identity registers — an identity mismatch is a latched inhibit, by
   design.
6. Only after all of the above: install a commissioned `policy`, commission
   a credential store (which unlocks the authenticator), and proceed to
   carefully supervised arming trials.

Any unknown is a stop condition, not an assumption: "safety and protocol
behavior must not be guessed" (`docs/CONTINUITY.md`).

---

## 5. Troubleshooting

| Symptom | Meaning / action |
| --- | --- |
| Every API call returns `401 authentication_required` | The fail-closed authenticator: no credential store is composed (`run` mode default). This is correct behavior, not an outage. Commission a credential store, or use simulate mode with its development token. |
| Simulate startup output has no token | The token prints exactly once at startup — fetch it from the first boot lines (`docker compose logs simulate`, or your terminal scrollback). It is never re-printed, never logged per-request, and works nowhere but simulate mode. |
| Process exits nonzero with `serving failed: supervision halted: ...` or `supervision failed during startup` | The supervisor halted: a kernel/actor component failed, every generation was fenced, authority revoked, bounded zero attempted, and serving stopped rather than serving a guarded API with control dead. Read the reason in the message, fix the cause, restart — boot is observe-only, so a restart is always safe. |
| `/healthz` is green but units show no telemetry | `/healthz` is liveness only (process up) — never readiness, never data. Check the authenticated `/api/v1/health` three-fact view; a unit failing decode reports no observations and cannot qualify (run-mode decode is intentionally unwired pre-commissioning). |
| Audit pages seem to repeat or skip | The cursor contract: `GET /api/v1/audit?after_sequence=<oldest delivered>` resumes with strictly older facts; `next_cursor` is `null` only on a short terminal page. Pass the previous page's oldest sequence — do not re-request with a cursor newer than live state. Durably-stored sequences continue across restarts; in-memory simulate sequences restart at zero with the process. |
| A unit is `inhibited` and arm is refused | Inhibit with a latched cause (critical blocking fault, identity/profile mismatch) needs `POST /api/v1/units/{unit_id}/inhibit/acknowledge` (arm scope, interactive principal) — and acknowledgement only clears the latch; the unit still needs stable qualifying samples to reach `DISARMED`, then an explicit arm. A still-present blocking fault re-latches on the next observation. |
| Emergency stop seems stuck | Latched stops are safety-critical state, never evicted: acknowledge the exact stop id via `POST /api/v1/emergency-stop/{stop_id}/acknowledge` (stop:acknowledge scope). Acknowledgement removes the latch so it cannot relatch. |
| Published Docker port unreachable | The controller currently binds its loopback listener inside the container (listener contract pending, `docs/DEFERRED_FINDINGS.md` item 16). Probe from inside the container (`docker compose exec controller python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/healthz').status)"`) until the serving host is configurable; `HEALTHCHECK` is unaffected. |

---

## 6. Container image and Compose

Files: `Dockerfile`, `compose.yaml`, `.dockerignore`, `web/nginx-spa.conf`.

Shape (contract, "Operations surface"):

- **Multi-stage.** A `node:24.19.0-slim` stage builds `web/dist` with
  corepack-pinned pnpm 11.22.0 (`pnpm install --frozen-lockfile && pnpm
  build`); a `python:3.12.10-slim` stage installs the pinned project
  (`pyproject.toml`) into its own virtual environment (`/opt/energypod-venv`)
  and carries the built console at `/app/web/dist` (ADR-0002: one image
  contains backend + built static assets).
- **Non-root.** Fixed-uid `energypod` user (10001), no home, no shell.
- **No secrets baked.** The `.dockerignore` keeps virtualenvs, caches, test
  state, local databases, and `.env*` out of the build context; the
  configuration (which itself carries only a secret *reference*) is mounted,
  never copied.
- **HEALTHCHECK.** `GET http://127.0.0.1:8080/healthz` from inside the
  container — liveness only, the single unauthenticated endpoint.
- **Config read-only.** `/etc/energypod/controller.yaml`, mounted `:ro`.
- **Data volume.** `/var/lib/energypod` (declared `VOLUME`), which is where
  the shipped configuration's SQLite path points.
- **Compose.** One `controller` service (observe-only sample config) plus a
  `simulate` profile service running `command: energypod simulate` with the
  simulator configuration, publishing the API port.

**The image has not been built.** Docker is not installed in the authoring
environment (`docs/CONTINUITY.md`, environment notes). Ledger step 12
therefore requires **static validation here and an actual image build in a
Docker-capable environment before any deployment**. The static validation
performed — and to re-run after editing these files — is documented in the
`Dockerfile` and `compose.yaml` headers:

1. `compose.yaml` parses as YAML and every referenced path exists
   (`./Dockerfile`, `./config/controller.yaml`,
   `./config/controller.simulate.yaml`);
2. every `Dockerfile` `COPY` source exists and survives the `.dockerignore`
   filters, and `COPY --from` references only declared stages;
3. both sample configurations round-trip the strict `ControllerConfig`
   model;
4. base-image tags exist on Docker Hub (`python:3.12.10-slim`,
   `node:24.19.0-slim`) and version pins match ADR-0002 / `pyproject.toml`
   (Python 3.12, Node 24.19.0, pnpm 11.22.0);
5. `web/nginx-spa.conf` is brace/quote balanced and proxies exactly the
   served prefixes (`/api/v1`, `/healthz`);
6. the simulate service's command matches the CLI contract spelling.

Then, in a Docker-capable environment:

```sh
docker build -t energypod-controller:0.1.0 .
docker compose up -d controller                      # observe-only
docker compose --profile simulate up -d simulate     # local simulator
docker compose logs simulate                         # fetch the dev token
```

---

## 7. End-to-end verification

The capstone end-to-end scenario lives at `tests/e2e/test_simulate_e2e.py`.
It boots the composed simulate runtime through `energypod.main.main` with an
injected runner, captures the printed development token, then drives the
real REST surface through the test client with live supervision: `/healthz`
unauthenticated, fail-closed 401s, authenticated snapshot, arm, dispatch,
and the supervised tick that carries the applied setpoint to the simulated
pod and records it in the audit trail — ending with the bounded-zero
shutdown. Run it with the full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```
