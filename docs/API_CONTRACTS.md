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
completeness. Optional advisory fields (`grid_power_w`, `load_power_w`) carry per-field quality but
stay outside the safety-critical completeness set ("Excess-solar accelerated charging (advisory)").

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

## Write-enabled run mode (live control)

- Write-enabled control composes ONLY when `mode: write_enabled` AND a
  policy AND enabled authentication are all configured (existing config
  gate) AND `timing.device_command_expiry_evidence` references the measured
  live watchdog trial (the 2026-08-22 direction trial measured an unrenewed
  objective expiry of ~3.5-4.0 s). The existing complete-budget validator
  must pass against that commissioned expiry: renewal cadence
  (`control_period_s`) plus write timeout plus margin fit strictly inside it
  (vendor 1 s and prior-integration 1.5 s cadences are the corroborated
  envelope; the commissioned cadence must not exceed them).
- In write-enabled mode the run-mode actor's stable-qualification threshold
  is the policy's `stable_samples_needed_to_rearm` (identity-pinned live
  decode qualifies a unit exactly as the simulator does); observe-only mode
  keeps the structural never-qualify wiring unchanged.
- External-writer preflight: at arm time (transition into ARMED_IDLE) the
  actor reads the served PQ objective readback (IoT 0x1060+17/+18). Any
  nonzero objective it did not itself write latches INHIBITED with cause
  `external_writer` (privileged acknowledgement required, re-latching while
  the foreign objective persists); an unreadable readback refuses the arm
  fail-closed. This makes single-writer authority structural, not assumed.
- The only writable registers remain `[1, signed P, signed Q]` at `0x0200`
  (negative P = charge, positive P = discharge — live-proven 2026-08-22);
  the transport write gate is unchanged and no other address is writable by
  any composition, mode, or tool.
- Emergency stop, fence, and shutdown behavior are unchanged and dominate
  renewal; a latched stop or inhibit during ACTIVE stops renewal writes
  immediately and issues the bounded zero.

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
  Each unit additionally carries a nullable `telemetry` summary projection from the latest
  observation — `soc_pct`, `bms_soc_pct`, `soh_pct`, `pack_voltage_v`, `pack_current_a`,
  `battery_watts`, `dynamic_charge_limit_w`, `dynamic_discharge_limit_w`, `cell_count`,
  `cell_min_v`, `cell_max_v`, `cell_spread_mv`, `temperature_min_c`, `temperature_max_c`,
  `active_faults`, `active_warnings` — every field null when that datum is absent from the
  observation, never zero-filled or fabricated. It performs no I/O beyond repository reads and
  never triggers control.
- The snapshot also carries the live emergency-stop latch state (2026-08-23): `active_stops` is a
  list, empty when nothing is latched, with one entry per non-acknowledged latched stop —
  `{"stop_id": str, "latched_at": iso-8601 str, "principal": str, "reason_codes": [str],
  "unit_ids": [str] | null}` where `unit_ids` is `null` exactly when the stop fenced the whole
  fleet this facade serves. An acknowledged stop leaves the list; a restart starts with none
  (the latch registry is process-local by design — the snapshot simply tells the truth). Each
  unit view additionally carries `inhibit_latched: bool` and `inhibit_cause: str | null` (the
  actor's recorded latch cause, null whenever the unit is not latched) so a console opened after
  a latch renders the release affordance from the snapshot alone.
- `unit_detail(principal, unit_id)` (REST `GET /api/v1/units/{unit_id}`, `observe` scope)
  returns the full latest observation projection for one unit: identity (`device_identity`),
  `protocol_profile`, `connection_epoch`, telemetry and cell sequences and capture times, all
  scalar measurements above, the complete `cell_voltages_v` and `temperatures_c` arrays, the
  per-field `quality` map, faults and warnings. Unknown unit ids are refused with the
  structured envelope. It is a read-only view of repository state.
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

## Excess-solar accelerated charging (advisory)

Purpose: fleet-wide surplus PV (export measured on any phase) charges the neediest battery at a
rate its own per-phase autonomy could never reach, under net-across-phases billing. The feature is
advisory-only and config-gated OFF by default.

### Advisory component, ordinary authority

- `energypod.application.excess_charge.ExcessChargeAdviser` is composed only when
  `excess_charging.enabled` is true. It owns no transport, no authorization path, no allocator or
  kernel role, and cannot import control adapters.
- It submits ordinary short-TTL `PowerIntent`s with `source: OPTIMIZER` through the facade's
  internal `submit_advisory_intent` — the POST-equivalent internal submit with the same
  validation, audit, and publication as `submit_intent`, mintage source pinned to `OPTIMIZER`,
  and never exposed on REST or MCP. Everything downstream is the existing path:
  arbiter → allocator → SafetyKernel → per-unit authority. The adviser has no special authority
  anywhere in that chain.
- The composed automation principal is `energypod:excess-adviser` (scopes `observe` + `dispatch`,
  non-interactive, site-bound). Audit attribution relies on principal plus the `optimizer` source
  tag: local console and agent traffic is `operator:local` + `manual`/`agent`, the adviser is
  always distinguishable.

### Operator precedence (pinned)

`IntentArbiter` priority (emergency stop > manual > agent > optimizer > schedule > idle; equal
priority by acceptance revision then stable id) already displaces the adviser whenever a manual or
agent intent is live — live-verified 2026-08-23, when a console manual intent superseded an
in-flight agent intent mid-window. The adviser also yields on its own: while any active intent
with priority above `OPTIMIZER` exists fleet-wide, it withdraws its intent (repository removal,
never a stop triple) and does not re-post until that intent has expired AND the entry hysteresis
re-qualifies.

### Deterministic export bound

For a charge intent with `source == OPTIMIZER`, the allocator computes — from the fleet
observations and policy it already receives — one additional min() term on the allocation demand:

```text
eligible_charge_w = min(max_charge_from_export_w,
                        max(0, floor(Σ_{u ∈ fleet} grid_power_w[u]) - export_headroom_margin_w))
```

- `grid_power_w` is the per-pod CT power at PCS `0x1000+17` (int16, unscaled W; vendor cite
  `SysControl.cs:500`); sign live-proven: negative = import, positive = export
  (PROTOCOL_EVIDENCE sections 4b/4c).
- The sum is over every unit in the policy's per-unit maps (the whole fleet): the arbitrage is
  net-across-phases. One phase exporting 1500 W while the target's phase idles is exactly the
  scenario the bound serves.
- The term can only lower power below today's limits — static per-unit, fleet, BMS dynamic,
  ramp, SOC ceiling/floor, apparent/reactive all still apply unchanged. It can never raise power
  above what today's path would authorize.

### Fail-closed freshness and quality

- The bound is 0 unless EVERY fleet unit's observation carries a `grid_power_w` that is finite,
  `quality == GOOD`, and no older than `export_telemetry_max_age_s` (mirroring the kernel's
  `telemetry_stale` pattern). One unreadable phase is never treated as zero export: a missing
  unit, a `None`/non-finite value, or a non-GOOD quality each collapse the bound to 0.
- Defense in depth: the SafetyKernel additionally rejects a non-zero export-bounded charge
  proposal with reason codes `export_evidence_missing`, `export_evidence_bad`, or
  `export_evidence_stale`, computed over the fleet observations the kernel already holds.
- Zero-watt proposals never accrue export deny reasons: zero is always permitted, and all-zero or
  partially-eligible allocations remain legitimate representations (never couple active direction
  to positive watts anywhere in this feature).

### Observation fields and readthrough

- `Observation` gains optional `grid_power_w` and `load_power_w` (signed; None when unsourced)
  with quality keys in a new `ADVISORY_QUALITY_FIELDS` set. The quality map contains exactly
  `QUALITY_FIELDS` (the ten safety-critical fields) or exactly `QUALITY_FIELDS ∪
  ADVISORY_QUALITY_FIELDS`; the wire decoder always emits the twelve-key shape, with MISSING for
  unserved sources.
- Both fields stay OUTSIDE `safety_data_complete` and the kernel's required-quality set: an
  ordinary (non-export) control decision must not start failing because a deployment's read plan
  does not serve the PCS block. Export-bounded control is gated by its own fail-closed bound,
  not by the general safety completeness set.
- The facade snapshot telemetry summary and the unit-detail projection expose both fields
  readthrough-style: nullable, never zero-filled, never fabricated.

### Read-plan tier promotion

With the feature enabled, the live decode strategy promotes the PCS live block `0x1000` (grid at
+17, load at +20) from the cold ring into the control-rate core, so `grid_power_w` refreshes
every telemetry cycle. The plan stays inside the commissioned cadence budget: steady-state
≤ 8 windows plus the probe (~0.9 s at the 0.1 s inter-frame gap, inside the 1.5 s control
period; bootstrap cycle ≤ 10 windows). With the feature absent or disabled the plan matches
today's in structure, budget, and ring period — PCS block included in the ~108 s cold ring —
with one rotation-phase change: the default ring now visits the PCS block first (cycle 8)
rather than at cycle 72, so advisory grid data appears within seconds of boot instead of
~108 s.

### Beat-autonomy hysteresis

While renewed, the adviser's objective REPLACES the pod's own self-consumption (its CT-following
autonomy resumes only after our renewal lapses); commanding less than the pod's autonomous rate
would slow charging — a regression the operator would feel. The adviser therefore:

- enters only when `achievable_w ≥ assumed_autonomous_charge_w + min_acceleration_w`, where
  `achievable_w = min(eligible_charge_w, target unit charge headroom, static unit charge cap)`;
- once intervening, continues while `achievable_w > assumed_autonomous_charge_w +
  exit_hysteresis_w` (`exit_hysteresis_w < min_acceleration_w`, validated at configuration
  time), so a dip between the two thresholds never oscillates;
- otherwise leaves autonomy alone entirely — no intent, no write, no stop triple.

The commissioned ramp limit (1000 W/s at the 1.5 s cadence) exceeds the autonomous rate, so the
first authorized tick already meets or exceeds autonomy once entry qualifies.

### Target selection

Exactly one unit at a time, never a fleet-wide dispatch: the neediest — lowest `system_soc_pct`
among units whose latest observation is controllable (lifecycle `ARMED_IDLE`/`ACTIVE`), below the
SOC charge ceiling, and with positive charge headroom; ties break by unit id. Non-controllable,
inhibited, or ceiling-blocked units are skipped, and the kernel's existing deny reasons remain
the backstop.

### Night writers and external writers

The feature is OFF by default and is enabled only for deliberate daytime operation (other apps
monitor read-only by day and write at night). The existing arm-time external-writer preflight
(served PQ objective readback at IoT `0x1060+17/+18`) is unchanged and dominates: a foreign
nonzero objective latches `INHIBITED` with cause `external_writer`, the target selector skips the
unit, and the kernel's `lifecycle_not_controllable` backstops. The adviser never writes registers
and never fights a latched inhibit.

### Renewal and the designed fail-safe

The adviser renews (remove previous, submit fresh) once per fleet cycle — bounded by the control
interval, after polls and before the kernel tick; a failing evaluation is survivable per cycle
(advisory) and never halts the fleet. Intent TTL is `intent_ttl_s` (≤ 300 s, the REST dispatch
cap; default 10 s). Any failure to renew — adviser stall, process death, bound collapse,
staleness, yield, or hysteresis exit — ends the intent by TTL, the kernel stops minting, the
actor stops writing, and the firmware watchdog (measured ~3.5-4.0 s) returns the pod to its own
CT-following autonomy. Hand-back is by non-renewal: the adviser issues no stop triple and no IDLE
intent on exit. Emergency stop, fences, and shutdown dominate renewal exactly as for any other
intent.

### Net-billing assumption (operator-confirmed PENDING)

The feature is energy arbitrage on export and assumes site billing is netted across phases. A
per-phase-billed site changes the ECONOMICS (import on the charging phase could be billed above
the export credit elsewhere) but changes NOTHING about safety: the bound caps at measured
physical export and every existing safety limit still applies. The operator must confirm net
billing before the feature is enabled in production.

### Policy and configuration keys

`ControlPolicy` gains the all-or-none export triple — `export_charge_limit_w` (positive),
`export_headroom_margin_w` (non-negative), `export_telemetry_max_age_s` (positive); absent (the
default) means export bounding is not armed, and the bound for an optimizer charge intent is then
0: an advisory charge may flow only from measured, armed export evidence.

| Key (under `excess_charging`) | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `false` | feature switch; an absent block is identical to disabled |
| `export_headroom_margin_w` | int > 0 | `200` | export kept on the grid before any advisory charge |
| `max_charge_from_export_w` | int > 0 | `2500` | hard cap on the bound; must not exceed `policy.max_unit_charge_w` (validated) |
| `export_telemetry_max_age_s` | float > 0 | `3.0` | freshness bound on grid evidence; must exceed `timing.control_period_s + timing.essential_read_timeout_s` (validated) |
| `assumed_autonomous_charge_w` | int > 0 | `520` | the evidenced daytime self-charge rate (~-520..-560 W observed) |
| `min_acceleration_w` | int > 0 | `100` | entry margin over autonomy |
| `exit_hysteresis_w` | int ≥ 0 | `50` | exit margin; strictly below `min_acceleration_w` (validated) |
| `intent_ttl_s` | float > 0 | `10.0` | adviser intent TTL; ≤ 300 s and > `control_period_s` (validated) |

Configuration gates: `excess_charging.enabled: true` is refused unless `mode: write_enabled` AND
a `policy` block is present (an observe-only composition structurally never actuates; an adviser
there is dead code refused at validation time), and every cross-validation above holds. The
default — no block, or `enabled: false` — composes no adviser, adds no policy export triple,
promotes no register tier, and changes nothing else.
