# ADR 0003: Runtime composition, service facade, event bus, and simulator

Date: 2026-08-22
Status: Accepted

## Context

ADR-0001 fixed the safety authority (sole-owner actors, fail-closed kernel,
generation fencing). ADR-0002 fixed one process with structured async tasks.
The reviewed component round delivered those parts, but nothing composes them
into a runnable product: there is no composition root, no application service
behind the guarded API, no event delivery source, no simulator, and no entry
point. Additionally, the actor inhibit model currently has no latched class,
even though ARCHITECTURE.md section 8.1 requires privileged acknowledgement
for latching causes.

## Decisions

### D1. One explicit composition root, observe-only boot

`energypod.runtime.composition.build_runtime(config)` is the only place that
may construct the full object graph: repositories (SQLite when a database path
is configured, in-memory otherwise), one fleet-wide
`AuthorityGenerationCoordinator`, the canonical `AuditEventFactory` (process
identity generated per build), one `ControlKernel`, one `EnergyPodActor` per
configured unit, one `EventBus`, one `EnergyServiceFacade`, and the FastAPI
and FastMCP adapters. Wiring errors are construction-time errors.

Boot is observe-only and disarmed: actors start in `BOOT -> OBSERVE_ONLY`,
arming state is never restored from persistence, and no active command or
authorization is restored. The only writer of arming state is an explicit,
authenticated arm operation after the unit qualifies.

### D2. `EnergyServiceFacade` is the sole application surface for adapters

The REST and MCP adapters already depend on one `EnergyService` protocol. The
production implementation is `energypod.application.service.EnergyServiceFacade`.
It owns:

- Intent acceptance: server-assigned monotonic `acceptance_revision`, expiry
  from `ttl_s`, rejection of superseded/duplicate idempotent submissions is
  the API adapter's concern; the facade validates domain-level acceptance only.
- Fleet snapshot assembly with `snapshot_sequence` taken from the event bus.
- Audit reads from the durable audit repository.
- Arming: per-unit explicit arm/disarm through the owning actor, allowed only
  when the actor reports the unit qualified and disarmed.
- Emergency stop: creates a latched stop intent through the same intent path
  the kernel arbitrates, records it in the stop registry, and fences the
  generation immediately rather than waiting for the next tick.
- Stop acknowledgement: exact-id, scoped, removes the latched stop from the
  intent repository so it cannot relatch (API_CONTRACTS already requires this).
- Inhibit acknowledgement (D5).

The facade never imports Modbus classes and never touches a transport.

### D3. In-process event bus with monotonic sequence

`energypod.application.events.EventBus` assigns one strictly monotonic
sequence per published event, exposes `snapshot_sequence()` and
`subscribe(after_sequence)` as the async iterator the WebSocket adapter
consumes, keeps a bounded most-recent retention window, and reports
`sequence_gap`-style discontinuity by comparison with the snapshot sequence.
The bus is the only `EventSource` implementation; the adapter's existing
recovery semantics are unchanged. Audit appends and observation publications
are events; slow consumers never block the safety path (bounded queue,
drop-to-resync).

### D4. Deterministic simulator at the transport port, not on the wire

`energypod.simulator.pod.SimulatedEnergyPod` models one unit as an in-memory
register bank implementing the evidence-backed IoT layout from
`register_layout` (identity, telemetry blocks, cell blocks, BMS/BECU
status), plus a device model: applied setpoint latching with watchdog expiry
back to idle, monotonic telemetry/cell sequences per poll, deterministic
telemetry derived from the applied setpoint and a seeded schedule, and
injectable fault/warning scenarios for testing.

`energypod.simulator.transport.SimulatorTransport` implements the actor's
transport port (`connect`, `read_holding`, `write_registers`, `close`) against
that register bank, enforcing the same evidence gates as production: only
`0x0200` PQ writes are accepted, everything else is rejected.

No TCP/RTU server is built in this milestone: exact Waveshare on-wire framing
is unverified evidence and remains a commissioning capture item. The simulator
is therefore wired through the transport port so the full actor/codec/layout
stack is exercised without pretending to know the wire format. A distinct
simulator deployment target composes the same runtime with simulator
transports and in-memory persistence.

Determinism requirements: identical scenario scripts produce identical
register values, sequence numbers, audit events, and event-bus sequences;
no wall clock inside the device model (all timing is injected monotonic
time).

### D5. Inhibit cause classes and latched acknowledgement

The actor records an inhibit cause class when entering `INHIBITED`:
`TRANSIENT`, `QUALIFIED`, or `LATCHED`, per ARCHITECTURE.md section 8.1.
`TRANSIENT`/`QUALIFIED` recover through stable qualifying samples to
`DISARMED` as already implemented. `LATCHED` (critical BMS/PCS/DCDC faults,
identity mismatch, repeated timing failures, external-writer evidence)
additionally requires an explicit privileged acknowledgement:
`EnergyServiceFacade.acknowledge_inhibit(unit_id)` exposed as
`POST /api/v1/units/{unit_id}/inhibit/acknowledge`, requiring the `arm` scope
and an interactive principal, idempotent, audited, and only clearing the
latch — the unit still returns through stable samples to `DISARMED`, never
directly to `ACTIVE`. Critical-fault classification comes from the policy's
blocking fault codes; acknowledgement never bypasses the safety kernel.

### D6. Entry point and supervision

`energypod.main` provides a CLI: `check-config` (validate and print the
effective configuration without side effects), `run` (compose the runtime and
serve the API), and `simulate` (same, forcing simulator transports and
in-memory persistence regardless of the database path). Supervision is
structured asyncio tasks in one process: the kernel tick loop at the
heartbeat cadence, per-unit actor poll/heartbeat scheduling, and event
publication. Supervisor failure fences every generation and shuts down
actors with the bounded-zero contract. `energypod.main:main` must remain
importable with no side effects at import time.

## Consequences

- The product becomes runnable and end-to-end testable entirely without
  hardware: `simulate` composes the real kernel, actors, facade, and API over
  deterministic simulated units.
- The API surface gains exactly one new operator endpoint (inhibit
  acknowledgement); MCP remains read-only by default.
- Simulator fidelity is bounded by PROTOCOL_EVIDENCE.md; anything not
  evidenced (notably watchdog timing and framing) stays parameterized and
  must be pinned at commissioning.
- SQLite remains the only durable repository; migration/backup is a later
  milestone but must not break `build_runtime` compatibility.
