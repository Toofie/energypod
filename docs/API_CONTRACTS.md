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
- `AuthorizationRepository.publish(batch)`, `current(unit_id, now_mono)`, `revoke(...)`.
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
