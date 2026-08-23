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
Optionally it carries `watts_by_unit`: one positive integer watt target per selected unit (frozen
mapping, normalized keys) whose key set equals the selected units and whose values sum to exactly the
fleet-total `watts`. `watts_by_unit` is a per-unit CAP on allocation, never a floor; IDLE intents
carry `None`. A target above a unit's static cap is bounded by that policy headroom at allocation
(clamped, shortfall unallocated) exactly as an over-cap scalar request is — it is never an
acceptance error.

`UnitSetpoint` is immutable and contains unit id, direction, non-negative watts, reactive vars,
generation, intent id, and monotonic authorization expiry. Domain power is not constrained by a wire
register. Direction-to-sign conversion and signed 16-bit range enforcement occur only in the
protocol adapter; out-of-range commands are rejected, never wrapped.

`Observation` is immutable and includes unit identity, wall timestamp, monotonic capture time,
sequence, lifecycle, protocol profile, system SOC, BMS SOC, SOH, signed battery watts, pack voltage
and current, dynamic charge/discharge limits, cells, temperatures, active faults/warnings, and a
quality map. Cell data carries its own monotonic capture time and sequence because it is polled less
frequently. Derived properties expose age, cell age, cell min/max/imbalance, safety-data
completeness, and the authoritative SOC (`authoritative_soc_pct` — the BMS figure, 2026-08-24).
Optional advisory fields (`grid_power_w`, `load_power_w`) carry per-field quality but
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
When headroom is scarce the request is distributed capacity-weighted across every participating
unit: proportional to each unit's remaining direction headroom, as an exact integer
largest-remainder split with a one-watt participation floor, so no participating unit is left at a
zero-watt proposal while another runs below its own headroom. A request smaller than the number of
participating units cannot give each unit its first watt and concentrates instead by capacity
priority (largest headroom first, ties by unit id). Ineligible units and units without usable
headroom keep explicit zero-watt proposals.

When the intent carries `watts_by_unit` (per-unit watt targets), every target is that unit's own
CAP: the capacity-weighted share is clamped to the unit's target; the watts that clamping released
— chiefly the shortfall of units whose target exceeds their headroom — are redistributed across
units still below their own targets, bounded by each unit's remaining target-versus-allocation gap
and headroom; a unit whose target is fully met stops absorbing redistribution. The fleet total
never exceeds the request (nor the export cap when one applies), and the result is exactly
`allocated = min(fleet demand, sum of per-unit serving capacities)` with `unallocated = watts -
allocated`; every scalar-path invariant above (unit-set equality, zero-watt non-participation,
permutation-invariant ties, export-cap composition, the concentration boundary — now per-target)
is preserved unchanged. A scalar `watts` intent keeps the pure capacity-weighted behavior exactly.

An optional `unit_ids` argument names the intent's SURVIVING scope under per-unit arbitration (a
non-empty subset of its selection): the allocation covers exactly those units — a scalar intent
distributes its whole demand across the survivors, a per-unit intent carries each surviving
unit's own target with the demand re-summed over the survivors — while `requested_watts` stays
the intent's own full request so the eroded share surfaces as unallocated.

`SafetyKernel.evaluate(proposed_setpoints, current_observations, previous_observations, policy,
now_mono) -> ControlDecision` is deterministic and side-effect free. Unknown, stale, invalid,
incomplete, contradictory, or implausibly jumping safety data rejects non-zero power. Zero/stop is
always permitted. It applies dynamic device limits, static unit limits, ramp limits,
SOC/cell/temperature/imbalance constraints and returns stable machine-readable reason codes.
Fleet limits apply PER DIRECTION across that direction's subtotal in the proposal set (see
"Concurrent per-unit operation" below). Cell
sequence monotonicity is non-decreasing: an unchanged cell sequence between consecutive
observations is permitted because cell blocks poll less frequently than the control rate, with
freshness enforced by the maximum cell age; a regressed cell sequence rejects.

#### BMS-authoritative SOC (2026-08-24)

The operator's ruling (2026-08-24): "I don't think it should get blocked like this. If there's a
disagreement, re-sync based on whatever the battery says." The battery's own BMS SOC is the
AUTHORITATIVE SOC for every SOC-based policy bound — `min_soc_pct` (the discharge floor),
`max_soc_pct` (the charge ceiling), and the SOC-jump check are all evaluated against
`bms_soc_pct` (the domain's `authoritative_soc_pct` derived property spells the same figure for
advisory consumers, including the excess-solar adviser's neediness and ceiling skip). A
system-vs-BMS divergence NEVER denies power on its own: the old `soc_disagreement` deny reason is
removed from the blocking set, because the system controller's SOC word (0x0100+17) is served once
per connection by the tiered read plan and may be hours stale on a cycled unit — the
"disagreement" it manufactures is mostly staleness, and staleness must not masquerade as a safety
objection from the battery. Divergence beyond `max_soc_disagreement_pct` (inclusive boundary
unchanged) surfaces as the informational reason code `soc_disagreement_observed`, carried ONLY on
authorizing decisions — the audit row and console see the warning; a rejected decision carries
deny reasons only, so the note can never be confused with a blocking code. Zero-watt and positive
proposals alike are unaffected by divergence, and every other SOC protection still blocks exactly
as before, now via the battery's own figure.

The demotion is COMPLETE for the quality gate too (SYNC_RESILIENCE_AUDIT B1, 2026-08-24): the
system SOC word is ADVISORY TELEMETRY, outside the kernel's required-quality set
(`quality_system_soc_pct` is no longer a deny reason), outside `Observation.safety_data_complete`
(the domain's `REQUIRED_SAFETY_QUALITY_FIELDS` is the nine control-rate fields), and outside the
actor's qualification judgment. The quality-map KEY stays (the decoder keeps emitting it; the
wire-decode vectors pin the twelve-key shape), and a non-GOOD or absent system SOC surfaces as the
informational reason code `system_soc_untrusted` — carried ONLY on authorizing decisions, exactly
like `soc_disagreement_observed` — never a denial and never a qualification reset.
`quality_bms_soc_pct` (the authoritative figure) keeps its full fail-closed gate: non-GOOD BMS
SOC still denies and still blocks qualification.

#### SOC-jump re-baseline (SYNC_RESILIENCE_AUDIT B2, 2026-08-24)

The `soc_jump` deny reason is removed from the blocking set. Both endpoints of the comparison are
honest fresh BMS reads; a > `max_soc_jump_pct` move between them is either a legitimate fast SOC
move across our own abandoned-poll gap or the battery's own estimate resync (PROTOCOL_EVIDENCE
13.17 records the observed large SOC jumps as an evidenced behavior of this fleet's BMS). The
battery wins: the fresh figure STANDS and the jump surfaces as the informational reason code
`soc_jump_observed`, carried ONLY on authorizing decisions — exactly the `soc_disagreement_observed`
pattern — and the next cycle's baseline is the new figure (automatic re-sync). The absolute
protections are untouched and are the real guards: a jumped figure that crosses `min_soc_pct` or
`max_soc_pct` is denied BY THAT BOUND, evaluated on the fresh figure, in the safe direction. The
implausible-data canary the jump check once served is carried by identity pinning (core-rate
0x8106) and the decoder's 0-100 domain validation.

#### First-observation baseline (SYNC_RESILIENCE_AUDIT B3, 2026-08-24)

The `previous_observation_missing` deny reason is removed, and the kernel's `_eligible` /
`_evidence_is_coherent` no longer require a previous observation for a selected unit: a unit's
FIRST observation is its own baseline. All pair-derived checks (order, sequence, epoch,
cell-sequence, SOC-jump) are vacuous when no baseline exists and are skipped; every value-judging
check (quality, staleness, SOC bounds, cells, temperatures, faults, dynamic limits) applies to the
first observation exactly as to any other. This removes the one-cycle block after every controller
restart (boot → first poll → first tick previously rejected every active proposal because the
missing thing was our own second sample, while the battery was readable and fresh). A unit with NO
observation at all is still denied (`observation_missing`, class D), and units that do hold a
previous observation keep the full pair-coherence checks.

`IntentArbiter.arbitrate(intents, now_mono) -> CycleArbitration` removes expired intents and
selects a PER-UNIT WINNER SET: for each unit, the highest-priority live intent claiming it wins
that unit (priority order unchanged: emergency stop > manual > agent > optimizer > schedule;
equal-priority conflicts on a unit resolve by highest server-assigned acceptance revision then
stable id ordering — the existing fleet-wide tie rules applied per unit). An intent's effective
scope is its selection minus the units higher-priority intents claimed; an intent whose entire
scope was claimed away is simply not represented that cycle and returns the moment a claimer
lapses. A live or latched emergency stop is the whole cycle — it claims exactly its own units, no
other intent is represented, and it latches exactly as before. `select()` remains the pinned
single-winner view: for a cycle held by exactly one intent the two agree. Emergency stop remains
latched until an operator with stop-acknowledge scope acknowledges the exact stop id;
acknowledgement removes the latched stop from the intent repository so it cannot immediately
relatch.

`ControlKernel.tick()` composes EVERY per-unit winner into ONE cycle: the allocator runs once per
represented intent over that intent's SURVIVING scope (`allocate_fleet_power`'s `unit_ids` — a
scalar intent distributes its whole demand across the survivors; a per-unit intent carries each
surviving unit's own target with the demand re-summed), the matcher binds every proposal to its
unit's winning intent (identity AND direction) and bounds each intent's proposals by its own
watts, one `cycle_id`/`decision_id` and one audit row carry the per-unit breakdown, and one
`AuthorizationBatch` carries per-unit capabilities whose intent, revision, and direction are their
own unit's winner's. The kernel then evaluates safety, creates and durably appends a canonical
correlated `AuditEvent`, and publishes short-lived
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

### Concurrent per-unit operation

The operator's requirement (2026-08-24): "I instructed MID to charge at 2,000 watts and RHS to
discharge at 1,000 watts. Only one operation functions at a time. I require both to function
concurrently whenever a battery request is made." Two or more accepted intents now run in the SAME
control cycle whenever their unit scopes are disjoint; the REST surface needs no schema change —
multiple `POST /intents` coexist and now run concurrently. The rules:

- Overlapping scopes resolve PER UNIT by priority (and by revision/id within a priority): a manual
  intent claiming `lhs`+`mid` against an agent intent claiming `mid`+`rhs` leaves the agent its
  `rhs` while the manual intent holds `lhs`+`mid` — the older intent's other units still run.
- Different units MAY run different directions in one cycle: charging one battery while
  discharging another is physically legitimate (independent phases). The fleet-wide
  `mixed_directions` rejection is replaced by per-unit coherence — each proposal's direction must
  equal its unit's winning intent's direction (enforced by the kernel's matcher; one unit proposed
  twice is still rejected as `duplicate_unit_setpoint`).
- Fleet limits apply PER DIRECTION across the cycle's subtotals: `fleet_charge_limit_w` bounds the
  charge subtotal and `fleet_discharge_limit_w` the discharge subtotal — never one blended budget.
- Every per-unit check — SOC bound, ramp, dynamic capability, cell/temperature, quality — applies
  per unit against ITS direction exactly as before. A denial zeroes ONLY that unit: a denied unit
  is a zero-watt non-participant for its direction (the non-participation doctrine extended to
  concurrency) while the other units, including opposite-direction units, still run. When NO unit
  can participate, the decision still fails closed to a whole-cycle rejection.
- One audit row per cycle carries the per-unit breakdown: `requested_watts_by_unit` (each unit's
  winner's own target, when any represented intent carried per-unit targets),
  `authorized_watts_by_unit`, and `directions_by_unit` (null on single-intent rows and on rows
  written before 2026-08-24). A row composed from several intents cannot honestly name one
  intent: it carries `intent_id: null`, correlates to its cycle (`cycle:<cycle_id>`), joins the
  represented principals, and keeps the dominant source. `requested_active_w` /
  `authorized_active_w` remain signed sums (charge negative, discharge positive) — the NET across
  the mixed cycle.
- The console can now hold N active request cards at once; each card's batteries follow its own
  intent, and `authorization.granted` bus events carry `watts_by_unit` and `directions_by_unit`
  per cycle so a card can label each battery's own authorized power and direction.
- `PowerIntent` itself does not change shape: direction stays per-intent — one request = one
  direction, as operators think. Concurrency comes from composition, never from per-unit
  directions inside one intent. IDLE and emergency semantics are unchanged (an idle intent holds
  only its own units to zero), and expiry stays per intent (an expired intent simply stops
  claiming units).

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
- `POST /api/v1/intents` carries exactly one watt form (the 2026-08-23 operator ruling: each
  setting is that battery's own request): either scalar `watts` (integer > 0, the fleet total —
  fully supported, unchanged) or `watts_by_unit` (a JSON object of one integer > 0 per unit id,
  whose key set equals `unit_ids` exactly). Sending both, sending neither, a missing or extra key,
  or a non-positive / non-integer value is a 422 before the service is reached. With
  `watts_by_unit` the facade derives the fleet total as the sum of the targets, and the acceptance
  view's `requested` projection and the `intent.accepted` payload carry the breakdown alongside the
  total. Per-unit values are capped by each unit's static policy limit at allocation (never an
  acceptance error), and per-unit proposals may never exceed the unit's own target.
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
- The snapshot additionally carries a top-level `intent` view (2026-08-24 cold-load fix) with the
  live request's per-unit figures, so a page opened mid-intent renders exact per-battery numbers
  instead of a labeled fleet total: `{"requested_watts_by_unit": Record[str, int] | null,
  "authorized_watts_by_unit": Record[str, int] | null, "directions_by_unit": Record[str, str] |
  null}` — the whole view is `null` when no live intent claims any unit (an expired intent claims
  none, and then the audit trail is not even read). The view is composed across ALL active
  intents with the same per-unit winner-set arbitration a kernel cycle uses, so under concurrent
  operations each unit's entry comes from THAT unit's winning intent: `requested_watts_by_unit`
  is the unit's own target when its winner carried `watts_by_unit`, else its exact integer share
  of the winner's scalar fleet total over its surviving scope (largest remainder over the sorted
  scope, summing to the intent's own total — the headroom-blind request-time projection, distinct
  from the capacity-weighted authorized split); `directions_by_unit` is each unit's winner's
  direction (units may differ under concurrent intents, including `idle` under a latched stop);
  `authorized_watts_by_unit` mirrors the FRESHEST `control_decision` audit row's per-unit
  authorized map — newest-first scan, first decision row in the window decides, restricted to the
  units a live intent still claims so an ended request's figures never linger — and is `null`
  when that row minted no batch, the window holds no decision, or the audit read fails (never an
  older row, never a fabricated figure). The per-unit `requested_power` scalars are unchanged and
  keep projecting the intent's own fleet total for every covered unit.
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
  authorization; only the kernel tick does that. The watt form is exactly one of scalar `watts`
  or `watts_by_unit` (one positive integer per selected unit, key set exactly the selection);
  with the per-unit form the facade derives the fleet total as the sum and the stored intent,
  `intent_accepted` audit fact set, and `intent.accepted` payload all carry the breakdown.
  `submit_advisory_intent` (composition-internal) stays scalar-only.
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
- Intent-lifecycle and grant events (2026-08-23): `intent.expired` publishes exactly once per
  intent whose acceptance window lapses (payload: `intent_id`, `source`, `direction`, `watts`,
  `unit_ids`); an intent that leaves by removal — cancellation or stop acknowledgement — never
  publishes an expiry. `authorization.granted` publishes when a batch lands in the authorization
  store (payload: `cycle_id`, `generation`, `unit_ids`), symmetric with `authorization.revoked`.
  `audit.appended` payloads additionally carry the event's `requested_active_w` and
  `authorized_active_w` so consoles render watt figures straight off the stream.

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

## Cancel intent

- `cancel_intent(principal, intent_id, idempotency_key, request_id)` (REST `POST
  /api/v1/intents/cancel`, body `{"intent_id": "<exact id>" | "current"}`, `dispatch` scope with
  NO interactive requirement) cancels the active intent: the repository removes it, the kernel's
  next tick finds no winner and revokes authority, and the device watchdog hands power back
  (~3.5-4 s). Stopping is the safety-positive direction, so automation may drive it.
- `"current"` resolves the newest active non-emergency intent; an unknown or no-longer-active id is
  the structured 404 `intent_not_found`, and nothing-active is the 409 `intent_not_cancelable`. A
  latched emergency stop is never cancellable here — it leaves only through its privileged
  acknowledgement.
- The mutation is idempotent (Idempotency-Key), audited as `intent_cancelled`
  (`result: cancelled`, the intent's identity and units), and published on the bus as
  `intent.cancelled` (`principal`, `intent_id`, `unit_ids`) so console request cards clear the same
  way they do on expiry.

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
priority by acceptance revision then stable id) already displaces the adviser on the units a
higher-priority intent claims — live-verified 2026-08-23, when a console manual intent superseded
an in-flight agent intent mid-window. The adviser also yields on its own, PER UNIT (2026-08-24
concurrent operations): when a higher-priority intent claims the adviser's own target (its scope
is exactly one unit) — or any emergency stop is live, since a stop dominates every unit — it
withdraws its intent (repository removal, never a stop triple) and does not re-post until that
claim has expired AND the entry hysteresis re-qualifies. A manual or agent intent claiming a
DIFFERENT battery no longer stands the advisory charge down: the arbiter runs both in one cycle.

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

## Device-mode telemetry and dispatch gating

- `Observation` gains four more ADVISORY words (2026-08-23 incident 1), same doctrine as the CT
  fields but with NO quality-map keys: `debug_mode_w` (readback `0x8100+0`; the vendor's PQ-dispatch
  precondition — MiniESapp.cs:2180 refuses sends when nonzero), `ctrl_mode_w` (`0x0100+1`; enum 1
  Remote / 2 Local, GlobalFun.cs:204), `work_mode_w` (`0x0100+2`, kept raw — MID's captured 7 is
  deliberately unmapped, field-mapping A-17), and `run_mode_w` (PCS live `0x1000+2`; enum 0
  Matching Load / 1 Remote PQ Power). Each is `None` when its block was not served; the convenience
  reads `debug_mode_active` and `ctrl_mode_remote` are `None` on absent evidence.
- The snapshot telemetry summary and unit-detail projection expose all four readthrough-style. In
  run mode the one-word debug readback rides the control-rate core (like the identity pair) so a
  mode flip is judged on the next poll; the system block still rides its existing tiers.
- Dispatch gating: `submit_intent` and the advisory twin refuse with
  `device_debug_mode_active: [units]` while any selected unit's decoded debug word is nonzero, and
  with `device_mode_not_remote: [units]` while its ctrlMode is not Remote (2 = Local). Absent
  evidence — a read plan without the mode blocks, or a unit yet to publish — refuses nothing: the
  advisory doctrine, with the safety kernel's staleness gates as the backstop. The controller never
  writes `0x8000`/`0x0101`; the mode words are read-only evidence.
- Read-plan tier correction + fresh re-read before refusal (SYNC_RESILIENCE_AUDIT B5, 2026-08-24):
  the system overview block (0x0100 — ctrlMode +1, workMode +2, the advisory system SOC +17) rides
  the COLD RING (one window every 8th cycle, ~108 s rotation) — it is no longer a once-per-process
  cycle-1 read, so the mode words and the advisory SOC semi-refresh instead of being pinned to
  process start. Because a cached ctrlMode word can still be ~a ring period stale, the
  `device_mode_not_remote` refusal path performs ONE bounded fresh read of the 0x0100 mode words
  through the owning actor (a serialized mailbox operation below heartbeat priority, bounded by
  `write_timeout_s`; one retry on a failed read) whenever the cached word would refuse: the cached
  word alone NEVER refuses. A fresh-confirmed non-Remote still refuses; two consecutive failed
  refresh reads refuse (genuinely unreadable, class D). The debug-mode half of the gate is
  unchanged — its word rides the control-rate core and is judged as decoded.

## Control-decision audit attribution

- A `control_decision` audit row whose cycle selected exactly ONE unit carries that unit's `unit_id`
  (2026-08-23 console Activity per-unit filters); a genuinely multi-unit decision stays fleet-level
  (`unit_id: null`) because one row cannot honestly name one of several units. Fleet-level fields —
  the full observation-sequence map, both watt figures, the cycle and decision ids, the fingerprints
  — are unchanged on every row, and the kernel still rejects a factory event that attributes the
  wrong unit.
- Every `control_decision` row carries the per-unit watt breakdowns (2026-08-23 fleet-row opacity
  fix): `authorized_watts_by_unit` is the decision's per-unit authorized watts (unsigned; null when
  no batch was minted, matching `authorized_active_w == 0`), and `requested_watts_by_unit` is the
  intent's own target map (null for scalar fleet-total intents). `audit.appended` bus payloads
  carry both maps too. Both fields are optional with null defaults, so durable rows written before
  they existed keep decoding; an unknown payload key is still refused.
- A decision held by a latched emergency stop correlates to the stop explicitly:
  `correlation_id = "emergency_stop:{stop_id}"` (other decisions keep
  `intent:{intent_id}:revision:{revision}`), so the Activity view names the stop on the row itself
  instead of inferring it from the newest latch event.
- A heartbeat renewal the fleet loop suppresses (2026-08-23 actuation-loss visibility) is audited as
  `heartbeat_failed` for its unit (`result: suppressed`) and named in the process log
  (`SUPERVISED HEARTBEAT FAILURE (<unit>)`), every suppressed cycle; supervision keeps running.

### Read-plan tier promotion

With the feature enabled, the live decode strategy promotes the PCS live block `0x1000` (grid at
+17, load at +20) from the cold ring into the control-rate core, so `grid_power_w` refreshes
every telemetry cycle. The plan stays inside the commissioned cadence budget: steady-state
≤ 8 windows plus the probe (~0.9 s at the 0.1 s inter-frame gap, inside the 1.5 s control
period; bootstrap cycle ≤ 10 windows). With the feature absent or disabled the plan matches
today's in structure, budget, and ring period — PCS block included in the ~108 s cold ring —
with one rotation-phase change: the default ring now visits the SYSTEM OVERVIEW first (cycle 8,
B5's cold-ring move) and the PCS block at cycle 16, so the advisory mode words and system SOC
appear within seconds of boot instead of at the ring's tail.

**Deny-triggered cell promotion (SYNC_RESILIENCE_AUDIT B4, 2026-08-24).** A control decision
carrying any cell-derived deny reason (`cell_voltage_low`, `cell_voltage_high`, `cell_imbalance`,
`cell_count_invalid`) promotes the 0x5200 cell window into the NEXT telemetry cycle of every
unit's actor, regardless of the every-3rd-cycle phase: the deny is then re-evaluated on FRESH
battery data at most one cycle later, so a violation that recovered in the live battery (a load
sag that lifted) authorizes instead of denying on the cached window, while a PERSISTENT fresh
violation keeps denying — the 2026-08-23 LHS 54 mV manual fresh-read confirmation, automated.
The promotion lasts exactly one cycle (a persisting deny re-promotes); the promoted plan is
≤ 9 windows plus the probe (~1.0 s at the 0.1 s inter-frame gap), inside the 1.5 s control
period and the 1.60 s renewal budget of the commissioned write-enabled timing. `cell_data_stale`
(15 s) and the whole-poll failure path are untouched: genuinely unreadable stays fail-closed.

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

Exactly one unit at a time, never a fleet-wide dispatch: the neediest — lowest authoritative SOC
(`authoritative_soc_pct`, the BMS figure per the 2026-08-24 ruling) among units whose latest
observation is controllable (lifecycle `ARMED_IDLE`/`ACTIVE`), below the
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
