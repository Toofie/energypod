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

Blocking code vocabulary (SYNC_RESILIENCE_AUDIT S1, 2026-08-24): both `blocking_fault_codes` and
`blocking_warning_codes` are configurable policy sets, and the codes they must carry are the
decoder's GENERATED strings — format `{prefix}_{bit}` over the fault catalog's word prefixes
(`PCS_Warning0_1`, `DCDC_Warning0_1`, `PCS_Fault0_0`, `Stack_Warning0_12`, ...; see
`adapters/modbus/faults.py` and PROTOCOL_EVIDENCE 9). The two EE-calibration signals the live
config once named in human terms are WARNING bits — `PCS_Warning0_1` ("EE Calibration Parameter
Out of Range") and `DCDC_Warning0_1` ("EEPROM Calibration Parameter Out of Range") — and the old
human-name entries could never match a decoded code (silently inert, fail-open). Both bits are
standing-active on this fleet's three pods and their severity is not established by the vendor
evidence, so the shipped example documents them without enabling them; the composition now wires
the configured warning set through to the policy instead of a hard-wired empty set.

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

Entries are immutable, timezone-aware local windows with day set, action, watts, selected units and
effective date range. Cross-midnight windows are supported; overlaps at the same priority are
rejected. Evaluation returns a short-lived schedule intent, never a hardware command. The full
operator-facing contract — postures, UI plan, ordering rationale — is `docs/DESIGN_SCHEDULES.md`;
this section is the wire contract.

- **Watt form (dual, exactly the `PowerIntent` rules):** an entry carries EITHER scalar `watts`
  (a non-negative integer fleet total — the original form, unchanged) OR `watts_by_unit` (one
  positive integer per selected unit; key set exactly `unit_ids`; the fleet total is the sum);
  never both, never neither. `idle` entries require scalar `watts: 0` and no mapping. The entry's
  form is carried verbatim onto the evaluated intent; per-unit targets are caps at allocation
  exactly as on REST dispatch.
- **Composition (the `excess_charging` block-presence doctrine):** a PRESENT `schedule:` config
  block composes the surface — both REST routes, the `ScheduleRunner` in the fleet cycle, the
  `schedule_state` snapshot projection. An ABSENT block composes nothing (no runner, no projection
  key) and both routes answer 409 `schedule_not_commissioned`. There is deliberately NO `enabled`
  key: the plan is the state (empty plan or all-entries-disabled = off); a second master switch is
  the invisible-starvation class. Keys: `allowed_windows_local` (list of `["HH:MM","HH:MM"]` local
  civil pairs, cross-midnight allowed, union = the allowed command set; DEFAULT `[["06:00",
  "20:00"]]` when the block omits it — the day-only YIELD posture; the shipped default doubles as
  the named `DAY_DEFAULT` constant, and "night" means any civil minute outside it) and
  `intent_ttl_s` (default 10.0; > `timing.control_period_s` and <= 300 s, validated). `posture` is
  derived read-only: `partition` when the allowed set covers any minute outside `DAY_DEFAULT`,
  else `yield`.
- **Night-writer coordination (CONTINUITY 2026-08-23 environment fact — other applications write
  these batteries at night):** publishing any ENABLED entry whose window (split across midnight
  when it crosses) has any minute outside the union of `allowed_windows_local` is REFUSED at the
  facade/REST layer with 409 `schedule_window_not_allowed` (`details`: `posture`,
  `allowed_windows_local`, `offending` entries+windows; message names the posture and the two
  honest paths — trim the windows, or the partition choice via a config revision widening the
  policy plus the one-time acknowledgement). The FIRST publish ever on a site whose enabled
  entries include any night minute must carry `"night_posture": "PARTITION_ACKNOWLEDGED"` unless
  the site holds the durable audit fact `schedule_night_windows_acknowledged` (the
  `excess_charging_economics_acknowledged` mechanics: keyed existence check at boot, never
  re-prompted, durable-append-FIRST — an audit failure refuses the publish); otherwise 409
  `night_posture_acknowledgement_required` (`details: {"acknowledgement":
  "PARTITION_ACKNOWLEDGED"}`). The console cannot widen the policy — only a config revision can.
  The CONTESTED posture is not implementable and not offered; the arm-time sole-writer preflight
  is unchanged and remains the structural enforcement under PARTITION.
- **Evaluation loop:** a `ScheduleRunner` ticks in the fleet cycle beside the excess adviser —
  after the polls and recovery pass, BEFORE the adviser step and the kernel tick (the schedule's
  claim is a published fact; the adviser observes it the same cycle), bounded and suppressed per
  cycle exactly like the advisory step. Each tick it reads the plan through `ScheduleRepository`
  (one singleton row — a publish lands within one cycle), evaluates, and maintains EXACTLY ONE
  live `PowerIntent` with `source: SCHEDULE`, keyed `(plan.version, entry_id)`: submit on open,
  remove-then-submit renewal while the window holds (the adviser's discipline), remove on window
  end / entry disable / plan change. Intent TTL is `intent_ttl_s`; window end is non-renewal and
  the firmware watchdog is the hand-back. Submissions enter through the composition-internal
  facade twin `submit_schedule_intent` (the `submit_advisory_intent` pattern: source pinned to
  `SCHEDULE`, intent-id prefix `schedule-`, same audit/publication contract, never routed) under
  the principal `energypod:schedule-runner` (observe + dispatch, non-interactive, site-bound).
- **Precedence (unchanged, pinned):** `emergency_stop > manual > agent > optimizer > schedule`,
  per unit. A window opening while a higher-priority intent runs WAITS: the runner never checks
  claims and never withdraws against a higher source — it keeps renewing and the arbiter
  represents it the cycle after the claimer lapses. A schedule never suppresses the adviser
  fleet-wide: suppression is per unit, by claim. The adviser's own yield is the new
  `excess_charging.yield_to_schedule` (bool, DEFAULT true): when true a live SCHEDULE intent
  claiming the adviser's target unit is a yield trigger exactly like MANUAL/AGENT (withdraw by
  removal; re-entry after claim expiry AND entry-hysteresis re-qualification); when false the
  adviser outranks schedules by arbiter and starves them invisibly (today's behavior — kept only
  as an explicit opt-out).
- **REST:** `GET /api/v1/schedule` (`observe`) returns `{"plan": <plan | null>, "policy":
  {"posture", "allowed_windows_local", "intent_ttl_s"}, "acknowledged_night_windows": bool,
  "next_action": <next occurrence | null>}`; `plan` is null before the first publish; wire entry
  shape: `{entry_id, days: ["mon",...], start_local, end_local ("HH:MM"), action ("charge"|
  "discharge"|"idle"), watts | watts_by_unit, unit_ids, effective_from, effective_until,
  priority, enabled}`. `PUT /api/v1/schedule` (dispatch scope + INTERACTIVE principal +
  Idempotency-Key) carries `{"expected_version": <int | null (null asserts no plan exists)>,
  "timezone", "entries", "night_posture"?}`. Validation order: 422 `validation_error`
  (shape/domain, per-entry); 409 `schedule_window_not_allowed`; 409
  `night_posture_acknowledgement_required`; 409 `schedule_version_conflict` (`ScheduleVersionConflict`
  mapped; `details: {"current_version": <int | null>}` — the console's answer is reload-and-
  re-apply, never a silent merge). The new plan version is `current + 1`; the mutation commits
  through `ScheduleRepository.replace` on the Impl-10 commit-then-audit pattern with
  `submit_intent`'s compensating shape (an audit/publication failure restores the prior plan).
  200: `{"version", "plan", "diff": {"added", "removed", "changed", "timezone_changed"},
  "acknowledged_night_windows", "next_action"}`.
- **Facade methods:** `get_schedule(principal)` (observe; repository + pure-function reads,
  never triggers control), `replace_schedule(principal, *, expected_version, timezone, entries,
  night_posture, idempotency_key, request_id)` (dispatch + interactive), and the internal
  `submit_schedule_intent` twin. The pure next-occurrence helpers (`next_start`, `window_end`)
  live beside `ScheduleEvaluator` and are the single implementation of every countdown (GET's
  `next_action`, the projection, the Home card); no client reimplements civil-time arithmetic.
- **Audit and events:** audit `schedule_replaced` (version from→to, diff summary, principal)
  and the durable-once `schedule_night_windows_acknowledged`; bus `schedule.replaced`
  (`{principal, version, diff}`), `schedule_window.opened` (`{entry_id, version, action, watts |
  watts_by_unit, unit_ids, ends_at}`) and `schedule_window.closing` (`{entry_id, version,
  unit_ids, reason: window_ended | plan_replaced | no_plan}`) — transitions only; countdowns are
  snapshot-derived. The snapshot carries a feature-detected top-level `schedule_state` (absent
  when the block is absent; single writer = the runner's post-tick update; `active` derives from
  `held_intent_id`): `{version, active, entry_id, held_intent_id, ends_at, ends_in_s, next,
  posture, last_action: idle|submit|renew|remove, last_tick_at, reason_codes}` with the pinned
  vocabulary `no_plan | no_window_open | window_open | waiting_for_higher_priority |
  window_ended | plan_changed | units_disarmed | unit_parked` (`units_disarmed`: a window is open and the
  plan holds, but no unit is controllable — the fleet sits disarmed, so the runner still
  publishes its claim yet nothing can act on it; joins the list additively, outranks
  `waiting_for_higher_priority`; `unit_parked` — 2026-08-24, DESIGN_POD_PARKING §3 —
  the same mechanism verbatim: the runner does NOT exclude parked units, it submits the
  published fact, the facade refuses `device_debug_mode_active` with park provenance, and
  the projection carries the code).

## API and MCP

- REST is versioned at `/api/v1`. The service API uses bearer authentication; reads require
  `observe` (and audit additionally requires `audit:read`), intent mutations require `dispatch`,
  and arming requires both `arm` and an interactive human principal. Maintenance is absent except
  the one guarded parking surface ("Pod parking" below — operator-only REST, never MCP). The
  browser session/OIDC adapter is a separate boundary and must use secure HTTP-only same-site
  cookies, CSRF protection, trusted origins, and recent-authentication policy before deployment; a
  raw ambient cookie is never accepted as a service-API bearer credential.
- MCP is read-only by default. Optional dispatch requires explicit configuration, the `dispatch`
  scope, and a separately issued rotatable automation credential (human operator sessions may also
  hold that scope). It submits ordinary bounded, expiring intents and cannot arm, acknowledge stops
  or inhibits, change policy, or use debug/maintenance modes — park/resume is the one
  debug/maintenance-class capability, and it is an operator-only REST surface (interactive human
  principal, typed confirmation; never an MCP tool — DESIGN_POD_PARKING §1/§11). Its audit view
  requires both `observe` and `audit:read`, matching the REST boundary.
- The MCP read surface (2026-08-24) is `get_snapshot`, `get_unit_detail(unit_id)` (the REST
  `GET /api/v1/units/{unit_id}` projection verbatim, `observe`; a malformed or unknown unit id is
  a tool error, never an empty view), `get_health`, `get_schedule()` (the Schedule §5 GET view as
  a read-only ride-along — no MCP surface can publish a plan, and an absent schedule block answers
  the refusal-shaped error), `get_observed_objectives(last)` (the night-writer window read,
  `Nh`/`Nd`, default `24h`), `get_energy_days(limit)`, `get_plant_history(from, to, unit_ids?,
  fields?, points?)`, and `get_recent_audit(limit)`. Every read requires `observe`, forwards to
  the one facade, and never re-implements a projection.
- The tool descriptions ARE the agent-loop contract (2026-08-24): poll `get_snapshot` no faster
  than the site control period (state cannot change between cycles); read
  `get_observed_objectives` and `get_schedule` before dispatching overnight or into a window, and
  `get_unit_detail`/`get_recent_audit` to diagnose a denial; submit only through `dispatch_intent`
  with an idempotency key (same key + same arguments replays the stored answer; same key +
  different arguments is refused); holding power means a fresh submission before TTL, never a
  renewal call; acceptance is not authority — the arbiter decides per unit (emergency stop >
  manual > agent > optimizer > schedule) and the safety kernel may clamp or deny — and a denied
  or fenced dispatch is never retried unchanged, nor is anything dispatched onto units claimed by
  a higher-priority source or latched by a stop/inhibit.
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
- Preflight discrimination — POD AUTONOMY vs foreign writers (2026-08-24 live
  blocker): the pods' own firmware autonomously self-charges by holding a PQ
  objective (measured ~-520..-560 W daytime and up to ~-2.27 kW deep
  self-charge; negative P = charge), and a fresh process has no write
  provenance, so the strict rule misread the battery's own self-consumption
  as a foreign writer after every restart — an unwinnable race against the
  ~1.34 s watchdog zero. Two sanctioned exceptions now exist, both recorded
  on the actor as `last_arm_classification` and audited on the `unit_armed`
  row (reason code `arm_pod_autonomy` / `arm_takeover_acknowledged`):
  - With the commissioned `policy.autonomous_charge_signature_max_w` band
    set, a nonzero readback this process did not write whose sign and
    magnitude match the pod's OWN signature — negative P within the band, Q
    zero — is classified POD AUTONOMY: the arm proceeds and the controller's
    next renewed objective replaces the pod's own (beat-autonomy doctrine).
    A discharge (positive) objective, a magnitude beyond the band, or any
    reactive component still latches `external_writer` exactly as before.
  - An operator may take over a beyond-band objective deliberately:
    `POST /api/v1/arm` with `{"confirmation":"ARM","takeover":"ACKNOWLEDGE"}`
    arms under an explicit, per-request, audited acknowledgement. Without it,
    the beyond-band objective still latches. No acknowledgement or
    classification is ever persisted — boot stays observe-only and a fresh
    process holds no provenance (unchanged).
- The writable registers are exactly two named blocks with value domains
  (DESIGN_POD_PARKING §5, 2026-08-24): `[1, P, Q]` at `0x0200` (negative P =
  charge, positive P = discharge — live-proven 2026-08-22) and the debug-mode
  word at `0x8000` with `v ∈ {0, 1}` (live-proven 2026-08-24,
  `docs/evidence/standby-cycle-2026-08-24.md`), reachable only through the
  separately named `write_debug_mode(value)` transport method — the generic
  `write_registers` path can never reach `0x8000` (pinned by an
  architecture-fitness test), the {0, 1} bound is enforced at the transport
  layer, and the named method is composed only when a `parking:` block is
  present (§5.1 there; values 2–6 remain permanently unexposed). No other
  address is writable by any composition, mode, or tool.
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
  `authorized_power` is a NON-CONSUMING peek at the single-use capability store: authority is
  minted as one single-use setpoint per heartbeat and is consumed by the actor's write, so a
  fully-authorized unit reads `null` here BETWEEN consumptions (the 2026-08-25 console UI audit
  proved this live). It is a liveness hint, never a standing figure — the standing authorized
  watts live in the snapshot's top-level `intent.authorized_watts_by_unit` (mirroring the
  freshest `control_decision` audit row) and in the `control_decision`/`authorization.granted`
  events.
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
  keep projecting the intent's own fleet total for every covered unit. The snapshot additionally
  carries a top-level `adviser_state` projection when the `excess_charging` block is composed
  ("Adviser state projection" under the excess-solar section) — absent otherwise, never
  null-standing-in-for-absent.
- `unit_detail(principal, unit_id)` (REST `GET /api/v1/units/{unit_id}`, `observe` scope)
  returns the full latest observation projection for one unit: identity (`device_identity`),
  `protocol_profile`, `connection_epoch`, telemetry and cell sequences and capture times, all
  scalar measurements above, the complete `cell_voltages_v` and `temperatures_c` arrays, the
  per-field `quality` map, faults and warnings. Unknown unit ids are refused with the
  structured envelope. It is a read-only view of repository state.
- `health(principal)` separates `liveness` (process-up), `service_readiness` (repositories and
  coordinator responsive), and `control_readiness` (every unit qualified and at least one
  armed, with blocking reasons listed per unit). Readiness never fabricates optimism: an
  unknown unit state is a reason, not an assumption. The report additionally carries a
  per-unit `units` recovery block from the self-healing awareness layer — see
  "Self-healing awareness layer" for `{unit_id, health_state, reasons, remediation_hint}`
  and the `actuation_incoherent` control-readiness reason.
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
- Mutation atomicity (Impl-10, one doctrine for every facade mutation): a mutation whose
  effect has an exact inverse and enables power is ATOMIC with its audit — `submit_intent`
  and `submit_advisory_intent` roll the stored intent back (a dispatch the caller saw fail
  leaves nothing to arbitrate), and `arm` COMPENSATES: a failed audit append stops the
  request, disarms every unit it armed, lands one `unit_armed`/`rolled_back` audit row per
  compensated unit (reason codes `audit_unavailable` + `disarmed`/`disarm_failed`), and
  raises the original failure — no unit is ever left physically armed behind a failed
  answer. A mutation with NO inverse appends its durable record BEFORE touching state:
  `acknowledge_inhibit` and `acknowledge_emergency_stop` refuse on a failed append with the
  latch fully intact and the call retryable (the row records the operator's command against
  the latch as issued; a mutation failure after the append leaves the latch for the retry,
  which lands its own row). A SAFETY-POSITIVE mutation never undoes or fails its completed
  stop work: `disarm` and `cancel_intent` stand, and a failed audit/publication is named in
  the response's `degraded` list (`audit_unavailable[:unit_id]`, `publish_unavailable`)
  exactly like `emergency_stop`'s; the disarm, cancel, and both acknowledgement responses
  carry `degraded: []` on the clean path.
- Degraded stop errors (Impl-11/Impl-15): `emergency_stop`'s error paths (store refused,
  unknown units) raise the original error with the uniform `DegradedReport`
  (`energypod.application.service.DegradedReport`: `stop_id`, `degraded` reason codes)
  attached; REST translates any error carrying it into `503 emergency_stop_degraded` with
  `details.stop_id` and `details.degraded`, keeping the exact-id acknowledgement usable —
  never a bare `internal_error` over safety work that landed.
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
- Night-writer detector (2026-08-23 census follow-up): `foreign_objective.observed` publishes
  on the alert tier only — one event per foreign episode (or per reason change inside one),
  payload `{unit_id, observed_at, active_w, reactive_var, classification, reason, lifecycle,
  claimed, run_mode_w, ctrl_mode_w, work_mode_w, debug_mode_w, grid_power_w, pv_evidence}`.
  Quiet-tier evidence (in-band autonomy samples, handback-grace samples) is NEVER published;
  it accumulates in the session record behind `GET /api/v1/objectives/observed`.

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
- Honest per-field quality (MUTATION-3/6, 2026-08-25): the composed simulate-mode decode
  derives every quality-map field from the served words with the production wire decoder's
  own fail-closed helpers (signed measurements, 0-100 percentages, non-negative dynamic
  limits) — never blanket GOOD. A sentinel limit word (0xFFFF/0x8000-style, or a
  malformed-injected complement such as 3000 ^ 0xFFFF = 62,535 unsigned = -3001 signed)
  decodes as BAD with the value absent, exactly as the live wire decode refuses it; an
  out-of-range percentage word fails its FIELD closed (BAD, value absent) instead of
  failing the whole poll. With a clean bank every field is GOOD and the decoded values are
  byte-identical to the previous behavior. `script_quality(field, quality)` /
  `clear_scripted_quality(field)` on the pod script one quality-map field's decode
  judgment for a scenario WITHOUT touching any served register word (the device keeps
  serving its bytes; only the decode's trust moves): BAD/MISSING also withdraw the
  field's decoded value, SUSPECT/STALE keep it; the field must be one of the twelve
  quality-map fields and the quality a `DataQuality` member.

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

### The activation surface (DESIGN_EXCESS_ACTIVATION, 2026-08-25)

The operator-facing three: the `adviser_state` projection, the
`excess_adviser.state_changed` event, and the guarded activation toggle. Nothing here
changes the adviser's authority, the export bound, the hysteresis, the yield rules, or any
safety path.

**Composition semantics (P6).** A PRESENT `excess_charging` block composes the machinery —
export triple armed, PCS live block promoted into the control-rate read plan, adviser
constructed, `adviser_state` present in the snapshot (including while suspended) — with
`enabled` gating PARTICIPATION. An explicit `enabled: false` composes suspended at boot and
is enableable at runtime; an ABSENT block composes nothing (no adviser, no triple, no tier
promotion, no projection key, toggle answers 409) — byte-identical behavior. The
commissioning gates bind to block-PRESENCE: a disabled block that could never be enabled
safely is refused at validation time. Effective participation is `enabled AND
acknowledged_economics`: an unacknowledged site composes SUSPENDED even with config
`enabled: true`.

**The `adviser_state` projection** rides the snapshot TOP LEVEL beside `intent` (feature
detected: the key is absent when the block is absent). Exact shape:

```json
"adviser_state": {
  "enabled": true, "enabled_origin": "runtime", "acknowledged_economics": true,
  "active": true, "hysteresis_state": "holding", "target_unit_id": "mid",
  "commanded_charge_w": 1400, "eligible_export_charge_w": 1600,
  "fleet_export_w": 1800, "export_evidence": "good", "charge_cap_w": 2500,
  "held_intent_id": "excess-3-100.000000", "last_action": "renew",
  "last_tick_at": "2026-08-25T11:04:31+00:00",
  "reason_codes": ["export_headroom_available"]
}
```

`active` derives from `held_intent_id` (never a lifecycle guess), `fleet_export_w` is
Σ `grid_power_w` over the fleet (positive = export) and is NULL on any missing/bad/stale
grid word — never zero-filled; `export_evidence` is the worst per-unit grid word under the
bound's own fail-closed rules (precedence `missing > bad > stale`). One writer: the fleet
loop's post-tick update; the toggle flips only the participation flag and the next tick
observes it. `reason_codes` is ONE pinned vocabulary — the tick's own codes verbatim
(`no_export_headroom`, `no_acceleration_over_autonomy`, `below_exit_hysteresis`,
`no_eligible_target`, `yielding_to_higher_priority`, `export_headroom_available`) plus
exactly the projection states the tick alone cannot see (`disabled_by_config`,
`disabled_by_runtime`, `economics_acknowledgement_required`, `export_evidence_missing`,
`export_evidence_bad`, `export_evidence_stale`, and `unit_parked` — 2026-08-24,
DESIGN_POD_PARKING §3 — which renders in place of `no_eligible_target` when every
otherwise-eligible unit's exclusion cause is park); it is never empty. When the rollup
collapses, the evidence word REPLACES `no_export_headroom` (whose definition requires GOOD
evidence).

**The `excess_adviser.state_changed` event** carries the same payload minus
`charge_cap_w`/`last_action`/`last_tick_at`, plus `"heartbeat": bool`. Publication is
throttled on the semantic tuple `(enabled, enabled_origin, acknowledged_economics, active,
hysteresis_state, target_unit_id, export_evidence, reason_codes)` — the watt figures ride
every publication but never trigger one. While `enabled` is true a heartbeat republish
fires every 30 s (a constant, not a config key); while disabled NOTHING publishes: the
state_changed carrying the disable is the last event.

**The guarded toggle** — `POST /api/v1/excess-charging` (arm scope; an interactive
principal additionally to enable; Idempotency-Key required):

```json
{"action": "enable" | "disable", "confirmation": "EXCESS", "economics": "NET_BILLED"}
```

`economics` is optional and consulted only on the first enable ever. The 200 body is
`{"feature": "excess_charging", "enabled", "enabled_origin", "persisted": false,
"acknowledged_economics", "adviser_state"}` — `persisted` is always false and spelled
anyway (P1: runtime toggles never survive restart; boot recomposes from config). Refusals
are the structured envelope with code `excess_charging_not_commissioned` (absent block),
`economics_acknowledgement_required` (`details: {"acknowledgement": "NET_BILLED"}`), or
`excess_enable_refused` (`details: {"reasons": [...], "unit_ids": [...], "stop_ids":
[...]}` — enable is refused while any unit runs under a manual/agent/schedule request or
any latched stop holds; latched inhibits do NOT refuse; disable is never refused). Every
call is audited (`excess_charging_toggled`, result `enabled`/`disabled`/`noop`) and
idempotent by key. The net-billing acknowledgement is ONE durable audit fact
(`excess_charging_economics_acknowledged`, deterministic event id, boot-loaded via one
keyed existence check, never re-prompted); its durable append lands BEFORE the latch
flips, and an append failure refuses the enable.

## Energy scorecard (advisory accounting — DESIGN_ENERGY_SCORECARD, 2026-08-26)

Accepted design, pending implementation. Pure advisory read path — no new authority, no
writes, no read-plan cadence change. Full contracts, the A-1 buy/sell pinning protocol,
and the ordered implementation plan live in `docs/DESIGN_ENERGY_SCORECARD.md`; this
section pins the wire-facing shapes.

- `Observation` gains six ADVISORY cumulative-energy fields (float kWh, `None` when the
  `0x4101` cold-ring window was not served this observation): `energy_grid_a_kwh`,
  `energy_grid_b_kwh` (NEUTRAL names — the pair's decode ORDER is vendor-confirmed but
  its buy/sell ROLE labels are evidence-open, field-mapping A-1; renaming to
  bought/sold happens only behind the `grid_counter_roles` config gate below),
  `energy_load_kwh`, `energy_pv_kwh`, `energy_charge_kwh`, `energy_discharge_kwh`
  (the charge/discharge role labels ARE capture-confirmed). Their quality keys join
  `ADVISORY_QUALITY_FIELDS` (the twelve-key quality map extends to eighteen by the same
  mechanism); the fields stay OUTSIDE every safety completeness set — an unserved
  energy block must never refuse power. Decode is low-word-first `uint32 × 0.1` in
  vendor pair order (PROTOCOL_EVIDENCE §6); the snapshot/unit-detail projections
  expose all six readthrough-style, nullable, never zero-filled.
- The scorecard's metric sources are pinned per metric (DESIGN §2): battery
  charged/discharged and load from DEVICE-COUNTER daily deltas (role-confirmed,
  device-side coverage); grid bought/sold from OUR OWN integration of the per-pod CT
  `grid_power_w` (`0x1000+17`, the control-grade PCS view ONLY — never merged with the
  `0x0137` view) at the control rate, sign-split import/export, zero-order-hold over
  observation capture times, gaps above `integration_max_gap_s` excluded (never
  interpolated) with a per-unit/day coverage fraction, worst-unit fleet rollup, and a
  `partial` day marker below `min_day_coverage_pct`. `charged_from_surplus_kwh`
  integrates measured battery watts over ticks where `adviser_state.active` and the
  adviser's target is that unit (the excess graduation economics evidence). A
  DECREASING cumulative is a reset, not a negative delta: the unit-metric-day is
  re-baselined and flagged `counter_reset_observed` (audited).
- `EnergyDayRecord` (frozen, per site-day in the site timezone; DST days store
  `utc_offset_minutes`): `{date, timezone, utc_offset_minutes,
  kind: complete|partial|in_progress, units: {unit → per-unit metrics +
  coverage_pct + metric_flags}, fleet: {the five sums + charged_from_surplus},
  sources, counter_cross_check {grid A/B deltas, consistency verdict},
  solar_production_measured: false}`. `null` per-unit figures mean source-absent-all-day,
  never 0. Solar production is NEVER presented as measured (site PV is not wired to the
  pod inputs — the PV counter is readthrough-only).
- **`GET /api/v1/energy/days?limit=N`** (observe; N ∈ 1..31 default 8, newest-last) →
  `{"days": [EnergyDayRecord...], "grid_counter_roles": "unpinned|vendor_labels|swapped",
  "solar_production_measured": false}`; answers 409 `energy_scorecard_not_commissioned`
  when the config block is absent. There is deliberately NO mutation on this surface.
  Paging is limit-only (no cursor): a day record is IMMUTABLE — the accountant
  finalizes a site-day exactly once and the ledger insert is first-writer-wins —
  so there is no revision marker and `energy.day_rolled` carries each date
  exactly once.
- Snapshot top-level `energy_today` (feature-detected: absent key when the block is
  absent) = the in-progress day's record plus `as_of`. Bus event `energy.day_rolled`
  (the completed record) is a rollover TRANSITION only — never a heartbeat. Audit
  facts: `energy_day_recorded`, `energy_counter_reset_observed`, and — when the
  operator lands the active pinning protocol — `energy_counter_roles_pinned`.
- Wire pins added with the implementation (2026-08-26, the console-coordination
  pass): the `fleet` block is exactly the per-unit metric shape MINUS
  `metric_flags` plus the worst-unit rollup (the five sums +
  `charged_from_surplus_kwh` + `coverage_pct`); `counter_cross_check
  .consistent_with` is `"vendor_labels" | "swapped" | "undiscriminating" |
  null` (null = the day could not discriminate: low coverage, a sub-0.5 kWh
  side, a grid-pair reset, or neither ordering fits the tolerance);
  `energy_today.as_of` is ALWAYS the site-local ISO string carrying its UTC
  offset (e.g. `2026-08-26T14:03:00+10:00`); and both `energy_today` and the
  days body carry `"tariff"` — `null` when the optional config keys are absent
  (kWh-only, no money figures anywhere), otherwise `{"currency": "AUD",
  "import_cents_per_kwh": 28.0, "export_cents_per_kwh": 9.0}` (the operator's
  own rates, labeled as theirs). The per-unit energy readthroughs
  (`energy_grid_a_kwh` .. `energy_discharge_kwh`, neutral A/B naming) ride the
  snapshot telemetry summary and the unit-detail projection ONLY when the
  block is composed.
- Config block `energy_scorecard:` (block-presence doctrine: PRESENT composes the
  accountant into the fleet loop + snapshot key + route; ABSENT is byte-identical with
  409; no `enabled` key — decommissioning removes the block): `grid_source:
  integrated|device_counter` (default `integrated`; `device_counter` REFUSED at
  validation while roles are unpinned), `grid_counter_roles: unpinned|vendor_labels|
  swapped` (default `unpinned`; a pinned value additionally requires the durable
  `energy_counter_roles_pinned` audit fact at boot — the excess-economics
  keyed-existence precedent), `integration_max_gap_s` (> `control_period_s`, ≤ 60),
  `min_day_coverage_pct` ((0, 100]), optional `tariff {currency,
  import_cents_per_kwh, export_cents_per_kwh}` (absent ⇒ kWh-only, no money figures).
- Persistence: an `EnergyLedgerRepository` port (`record_day`, `get_day`,
  `latest_days`, `load_baseline`/`save_baseline` — the live-day baseline is durable so
  counter deltas survive restarts by design) with memory and SQLite adapters (a
  schema_version migration); MCP gains the read-only `get_energy_days(limit)` tool.

## Plant history (telemetry historian — DESIGN_PLANT_HISTORY)

Accepted design, pending implementation. A durable time-series record of the
plant: a historian in the fleet loop samples the observation stream at a
configured cadence into SQLite (schema_version 3), and one observe-scope
route serves windowed, server-downsampled series for the console's History
view. Observability only — full contracts, sizing math, and the ordered plan
live in `docs/DESIGN_PLANT_HISTORY.md`; this section pins the wire-facing
shapes.

- **Sampler (DESIGN §2):** a `TelemetryHistorian` ticks once per fleet cycle
  beside the energy accountant (after the polls, before the kernel tick),
  bounded and suppressed — a failed append is a GAP, never a delay to
  control. Cadence `sample_interval_s` (default 30.0; > `control_period_s`,
  ≤ 3600; 2,880 rows/unit/day); `sampled_at` is the tick's wall clock at
  second precision, one timestamp shared by every unit sampled in the tick.
  A unit's LATEST observation is sampled only when no older than
  `max(3 × control_period_s, sample_interval_s)` — a stale latest writes NO
  row (controller down and unreadable telemetry are absent rows, never
  interpolated; the API and charts show gaps as gaps). Each row: the 15
  numeric observables (`system_soc_pct`, `bms_soc_pct`, `soh_pct`,
  `battery_watts`, `grid_power_w`, `load_power_w`, `pack_voltage_v`,
  `pack_current_a`, `cell_min_v`, `cell_max_v`, `cell_spread_mv`,
  `temperature_min_c`, `temperature_max_c`, `dynamic_charge_limit_w`,
  `dynamic_discharge_limit_w` — all null when absent, never zero-filled),
  `lifecycle`, `health_state`, the four mode words, a per-row `quality`
  rollup (worst over `REQUIRED_SAFETY_QUALITY_FIELDS` plus the CT fields when
  composed; precedence `missing > bad > stale > suspect > good`; the
  cold-ring `system_soc_pct`/`soh_pct` are excluded — their staleness is the
  documented advisory doctrine, not degradation), and the commanded triple
  `commanded_source` (`manual|agent|schedule|excess_adviser|night_adviser|
optimizer`)/`commanded_direction`/`commanded_w` (the per-unit winner and
  peeked authority at sample time; all null when no intent claims the unit).
  NOT recorded: per-cell voltages and the full temperature array (min/max/
  spread only — the per-cell history question is answered in the design §3.6),
  any kWh figure (the scorecard's), and any decision fact (the audit trail's).
- **Persistence (DESIGN §2.2–2.4):** `telemetry_sample` keyed
  `(unit_id, sampled_at)` WITHOUT ROWID, plus `telemetry_rollup_hourly`
  (per-field min/max/mean, `sample_count`, `worst_quality`; an empty hour
  writes no row) — schema_version 3, migration in place, riding the EXISTING
  database path. Maintenance (one transaction: rollup the hours past the
  horizon, then prune — prune only inside the committing transaction) runs at
  boot and on the first tick after each site-local midnight. Defaults:
  full-resolution 14 days (≈ 24 MB) + hourly rollups forever (≈ 10 MB/year);
  steady state ≈ 25–40 MB.
- **Config block `plant_history:`** (block-presence doctrine; no `enabled`
  key): `sample_interval_s: 30.0`, `retention_full_resolution_days: 14`
  (1..3650), `retention_rollup_days: 0` (0 = forever). A PRESENT block
  composes the historian, the route, the MCP tool, and the snapshot's
  feature-detected `history_state` (`{sample_interval_s,
  retention_full_resolution_days, last_sample_at: {unit → iso | null}}`);
  an ABSENT block composes nothing and the route answers 409
  `plant_history_not_commissioned`. A PRESENT block REQUIRES the `storage`
  block (durable history is the point; a silently in-memory historian is the
  invisible-off class); `simulate` composes the in-memory adapter.
- **`GET /api/v1/history?from&to&unit_ids&fields&points`** (observe scope;
  read-only, no mutation exists): `from`/`to` REQUIRED ISO-8601 WITH explicit
  offset, `from < to`, window ≤ 31 days; `unit_ids` comma-separated
  configured ids (default all); `fields` from the vocabulary above plus the
  step-encoded `lifecycle`, `health_state`, `commanded` (default
  `bms_soc_pct,battery_watts,grid_power_w,temperature_min_c,
temperature_max_c`); `points` 50..2000 (default 600). Errors: 422
  `validation_error` for every parameter rule (no bare 400 on this surface);
  409 `plant_history_not_commissioned`; a window before the first sample is
  a 200 with empty series and `first_sample_at: null`. One resolution per
  response, chosen by the data horizon: `resolution: "full"` (raw samples)
  when `from` is at/after the oldest retained full-resolution sample, else
  `"hourly"` (rollups) for the whole window.
- **Downsampling is pinned server-side (DESIGN §3.2):** classic LTTB per
  series to `points`, first/last samples always retained, deterministic
  (ties to the earlier sample), and every emitted point a REAL stored sample
  — never a synthesized mean (bucket-mean aggregation was rejected: it
  fabricates values and flattens charge bursts). Each series additionally
  carries `window_min`/`window_min_at`/`window_max`/`window_max_at`/
  `sample_count` over ALL rows in the window, so peaks that downsampling
  drops are still reported.
- **Response:** `{"from", "to", "resolution", "points", "fields", "units":
  {unit → {first_sample_at, last_sample_at, sample_count, quality_worst,
  "gaps": [{from, to}], "series": {field → {window_min, window_min_at,
  window_max, window_max_at, "points": [{t, v} | {t, v, min, max, n} at
  hourly resolution]}}, "lifecycle_changes"/"health_state_changes":
  [{t, v}], "commanded_changes": [{t, source, direction, watts}]}},
  "fleet": {"series": {grid_power_w, load_power_w, battery_watts}, "gaps"}}`.
  Gaps are server-computed (full: row spacing > 3 × `sample_interval_s`;
  hourly: any missing hour between first and last) and never interpolated.
  Fleet sums are computed over RAW rows BEFORE downsampling, and a fleet
  point exists only where EVERY unit in the requested set has a row (one
  unreadable phase is never zero). Null-valued points are never emitted —
  an absent datum is not zero. MCP gains the read-only
  `get_plant_history(from, to, unit_ids?, fields?, points?)` tool.
- **Standing pins:** history is never safety-authoritative — no kernel,
  gate, adviser, or toggle reads it; nothing restores from it at boot; it
  writes no audit facts and publishes no bus events (samples are
  projections, not acts); the energy scorecard stays the kWh authority
  (cross-linked in the console, never duplicated); and the audit trail owns
  decisions — a history row's commanded triple is the sampled 30 s
  projection, and where the two disagree the audit row is the record.

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
  writes `0x0101`; `0x8000` is written only as the {0, 1} parking exception (the named
  `write_debug_mode` path under the `parking:` block, DESIGN_POD_PARKING §5 — live-proven
  2026-08-24, `docs/evidence/standby-cycle-2026-08-24.md`; values 2–6 structurally refused);
  outside that exception the mode words are read-only evidence.
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

## Self-healing awareness layer (recovery detection)

`energypod.application.recovery` (2026-08-24, accepted from
docs/POD_RECOVERY_RESEARCH.md ladder rung R4 plus the promoted P1 items vi and iii). The
batteries self-heal from command-state, communication, and estimation problems by design;
this layer is the operator's honest visibility into that self-healing and its failure. It
is PASSIVE by construction: audit facts and bus events only — no write path, no latch, no
block, no refusal originates here (the actor's `external_writer` latch remains the only
latching mechanism and is only ever READ). No new register writes exist: `0x8000`, standby
cycling, and anything touching the write scope are excluded and need separate operator
authorization per the research.

- **Actuation-coherence watchdog (P1 vi)**: per unit, per fleet cycle, supervision peeks
  the authority the heartbeat is about to consume and compares the polled measured battery
  power against the PRE-COMMAND baseline of the authorization episode. A cycle is
  confidently ACTUATING when movement ≥ max(50% of the authorized figure,
  `actuation_coherence_min_movement_w`), confidently STILL when movement is below
  min(same two), and INCONCLUSIVE in the dead zone between (which keeps tiny setpoints out
  of court while a fully silent pod still alarms). `actuation_coherence_cycles` consecutive
  still cycles raise exactly ONE `actuation_incoherent` audit fact plus one
  `actuation.incoherent` bus event per episode — throttled until a coherent cycle or the
  authorization ending re-arms it. Known limit, pinned: a mid-flight loss under an
  UNCHANGED command (the pod pinned at a level it already delivered) is indistinguishable
  from delivery by movement and is not this watchdog's case.
- **Objective echo read-back (P1 iii, vendor precedent `DebugModeRead`-after-write)**: on
  the coherence trigger ONLY (never per heartbeat), one bounded fresh read of the served
  objective (IoT `0x1060+17/+18`, the arm-preflight readback window) through the owning
  actor's serialized mailbox, classified against the last objective that actor applied:
  `echo_matches_write` (transport fine — the incoherence is pod-side, the wedge
  signature), `objective_not_served` (readback zero while authorized — mode/autonomy
  conflict), `external_writer` (a foreign nonzero objective, riding the existing
  vocabulary), or `echo_unreadable` (the discriminator could not run; never guessed).
  Audited as `objective_echo` with the read value, and carried on the health reasons, the
  detection event payload, and the remediation hint.
- **Unresponsiveness classifier (R4)**: per unit, a DERIVED `health_state` recomputed
  from the latest cycle facts (no latching beyond existing mechanisms; boot is
  `healthy`-by-observation). Vocabulary, in classification precedence order:
  `unreachable` (a TCP connect failure — the gateway class; the pod behind it may be
  fine), `not_responding` (K=3 consecutive read failures while the path connects — the
  firmware-wedge signature), `foreign_writer` (the existing latched
  `external_writer` inhibit), `inhibited` (any other inhibit/latched state),
  `actuation_incoherent` (the watchdog verdict, reasons carrying the echo
  classification), `self_healing` (quiet and informational:
  `requalifying_after_inhibit`, `cell_balancing` above the 50 mV operator early-warning
  line, `autonomous_self_charge` — uncommanded, in the commissioned band), `healthy`.
  Transitions publish exactly one `unit.health_changed` bus event
  (`{unit_id, from, to, reasons}`).
- **Unexpected-autonomy evidence recorder**: measured battery power outside
  `expected_autonomy_band_w` while NO intent claims the unit appends one
  `unexpected_autonomy` audit fact per unit per 60 s (figures pinned in the request
  fingerprint: measured watts, SOC, mode words, the band) and one quiet-tier
  `unit.unexpected_autonomy` bus payload carrying the same figures
  (`{unit_id, measured_watts, soc_pct, debug_mode_w, ctrl_mode_w, work_mode_w,
  run_mode_w}`). Pure evidence: no block, no alarm tier, health stays
  healthy/self_healing. A claim-state read failure treats every unit as claimed
  (fail-safe for evidence).
- **Honest terminal guidance (R5 rail)**: `remediation_hint` appears on the health view
  only where remote recovery is genuinely exhausted — `not_responding`, and
  `actuation_incoherent` once the echo classifies `echo_matches_write` ("pod not
  responding — remote recovery exhausted; physical restart required", with the vendor
  MiniES-app post-restart checklist and the research reference). `objective_not_served`
  carries the vendor-app mode checklist instead (check Debug Mode = Normal Mode and
  SysControlMode = Remote, then re-dispatch); `external_writer` and unknown echoes carry
  no terminal hint.
- **Surfacing**: every snapshot unit carries `health_state: str | null`,
  `health_reasons: [str] | null`, `remediation_hint: str | null` (nulls when no monitor
  is wired or its projection fails — detection never gates a read, and no state is ever
  fabricated). `health()` carries a per-unit `units` block
  (`{unit_id, health_state, reasons, remediation_hint}`) and its `control_readiness`
  gains `"<unit>:actuation_incoherent"` — a unit authorizing without actuating is not
  ready to act. The bus vocabulary additions are `unit.health_changed`,
  `actuation.incoherent`, and `unit.unexpected_autonomy`.
- **Config keys** (`policy` block; all defaulted, so unchanged configurations keep
  today's behavior — detection only, no control path consumes them):
  `actuation_coherence_cycles: 4`, `actuation_coherence_min_movement_w: 150`,
  `expected_autonomy_band_w: [-2600, 300]` (a strictly ascending integer pair whose
  bounds span the pods' negative self-charge to small positive float region). The K=3
  read-failure streak and the 60 s evidence throttle are module constants
  (`energypod.application.recovery`).
- **Supervision driving**: the fleet cycle peeks the authority before the heartbeats,
  classifies every bounded poll outcome (`TransportConnectionError` → gateway class;
  any other escaped failure → read-failure streak; any success clears both), and runs one
  bounded, fully suppressed detection pass per unit after the polls and before the kernel
  tick — detection can never delay renewal or control, and every audit/bus failure inside
  the monitor is itself suppressed.

## Night-writer detector (foreign-objective observation)

`energypod.application.foreign_objective` (2026-08-23 census queue item, PRODUCT_NEXT §2 S2).
Zero-extra-frames periodic sampling of the served PQ objective while the controller commands
nothing, honestly classifying and recording what — if anything — commands the batteries
overnight. It closes the census's 7.3 h overnight audit blind spot with timestamped evidence
and gives the pending night-partition operator decision (DESIGN_SCHEDULES §8) its
night-window characterization. PASSIVE by construction, exactly like the awareness layer:
audit facts, bus events, and in-memory evidence records only — no write path, no latch, no
block, no refusal originates here.

**Motivation note (correcting the record).** The 2026-08-23 census closed "day shift is
clean" and queued this detector against a 7.3 h unverified overnight window; the later
night-load investigation's "no external writer" conclusion was drawn from EVENING sampling
and arm-moment latches only — a pre-midnight window — and is superseded: the site's existing
Docker solution charges ALL THREE batteries at 2,500 W per unit from 00:00 to 06:00 nightly.
That writer is KNOWN and EXPECTED. This detector is the component that actually observes the
night window, and its classification treats that writer as an expected pattern (quiet-tier
evidence with a clear characterization), not an intruder.

**Honest limits of the signature (stated up front, B6/ADD-1 doctrine).** The served-objective
words alone CANNOT distinguish an external charge command from the pod's own self-charge:
the night writers historically CHARGE (negative objectives), and negative objectives sit
INSIDE the commissioned autonomy band — the pods' own firmware holds ~-520..-700 W daytime
CT-following self-charge and up to ~-2.27 kW deep self-charge on the same register, and the
nightly −2500 W scheduled charge sits barely inside the band's −2600 edge. So the detector
records EVERY nonzero sample as timestamped evidence (unit, active/reactive words,
mode words, our lifecycle/claim state) and drives the ALERT tier from pattern rules on top;
plain in-band float/self-charge never alerts. EVIDENCE first, alert second, and never a false
alarm on the pods' normal autonomy (the operator's consistent direction).

- **Sampling (zero extra frames).** The served objective (PCS detail block `0x1060+17/+18`,
  PROTOCOL_EVIDENCE 4b — the same window the arm preflight reads; already in the shipped read
  plan) decodes into every observation as three advisory fields following the device-mode-word
  doctrine (never quality-map keys — the 10/12/18-key legal map set is unchanged):
  `served_active_objective_w` and `served_reactive_objective_var` (signed int16, unscaled
  watts/var; null until the block has been served once) and `objective_captured_at_mono`
  (the capture time of the serving the words came from — the live tiered plan serves the block
  on the cold ring, so between rotations the words ride from cache with their ORIGINAL capture
  time). The detector issues no reads of its own: a sample's cadence is bounded below by
  `foreign_objective_sample_interval_s` and above by the read plan's serving period of the
  detail block (~96–108 s live at the 1.5 s control period; every telemetry cycle in
  simulate). Every read still runs through the owning actor's serialized mailbox — the
  awareness layer's bounded-read pattern, on the poll path.
- **Sample eligibility (all must hold).** The observation's objective words are FRESH (their
  capture time advanced since this unit's last recorded sample); the interval floor elapsed;
  the unit's lifecycle is one of `observe_only`, `disarmed`, `armed_idle`, `inhibited` (never
  `boot`, `active`, `stopping`, `disconnected` — an inhibited unit is by definition
  uncommanded and its evidence matters most); NO live intent claims the unit; and the
  handback grace elapsed since the unit was last claimed, authorized, or `active`.
- **A failed sample is a gap, never an alarm.** A failed poll means no fresh observation and
  therefore no sample; a decode without the words is not a sample; every failure inside the
  detector (audit append, bus publish, repository read) is fully suppressed and leaves the
  session record and any open episode untouched.
- **Classification, per recorded sample, first match wins.** With `P` the active word, `Q`
  the reactive word, and the band the commissioned `expected_autonomy_band_w`:
  1. `P == 0 and Q == 0`: NOTHING is recorded ("zero = nothing"); any open foreign episode
     closes silently; the pattern streaks reset.
  2. **Within handback grace** (our own claim/authority ended less than
     `foreign_objective_handback_grace_s` ago — the observed watchdog hand-back is 4–8 s, the
     residue of our own lapsed objective): recorded quiet with classification
     `handback_grace`; the streaks reset; an open episode closes. Our own intent serving then
     lapsing is thereby self-observed and never foreign.
  3. `Q != 0`: `foreign_objective_observed`, reason `reactive_objective_observed` — the pods'
     own signature carries `Q == 0` (ADD-1), so any reactive component is not pod autonomy.
  4. `P` outside the commissioned band: `foreign_objective_observed`, reason
     `outside_autonomy_band` — the same envelope the unexpected-autonomy recorder and the
     arm-time preflight consume, on the FIRST qualifying sample.
  5. **The expected nightly charge (the site's own scheduled writer), recognized FIRST.** A
     charge sample classifies quiet as `expected_nightly_charge` when ALL hold:
     `foreign_objective_expected_charge_w` is commissioned; `P` is in-band AND within the
     expected magnitude class (`0.8 ×` .. `1.2 ×` the commissioned figure — the scheduled
     −2500 W charge, whatever small regulation drift the served words carry); and the
     pattern is SYNCHRONIZED across at least `foreign_objective_expected_min_units` units
     (default 2, counting this one): that many units' most recent recorded samples lie
     within the sync horizon (`max(5 × foreign_objective_sample_interval_s, 300 s)` of this
     sample's time) and are themselves charges inside the same expected class. The
     synchronized GROUP is the discriminator: the pods' own self-charge is per-pod and
     PV-correlated, while the site scheduler starts identical charges on multiple
     batteries at the same moment — and it charges the batteries it chooses, not
     necessarily every one (observed live at commissioning, 2026-08-24 00:00 AEST: lhs and
     mid held −2500 W together while a full rhs floated — a 2-of-3 nightly charge). A
     LONE pod's charge never qualifies. A sample recognized here never escalates under
     either pattern rule below, whatever its run-mode word reads (the scheduler's writes
     put the PCS into Remote PQ mode exactly like any writer's).
  6. In-band nonzero otherwise: quiet evidence `pod_autonomy_objective_observed`, UNLESS a
     pattern rule escalates (both require `foreign_objective_sustained_samples` CONSECUTIVE
     qualifying samples; any non-qualifying sample resets the streak):
     - `sustained_remote_mode_objective` — every one of the last N consecutive samples held a
       nonzero in-band objective while the advisory run-mode word read `1` ("Remote PQ Power",
       the vendor's written-objective state, GlobalFun.cs:178-188). The pod's own CT-following
       autonomy reads `0` ("Matching Load" — live-observed 2026-08-23: lhs held +695..914 W
       uncommanded after dark in exactly that mode).
     - `sustained_charge_without_pv_evidence` — every one of the last N consecutive samples
       held `P <= -foreign_objective_self_charge_class_w` (default 1000 W, above the observed
       ~-520..-700 W typical self-charge, below the -2.27 kW deep class) with NO PV evidence.
       PV evidence per sample is the SAME observation's advisory `grid_power_w > 0` (the site
       is exporting — surplus PV plausible); import or absent means no evidence. Site PV is
       never presented as measured (DESIGN_ENERGY_SCORECARD doctrine).
  7. **Deliberate refinement, recorded:** the blanket "any positive-discharge objective at an
     hour with no PV evidence" rule was considered and REFUSED — the 2026-08-23 lhs evidence
     (a steady uncommanded +695..914 W hold after dark in Matching Load mode; config rev 5
     widened the band's positive edge to +1000 for exactly that behavior) proves in-band
     positive objectives at no-PV hours are normal pod autonomy. In-band positives stay quiet
     evidence; the remote-mode rule is the discriminator that separates a foreign writer's
     discharge from the pod's own load-following, and out-of-band positives already alert
     under rule 4.
- **Alert semantics.** Exactly ONE `foreign_objective_observed` audit fact plus ONE
  `foreign_objective.observed` bus event per (episode, reason): opening a foreign episode, or
  a reason CHANGE while an episode is open (a materially different signature re-alerts). A
  sustained foreign objective never re-alerts on every sample — the session record carries
  the continuous evidence. A quiet, grace, or zero sample closes the episode silently (the
  closing is visible on the surfaces as `foreign_active: false`).
- **Session record.** Every recorded sample (`unit_id`, `observed_at` wall time, active and
  reactive words, classification and reason, our lifecycle and claim state, the four mode
  words, `grid_power_w`) accumulates into a per-unit rolling session: in-memory only,
  retained 7 days or 4096 samples per unit (whichever binds first), reset by a restart — the
  durable audit trail keeps the alerts, and gaps between samples are inferable from the
  cadence, never fabricated.
- **Read surface.** `GET /api/v1/objectives/observed?last=24h` (observe scope; `last` is
  `Nh`/`Nd`, 1 h..168 h inclusive, default `24h`; anything else is 422 `validation_error`)
  answers the window characterization:
  `{as_of, last, window_s, units: [{unit_id, first_seen_at|null, last_seen_at|null,
  sample_count, charge_sample_count, discharge_sample_count, min_active_w|nul,
  typical_active_w|null (the LOWER median of the in-window recorded samples),
  max_active_w|null, classification_counts ({pod_autonomy_objective_observed,
  expected_nightly_charge, handback_grace, foreign_objective_observed} -> in-window counts),
  foreign_episode_count, foreign_active, foreign_reason|null,
  last_objective_observed|nul}]}`. `sample_count` counts NONZERO recorded samples only
  (zeros are not samples); the sign counts split them by the active word's sign. The nightly
  writer's characterization — synchronized first-seen across units, the six-hour last-seen
  span, the magnitude class, and the `expected_nightly_charge` count — is exactly what this
  surface hands the night-partition decision. Pure read: no mutation exists on this surface.
- **Snapshot and health.** Every snapshot unit and every `health()` units entry carries
  `last_objective_observed`: null before any recorded sample, else the COMPACT
  `{observed_at, active_w, reactive_var, classification, reason}` (the endpoint's per-unit
  `last_objective_observed` is the full evidence record). The detector composes
  ALWAYS — no config block exists for it, observe-only included — so the key is never absent,
  only null. `health_state` and `control_readiness` gain NO vocabulary from this feature.
- **Discrimination note.** The arm-time sole-writer preflight and its `external_writer` LATCH
  are untouched: this detector observes and reports; the latch remains the enforcement point.
  Concretely for the nightly writer: arming AFTER the 06:00 clear is an ordinary sole-writer
  arm; arming MID-CHARGE (03:00, a −2500 W objective on the wire) is judged entirely by the
  standing ADD-1 doctrine — with the commissioned `autonomous_charge_signature_max_w: 2500`
  the readback classifies `pod_autonomy` and the arm PROCEEDS by beat-autonomy (our renewal
  replaces the scheduled writer's objective — coordinate, don't fight), while a discharge,
  reactive, or beyond-signature objective latches `external_writer` exactly as before. The
  detector's quiet `expected_nightly_charge` classification changes none of that: expected is
  not sanctioned, it is characterized.
- **Config keys** (`policy` block, all defaulted — an absent policy block uses the pinned
  defaults so observe-only deployments detect with the same eyes; detection only, no control
  path consumes them):
  `foreign_objective_sample_interval_s: 30.0` (1..3600, the MINIMUM spacing between recorded
  samples), `foreign_objective_sustained_samples: 3` (1..100),
  `foreign_objective_self_charge_class_w: 1000` (1..50000),
  `foreign_objective_handback_grace_s: 12.0` (1..300),
  `foreign_objective_expected_charge_w: 2500 on the live-write example, `None` by default —
  the strict posture until the operator commissions the site's own scheduled writer as
  expected — and `foreign_objective_expected_min_units: 2` (2..50): how many units must
  hold the synchronized charge for the expected-writer recognition (2 = a synchronized
  pair; a lone pod never qualifies).
- **Supervision driving.** One bounded, fully suppressed observation pass per fleet cycle,
  after the polls and the recovery pass and before the schedule runner: the pass reads each
  unit's fresh observation (poll-failed units contribute nothing), the live claim set, and
  the peeked authority; a failure anywhere inside it can never delay renewal or control.

## Off-peak night charge (the night strategy adviser — DESIGN_NIGHT_CHARGE, 2026-08-26)

The operator-confirmed replacement for the site's Docker solution (all three batteries at
2,500 W per unit, 00:00–06:00 nightly, dropping to a very low rate when demand is high). A
`NightChargeAdviser` (`energypod.application.night_charge`) submits ordinary short-TTL
`OPTIMIZER` CHARGE intents — a strategy layer in the excess-adviser pattern, NOT schedule
conditionals — computing per unit, inside the commissioned window and while enabled: reach the
SOC ceiling (`policy.max_soc_pct`, the BMS-authoritative figure) by window end at ≤
`rate_cap_w`, under the pinned pacing rule (`cap_first` — the default, Docker parity — or
`even`, deadline-paced from measured SOC each tick, self-correcting toward cap when behind).
Batteries at/above the ceiling (or with zero dynamic charge headroom) sit out as zero-watt
non-participants. Full design and rationale: `docs/DESIGN_NIGHT_CHARGE.md`.

- **Source pin.** `OPTIMIZER` (dynamic, evidence-reactive strategy — the arbiter's adviser
  class), submitted through the composition-internal facade twin `submit_night_intent` (the
  `submit_advisory_intent` pattern: source pinned, intent-id prefix `night-`, same
  audit/publication contract, never routed on REST or MCP) under the principal
  `energypod:night-adviser` (observe + dispatch, non-interactive, site-bound). The night
  adviser ALWAYS yields per unit to a live SCHEDULE claim (no opt-out flag; the published fact
  beats the opportunist — the schedules-stay-day recommendation, which this feature is the
  night half of).
- **The demand rule — LOAD words, never grid words (the pinned correctness catch).** While
  `demand_w` exceeds `demand_threshold_w` (default 1000 W), every participating unit STANDS
  DOWN (the operator's directive — the one behavior, no selector): zero-watt
  non-participation, the unit excluded from the submission (the facade refuses zero-watt
  per-unit targets), the TTL lapse plus watchdog handing the pod back to its own autonomy
  — resuming below `threshold − hysteresis` or at window end (fleet/unit phase
  `standing_by_on_demand`). Honest trade, stated as such: during a stand-down the pod's own
  matching autonomy may serve part of the load. The measurement is the per-pod CT
  `load_power_w` (`demand_scope: fleet` default = max(0, Σ); `per_phase` the option) — the
  grid word includes the adviser's OWN charging draw and would self-hold forever at cap
  rates. Evidence quality is the scorecard's family (per-field quality, worst-word-wins
  `demand_evidence` `good|missing|bad|stale`, freshness `demand_telemetry_max_age_s`); a
  non-good word FAILS CLOSED TO HOLD at `hold_rate_w` (default 100 W — the EVIDENCE-FAILURE
  fallback rate alone, never a demand behavior; config refuses zero) — the stand-down
  answers measured demand, never missing data — loudly visible. Resume pacing only below
  `demand_threshold_w − demand_exit_hysteresis_w` (default 200); the hold latch resets at
  window boundaries.
- **Composition (block-presence doctrine).** A PRESENT `night_charging:` block composes the
  adviser, the `night_charge_state` projection, the `night_charge.state_changed` events, the
  guarded toggle route, and the PCS live-block promotion (the promotion predicate widens to
  either-block — `0x1000` at control rate is load-bearing for the demand freshness bound; the
  budgeted plan is unchanged). An ABSENT block composes nothing — byte-identical snapshot,
  and the toggle answers 409 `night_charging_not_commissioned`. Keys: `timezone` (REQUIRED,
  IANA), `window_local` (default `[["00:00","06:00"]]`, cross-midnight allowed), `enabled`
  (default `false`; the excess activation doctrine verbatim — runtime state never persists,
  `enabled_origin: config|runtime`, the toggle flips participation only), `rate_cap_w` 2500
  (≤ `policy.max_unit_charge_w`), `demand_threshold_w` 1000, `demand_exit_hysteresis_w` 200
  (< threshold), `hold_rate_w` 100 (0 < hold < cap — the evidence-failure fallback rate
  alone), `demand_scope` fleet, `pacing`
  `cap_first` (`even` requires the `assumed_capacity_wh` per-unit map, key set exactly the
  fleet), `demand_telemetry_max_age_s` 3.0 (> `control_period_s +
  essential_read_timeout_s`), `intent_ttl_s` 10.0 (> `control_period_s`, ≤ 300 s) — all
  validated on block-PRESENCE with `mode: write_enabled` + `policy` present.
- **The PARTITION grant (required; the two existing mechanisms, no new ones).** (1) The union
  of `schedule.allowed_windows_local` (a PRESENT schedule block) must cover
  `night_charging.window_local` ENTIRELY — a config-time cross-validation, so commissioning
  night charge and granting the partition are ONE config revision + restart; the refusal
  names the widening path. (2) The durable night acknowledgement
  `schedule_night_windows_acknowledged` (one site fact) gains a SECOND capture path: the
  first enable (boot-config or toggle) carries `"night_posture":
  "PARTITION_ACKNOWLEDGED"` unless the fact already exists (either surface's capture counts;
  durable-append-FIRST, keyed boot load, never re-prompted); otherwise 409
  `night_acknowledgement_required` (`details: {"acknowledgement": "PARTITION_ACKNOWLEDGED"}`)
  and an unacknowledged site composes SUSPENDED with reason `night_acknowledgement_required`.
  The arm-time sole-writer preflight is unchanged and remains the structural enforcement.
- **Tick and precedence.** One bounded, suppressed tick per fleet cycle AFTER the excess
  adviser and BEFORE the energy accountant (published facts first, then opportunists in
  economics order — free surplus before paid import): the excess adviser is deliberately
  UNCHANGED; the night adviser excludes, at submission time, every unit claimed by a live
  EMERGENCY_STOP (any claim — withdraw entirely), MANUAL, AGENT, SCHEDULE, or not-own
  OPTIMIZER intent (the dawn-corner rule, one-sided by economics; exclusion keeps the
  equal-priority tie deterministic). Exactly ONE held intent (remove-then-submit renewal,
  `watts_by_unit` per participating unit, TTL `intent_ttl_s`); hand-back at window end is
  NON-RENEWAL (TTL + the ~3.5–4.0 s watchdog); no stop triples, no idle/zero-watt intents,
  ever. The runner cannot self-arm: participation requires ARMED_IDLE/ACTIVE per unit, and a
  disarmed fleet renders reason `units_disarmed` (the S3 lesson, designed in) — arm persists
  across windows and falls on every restart (the standing re-arm ritual, honestly surfaced).
- **Activation surface.** `POST /api/v1/night-charging` mirrors the excess toggle: `arm`
  scope (an enable additionally requires an INTERACTIVE principal), Idempotency-Key, body
  `{"action": "enable"|"disable", "confirmation": "NIGHT", "night_posture"?}`, audited
  `night_charging_toggled` on the Impl-10 pattern; 200 `{"feature", "enabled",
  "enabled_origin", "persisted": false, "acknowledged_partition", "night_charge_state"}`.
  Refusals: 422 `validation_error`; 409 `night_charging_not_commissioned`; 409
  `night_acknowledgement_required`; 409 `night_enable_refused` (details: reasons +
  unit_ids/stop_ids — manual/agent/schedule claims and latched stops refuse, latched INHIBITS
  do not; `disable` is never refused).
- **Projection and events.** Feature-detected top-level `night_charge_state` (absent when
  the block is absent; single writer = the fleet loop's post-tick update; `active` derives
  from `held_intent_id`): `{enabled, enabled_origin, acknowledged_partition, posture, active,
  phase, window{start_local,end_local,timezone}, window_ends_at, window_ends_in_s,
  next_window_at, pacing, rate_cap_w, hold_rate_w, demand_scope, demand_threshold_w,
  demand_w, demand_evidence, held_intent_id, units[{unit_id, soc_pct, phase, target_w,
  reason}], last_action, last_tick_at, reason_codes}`. Fleet `phase`: `idle | pacing |
  holding_on_demand | standing_by_on_demand | complete | skipped_full` (`holding_on_demand`
  is the fail-closed evidence hold alone); per-unit adds `sitting_out`. Reason
  vocabulary (ONE): `outside_window, window_open, on_plan, deadline_at_risk,
  demand_above_threshold, demand_below_exit, demand_evidence_missing, demand_evidence_bad,
  demand_evidence_stale, at_ceiling, no_charge_headroom, target_reached,
  no_eligible_units, units_disarmed, unit_parked, yielding_to_higher_priority,
  disabled_by_config, disabled_by_runtime, night_acknowledgement_required`
  (`unit_parked` — 2026-08-24, DESIGN_POD_PARKING §3 — joins additively and outranks
  `units_disarmed` for a parked unit: resume, not arm, is the true next step). Bus
  `night_charge.state_changed`: published on the semantic tuple `(enabled, enabled_origin,
  acknowledged_partition, active, phase, active_unit_ids, demand_evidence, reason_codes)`
  — watts/SOC ride but never trigger — with the 30 s heartbeat while enabled and NOTHING
  while disabled (the excess `excess_adviser.state_changed` mechanics verbatim).
- **Honesty pins.** Charge-only window — the strategy never discharges, and the no-cycling
  guarantee is bounded by the TTL/watchdog hand-back gap, stated as such. The off-peak
  import cost is the operator's tariff question (`energy_scorecard.tariff`); the scorecard
  measures the kWh from day one. Hold rates sit inside the coherence watchdog's dead zone —
  no false alarms and no protection there; the scorecard is the hold's verifier. The
  cutover sequence (grant → arm → stand Docker down → enable → one supervised night →
  decommission, verified by the night-writer detector's quiet window) and the verbatim
  operator decisions: DESIGN_NIGHT_CHARGE §3.4 and §8.

## Pod parking (the sanctioned standby mode — DESIGN_POD_PARKING, 2026-08-24)

Parking a pod writes the vendor debug-mode register `0x8000 ← 1` (Standby)
through the controller's named `write_debug_mode` transport path; resuming
writes `0x8000 ← 0` (Normal). Live-proven on rhs 2026-08-24
(`docs/evidence/standby-cycle-2026-08-24.md`): Standby parks the PCS control
path (measured power → 0) while comms, telemetry, pack voltage, and SOC
reporting stay alive, and the exit write returns the pod to Normal in ~1 s with
no wedge. **Parking is not electrical isolation** — the battery stays connected
at full voltage (166 V observed while parked); never perform physical work on a
parked pod, and the lease countdown is policy, never safety. Values 2–6
(Charge, Discharge, Circulation, Fixing SOC, Verify Capacity) are permanently
unexposed: unreachable in code (transport-layer {0, 1} validation), refused on
approach, never normalized by any surface. Full doctrine, lease durability,
restart semantics, simulator contract, and test matrix:
`docs/DESIGN_POD_PARKING.md`; this section pins the wire-facing shapes.

All three routes require the `arm` scope AND an interactive human principal
plus an Idempotency-Key (the shared `mutation()` wrapper); refusals are typed
409 envelopes with pinned details shapes.

### POST /api/v1/units/{unit_id}/park

Request: `{"confirmation": "PARK", "reason": "<1..500, required>", "lease_s":
<60..max_lease_s, optional, default min(default_lease_s, max_lease_s)>}`.

200: `{unit_id, action: "park", prior_word, written_value: 1, readback_word,
verified: true, as_of, lease: {parked_at, expires_at, max_total_s, reason,
authorizer, epoch}, prior_state: {lifecycle, measured_watts}}`. The response is
synchronous, not an action object — write→readback is one serialized actor
operation bounded well under a second; a replayed Idempotency-Key answer
describes the ORIGINAL operation, never current state, and a retried park
after restart or entry eviction resolves as `park_already_parked`.

Refusals, details shapes pinned:

- 409 `park_not_commissioned` — no `parking:` block, or mode not
  write_enabled: `{"cause": "block_absent" | "mode_not_write_enabled"}`.
- 409 `park_conflict_refused` — armed / under intent / latched stop:
  `{"units": [{"unit_id", "cause"}]}` (the arm outcomes shape; singular unit on
  a per-unit route).
- 409 `park_already_parked` — `{"lease": {...}}`; renew instead.
- 409 `park_mode_out_of_scope` — `prior_word ∈ {2..6}`:
  `{"prior_word", "vendor_name"}` (GlobalFun.cs:152-165). The controller never
  transitions a vendor-directed mode it did not set.
- 409 `park_write_failed` — transport refused/timeout: `{"error_class"}`. One
  bounded retry, then refuse.
- 409 `park_readback_unverified` — ACKed but 0x8100 ≠ 1 after ONE retry:
  `{"prior_word", "written_value", "readback_word", "retries"}`. No lease; the
  `write_unverified` posture applies if the word moved.

### POST /api/v1/units/{unit_id}/park/renew

Request: `{"confirmation": "RENEW", "lease_s": <60..cap, required>}` — sliding:
`new expires_at = now + lease_s`, never past `parked_at + max_lease_s`
(anti-rollover; after expiry a NEW park requires fresh confirmation — an
ordinary `PARK`, keyed to a new lease). 200 mirrors park's lease object plus
`as_of`. Refusals: 409 `park_lease_cap_reached` (`{"parked_at", "max_total_s",
"requested_expires_at"}`) and 409 `park_lease_absent` (never parked / already
closed — details carry the closing row's origin and time).

### POST /api/v1/units/{unit_id}/resume

Request: `{"confirmation": "RESUME", "takeover": "FOREIGN"?}` — `takeover` is
required exactly when `prior_word == 1` with no controller lease (the arm
takeover pattern: per-request, audited `foreign_takeover_acknowledged`, never
persisted). Resuming our own expired lease needs no takeover — the operator's
fresh RESUME is the act.

200: `{unit_id, action: "resume", prior_word, written_value: 0, readback_word,
verified: true, as_of, origin: "operator" | "foreign" | "none", checklist:
{comms_age_s, soc_drift_pct, soc_pct_at_park, measured_watts_now,
faults_while_parked, faults_retention_note, latched_stops: [ids],
latched_inhibit: bool}}`. Resume on a Normal word is a no-op 200, `origin:
"none"` — idempotent honesty, no error theater.

Refusals: the commissioning/write/readback twins above; 409
`park_foreign_word_acknowledgement_required` (`{"acknowledgement": "FOREIGN",
"prior_word", "observed_since"}`); 409 `park_mode_out_of_scope` for
`prior_word ∈ {2..6}` — normalizing a vendor-directed mode is its own
acknowledged act, never a resume alias; 409 `resume_stop_latched` when a
latched emergency stop names the unit (resume would re-enable autonomy under a
standing stop instruction — acknowledge the stop first).

### Audit doctrine pins that shape the responses

- **Durable-first, no inverse** (the `acknowledge_inhibit` pattern): the audit
  row is appended BEFORE the register write with `result: pending`, then
  completed (`parked` / `refused`) by the verified write→readback. A failed
  append refuses the park (fail-closed, retryable); a failed write leaves
  `refused`, mints no lease, and — if the word actually moved — takes the
  `write_unverified` posture. A resume is never rolled back by bookkeeping;
  `degraded: [audit_unavailable]` rides the response exactly like disarm's.
- **The lease is the expiry ALARM, not the expiry ACTOR**: at TTL expiry the
  controller performs no write — it appends `unit_park_expired`, publishes the
  alert-tier event, moves the projection to `{parked: true, expired: true}`, and
  holds `health_state: PARKED` with the hint "lease expired — Resume is an
  operator act". Boot reconstructs and alarms the same way; boot never parks,
  never un-parks.
- **Divergence is alarmed, never fought**: `word=1, no lease` → `origin:
  foreign`; `word=0, lease open` → observed foreign resume — the lease closes
  as `unit_resumed` / `result: observed_foreign` (`written_value: null`, no
  write), one alert-tier event carries `origin: foreign` + the observed
  transition time, and the following dispatch-provenance window renders
  `resume_provenance: {observed_at, origin: foreign}`. A foreign park over our
  unchanged lease renders `park_state.foreign_rewrite: true` — named, no write.
  The controller never re-parks in response.
- **Single-flight per unit**: every mutation runs guards AND state transition
  inside one critical section carrying `expected_lease_epoch`; a mutation on a
  closed epoch refuses (`park_lease_absent` family) instead of acting on a
  stale view. The write→readback→verify sequence is ONE actor-mailbox message —
  a heartbeat PQ write can never interleave between our write and readback.
  Arm-while-parked is legal (dispatch remains the gate); park re-checks armed
  inside the critical section.

### State, projections, and vocabulary

- `park_state` per unit (snapshot + unit detail): `{parked: bool, origin:
  "operator" | "foreign" | "unrecorded" | "none", parked_at, lease_expires_at,
  max_total_s, remaining_cap_s, expired: bool, reason, authorizer,
  foreign_rewrite: bool?, write_unverified: bool?, foreign_mode: {word, name,
  first_observed_at}?}` — absent key when uncommissioned. `unrecorded` covers
  word=1 with no lease AND no foreign evidence (crash-after-write residue);
  `foreign` is reserved for the vendor-app/foreign-flip class. A lease ends
  only by verified resume write, observed foreign resume, or expiry
  (alarm-only); under any other ending it persists in the terminal sub-state
  `write_unverified` (`parked: true, write_unverified: true`, health reason
  `park_write_unverified`) — a failed write never reclassifies a
  controller-minted lease as foreign.
- `health_state` gains `PARKED`, ladder position between `actuation_incoherent`
  and `self_healing`, implemented as a composable state — the healing-reason
  list is computed first (pure), then `("parked", *healing)`, so a
  parked+balancing pod keeps its balancing visibility. `unreachable`,
  `not_responding`, `foreign_writer`, `inhibited`, `actuation_incoherent`
  legitimately outrank a park (fault beats operator state — pinned so nobody
  "fixes" it). `unit.health_changed` carries the transitions both directions.
- Dispatch: the existing `device_debug_mode_active` refusal fires as today;
  its details gain `parked_provenance: {parked_at, authorizer, reason,
  lease_expires_at}` when the ledger names the unit, and `resume_provenance:
  {observed_at, origin: foreign}` for the window after an observed foreign
  resume. A word ∈ {2..6} renders `foreign_mode` in the details ("device in an
  unexposed vendor mode"), never `parked: true`. Control-readiness reasons gain
  `unit:parked`.
- **Adviser vocabulary (`unit_parked`, additive — one vocabulary per feature,
  DESIGN_POD_PARKING §3):**
  - Schedule projection (`schedule_state.reason_codes`): the `units_disarmed`
    mechanism verbatim — the ScheduleRunner does NOT exclude parked units (it
    stays dumb per DESIGN_SCHEDULES §7 "no claim checks"): it submits the
    published fact, the facade refuses `device_debug_mode_active` with park
    provenance, and the projection carries `unit_parked` as a projection-level
    code.
  - Night-charge projection (`night_charge_state.reason_codes`): joins
    additively and outranks `units_disarmed` for a parked unit — resume, not
    arm, is the true next step.
  - Excess adviser (`adviser_state.reason_codes`): renders in place of
    `no_eligible_target` when every otherwise-eligible unit's exclusion cause
    is park.
  - Foreign PQ objectives observed on a parked unit (the night-writer case):
    the detector annotates those samples `parked: true` — never silent, never an
    alert by itself; per the pinned conservative simulator model an ignored
    write leaves the served-objective words unchanged, so a foreign write
    during our park shows at readback and the next arm classifies it by the
    standing rules (possibly `external_writer` + takeover).
- Audit rows: `unit_parked`, `unit_park_renewed`, `unit_resumed`,
  `unit_park_expired` — per unit; `result` ∈ {pending → parked, renewed,
  resumed, observed_foreign, expired, refused}; reason codes carry
  `readback_verified` / `readback_mismatch` / `foreign_takeover_acknowledged` /
  `adopted_foreign_park` (a park over a foreign word=1 records the origin
  transition — the ledger never claims we initiated a park we inherited);
  payload `{prior_word, written_value, readback_word, verified, origin,
  authorizer, reason, lease fields, epoch, checklist on resume}`. The durable
  `park_leases` row is written in the same transaction boundary as the audit
  append — the table is the machine truth, the audit row the narrative.
- Bus events: `unit.parked`, `unit.park_renewed`, `unit.resumed`,
  `unit.park_expired` (alert tier on expiry), typed payloads mirroring the rows.
- MCP observes and recommends; it does not park: v1 ships no MCP mutation tool;
  `get_unit_detail`/`get_snapshot` gain the park projection, and the agent-loop
  contract text gains "never dispatch onto a parked unit; recommend the
  park/resume cycle to the human operator for the wedge signature."

### Commissioning (the `parking:` block)

Block-PRESENCE doctrine: `max_lease_s: 14400` (360..86400; `lease_s` ∈ [60,
max_lease_s]) and `default_lease_s: 14400` (>= 60, <= max_lease_s), validated
at config load — a PRESENT block is refused unless `mode: write_enabled` AND a
`policy` block is present (a present block on an observe-only site is a
validation error, never silently incapable). `policy.debug_modes_enabled`
remains the always-false tombstone it is today, superseded by nothing: the
`parking:` block is the one and only policy flag that can ever compose this
write.

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

Configuration gates (activation package, 2026-08-25): the cross-validations above bind whenever
the `excess_charging` block is PRESENT, and a present block is refused unless `mode:
write_enabled` AND a `policy` block is present (an observe-only composition structurally never
actuates; an adviser there is dead code refused at validation time). Composition semantics: a
PRESENT block (gates validated) composes the machinery — the policy export triple, the PCS-block
read-tier promotion, the adviser object, and the snapshot's `adviser_state` projection — with
`enabled` gating PARTICIPATION only (`true` participates at boot, subject to the
economics-acknowledgement latch below; an explicit `false` composes suspended and is enableable at
runtime through the activation toggle). An ABSENT block composes nothing — no adviser, no export
triple, no tier promotion, no `adviser_state` — exactly as before, and the activation toggle
answers `excess_charging_not_commissioned`. The PCS promotion while suspended is the budgeted
cost (steady plan ≤ 8 windows + probe, inside the commissioned control period) that keeps the
per-phase grid/load figures live in the console whether or not the adviser participates.

### Adviser state projection (`adviser_state`) — activation package, 2026-08-25

The snapshot (REST and the event stream's first frame) carries a top-level `adviser_state` beside
`intent` — the same feature-detected addition pattern. The key is ABSENT when the
`excess_charging` block is absent (nothing composed) and PRESENT whenever the block is present
and its gates validated, including while suspended. `health` is unchanged.

```json
"adviser_state": {
  "enabled": true,
  "enabled_origin": "runtime",
  "acknowledged_economics": true,
  "active": true,
  "hysteresis_state": "holding",
  "target_unit_id": "mid",
  "commanded_charge_w": 1400,
  "eligible_export_charge_w": 1600,
  "fleet_export_w": 1800,
  "export_evidence": "good",
  "charge_cap_w": 2500,
  "held_intent_id": "opt-3f9c21",
  "last_action": "renew",
  "last_tick_at": "2026-08-25T11:04:31+10:00",
  "reason_codes": ["export_headroom_available"]
}
```

- `enabled` — participating this process; `enabled_origin` is `"config"` (the boot-composed value,
  untouched) or `"runtime"` (last changed by the toggle — the honest "until restart" marker).
- `active` — an adviser intent is live right now; derived from `held_intent_id` (never a lifecycle
  guess), equivalently `hysteresis_state == "holding"`.
- `hysteresis_state` — `"inactive"` (disabled by config, disabled at runtime, or suspended on the
  pending acknowledgement) / `"entering"` (enabled, evaluating entry, including the post-yield
  re-qualification wait) / `"holding"` (intervening) / `"exiting"` (the last tick withdrew;
  persists until the next tick). The projection is tick-granular; `last_tick_at` says when.
- `commanded_charge_w` — the last tick's proposed watts (the achievable min); 0 when not
  commanding. `eligible_export_charge_w` — the deterministic bound from the last tick.
- `fleet_export_w` — Σ `grid_power_w` over every fleet unit (positive = export); NULL when any
  unit's grid evidence is missing/bad/stale — one unreadable phase is never treated as zero
  export. `export_evidence` is the fleet rollup (`good|missing|bad|stale`) under the bound's own
  fail-closed rules.
- `charge_cap_w` — the composed `max_charge_from_export_w`.
- `last_action` — the domain action vocabulary verbatim (`idle|propose|renew|withdraw`).
- `reason_codes` — ONE pinned vocabulary (the implemented decision codes kept verbatim, plus the
  projection-level states): `disabled_by_config`, `disabled_by_runtime`,
  `economics_acknowledgement_required`, `export_evidence_missing`, `export_evidence_bad`,
  `export_evidence_stale`, `no_export_headroom`, `no_acceleration_over_autonomy`,
  `below_exit_hysteresis`, `no_eligible_target`, `unit_parked`, `yielding_to_higher_priority`,
  `export_headroom_available` (`unit_parked` — 2026-08-24, DESIGN_POD_PARKING §3 — renders in
  place of `no_eligible_target` when every otherwise-eligible unit's exclusion cause is park).
- The projection has ONE writer (the fleet loop's post-tick update; the toggle flips only the
  participation flag and the next tick observes it), so it can never claim inactive while an
  adviser intent is still live.

### Activation events — `excess_adviser.state_changed`

The bus publishes the same projection vocabulary when the semantic state tuple changes —
`(enabled, enabled_origin, acknowledged_economics, active, hysteresis_state, target_unit_id,
export_evidence, reason_codes)`; the watt figures ride every publication but are NOT triggers
(while holding, commanded watts re-price with export every tick, and that cadence already reaches
consoles through the snapshot cadence and the decision/audit frames). While `enabled`, a full
payload republishes every 30 s with `"heartbeat": true`; while disabled there is no heartbeat.
Payload: every `adviser_state` field except `charge_cap_w`/`last_action`/`last_tick_at`, plus
`"heartbeat": bool`. The toggle itself publishes no dedicated event — the resulting state change
does, and the REST 200 carries the projection for optimistic adoption.

### Activation surface — the guarded runtime toggle

`POST /api/v1/excess-charging` mirrors the inhibit-acknowledgement guarded-confirmation pattern
applied to a feature gate: Bearer auth with the `arm` scope (an `enable` additionally requires an
INTERACTIVE principal — the arm/disarm asymmetry, enabling being control-adjacent and disabling
safety-positive), an `Idempotency-Key` header, a typed confirmation, audit, and publication.
Body: `{"action": "enable"|"disable", "confirmation": "EXCESS", "economics": "NET_BILLED"?}` —
`economics` is optional and consulted only on the first enable ever (below). Response 200:
`{"feature", "enabled", "enabled_origin", "persisted": false, "acknowledged_economics",
"adviser_state"}` — `persisted` is always false and spelled anyway. Every call is audited as
`excess_charging_toggled` (result `enabled`/`disabled`/`noop`) on the atomic commit-then-audit
pattern; refusal envelopes:

- 422 `validation_error` — unknown action, wrong/missing confirmation, `economics` present but
  not exactly `"NET_BILLED"`.
- 409 `excess_charging_not_commissioned` — no `excess_charging` block; there is nothing to toggle.
- 409 `economics_acknowledgement_required` — `enable` with no captured acknowledgement on this
  site and no `economics` field.
- 409 `excess_enable_refused` — `enable` while any unit is ACTIVE under another intent
  (`unit_active_under_intent`) or a latched stop holds (`latched_stop_holds`); `details` names the
  units and stop ids. Latched inhibits do NOT refuse (the selector skips those units honestly).
  `disable` is never refused.

Pinned policies: (1) runtime state does NOT persist — boot composes from the config file, the
config stays the source of truth, a restart while enabled-in-config re-enables, and a runtime
disable is operational only until reboot (`enabled_origin: "runtime"` marks it); (2) the
net-billing confirmation is captured ONCE as the explicit audit fact
`excess_charging_economics_acknowledged` (durable, never re-prompted, loaded at boot) and is
required before the FIRST enable ever succeeds on a site — a first enable either carries
`"economics": "NET_BILLED"` or is refused; a site without the captured fact composes SUSPENDED
with reason `economics_acknowledgement_required` (even a config `enabled: true` cannot
participate without it; the append is durable-first, and an audit failure refuses the enable);
(3) the toggle flips PARTICIPATION only — it can never change `max_charge_from_export_w`, relax a
commissioning gate, promote a tier, or change the mode — and the adviser still yields to every
higher-priority source, unchanged.
