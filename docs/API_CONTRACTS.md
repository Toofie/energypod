# Internal contracts v1

These names are the test-first contract for the initial implementation.

## Domain

### Values and enums

- `Direction`: `CHARGE`, `DISCHARGE`, `IDLE`.
- `DataQuality`: `GOOD`, `STALE`, `MISSING`, `BAD`, `SUSPECT`.
- `UnitLifecycle`: `BOOT`, `OBSERVE_ONLY`, `DISARMED`, `ARMED_IDLE`, `ACTIVE`,
  `INHIBITED`, `STOPPING`, `DISCONNECTED`.
- `IntentSource`: `EMERGENCY_STOP`, `MANUAL`, `AGENT`, `OPTIMIZER`, `SCHEDULE`.
- `DecisionStatus`: `AUTHORIZED`, `CLAMPED`, `REJECTED`, `REVOKED`.

`PowerIntent` is immutable and contains id, source, selected unit ids, direction, non-negative watts
(fleet total), duration, server-assigned acceptance monotonic time, server-assigned acceptance
revision, and actor identity. Clients cannot choose ordering metadata. Idle must have zero watts.

`UnitSetpoint` is immutable and contains unit id, direction, non-negative watts, reactive vars,
generation, intent id, and monotonic authorization expiry. Domain power is not constrained by a wire
register. Direction-to-sign conversion and signed 16-bit range enforcement occur only in the
protocol adapter; out-of-range commands are rejected, never wrapped.

`Observation` is immutable and includes unit identity, wall timestamp, monotonic capture time,
sequence, lifecycle, protocol profile, system SOC, BMS SOC, SOH, signed battery watts, pack voltage
and current, dynamic charge/discharge limits, cells, temperatures, active faults/warnings, and a
quality map. Cell data carries its own monotonic capture time and sequence because it is polled less
frequently. Derived properties expose age, cell age, cell min/max/imbalance, and safety-data
completeness.

`ControlPolicy` is immutable, strict, and versioned. It contains static per-unit/fleet limits,
SOC/cell/temperature/imbalance limits, SOC consistency and jump thresholds, telemetry/cell maximum
ages, authorization lifetime, heartbeat interval, ramp limit, stable samples needed to rearm,
reactive limit (zero by default), blocking fault codes, and debug-mode enable (false by default).

## Application ports

- `Clock.wall_now() -> datetime`, `Clock.monotonic() -> float`, async `Clock.sleep(seconds)`.
- `UnitIO.read_holding(address, count)`, `write_registers(address, values)`, `close()`.
- `ObservationRepository.latest(unit_id)`, `all_latest()`, `append(observation)`, `history(...)`.
- `AuthorizationRepository.publish(batch)`, `current(unit_id, now_mono)` (single-use),
  `peek(unit_id)` (non-consuming projection read returning the currently valid capability —
  not-before satisfied and unexpired — or `None`; used by snapshot views, never by control),
  `revoke(...)`.
- `IntentRepository.add(intent)`, `active(now_mono)` (expiry-filtered; emergency-stop
  intents stay active until acknowledged), `remove(stop_id)` for acknowledged stops.
- `AuditRepository.append(event)`, `recent(limit)`.
- `ScheduleRepository.get()`, `replace(version, entries)`.
- Provider families: `TariffProvider`, `WeatherProvider`, `PvForecastProvider`,
  `LoadForecastProvider`. They publish advisory data only and cannot import control adapters.

## Safety kernel and arbitration

`FleetAllocator.allocate(intent, observations, policy) -> ProposedSetpoints` deterministically
allocates a fleet-total request across selected units using direction-specific headroom, without
reversing direction or multiplying the request, and with an exact sum after capacity clamping.

`SafetyKernel.evaluate(proposed_setpoints, current_observations, previous_observations, policy,
now_mono) -> ControlDecision` is deterministic and side-effect free. Unknown, stale, invalid,
incomplete, contradictory, or implausibly jumping safety data rejects non-zero power. Zero/stop is
always permitted. It applies dynamic device limits, static unit/fleet limits, ramp limits,
SOC/cell/temperature/imbalance constraints and returns stable machine-readable reason codes. Cell
sequence monotonicity is non-decreasing: an unchanged cell sequence between consecutive
observations is permitted because cell blocks poll less frequently than the control rate, with
freshness enforced by the maximum cell age; a regressed cell sequence rejects.

`IntentArbiter.select(intents, now_mono)` removes expired intents and applies priority:
emergency stop > manual > agent > optimizer > schedule > idle. Equal-priority conflicts resolve by
highest server-assigned acceptance revision then stable id ordering. Emergency stop remains latched
until an operator with stop-acknowledge scope acknowledges the exact stop id; acknowledgement removes
the latched stop from the intent repository so it cannot immediately relatch.

`ControlKernel.tick()` obtains the selected intent and current observations, evaluates safety,
creates and durably appends a canonical correlated `AuditEvent`, and publishes short-lived
authorizations. The canonical audit-event factory is required at construction; a kernel cannot be
composed without one, so authority is never granted on a degraded audit trail. The audited
observation basis is every observation the kernel held for the cycle; selected units without
telemetry contribute no sequences and cannot crash the audit path. Every renewal receives a fresh
`cycle_id` within the current fencing `generation`. Generation advances only when an invalidating
event (stop, reconnect, replacement, inhibit, shutdown, or explicit revocation) fences older work.
A cycle fenced between minting and publication still receives a durable audit record, appended
after the revocation with zero authorized watts, so fencing never produces an audit gap. If
evaluation or audit persistence fails, it revokes all authorization. Repeated cycles in one
healthy generation are permitted, but a consumed `(unit_id, generation, cycle_id)` capability can
never be replayed.

## Unit actor

One `EnergyPodActor` owns one transport. No other object receives that transport.

- Starts in observe-only and verifies profile, identity, expected cell count and stable observations.
- Polls using the selected register-layout strategy and publishes observations.
- Before every non-zero heartbeat it asks `AuthorizationRepository.current(unit_id, now)`.
- It writes only if authorization is current, generation is not older, lifecycle permits control,
  and telemetry connection epoch and sequence exactly match the observation bound into the
  authorization. A newer unassessed observation requires a new authorization rather than reusing
  authority derived from older evidence.
- Replacement/stop increments the generation fence. An older task can never write afterward.
- Write failure revokes locally, attempts one bounded zero write, inhibits the unit, and stops renewal.
- The actor exposes a public bounded-zero request that enqueues exactly one bounded zero write
  through the mailbox; the service facade uses it for emergency stop. A unit fence (including
  reconnect fences) revokes that unit's outstanding authorizations, and snapshot projections
  reflect that revocation.
- Inhibit is never cleared by bad telemetry: a non-qualifying observation resets the stable-sample
  count and preserves the inhibit. Non-latching recovery requires the configured count of stable
  qualifying observations and returns the unit to `DISARMED`, never directly to `ACTIVE`; nonzero
  power still requires an explicit arm.
- Cancellation/shutdown revokes, fences, attempts bounded zero, closes transport, and relies on
  firmware expiry if zero cannot be delivered. The bounded zero attempt is made even when the
  mailbox owner task is already dead; shutdown never closes a transport without first trying zero.
- Reads and writes are serialized. Heartbeat deadlines have priority over telemetry reads; an overdue
  read is abandoned rather than delaying a heartbeat past its safety margin.

## Protocol adapter

- Explicit `WAVESHARE_RTU_OVER_TCP` profile uses PyModbus `FramerType.RTU`, configurable device id,
  zero-based addresses, FC03 and FC16.
- IoT profile detection: read 7 holding registers at 0x5000; register 0 > 10. String-enable offset 4,
  BIC count offset 5. Unknown profile or unexpected BIC count is observe-only.
- Legacy reads may be decoded for observation, but legacy writes are disabled until separately
  commissioned.
- PQ command is FC16 at 0x0200 with `[1, signed-P, signed-Q]`; stop is `[1,0,0]`.
- Field-specific endianness and scaling come from `PROTOCOL_EVIDENCE.md`; no global endian rule.

## Schedule

Entries are immutable, timezone-aware local windows with day set, action, positive fleet watts,
selected units and effective date range. Cross-midnight windows are supported; overlaps at the same
priority are rejected. Evaluation returns a short-lived schedule intent, never a hardware command.

## API and MCP

- REST is versioned at `/api/v1`. The service API uses bearer authentication; reads require
  `observe` (and audit additionally requires `audit:read`), intent mutations require `dispatch`,
  and arming requires both `arm` and an interactive human principal. Maintenance is absent. The
  browser session/OIDC adapter is a separate boundary and must use secure HTTP-only same-site
  cookies, CSRF protection, trusted origins, and recent-authentication policy before deployment; a
  raw ambient cookie is never accepted as a service-API bearer credential.
- MCP is read-only by default. Optional dispatch requires explicit configuration, the `dispatch`
  scope, and a separately issued rotatable automation credential (human operator sessions may also
  hold that scope). It submits ordinary bounded, expiring intents and cannot arm, acknowledge stops
  or inhibits, change policy, or use debug/maintenance modes. Its audit view requires both
  `observe` and `audit:read`, matching the REST boundary.
- `GET /api/v1/audit` accepts `after_sequence` (integer >= 0, default absent) passed through
  to the facade as the oldest-delivered cursor; pagination continues until `next_cursor` is
  null. `POST /api/v1/disarm` mirrors arm with the `arm` scope but does not require an
  interactive principal (disarming is safety-positive); body `{unit_ids}`, per-unit outcomes.
- Browser event-stream handshake: browsers cannot set an Authorization header on a WebSocket.
  `POST /api/v1/events/session` (Bearer, `observe` scope) returns `{ticket, expires_in_s}` —
  a single-use opaque ticket with a short TTL (<= 30 s) bound to that principal and to the
  events stream only. The WebSocket handshake must offer
  `Sec-WebSocket-Protocol: energypod-events, <ticket>`; the server validates and consumes the
  ticket at handshake, accepts the `energypod-events` subprotocol, and proceeds with that
  principal's `observe` scope. A consumed, expired, or absent ticket refuses the handshake.
  Query-string credentials remain prohibited in all cases; non-browser clients may keep using
  the Authorization header.
- Both adapters depend on one `EnergyService` application facade and never import Modbus classes.
- Errors are `{code, message, details, request_id}`; decisions expose requested, authorized and
  measured power separately.
- The `/api/v1/events` WebSocket requires `observe`, authenticates only via the Authorization
  header (never query-string credentials), and trusts a browser origin only when it is in an
  explicitly configured trusted-origin set or is same-origin with the request host; there is no
  test-environment bypass in production origin validation. A failure after accept still sends the
  structured error envelope and a clean 1011 close.
- Latched emergency stops are safety-critical state, not a bounded replay cache: they remain
  acknowledgeable through the exact-id acknowledgement endpoint regardless of how many stops
  accumulate, and the idempotency capacity bound never evicts a latched stop.

## Operations surface (Milestone C)

- SQLite durable stores carry a `schema_version` from day one. `energypod db migrate`
  applies pending migrations transactionally and refuses unknown/newer versions;
  `energypod db backup --out FILE` produces a consistent snapshot via the SQLite backup
  API (never a mid-write file copy) and refuses to overwrite an existing file;
  `energypod db restore --in FILE` validates the schema version and integrity before
  swapping it in atomically (temp file + rename), and never runs while a server holds
  the database open (refuses with a clear error).
- `energypod simulate` mints one deterministic development principal (full scopes,
  interactive, token printed once to stdout at startup) when no credential store is
  configured — simulator deployments only, never `run` mode, never against hardware.
  `run` mode without a credential store stays fail-closed (all bearer auth refused).
- `GET /healthz` is the only unauthenticated endpoint: liveness only (process up),
  never readiness, never data. It exists for container orchestration; `/api/v1/health`
  remains the authenticated three-fact health view.
- Container image: multi-stage (web build then runtime), non-root user, no secrets
  baked in, `HEALTHCHECK` against `/healthz`, config mounted read-only, data volume
  for the SQLite path. Compose ships one controller + the simulator profile for
  local operation.

## Application service facade

`energypod.application.service.EnergyServiceFacade` is the only implementation of the
`EnergyService` protocol behind REST and MCP. It composes the intent, observation,
authorization, audit, and schedule repositories, the kernel control surface, the fleet
coordinator, the event bus, and per-unit actor handles.

- `snapshot(principal)` returns one immutable fleet view: `site_id`, `snapshot_sequence`
  (from the event bus), `captured_at` wall time, and per-unit `lifecycle`, `telemetry_age_s`,
  `quality`, `requested_power` (from the newest active intent for that unit), `authorized_power`
  (from the current authorization, if any), and `measured_watts` (from the latest observation).
  It performs no I/O beyond repository reads and never triggers control.
- `health(principal)` separates `liveness` (process-up), `service_readiness` (repositories and
  coordinator responsive), and `control_readiness` (every unit qualified and at least one
  armed, with blocking reasons listed per unit). Readiness never fabricates optimism: an
  unknown unit state is a reason, not an assumption.
- `recent_audit(principal, limit)` is a bounded, newest-first read over the durable audit
  repository with a stable cursor; it never mutates.
- `submit_intent(...)` validates and accepts one intent with a server-assigned monotonic
  `acceptance_revision`, stores it through the intent repository, and returns the acceptance
  view (`intent_id`, `acceptance_revision`, `accepted_at_monotonic`, `status: accepted`,
  requested/authorized/measured projections). It never writes hardware and never publishes
  authorization; only the kernel tick does that.
- `arm(principal, unit_ids)` arms exactly the requested qualified, disarmed units through
  their owning actors and reports per-unit outcomes; partial failure is visible, never
  silent. Arming is refused for unknown units, unqualified units, or units inhibited with a
  latched cause that has not been acknowledged. Disarm is the same path inverted and always
  succeeds for known units.
- `emergency_stop(principal, unit_ids, reason)` creates one latched stop intent through the
  intent repository, immediately advances the fleet generation (fencing all outstanding
  authority before returning), revokes outstanding fleet authorizations, requests the bounded
  zero through the affected actors, and records a stop id that
  `acknowledge_emergency_stop(principal, stop_id)` accepts exactly; acknowledgement removes
  the latched stop so it cannot relatch. A latched stop intent carries a fixed long duration
  (at least 24 hours) and is removed only by acknowledgement, never by TTL expiry.
- Every facade mutation is audited and published to the event bus. The facade rejects
  cross-site principals and never trusts caller-supplied identity, revisions, or sequences.

## Event bus

`energypod.application.events.EventBus` is the only `EventSource`.

- Every published event receives one strictly monotonic sequence; there are no gaps for
  successfully published events. `snapshot_sequence()` is the sequence of the latest snapshot
  state.
- `subscribe(after_sequence)` yields events with `sequence > after_sequence` in order. When
  `after_sequence` is older than the retained window, subscription starts at the next event
  after the window with an explicit discontinuity marker so clients resynchronize from a
  snapshot instead of replaying stale history.
- Retention is a bounded most-recent window. Publishers are never blocked by slow consumers:
  the bounded per-subscriber queue drops to a resync marker.
- Event bodies are JSON-serializable, credential-free, and carry at least `type`, `sequence`,
  `occurred_at`, and a minimal payload; additional non-secret metadata (for example a unique
  event id) is permitted. Observation, decision/audit, lifecycle, arming, intent-acceptance,
  and stop events are the initial vocabulary (`intent.accepted` for facade intent acceptance).

## Runtime composition and entry point

- `energypod.runtime.composition.build_runtime(config)` constructs the whole graph
  (repositories, coordinator, audit factory, kernel, actors, event bus, facade, API app, MCP
  server) and is the only composition point. Construction validates wiring eagerly; a
  misconfiguration raises before any task starts. It accepts a `simulate` flag and an
  injectable deterministic `clock`, derives `ControlPolicy.heartbeat_interval_s` from
  `timing.control_period_s`, and exposes drivable handles (config, kernel, actors, facade,
  event bus, clock, repositories, and — in simulate mode — per-unit simulator scenario
  handles including link drop/restore and connection-epoch control).
- When no database path is configured, or in simulate mode, persistence is entirely
  in-memory; SQLite is used only for the durable audit and schedule stores.
- Boot is observe-only: no arming, authorization, or active command is restored from
  persistence; the process starts disarmed regardless of prior state.
- `energypod.main` exposes `check-config` (validate and print the effective configuration,
  zero side effects), `run` (serve the composed API), and `simulate` (compose with simulator
  transports and in-memory persistence regardless of configured database). Both `--config PATH`
  and a positional `PATH` are accepted spellings. Importing `energypod.main` has no side
  effects (module-level constants and `from __future__` imports are fine; no configuration
  loading, server start, or IO at import); `main(argv, server_runner=None)` returns an exit
  code, accepts an injected async server runner (defaulting to uvicorn, receiving the built
  app and serving parameters), and is callable from tests without process teardown tricks.
- Supervision runs as structured asyncio tasks in one process: the kernel tick loop at the
  heartbeat cadence, per-unit actor loops, and event publication. Supervision starts and
  stops through the application lifespan. Supervisor or task failure fences every generation
  and runs actor shutdown with the bounded-zero contract before the process exits.
- The simulator's scenario hooks include link drop/restore, connection-epoch control, and
  malformed-register/fault injection; measured telemetry is a deterministic function of the
  applied setpoint and scripted time (deterministic and directionally correct — the contract
  does not demand bit-exact equality with the commanded P).

## Deterministic simulator

- `energypod.simulator.pod.SimulatedEnergyPod` models one unit as a register bank over the
  evidence-backed IoT layout: identity/profile registers, layout probe (`0x5000` seven
  registers, register 0 > 10), telemetry blocks, cell blocks with their own slower capture
  cadence, and BMS/BECU status words. Reads return exactly what the evidence matrix decodes.
- The device model latches applied `0x0200` PQ writes, expires to idle under watchdog
  timeout, derives deterministic telemetry from the applied setpoint on a seeded schedule,
  and advances monotonic telemetry/cell sequences per poll. All timing is injected
  monotonic time; the device model never reads a clock and never uses randomness without an
  injected seed. Identical scenario scripts produce identical register values, sequences,
  audit events, and event-bus sequences.
- `energypod.simulator.transport.SimulatorTransport` implements the actor transport port
  against the register bank and enforces the production write gate: only `0x0200` PQ writes
  are accepted; any other write raises. It never opens a socket: on-wire framing stays a
  commissioning capture item and must not be simulated as if known.
- Scenario hooks inject faults, warnings, disconnects, and malformed registers for testing.
  The simulator is a distinct deployment target composed through `build_runtime` with
  simulator transports and in-memory persistence.

## Inhibit acknowledgement

- Entering `INHIBITED` records a cause class: `TRANSIENT`, `QUALIFIED`, or `LATCHED`
  (critical blocking faults, identity mismatch, repeated timing failure, or external-writer
  evidence per policy). The implemented latched causes in this milestone are critical
  blocking faults and identity/profile mismatch; repeated-timing-failure and
  external-writer latching are deferred until their detection is evidence-backed.
- `TRANSIENT`/`QUALIFIED` recover through stable qualifying samples to `DISARMED` (existing
  behavior). `LATCHED` additionally requires one explicit acknowledgement:
  `POST /api/v1/units/{unit_id}/inhibit/acknowledge`, requiring the `arm` scope and an
  interactive principal, idempotent, audited, and published as an event.
- Acknowledgement only clears the latch; the unit still needs stable qualifying samples to
  reach `DISARMED`, then an explicit arm. It never bypasses the safety kernel, and a still
  present blocking fault re-latches on the next observation.
- The actor exposes the inhibit cause class and its latched flag through the facade
  snapshot; the REST surface is exactly this one new endpoint and nothing else.
