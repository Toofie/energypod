# EnergyPod Manager Test Strategy

## 1. Purpose

This document is the quality contract for a clean-room EnergyPod manager. It defines how requirements are converted into executable evidence before implementation, how safety-critical behavior is reviewed, and which gates must pass before software may progress from simulation to shadow operation and, eventually, controlled actuation.

The strategy covers:

- protocol encoding, decoding, framing, and evidence;
- one-actor-per-unit concurrency and command-lease behavior;
- the continuously evaluated safety kernel;
- intent validation, priority, arbitration, and expiry;
- timezone-aware and cross-midnight scheduling;
- SQLite persistence and auditability;
- REST and MCP contracts;
- an independent device and network simulator;
- Docker startup, supervision, shutdown, and recovery;
- the React user interface;
- system-level degraded operation and failure containment.

Tests reduce risk; they do not prove absolute correctness. Safety claims must be supported by independent protocol evidence, executable tests, review records, and controlled operational validation. No simulator-only result is sufficient evidence for a physical-write claim.

## 2. Non-negotiable principles

1. **Safety fails closed.** Missing, stale, non-finite, contradictory, implausible, or untrusted safety data prohibits nonzero actuation.
2. **Every physical command is leased.** A nonzero write is permitted only under a current authorization produced from a current intent and a fresh observation. Every renewal re-evaluates safety.
3. **One writer owns each unit.** REST, MCP, schedules, optimizers, and the UI submit intents. They never access Modbus transports or drivers.
4. **Time is explicit.** Control expiry uses a monotonic clock. Human schedules use timezone-aware civil time. Tests inject both clocks.
5. **Old work cannot become current.** Generation or fencing semantics must prevent a cancelled or replaced command from writing after its successor or after stop.
6. **Boot is non-actuating.** Startup and restart enter observe-only or disarmed state. Manual commands are never silently restored.
7. **Evidence sources are independent.** Production codecs, the simulator, and golden-vector generation must not share implementation logic.
8. **A test must fail for the intended reason.** Test suites must collect successfully throughout development; expected failures are assertions, not import or collection errors.
9. **No unreviewed weakening.** Changing a test to make an implementation pass requires the same review and traceability as changing a requirement.
10. **Determinism by default.** Unit and component tests use virtual clocks, deterministic schedulers, seeded generators, and bounded deadlines. Uncontrolled sleeps and wall-clock assertions are prohibited.
11. **Coverage is necessary but not sufficient.** Branch, condition, state-transition, mutation, fault-injection, and independent evidence coverage are all required.
12. **Production and test boundaries remain visible.** Simulator conveniences, debug modes, fixture backdoors, and test credentials must not be present in production composition.

## 3. Criticality model

Every requirement, test, and module is assigned one criticality level.

| Level | Meaning | Examples | Minimum consequence of failure |
|---|---|---|---|
| `S0` | Physical safety or uncontrolled-energy risk | authorization, limits, stale telemetry, stop, command lease, sign and magnitude encoding | Block merge and release; revoke actuation qualification |
| `S1` | Control integrity, security, or durable accountability | arbitration, identity, API authorization, audit ordering, schedule direction | Block merge and release |
| `S2` | Operational reliability | reconnect, degraded fleet, readiness, migrations, UI stale-state display | Block release; merge only with approved isolation |
| `S3` | Presentation or convenience | visual polish, noncritical sorting, help text | May be deferred with an owned issue |

Criticality is determined from impact, not from package name. A UI emergency-stop interaction is `S0`; a safety-kernel status label may be `S2`.

## 4. Requirement and evidence traceability

### 4.1 Identifiers

The following stable identifiers are mandatory:

| Artifact | Identifier format | Example |
|---|---|---|
| System requirement | `REQ-<AREA>-NNN` | `REQ-SAFE-014` |
| Safety invariant | `INV-<AREA>-NNN` | `INV-ACTOR-003` |
| Hazard | `HAZ-NNN` | `HAZ-007` |
| Protocol claim | `PC-<PROFILE>-NNN` | `PC-WAVE-012` |
| Evidence item | `EV-<TYPE>-NNN` | `EV-CAPTURE-021` |
| Golden vector | `GV-<PROFILE>-NNN` | `GV-WAVE-008` |
| Test case | `T-<LEVEL>-<AREA>-NNN` | `T-COMP-SAFE-032` |
| Architecture decision | `ADR-NNN` | `ADR-006` |
| Known uncertainty | `UNC-NNN` | `UNC-004` |

Identifiers are never reused after deletion. Superseded items remain searchable and point to their replacements.

### 4.2 Traceability records

Every executable test must carry machine-readable metadata containing:

- test identifier;
- requirement and invariant identifiers;
- hazard identifiers where applicable;
- criticality;
- evidence or protocol-claim identifiers for protocol tests;
- test level and taxonomy tags;
- owner and independent reviewer;
- deterministic seed when generated;
- simulator scenario identifier when used.

Every requirement must map to:

1. at least one positive test;
2. at least one negative or boundary test;
3. a failure-mode or fault-injection test for `S0` and `S1` requirements;
4. at least one independent evidence item for protocol claims;
5. all state transitions affected by the requirement;
6. an operational validation step if it makes a physical-device claim.

The generated traceability report is a release artifact. It must identify:

- requirements with no tests;
- tests with no current requirement;
- hazards without mitigations;
- protocol claims without evidence;
- `S0`/`S1` tests without independent review;
- skipped, quarantined, or expected-failure tests;
- surviving mutations affecting traced requirements.

Any uncovered `S0` or `S1` item fails the build. Orphan tests are reviewed rather than deleted automatically because they may reveal an undocumented requirement.

### 4.3 Protocol evidence grades

Protocol claims use one evidence grade:

| Grade | Meaning | Permitted use |
|---|---|---|
| `E0` | Assumption or inference | Documentation and experiment design only |
| `E1` | Seen in decompiled or historical source | Read-only implementation behind an explicit profile |
| `E2` | Corroborated by two independent sources or a captured vendor transaction | Simulator and shadow-mode validation |
| `E3` | Reproduced against the target hardware with decoded readback | Controlled actuation, subject to safety approval |
| `E4` | Repeated on every configured unit and retained as an acceptance trace | Production profile qualification |

No writable protocol behavior may be enabled from `E0` or `E1` evidence alone. Sign convention, units, scaling, register address, slave ID, framing profile, response validation, renewal interval, and safe fallback are separate claims and are graded separately.

## 5. Test levels

| Level | Scope | External dependencies | Typical duration |
|---|---|---|---|
| Static | Types, schemas, lint, dependency and architecture rules | None | Seconds |
| Unit | One pure component or value object | In-memory fakes only | Seconds |
| Property | Broad generated input space and invariants | Deterministic generators | Seconds to minutes |
| State-machine | Stateful model versus system under test | Virtual clock and model transport | Minutes |
| Component | One adapter or service through public ports | Independent simulator or temporary SQLite | Minutes |
| Contract | REST, MCP, persistence, event, and protocol boundaries | Real serializers and schemas | Minutes |
| Integration | Composed backend or UI/backend slice | Simulator, SQLite, built frontend | Minutes |
| System | Built container as deployed | Docker network, independent simulator | Minutes |
| Hardware-in-the-loop | Target gateway and EnergyPod | Controlled, supervised environment | Explicitly scheduled |
| Operational acceptance | Shadow mode and bounded low-power trials | Live three-unit installation | Manual approval only |

Fast suites run on every change. System, mutation, endurance, hardware, and operational suites run at explicitly defined gates. A slower suite does not replace a lower-level deterministic test.

## 6. Test taxonomy

### 6.1 Protocol and transport

Protocol tests establish byte-level compatibility without treating the simulator as an oracle.

Required coverage:

- RTU-over-TCP and any separately supported MBAP/TCP profile are distinct types and cannot be selected implicitly.
- Zero-based PDU addressing versus human register numbering is tested at every adapter boundary.
- Unit/slave identifier is serialized and validated as configured.
- FC03 reads and FC16 writes produce exact frames, including CRC for RTU framing.
- Signed 16-bit active and reactive power rejects overflow instead of wrapping.
- Word order is field-specific; no global endian assumption is allowed.
- Scaling, signedness, status enums, bitmasks, and field widths have minimum, maximum, zero, and malformed-input cases.
- Partial frames, concatenated frames, delayed bytes, wrong transaction/unit/function, wrong byte count, bad CRC, exception responses, and disconnects are rejected.
- Write acknowledgements must match the requested address and quantity.
- Readback mismatch and unexpected objective echoes are surfaced as control-integrity faults.
- Layout and identity detection cannot silently fall back to a writable profile.
- Cell-count-dependent layouts reject overlap, truncation, and impossible counts.
- Unknown enum values and reserved fault bits remain representable and observable.
- Retry policy respects the command deadline and cannot starve lease renewal.
- Transport reconnect never duplicates a non-idempotent write or bypasses re-authorization.

Protocol property tests must include:

- round-trip encoding/decoding for valid fields where a true inverse exists;
- decoder totality: arbitrary byte sequences either produce a validated value or a typed error, never an unbounded exception or partial trusted object;
- frame length and CRC properties;
- monotonic scaling and exact boundary behavior;
- two's-complement equivalence over the full signed 16-bit domain;
- no accepted request can serialize outside its declared register range or quantity;
- unknown bits survive decode/encode when the contract requires lossless representation.

#### Golden vectors

Golden vectors are immutable, versioned fixtures containing exact request bytes, response bytes, interpreted values, transport profile, and provenance. They must be obtained from one or more of:

- packet captures of the original vendor application;
- captures from the established working integration;
- vendor documentation independently transcribed and double-reviewed;
- supervised target-device observations.

Golden vectors must **not** be generated by:

- production codec code;
- the simulator;
- a helper importing production constants or conversion functions.

Each vector records:

- `GV-*` identifier and evidence grade;
- source artifact hash and capture timestamp;
- device/profile identity with secrets removed;
- direction, raw bytes, and frame boundaries;
- register notation and resolved PDU address;
- expected decoded value and engineering unit;
- known ambiguities and reviewer sign-off.

At least two independently derived vectors are required for every writable field: one nominal and one boundary or signed value. Corrupt variants are derived by an independent fixture utility and assert precise rejection categories. Golden-vector changes require protocol-owner and safety-reviewer approval.

### 6.2 Per-unit actor concurrency and lease control

Each physical unit has one actor that exclusively owns its transport. Tests must prove the ownership and ordering invariants, not merely observe expected final values.

Required state coverage includes:

`BOOT`, `OBSERVE_ONLY`, `DISARMED`, `ARMED_IDLE`, `ACTIVE`, `INHIBITED`, `STOPPING`, and `DISCONNECTED`, plus every permitted and forbidden transition.

Required actor tests:

- only the actor can invoke transport reads or writes;
- concurrent callers enqueue intents/events and never create concurrent socket access;
- poll, authorize, renew, zero, reconnect, and shutdown operations are deadline-aware;
- each nonzero renewal consumes a current authorization for the same unit, generation, and setpoint;
- replacement increments a generation/fencing token before old work can continue;
- a cancelled task cannot write after stop, replacement, inhibition, or actor shutdown;
- an old response cannot update a newer observation or revive an expired command;
- a late write acknowledgement cannot mark a replaced generation successful;
- explicit stop attempts a bounded zero write and then ceases renewal so device fallback remains available;
- failed zero write is reported as uncertain physical state, not as success;
- actor failure is isolated per unit while fleet policy sees the degraded condition;
- reconnect returns through identity verification and fresh-observation qualification before actuation;
- queue growth is bounded and command replacement coalesces safely;
- backpressure cannot delay emergency stop behind telemetry or low-priority work;
- shutdown is idempotent under repeated and concurrent requests.

Concurrency tests use a controllable transport with barriers at every await boundary. The suite must systematically pause before and after connect, read, decode, safety evaluation, enqueue, dequeue, write, acknowledgement, zero, disconnect, and audit publication. It then explores cancellation, replacement, timeout, and shutdown at each barrier.

Randomized schedule tests run many seeded interleavings and preserve failing seeds. Stress tests assert invariants from an append-only operation history rather than relying on timing or log text. No actor test may use a real-time sleep to coordinate tasks.

### 6.3 Continuous safety kernel

The safety kernel is deterministic and side-effect free: current policy plus current validated observation plus current intent produces an authorization or an explicit inhibition. Its output is short-lived and unit-specific.

The following dimensions require decision-table tests, pairwise tests, boundaries, and selected full combinations:

- armed/disarmed state;
- telemetry presence, age, sequence, source, and quality;
- SOC minimum/maximum and hysteresis;
- SOC slew, jumps, quarantine, and stable-sample rearming;
- system SOC versus BMS SOC agreement;
- minimum/maximum cell voltage;
- cell imbalance, including policy boundaries around observed values such as `0.024 V` without assuming that observation is itself a safe threshold;
- minimum/maximum temperature and sensor spread;
- PCS, DCDC, BMS, and BECU faults and warnings, including EE/EEPROM calibration warnings;
- static configured limits and dynamic device charge/discharge limits;
- active power, reactive power, apparent-power envelope, and ramp rate;
- site import/export and per-phase limits;
- device identity, protocol profile, run/debug mode, and competing-writer indicators;
- fleet quorum and degraded-unit policy;
- audit availability for actuation decisions;
- authorization TTL and command generation.

Core safety properties:

- adding a blocking fault cannot turn an inhibition into authorization;
- making required telemetry older cannot make a denied command permissible;
- increasing requested magnitude cannot produce a larger authorized magnitude than every applicable limit;
- authorization is always bounded by intent, policy, device, site, and signed-wire limits;
- charge and discharge limits are directionally correct at zero and both boundaries;
- `P² + Q²` never exceeds the authorized apparent-power envelope;
- non-finite values are never ordered, clamped, or coerced into safe values;
- an authorization cannot outlive its intent, observation freshness window, policy version, generation, or actor epoch;
- changing any input used by a safety decision invalidates reuse of the old authorization;
- an unknown rule result combines as deny, never allow;
- a stop or emergency-stop request always yields zero/non-renewal regardless of lower-priority inputs.

State-machine tests cover qualification, arming, activation, inhibition, cooldown, stable-sample rearming, manual acknowledgement where required, and shutdown. Every transition must assert both the decision and its machine-readable reason set. Tests also verify that reason ordering is stable without discarding simultaneous causes.

### 6.4 Intent validation and arbitration

An intent describes a desired outcome; it is not a hardware command. Every intent has a source, scope, creation time, monotonic TTL or explicit non-actuating persistence, requested direction and magnitude, idempotency key, and authenticated principal where applicable.

Required tests:

- malformed, unbounded, expired, future-dated, unauthorized, and replayed intents are rejected;
- public APIs use explicit direction plus nonnegative magnitude; signed conversion exists only in the protocol adapter;
- fleet-total and per-unit semantics are distinct and cannot be confused;
- priorities are fully ordered and ties have deterministic resolution;
- emergency stop dominates every command source;
- manual override, schedule, optimizer, and idle precedence follows the approved policy;
- a lower-priority intent becomes eligible only after the winner expires or is withdrawn;
- withdrawal, replacement, and idempotent retry have unambiguous outcomes;
- partial fleet targeting cannot accidentally broadcast to all units;
- unit identity, not array position, determines routing;
- arbitration produces no transport effects and exposes no driver reference;
- policy changes cause deterministic re-arbitration;
- conflicting charge and discharge requests never combine by accidental signed arithmetic;
- unsupported reactive power and maintenance/debug requests are rejected at ingress;
- restart does not restore manual actuation intents.

Property tests generate arbitrary intent sets and assert determinism, permutation invariance where priorities differ, idempotency, winner validity, scope containment, and emergency-stop dominance. Stateful tests model create, replace, expire, withdraw, policy-update, and restart operations against a small reference arbiter.

### 6.5 Scheduling, midnight, and timezones

Schedules produce ordinary short-lived intents and never bypass arbitration or safety.

Required test matrix:

- inclusive/exclusive boundary semantics at start and end;
- same-day and cross-midnight windows;
- day-of-week ownership for the after-midnight portion;
- adjacent, nested, duplicate, and overlapping entries;
- explicit overlap rejection or documented deterministic priority;
- timezone identifiers from the IANA database;
- system timezone differing from schedule timezone;
- UTC offset changes and tzdata updates;
- daylight-saving spring gaps and autumn folds in representative zones;
- Australia/Brisbane behavior as a no-DST deployment zone;
- leap day, month/year boundary, and leap-second-neutral behavior;
- clock moved forward/backward, host suspend/resume, and monotonic versus civil-clock divergence;
- atomic schedule version update and rollback;
- restart inside an active window;
- persistence of direction, magnitude, unit scope, source, and policy through the complete schedule pipeline;
- stale schedule versions and concurrent edits;
- disabled entries and empty schedules;
- invalid or noncanonical time values;
- optimizer-created schedules obeying the same schema and authorization rules.

A reference interval model, implemented independently from the production scheduler, supplies expected active intervals. Property tests generate schedules and instants across multiple zones and compare the production result with the reference model. Metamorphic properties include UTC/local-time equivalence, stable behavior under input ordering, and splitting a non-overlapping interval without changing covered instants.

Tests freeze the timezone database version in CI while a separate compatibility job evaluates the newest supported tzdata and reports semantic changes.

### 6.6 SQLite state and audit journal

SQLite tests use the real SQLite engine and filesystem semantics. Mocked repositories are appropriate for application-unit tests but cannot satisfy persistence qualification.

Required tests:

- schema creation from an empty database;
- forward migration from every supported released schema;
- migration rollback or safe startup refusal after interruption;
- WAL configuration, busy timeout, foreign keys, and integrity checks;
- transaction atomicity across intent, decision, dispatch, acknowledgement, and fault lifecycle records where the model requires it;
- globally and per-unit determinable event order;
- monotonic sequence allocation under concurrent readers/writers;
- idempotency-key uniqueness and replay result stability;
- timestamps preserve UTC instant, source timezone where needed, and monotonic durations without pretending monotonic values survive restart;
- active/cleared fault lifecycle, severity, stack identity, observation sequence, and repeated occurrence;
- restart durability and no automatic resurrection of expired/manual actuation;
- audit queue saturation inhibits new actuation when durable recording is required;
- disk full, read-only filesystem, permission loss, I/O error, corruption, locked database, and process death at transaction boundaries;
- bounded retention, archival, and checkpoint behavior without blocking the control loop;
- sensitive data is excluded or redacted;
- append-only application permissions for audit facts and explicit corrective events instead of silent mutation;
- backup/restore and integrity verification.

Crash tests terminate a separate process at instrumented commit points, reopen the database, run integrity checks, and verify allowed pre- or post-transaction states. There must be no impossible hybrid state. Durability expectations (`synchronous` mode and acknowledged-commit semantics) are explicit and tested on the deployment filesystem class.

Audit contract tests assert canonical event schemas, reason codes, correlation IDs, policy/configuration versions, actor generation, principal, requested and authorized values, raw acknowledgement metadata, and uncertainty markers. Wall-clock timestamps alone must never be used to infer causality.

### 6.7 REST contracts

REST tests begin from the versioned OpenAPI contract and exercise the deployed ASGI/HTTP boundary, not only service methods.

Required tests:

- request and response schema conformance, including rejection of unknown safety-relevant fields;
- explicit API versioning and backward-compatibility policy for released versions;
- authentication is mandatory for mutation and empty credentials cannot start a network-accessible control service;
- authorization scopes distinguish observe, schedule, dispatch, arm, stop, and administration;
- emergency-stop semantics and availability are documented and tested without weakening authentication controls;
- idempotency keys, replay windows, and duplicate concurrent requests;
- optimistic concurrency/version checks for schedules and policy;
- status codes and stable machine-readable error bodies for validation, conflict, inhibition, unavailable audit, stale state, and uncertain dispatch;
- request size, rate, timeout, and connection limits;
- hostile JSON values, duplicate keys, Unicode edge cases, NaN/Infinity, and content-type confusion;
- no endpoint accepts raw register addresses, force/debug modes, or transport access;
- health endpoints separately report liveness, service readiness, and control readiness;
- telemetry responses expose age, quality, source, and staleness rather than silently carrying old values;
- sensitive configuration and credentials never appear in responses, errors, metrics, or OpenAPI examples;
- CORS, CSRF strategy, trusted proxy behavior, and secure headers match the deployment model.

Contract fuzzing uses the OpenAPI schema for valid and invalid request generation. Consumer-driven contracts cover the React client without making frontend snapshots the API oracle.

### 6.8 MCP contracts

MCP is read-only by default. Mutating tools require an explicit deployment feature, dedicated scopes, and the same intent path as REST.

Required tests:

- tool discovery exposes only configured tools and never leaks hidden maintenance capabilities;
- input/output JSON schemas reject unknown or ambiguous fields;
- tool descriptions accurately distinguish observation, proposal, and actuation;
- read-only principals cannot mutate through direct tools, resources, prompts, aliases, or crafted payloads;
- mutating tools submit bounded intents and cannot hold or renew hardware leases themselves;
- every mutation has principal, correlation, idempotency, audit, and policy context;
- stale telemetry and inhibition reasons are preserved in tool results;
- prompt text cannot override authorization, safety, magnitude, target, or expiry constraints;
- replayed and concurrent calls are deterministic;
- cancellation and client disconnect do not leave an unbounded intent;
- rate, size, timeout, and error contracts are enforced;
- REST and MCP produce equivalent application outcomes for equivalent authorized intents;
- an agent cannot address raw Modbus operations, enable maintenance/debug modes, or modify safety policy unless a separately approved administrative contract exists.

Schema snapshots are reviewed semantically. A snapshot update alone is not evidence that a breaking or unsafe MCP change is acceptable.

### 6.9 Independent simulator

The simulator is a test instrument, not the source of protocol truth. It runs as a separate package/process and must not import production protocol codecs, register constants, domain models, or safety rules.

The simulator must support:

- byte-oriented RTU-over-TCP sessions and any separately declared transport profile;
- independent request parsing and response construction;
- three separately identified units with asymmetric latency and failures;
- configurable telemetry, device limits, fault words, warnings, cell data, and objective echoes;
- command lease expiry and configurable fallback behavior marked as simulated rather than proven firmware fact;
- partial packets, packet coalescing, corrupt CRC, malformed lengths, Modbus exceptions, disconnects, reconnects, and connection refusal;
- delayed, lost, duplicated, and reordered responses where the transport permits observation of them;
- device restart, identity change, layout mismatch, competing-writer effects, and stale readback;
- deterministic scenario scripts and event histories;
- a real-time mode for manual UI work and a virtual-time mode for automated testing.

Simulator qualification tests run golden vectors directly against the simulator and production adapter independently. Agreement between simulator and production is useful only when both also agree with the vector. Deliberately incorrect canary vectors must be rejected by both, preventing a vacuous always-pass harness.

Simulator scenarios are versioned and traced to the failure mode they represent. A scenario must declare which behaviors are measured facts, configurable hypotheses, or deliberately synthetic faults.

### 6.10 Docker lifecycle and deployment

System tests exercise the built image using the same entry point, user, filesystem permissions, health checks, configuration loading, and signal handling intended for deployment.

Required tests:

- image builds reproducibly from locked dependencies and contains no development secrets, caches, simulator, compilers, or unintended source artifacts;
- process runs as a non-root user with dropped capabilities and a read-only root filesystem except explicit data paths;
- absent, malformed, unknown, duplicate, or unsafe configuration refuses startup;
- startup is observe-only/disarmed and requires identity plus stable fresh observations before control readiness;
- restart never restores manual actuation or silently arms the system;
- only one controller instance can own a deployment, with a tested failure mode for lock loss or duplicate startup;
- `SIGTERM` revokes authorization, fences old generations, attempts bounded zero, flushes audit within policy, and exits before the orchestrator grace period;
- repeated `SIGTERM` and shutdown during connect/read/write/reconnect are safe and idempotent;
- `SIGKILL`, runtime crash, event-loop stall, and host/network loss stop renewals so the simulated hardware lease expires;
- controller-task death makes control readiness fail and terminates or inhibits the service according to supervision policy;
- liveness, readiness, and control-readiness probes distinguish process availability, dependency readiness, and permission to actuate;
- database unavailable/full/corrupt, gateway unavailable, one-unit failure, and total network isolation have explicit readiness and inhibition outcomes;
- bounded logs and audit storage cannot exhaust the container unnoticed;
- configuration and secret rotation do not expose values and never cause implicit rearming;
- container timezone does not alter timezone-aware schedule semantics;
- supported architecture and resource-limit tests cover CPU starvation, memory pressure, and file-descriptor exhaustion;
- upgrades and rollbacks preserve compatible database state and begin disarmed.

Endurance tests run a three-unit simulated deployment through polling, schedules, command replacement, faults, reconnect storms, and audit writes for an accelerated equivalent of at least seven days. They assert bounded memory, task count, queue depth, file descriptors, database growth, and latency percentiles.

### 6.11 React UI

The UI is a safety-relevant client but never a safety authority. Backend state, timestamps, reasons, and acknowledgements remain authoritative.

Required test layers:

- pure unit/property tests for formatters, unit conversions, validation, reducer/state transitions, and timezone display;
- component tests for loading, live, stale, disconnected, inhibited, uncertain, and permission-denied states;
- API contract tests generated from the versioned schema;
- browser integration tests against the real backend and independent simulator;
- accessibility tests plus keyboard-only and screen-reader-oriented workflows;
- visual-regression tests at supported breakpoints and themes;
- end-to-end tests for observation, scheduling, manual bounded intent, stop, reauthentication, and reconnection.

Safety-relevant UI assertions:

- charge and discharge are represented by words and consistent symbols/colors, never color alone;
- requested, authorized, measured, and acknowledged power are visually distinct;
- fleet-total and per-unit values cannot be confused;
- telemetry age and quality are prominent; stale values are not presented as live;
- SOC jumps, cell imbalance, warnings, and inhibition reasons remain visible without relying on transient notifications;
- units are keyed by verified identity, not list position;
- a submitted command is shown as pending until accepted and is never shown as physically active from HTTP success alone;
- uncertain acknowledgement is distinct from success and failure;
- destructive or unusual actions require deliberate confirmation proportional to risk, without impeding emergency stop;
- navigation, reload, duplicate clicks, offline mode, and multiple browser tabs cannot create unintended duplicate intents;
- authorization expiry removes controls and cannot leave an apparently active local state;
- maintenance/debug modes are absent from the production UI;
- all charts expose units, timezones, gaps, downsampling, and stale intervals accurately.

Accessibility target is WCAG 2.2 AA for tested workflows. Automated checks are supplemented by manual keyboard, focus-order, zoom, contrast, reduced-motion, and screen-reader review. Visual snapshots are narrow assertions for intentional layout; they do not replace semantic tests.

### 6.12 Fleet and end-to-end behavior

Cross-component tests validate:

- independent MID/RHS/LHS identity and routing without assuming address or array order;
- one slow or failed gateway does not block other unit actors;
- fleet-total allocation respects every unit, site, and phase limit;
- unit removal/reappearance triggers reallocation only through a fresh safety cycle;
- no atomic three-unit transaction is assumed;
- partial dispatch results are represented explicitly and audited;
- emergency stop reaches all actors with priority and reports any uncertain unit;
- schedule, manual, optimizer, REST, and MCP paths converge on the same arbiter and safety kernel;
- a fault arriving between initial request and heartbeat inhibits the next renewal;
- a telemetry update arriving during an in-flight write cannot authorize a stale follow-up;
- external objective changes or unexpected modes inhibit control;
- read-only shadow mode emits proposed decisions but sends no writes at any layer.

## 7. Property, state-machine, concurrency, and fault-injection standards

### 7.1 Property testing

Properties are stated before generators. Generators must include valid, invalid, boundary, and structurally adversarial values. Shrunk counterexamples and seeds are retained in the regression corpus.

Required domains include signed register values, frames, telemetry, safety policies, intent sets, time intervals, timezone instants, fleet allocations, API payloads, and audit event sequences.

Generator coverage is reported: boundary buckets, invalid classes, enum values, fault bits, timezone transitions, and policy branches must all be exercised. A large example count without domain coverage is insufficient.

### 7.2 Model-based state-machine testing

Reference models must be smaller and structurally independent from production code. Required models cover:

- actor lifecycle and command generations;
- authorization qualification/inhibition/rearming;
- intent arbitration and expiry;
- schedule activation;
- fault lifecycle;
- durable audit transaction outcomes.

Commands, preconditions, postconditions, and invariants are explicit. Failing operation sequences are minimized and stored as regression tests.

### 7.3 Concurrency testing

Concurrency tests use virtual time and explicit synchronization points. They must include:

- cancellation at every await boundary;
- simultaneous stop, replacement, fault arrival, and reconnect;
- delayed completion from an old generation;
- actor crash while queues contain work;
- database backpressure during lease renewal;
- repeated start/stop and reconnect storms;
- scheduler and manual intent races;
- multiple API/MCP clients retrying the same and different idempotency keys.

Each test asserts history invariants such as single transport owner, generation monotonicity, no nonzero write without authorization, and stop dominance. Race tests run repeatedly under randomized deterministic scheduling in CI and at higher counts nightly.

### 7.4 Fault injection

Faults are injected at boundaries, never by patching away the behavior under test. The matrix includes:

- network: refusal, timeout, half-close, reset, latency, loss, corruption, partial frame;
- protocol: CRC, length, unit, function, exception, acknowledgement, range, identity, layout;
- device: restart, stale telemetry, SOC jump, non-finite value, contradictory status, warning/fault, dynamic limit collapse, external writer;
- process: task exception, cancellation, event-loop stall, `SIGTERM`, `SIGKILL`, restart;
- persistence: busy, full, corrupt, permission denied, interrupted commit, slow checkpoint;
- time: monotonic advance, wall-clock jump, suspend/resume, DST transition, tzdata change;
- resource: CPU quota, memory pressure, full queues, descriptor pressure, log volume;
- client: disconnect, duplicate request, replay, malformed data, stale version, expired authentication.

For every injected fault, the test specifies the expected physical-command consequence, externally visible state, audit record, recovery preconditions, and whether manual rearming is required.

## 8. Test doubles and independence rules

Test doubles are named by their fidelity:

- **Stub:** fixed response; no behavioral assertions.
- **Spy:** records calls/history.
- **Fake:** working lightweight implementation with a declared semantic subset.
- **Model:** independent reference behavior for comparison.
- **Simulator:** external protocol peer operating on raw frames.

Rules:

- Safety-kernel unit tests may fake ports but must use real domain values and policy validation.
- Protocol component tests use raw frames and the independent simulator, never a mocked codec response.
- SQLite qualification uses real temporary database files.
- REST/MCP contract tests traverse real serialization and authentication middleware.
- UI end-to-end tests use a real backend composition with the simulator.
- A fake cannot satisfy a release criterion for the adapter it replaces.
- Shared test builders may create data, but they may not compute the expected result with production logic.
- Expected values are literal, independently calculated, or produced by a reviewed reference model.

## 9. Coverage and quality gates

Coverage is measured on code exercised by deterministic automated tests. Generated code and declarative type-only files may be excluded only through reviewed configuration.

| Scope | Statement/line | Branch | Additional gate |
|---|---:|---:|---|
| `S0` safety kernel, lease authorization, actor fencing, protocol writes, stop path | 100% | 100% | 100% decision-condition and state-transition coverage |
| `S1` arbiter, schedules, auth, audit lifecycle, API mutation paths | 100% | 100% | All error categories and transitions covered |
| Other backend domain/application code | 95% | 95% | No uncovered exception/recovery path |
| Backend adapters overall | 90% | 90% | Contract and fault-injection suites pass |
| React safety-relevant state/actions | 100% | 100% | Required UI state-transition coverage |
| React code overall | 90% | 85% | Critical workflows pass browser and accessibility tests |
| Repository overall | 95% | 90% | No criticality-weighted regression |

Coverage may not be raised with assertions that merely execute lines. Tests must demonstrate a meaningful observable outcome or invariant.

### 9.1 Mutation testing

Mutation testing is mandatory after baseline tests are stable.

- `S0`: kill 100% of non-equivalent mutants. Zero survived mutants may affect comparison boundaries, signs, units, boolean operators, expiry, generation checks, fault combination, zeroing, or authorization.
- `S1`: at least 95% mutation score, with every survivor reviewed and none allowed in authorization, identity, priority, persistence atomicity, or schema-validation behavior.
- Other backend domain/application code: at least 90%.
- React reducers, validators, unit conversions, and safety-relevant rendering logic: at least 90%, with zero survivors that hide stale/inhibited/uncertain state or invert direction.

Equivalent, unreachable, and tooling-invalid mutants are excluded only by a recorded two-person review tied to source line and requirement. Timeouts are not counted as killed mutants. Mutation reports and survivor dispositions are release artifacts.

### 9.2 Static and structural gates

The build also requires:

- strict type checking with no unreviewed suppressions in `S0`/`S1` code;
- lint and formatting consistency;
- architecture tests enforcing dependency direction and banning UI/API-to-transport access;
- dependency vulnerability and license review;
- secret scanning;
- locked dependency and container-image provenance checks;
- no skipped, focused, quarantined, or expected-failure `S0`/`S1` tests;
- zero known flaky critical tests.

### 9.3 Flake and repeatability gates

- Changed concurrency and state-machine tests run at least 100 deterministic schedules before merge.
- The nightly suite runs at least 1,000 schedules per critical concurrency property and stores all seeds.
- Any critical test that produces inconsistent results is treated as a product/test defect and blocks release.
- Test reruns may diagnose a failure but may not convert a failing build to passing automatically.
- CI records OS, runtime, dependency lock hash, timezone database version, image digest, random seeds, and test-shard assignment.

## 10. TDD workflow and review loops

Development proceeds in vertical, safety-reviewable increments. Each increment follows this loop:

1. **Requirement and hazard definition**
   - Specify observable behavior, invariants, forbidden behavior, limits, failure state, and evidence grade.
   - Assign stable IDs and criticality.
   - Resolve ambiguity or record it as `UNC-*`; do not encode a guess as expected behavior.

2. **Test design by a test author**
   - Write positive, boundary, negative, property, transition, and fault cases appropriate to criticality.
   - Select independent oracle/evidence.
   - Demonstrate that the test suite collects and the new test fails for the intended missing behavior.

3. **Adversarial test review by an independent reviewer**
   - Challenge oracle independence, missing boundaries, sign/unit mistakes, race windows, fail-open assumptions, and overfitting.
   - Compare against requirements, hazards, protocol evidence, and neighboring contracts.
   - Add counterexamples and mutation hypotheses before approval.

4. **Implementation by a different author where practical**
   - Implement the minimum coherent design that satisfies approved contracts.
   - Do not edit approved expected outcomes without returning to step 1.

5. **Implementation review**
   - Check correctness, dependency direction, single-writer enforcement, failure semantics, observability, and maintainability.
   - Review inheritance and abstraction for substitutability and actual reuse. Composition and protocols are preferred where inheritance adds coupling without a stable behavioral subtype.

6. **Independent verification**
   - Run unit, property, state-machine, concurrency, contract, and fault tests as applicable.
   - Run coverage and mutation analysis.
   - Inspect survivor mutations and test histories, not only the pass count.

7. **Integration review**
   - Exercise the component through its production boundary with the independent simulator or real persistence/runtime.
   - Update traceability and verify no previously proven invariant regressed.

8. **Close or iterate**
   - Close only when all criticality-specific gates pass and review findings are resolved.
   - New evidence or a surviving meaningful mutant restarts the loop at the appropriate earlier step.

For `S0` work, test author, implementation author, and final safety reviewer must be at least two distinct agents/people, and the final reviewer must not have authored the implementation. Automated agent reviews are advisory until a human authorizes live-device progression.

Review is evidence-driven, not an unbounded demand for “100% correctness.” A review loop exits when its explicit requirements, hazards, test classes, mutation gates, and acceptance evidence are complete with no open `S0`/`S1` finding.

### 10.1 Review checklist

Every test review asks:

- Does the test trace to a current requirement and hazard?
- Is the expected result independent from the code under test?
- Could both simulator and implementation share the same error?
- Are units, sign, scaling, word order, address notation, and identity explicit?
- Are zero, exact boundary, just-inside, and just-outside values present?
- What happens for missing, stale, non-finite, contradictory, and unexpected data?
- What happens if cancellation, replacement, stop, or restart occurs at each await point?
- Does the test prove absence of forbidden writes, not merely presence of expected writes?
- Is time virtual and are scheduling semantics timezone-aware?
- Does a failure produce correct state, reason, audit, and recovery requirements?
- Would an inverted conditional, removed limit, changed sign, extended TTL, or ignored error be caught?
- Can the test pass vacuously because no event occurred?
- Are assertions made through public behavior rather than private implementation shape?
- Is the test stable under input ordering and repeated deterministic schedules?

## 11. Test suite organization and markers

The eventual test tree should make levels and independence visible. Exact framework-specific paths may change through an ADR, but the separation must remain:

```text
tests/
  unit/
    domain/
    application/
  property/
  state_machine/
  component/
    protocol/
    sqlite/
    rest/
    mcp/
  contract/
  integration/
  system/
  endurance/
  hil/
  operational/
  fixtures/
    golden/
    captures/
    scenarios/
web/
  src/**/*.test.*
  e2e/
```

Mandatory markers/tags include criticality, level, area, hazard, simulator, Docker, destructive/HIL, and duration class. Hardware and operational tests are excluded by default and require an explicit target profile and authorization token that cannot be present in ordinary CI.

## 12. CI and release stages

### Pull request

- traceability validation;
- static/type/architecture checks;
- backend and frontend unit tests;
- changed-area property and state-machine tests;
- component and contract tests;
- golden-vector tests;
- UI component and accessibility tests;
- coverage gates;
- changed `S0`/`S1` mutation tests;
- independent review approval.

### Main branch

- full property/state-machine corpus;
- randomized deterministic concurrency schedules;
- full integration suite;
- built-container lifecycle tests;
- browser tests and visual regression;
- full backend and safety-relevant frontend mutation suite;
- dependency, image, secret, and provenance scans.

### Nightly

- high-count concurrency and property runs;
- fault matrix;
- endurance/resource tests;
- newest supported tzdata compatibility;
- database crash-point matrix;
- reconnect and latency distributions;
- full mutation analysis where too slow for main.

### Release candidate

- clean build from lockfiles;
- all traceability and review records complete;
- no open `S0`/`S1` defect or uncertainty required for the release mode;
- simulator system qualification;
- upgrade/rollback and backup/restore tests;
- signed artifacts and recorded image digest;
- operational test plan approved separately.

No CI pipeline may contact the live home installation. Hardware-in-the-loop and operational jobs require an explicit, isolated environment and cannot be triggered by untrusted code changes.

## 13. Progression to live operation

Live qualification is staged and reversible:

1. **Offline:** golden vectors, simulator, and all automated gates.
2. **Read-only hardware:** identity, layout, scaling, freshness, fault, and telemetry comparison on each unit; no writes available in the build profile.
3. **Shadow mode:** compute intents, arbitration, safety decisions, allocations, and would-be renewals; compare with observed system behavior without writes.
4. **Controlled zero-write validation:** only after protocol write framing reaches the required evidence grade and under an approved procedure.
5. **Bounded low-power single-unit trial:** supervised, one unit at a time, explicit direction/readback verification, independent stop path, and conservative limits.
6. **Degraded and expiry validation:** verify renewal loss and fallback behavior without assuming the timeout from historical software.
7. **Three-unit controlled trial:** only after identity/routing and per-unit isolation are proven.
8. **Limited production:** conservative policy, MCP mutations disabled, detailed review after a defined observation period.

Every stage has preconditions, abort criteria, rollback, an observer, and retained raw evidence. Passing one device does not qualify the other devices. Firmware or gateway changes revoke affected evidence grades until revalidated.

## 14. Exit criteria by subsystem

### Protocol

- All writable claims are at least `E3` before controlled actuation and `E4` before general production enablement.
- Golden vectors pass independently against production adapter and simulator.
- Malformed-frame and acknowledgement fault matrix passes.
- No unresolved sign, scaling, addressing, framing, identity, or lease uncertainty is required by the enabled profile.

### Actor and safety

- All invariants have positive, negative, state-machine, concurrency, and fault evidence.
- `S0` coverage and mutation gates pass with no unexplained survivor.
- Stop, cancellation, expiry, stale data, reconnect, and old-generation races are proven.
- A critical supervisor failure cannot leave software renewal active.

### Scheduling and arbitration

- Priority, expiry, scope, direction, versioning, midnight, timezone, and DST matrices pass.
- Emergency-stop dominance and restart non-restoration are proven.
- Reference-model comparisons pass over the configured generated corpus.

### Persistence and interfaces

- Migration, crash, disk-fault, idempotency, authentication, authorization, and schema fuzzing pass.
- Audit unavailability has the approved fail-closed behavior.
- REST/MCP cannot bypass the intent and safety path.

### Container and UI

- Lifecycle, health separation, signal, resource, upgrade, accessibility, browser, and visual gates pass.
- The UI never represents request acceptance as measured physical action.
- The released image starts disarmed and contains only approved production assets.

## 15. Defect and exception policy

- No open `S0` or `S1` defect is acceptable in a release that enables the affected path.
- A flaky `S0`/`S1` test is a blocking defect, not a candidate for quarantine.
- Coverage, mutation, evidence-grade, or review exceptions for `S0` are not permitted.
- An `S2` exception requires documented impact, containment, owner, expiry date, and a test proving the containment.
- Test skips require a linked defect and may not silently pass a gate.
- Known protocol uncertainties disable only the affected feature/profile when isolation is demonstrably complete; otherwise they disable actuation.

## 16. Initial test-design sequence

The clean rewrite should design tests in this order because each later layer depends on the earlier safety contracts:

1. Units, signs, limits, identities, clocks, freshness, and strict validation value objects.
2. Protocol golden vectors and independent raw-frame parser expectations.
3. Safety decision tables and properties.
4. Intent model, arbitration, expiry, and emergency-stop dominance.
5. Actor state machine, sole ownership, generations, cancellation, and lease renewal.
6. Independent simulator and protocol component tests.
7. SQLite event/audit contract and crash behavior.
8. Schedule interval model, timezones, versioning, and intent production.
9. REST and MCP contracts, authentication, authorization, and idempotency.
10. Fleet coordination and degraded operation.
11. Docker lifecycle and supervision.
12. React component, accessibility, contract, visual, and browser tests.
13. End-to-end shadow operation and staged hardware qualification.

Implementation begins for a component only after its requirement, hazard, test design, oracle, and adversarial test review are approved. This ordering does not require a monolith: independent components may proceed in parallel once their shared contracts are stable and their integration tests are defined.

