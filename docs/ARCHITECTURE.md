# EnergyPod Manager Architecture

**Status:** Proposed implementation contract  
**Target runtime:** Python 3.12 in a single Docker container  
**Deployment:** One site, three independently connected EnergyPods  
**Safety posture:** Fail closed for nonzero power; boot and restart disarmed

## 1. Purpose

EnergyPod Manager observes and coordinates three grid-connected battery units through independent Waveshare Ethernet gateways. It presents one coherent control plane to operators, schedules, future optimization services, and future agents without allowing any of those clients to write Modbus registers directly.

This document defines the production architecture and the constraints an implementation must satisfy. It deliberately does not treat the reverse-engineered vendor application or previous manager implementations as architectural templates. They are protocol and operational evidence only.

The first production objective is safe, observable manual and scheduled active-power control. Tariffs, weather, solar forecasts, load forecasts, optimization, and MCP integration are extension points. They cannot weaken or bypass the same control path.

## 2. Scope and non-goals

### 2.1 In scope

- Continuous read-only monitoring of three independently addressed units.
- Bounded charge, discharge, and idle intents.
- Deterministic arbitration when multiple intent sources compete.
- Continuous safety evaluation using current telemetry and site policy.
- Renewable, short-lived per-unit power authorization.
- Per-unit lifecycle management, connection isolation, and failure containment.
- Authenticated operator and automation APIs.
- Durable configuration, schedules, audit events, and operational metadata.
- A polished web application backed by the same API used by other clients.
- Provider interfaces for tariff, weather, PV, load, and forecast data.
- A future optimizer and MCP adapter that remain outside the actuation boundary.
- Container lifecycle, health reporting, observability, backup, and recovery.

### 2.2 Explicitly out of scope for the normal production service

- Raw register write endpoints.
- Firmware update, EEPROM/calibration writes, SOC fixing, capacity verification, circulation, and other maintenance/debug modes.
- A persistent force-charge or force-discharge mode that defeats the unit's watchdog or autonomous fallback.
- Reactive-power control until its sign, units, limits, and site obligations are independently validated.
- Automatic restoration of a manual command after process or host restart.
- Allowing an LLM, MCP client, UI, scheduler, or provider adapter to own a socket or construct a hardware command.
- Treating inferred protocol behavior as verified fact.

Maintenance tooling, if it is ever developed, is a separately packaged, locally operated capability with different authorization and commissioning procedures. It is not a hidden route in the production API.

## 3. Evidence and assumptions

Architecture must distinguish observed facts from hypotheses. The protocol package shall maintain an evidence ledger for every writable field and safety-critical telemetry field with one of these levels:

| Level | Meaning | Control eligibility |
|---|---|---|
| Captured | Confirmed by byte-level traffic from the deployed application or a controlled device test | Eligible after tests reproduce the capture |
| Source-confirmed | Supported by two independent code paths or vendor source plus observed behavior | Eligible with commissioning validation |
| Inferred | Plausible from one source, names, or historical behavior | Observe only |
| Unknown/conflicting | Sources disagree or behavior is not understood | Must not be used for actuation |

Current evidence supports three separate gateways, RTU framing transported over TCP, slave address 4, a signed active-power objective, and repeated transmission by historical applications. The exact firmware lease duration, refresh margin, objective sign, identity fields, mode prerequisites, and all force/debug-mode behavior must still be proven against the deployed units before writes are enabled. Deployment addresses such as `192.168.1.11` through `192.168.1.13` and port `4196` are configuration, not domain constants.

No numeric battery safety threshold is invented by this architecture. Initial thresholds must come from authoritative equipment limits or a documented commissioning decision. For example, a measured 24 mV cell spread is useful telemetry but is not, by itself, evidence for a universal inhibit threshold.

## 4. Non-negotiable invariants

1. **One socket owner:** exactly one per-unit actor owns, opens, reads, writes, reconnects, and closes that unit's gateway connection.
2. **Fresh authorization:** a unit actor may renew nonzero output only with a fresh, monotonic `AuthorizedSetpoint` created for that unit, generation, and control cycle.
3. **Continuous revalidation:** the control kernel re-evaluates the winning intent, latest telemetry quality, dynamic device limits, site limits, and lifecycle state before every nonzero renewal.
4. **No authority reuse:** an authorization is single-cycle, non-transferable, bounded by a monotonic deadline, and unusable after generation change, inhibit, stop, or reconnect.
5. **Fail closed:** missing, stale, malformed, contradictory, non-finite, or implausible safety data prevents nonzero authorization.
6. **Generation fencing:** after replacement, stop, inhibit, or disconnect, an older command generation can never write again.
7. **No bypass:** only the unit actor can reach a transport, and only the control kernel can create an authorized setpoint. Adapters submit intents or observations.
8. **Safe restart:** process, container, or host restart returns every unit to read-only observation and then disarmed state. Manual intents and arming are never restored.
9. **Independent failures:** failure or latency of one gateway cannot block control cycles or lifecycle progress for another unit.
10. **Site constraints:** fleet and phase constraints are applied after unit capability constraints and before authorization.
11. **Auditable actuation:** every intent selection, authorization, write attempt, acknowledgement, rejection, inhibit, and state transition has a correlated audit record.
12. **Firmware fallback remains final protection:** when safe renewal cannot complete on time, transmission stops and the device's independently commissioned watchdog/fallback behavior is allowed to take effect.

These invariants are acceptance criteria, not implementation aspirations.

## 5. Architectural style

The system uses ports and adapters around a deterministic domain and application core. Dependencies point inward. Infrastructure code may depend on application contracts; the core never imports Modbus, HTTP, database, container, or UI libraries.

```text
Web UI     REST API     future MCP     schedule admin
   \          |            |              /
        authenticated inbound adapters
                       |
                 Intent service
                       |
                Intent arbiter
                       |
Site observations -> Fleet coordinator -> per-unit control kernel
                                            |
                                 AuthorizedSetpoint
                                            |
                           UnitActor MID / RHS / LHS
                               |         |         |
                         gateway A  gateway B  gateway C

Provider adapters -> normalized tariff/weather/forecast observations
                              |
                        optimizer strategy
                              |
                         bounded intent

All decisions and effects -> audit and telemetry ports -> persistence adapters
```

### 5.1 Dependency rings

| Ring | Responsibility | May depend on |
|---|---|---|
| Domain | Immutable values, lifecycle rules, telemetry quality, intent ordering, limits, safety decisions | Python standard library and domain types only |
| Application | Use cases, arbitration, fleet allocation, control-cycle coordination, supervision | Domain and declared ports |
| Ports | Structural contracts for clocks, units, repositories, providers, audit, identity, and authentication | Domain contracts |
| Adapters | Waveshare/Modbus, SQLite, REST, web assets, metrics, provider clients, future MCP | Application and ports |
| Runtime | Configuration validation, dependency composition, task supervision, signal handling | All rings for composition only |

The runtime is the only composition root. No global service locator, ambient socket, singleton object, or adapter lookup is permitted in the core.

### 5.2 Package responsibilities

The eventual source layout should express the rings rather than technical convenience:

| Area | Contents |
|---|---|
| `domain` | units, intents, setpoints, observations, quality, limits, lifecycle, safety results |
| `application` | intent service, arbiter, fleet coordinator, control kernel, unit supervision, query services |
| `ports` | clock, unit channel, repositories, audit sink, identity, provider, health, authentication |
| `adapters/protocol` | register profiles, codecs, framing, response validation, captured-trace fixtures |
| `adapters/waveshare` | RTU-over-TCP channel and connection policy |
| `adapters/api` | REST/WebSocket transport, schemas, authorization, idempotency |
| `adapters/persistence` | transactional metadata, schedules, audit, telemetry retention |
| `adapters/providers` | tariffs, weather, forecasts, site meters, future integrations |
| `runtime` | validated settings, composition, structured concurrency, signals, health |
| `web` | separately built frontend served as immutable assets or through a dedicated edge |

## 6. Domain model

### 6.1 Identity

A unit has a stable configured logical ID, expected gateway endpoint, expected protocol profile, and independently observed device identity. Physical labels such as MID, RHS, and LHS are metadata, never inferred from list position.

Before a unit becomes controllable, observed identity must match its configured binding. Duplicate endpoints, duplicate logical IDs, duplicate observed device identities, unexpected layouts, or unsupported cell topology prevent arming. Reconnection repeats identity verification.

### 6.2 Intent

An intent expresses a desired outcome, not a register value. It contains:

- Intent ID and idempotency key.
- Source category and authenticated principal.
- Scope: site, selected units, or an explicit unit.
- Direction: charge, discharge, or idle.
- Positive requested active power in watts; protocol sign conversion stays in the protocol adapter.
- Creation and monotonic expiry information.
- Optional wall-clock validity window for schedules, interpreted in an explicit site timezone.
- Reason, correlation ID, and policy metadata.
- Revision for optimistic concurrency.

Clients cannot choose their priority, extend a maximum TTL, name an internal generation, submit encoded register values, or request force/debug modes. Every non-idle intent is bounded. A schedule is not a long-lived hardware command; it is a rule that proposes a fresh bounded intent while its window remains valid.

Monotonic timestamps are process-local and are never persisted for replay. On restart, persisted schedules are re-evaluated against current wall time, while manual and automation intents expire permanently.

### 6.3 Intent priority and arbitration

Safety is a gate, not an intent priority. No higher-priority source can override an inhibit, stale telemetry, hardware limit, lifecycle restriction, or site limit.

The default source order is fixed by policy and cannot be supplied by clients:

| Priority | Source | Allowed effect |
|---:|---|---|
| 100 | Latched emergency stop | Idle only; revokes all nonzero authority until acknowledged by an authorized operator |
| 80 | Authenticated operator override | Bounded charge, discharge, or idle for selected units |
| 70 | Site/grid constraint adapter | Reduce power or force idle; positive dispatch requires separate enablement |
| 60 | Approved automation or future MCP client | Bounded intent within a dedicated scope and lower maximum power/TTL |
| 50 | Optimization strategy | Bounded proposal based on normalized forecasts and prices |
| 40 | Static schedule | Bounded proposal generated from a versioned schedule |
| 0 | Default | Idle |

The production default allows site/grid constraints to reduce or cancel output, not increase it. Any later demand-response actuation requires its own ADR and commissioning evidence.

Arbitration is deterministic. It filters invalid or expired intents, applies scope, selects the highest fixed source priority, then uses the greatest accepted revision and a stable intent ID as tie-breakers. Every winner and loser is recorded with a reason. Submitting a new intent never mutates an existing one in place; it supersedes by revision.

An idle or emergency-stop intent takes effect by revoking the current generation before any bounded attempt to write zero. Safety does not depend on the success of that zero write.

### 6.4 Observation and telemetry quality

Raw register values are retained at the adapter boundary. Decoding creates typed observations with:

- Unit and protocol-profile identity.
- Per-connection epoch and monotonically increasing sample sequence.
- Monotonic sample time and UTC receipt time.
- Source register range and decode revision.
- Value, engineering unit, and raw representation.
- Quality status and reason codes.
- Completeness information for arrays such as cell voltages and temperatures.

Quality is explicit:

| Quality | Meaning | Safety use |
|---|---|---|
| Good | Fresh, complete, in range, internally consistent, and decoded under the expected profile | Eligible |
| Suspect | A jump, mismatch, partial set, or unconfirmed semantic was detected | Visible, but not eligible for nonzero control when safety-relevant |
| Bad | Malformed, non-finite, impossible, faulted, or identity/profile mismatch | Inhibits affected control |
| Missing | No accepted value exists in this connection epoch | Inhibits affected control |
| Stale | Age exceeds the policy for that field | Inhibits affected control |

Freshness is measured with the monotonic clock. Wall-clock changes cannot make old telemetry appear new. Aggregate snapshots expose the oldest required-field age and all quality reasons; they never replace missing values with zero.

Safety-critical observations include, where independently validated:

- System and BMS SOC, SOH, and consistency between reported SOC sources.
- Pack voltage, current, active power, and direction consistency.
- Minimum and maximum cell voltage, cell spread, cell count, and array completeness.
- Minimum and maximum cell and pack temperatures.
- Dynamic BMS/PCS/DCDC charge and discharge limits.
- BMS, PCS, DCDC, and system faults and warnings, including EE/EEPROM calibration warnings.
- Operating mode, enable state, objective echo, and any remote-dispatch/watchdog indication.
- Gateway communication health, response framing, identity, and protocol layout.
- Site import/export and per-phase measurements when those constraints are enabled.

SOC discontinuities such as a 20% to 90% jump are quarantined. The new reading is marked suspect, nonzero output is inhibited, and rearming requires a configured number and duration of stable, mutually consistent samples. Cell spread, temperature, and SOC thresholds support warning, derating, inhibit, and latching behavior, but their numeric values require authoritative configuration.

### 6.5 Limits and setpoints

Public and domain power values use direction plus a non-negative magnitude. Signed 16-bit encoding is confined to a validated protocol codec that rejects out-of-range values rather than wrapping them.

The fleet coordinator applies constraints in this order:

1. Intent scope and requested direction.
2. Per-unit static commissioning bounds.
3. Fresh dynamic charge/discharge limits reported by the unit.
4. SOC, temperature, cell, warning, and fault derating or inhibit rules.
5. Ramp-rate and reversal dead-band rules.
6. Unit apparent-power envelope if reactive power is later enabled.
7. Site import/export and per-phase constraints.
8. Deterministic allocation across eligible units.

A site request is never copied blindly to all units. Allocation produces explicit per-unit targets whose sum does not exceed the site request. The algorithm accounts for eligibility, dynamic capability, SOC goals, phase/site limits, and ramp bounds. It is deterministic for a given input snapshot and policy version.

### 6.6 AuthorizedSetpoint

`AuthorizedSetpoint` is an internal capability produced only by the control kernel. It carries:

- Unit ID and connection epoch.
- Command generation and control-cycle ID.
- Winning intent ID and revision.
- Explicit per-unit active-power target.
- Monotonic issue time, not-before time, and hard deadline.
- Observation sequence and maximum observation age used in the decision.
- Effective static, dynamic, site, and ramp limits.
- Policy/configuration version and decision correlation ID.

It cannot be persisted and replayed, renewed by an adapter, transferred to another unit, or used twice. The unit actor rejects it if any binding differs from its current epoch, generation, cycle, or unit, or if its monotonic deadline has arrived.

## 7. Control-cycle architecture

### 7.1 Cycle contract

Each unit has a deadline-budgeted control cycle supervised by the application layer:

1. The unit actor performs the essential read set through its sole socket.
2. The protocol adapter validates framing, function, slave, register count, and values.
3. The actor publishes a sequenced observation for the current connection epoch.
4. The arbiter selects the current intent.
5. The fleet coordinator calculates explicit per-unit targets under site constraints.
6. The deterministic safety kernel evaluates lifecycle, freshness, quality, faults, warnings, limits, transitions, and target.
7. The audit writer durably accepts the decision within the cycle budget.
8. The kernel issues a single-cycle `AuthorizedSetpoint`, or a rejection/inhibit result.
9. The actor rechecks epoch, generation, cycle, deadline, and local connection state immediately before transmission.
10. The actor encodes and writes the objective, validates the acknowledgement, records the effect, and schedules the next cycle.

Optional or slow telemetry reads run only when the remaining deadline budget permits. They cannot delay an essential read or a required renewal. There is no independent heartbeat task, generic retry loop, or API-triggered write task.

### 7.2 Time budgets

Lease timing is a commissioned property, not a hard-coded assumption. Configuration records the measured device expiry distribution and derives conservative values for:

- Control period.
- Essential-read deadline.
- Kernel and audit deadline.
- Write and acknowledgement deadline.
- Renewal margin.
- Maximum jitter.

Startup refuses write-enabled operation when the complete worst-case cycle budget does not fit inside the commissioned renewal window with the required safety margin. Retries are deadline-aware and may occur only when the remaining budget safely accommodates them. When the budget is exhausted, the actor does not transmit a late nonzero command.

All safety deadlines use an injected monotonic clock. UTC is used for schedules, presentation, and audit correlation only.

### 7.3 Generation and cancellation ordering

Every condition that invalidates control increments a unit-local generation: new winner, explicit idle, emergency stop, safety inhibit, disconnect, reconnect, identity change, disarm, and shutdown.

Before cancellation or cleanup, the supervisor publishes the new generation. Actor mailboxes discard older generations. An operation checks its generation before encoding, before socket write, and before accepting an acknowledgement. A late completion can update diagnostics but cannot restore authority or lifecycle state.

This ordering eliminates the race in which an old renewal writes after a stop or replacement.

### 7.4 Write acknowledgement and readback

A transport call succeeding is not evidence that the device accepted the command. The adapter validates the Modbus response against the request. When a validated objective echo or operating-mode field exists, a later essential read must agree within a configured number of cycles. Mismatch suggests a competing writer, device rejection, or protocol error and moves the unit to `INHIBITED`.

No optimistic UI success is reported before the intent is accepted. The UI separately presents intent acceptance, current authorization, write acknowledgement, measured response, and final state.

## 8. Per-unit lifecycle state machine

Each actor has one explicit lifecycle. The fleet exposes an aggregate view but does not collapse independent unit state.

```text
BOOT -> OBSERVE_ONLY -> DISARMED -> ARMED_IDLE -> ACTIVE
          |               |            |          |
          +---------------+------------+----------+--> INHIBITED
                          ^            ^          |
                          |            +----------+
                          +---- qualified recovery + operator acknowledgement

Any nonterminal state ------------------------------> STOPPING
```

| State | Entry conditions | Permitted behavior | Exit conditions |
|---|---|---|---|
| `BOOT` | Process composition and unit actor creation | Validate configuration; acquire singleton; no network writes | Runtime ready moves to `OBSERVE_ONLY`; fatal configuration failure stops startup |
| `OBSERVE_ONLY` | Initial connection or reconnect | Read only; identify device/profile; establish telemetry baseline | Verified identity/profile and stable complete samples move to `DISARMED`; anomaly moves to `INHIBITED` |
| `DISARMED` | Observation checks passed, or recovery from disconnect/restart | Read and display; reject all nonzero intents | Explicit authorized arm plus current readiness moves to `ARMED_IDLE`; anomaly moves to `INHIBITED` |
| `ARMED_IDLE` | Operator or approved policy arms a ready unit | Observe; accept arbitration; no nonzero renewal without a fresh winning intent | Fresh safe authorization moves to `ACTIVE`; disarm moves to `DISARMED`; anomaly moves to `INHIBITED` |
| `ACTIVE` | A fresh authorization is transmitted | Run deadline-budgeted read/evaluate/write cycles | Intent expiry/idle moves to `ARMED_IDLE`; disarm moves to `DISARMED`; any unsafe condition moves to `INHIBITED` |
| `INHIBITED` | Safety, quality, identity, conflict, timing, or control failure | Revoke generation immediately; bounded zero attempt; then observe without nonzero renewal | Non-latching cause requires stable qualified samples; latching cause additionally requires authorized acknowledgement; recovery returns to `DISARMED`, never directly to `ACTIVE` |
| `STOPPING` | SIGTERM, service shutdown, actor replacement, or fatal supervisor failure | Reject intents; revoke generation; bounded zero attempt; close connection; flush terminal records | Terminal for this actor instance |

Arming is a control-plane action with separate authorization from setting power. It expires on process restart, actor restart, disconnect, identity change, or inhibit. Fleet-wide arming is expanded into independently audited per-unit actions; partial failure is visible.

### 8.1 Inhibit classes

| Class | Examples | Recovery |
|---|---|---|
| Transient | One timeout, brief site-meter staleness while already idle | Required fresh stable samples; remains disarmed |
| Qualified | SOC jump, incomplete cells, objective mismatch, renewal deadline miss | Stable observation window plus operator-visible reason; remains disarmed |
| Latched | Critical BMS/PCS/DCDC fault, EE/EEPROM calibration warning per policy, identity mismatch, external writer, repeated timing failure | Cause cleared, stable window, and privileged operator acknowledgement |
| Fatal configuration | Invalid or absent safety policy, unsupported profile, duplicate identity, impossible time budget | Correct configuration and restart |

Policies may make a class stricter, never weaker than the implementation's non-overridable invariants.

## 9. Continuous safety kernel

The safety kernel is deterministic and side-effect free. Its complete input is a versioned policy, lifecycle snapshot, winning intent, current unit observation, site observation, prior control state, and monotonic cycle time. Its output is an authorization, derated authorization, idle decision, or inhibit decision with machine-readable reasons.

Rules are composed in a fixed pipeline:

1. Configuration and commissioning validity.
2. Lifecycle and arming eligibility.
3. Identity, profile, connection epoch, and single-writer checks.
4. Required telemetry presence, age, completeness, finiteness, plausibility, and consistency.
5. Fault and warning policy.
6. SOC, cell voltage/spread, temperature, and battery capability constraints.
7. Direction-specific dynamic power limits.
8. Ramp and reversal constraints.
9. Site import/export and phase constraints.
10. Authorization deadline and cycle-budget checks.

Each rule returns structured evidence and cannot perform I/O. Denial and derating reasons survive through API presentation and audit. Safety rules use composition and ordered policies; subclassing a base rule merely to share syntax is prohibited.

The kernel evaluates every cycle even when the requested target has not changed. A previously safe intent does not imply a currently safe renewal.

## 10. Unit actor and transport boundary

There is one actor, one mailbox, and one connection state machine per unit. The actor is the only component holding a gateway channel reference. It serializes reads and writes itself; exposing a lock around a shared client is not equivalent and is prohibited.

The actor owns:

- Connect, identity probe, reconnect backoff, and close.
- Current connection epoch and command generation.
- Essential and optional polling schedules.
- Deadline-aware execution and cancellation.
- Authorization validation immediately before write.
- Request/response correlation and acknowledgement validation.
- Local communication metrics and lifecycle events.

The actor does not own:

- Intent priority.
- Safety policy decisions.
- Fleet allocation.
- User authentication.
- Schedule interpretation.
- Tariff or forecast logic.

The Waveshare adapter exposes an RTU-over-TCP transport profile explicitly. Ordinary Modbus TCP/MBAP and the vendor application's reverse-connected TCP mode are different profiles and cannot be selected accidentally. Protocol framing, CRC behavior, slave handling, address base, timeout, and response validation are covered by captured byte-level tests and pinned dependency behavior.

Connection loss does not use nested locks or recursive connection acquisition. Reconnect creates a new epoch, invalidates every old authorization, and returns through `OBSERVE_ONLY` and `DISARMED`.

## 11. Fleet coordination and degraded operation

The fleet coordinator consumes independent unit observations and one site observation. It never opens unit connections.

The default policy permits degraded operation only when all of these are true:

- The site intent explicitly permits a subset of units.
- The remaining units have verified identity, fresh telemetry, current dynamic limits, and are armed.
- Site and phase constraints can still be proven with fresh meter data.
- Allocation is recomputed rather than preserving stale per-unit values.
- The degraded decision is visible and audited.

Otherwise the site request resolves to idle. Quorum is a policy input, not an assumption that two of three units are always safe. A failed unit never causes the same requested watts to be copied to each survivor.

## 12. Ports and adapters

Ports describe capabilities in domain language and are narrow enough to prevent bypass.

### 12.1 Required outbound ports

| Port | Responsibility | Important constraint |
|---|---|---|
| Monotonic clock | Safety deadlines and ages | Testable; distinct from wall clock |
| Wall clock | UTC audit and timezone-aware schedules | Never establishes freshness |
| Unit channel factory | Create one channel for a verified transport profile | Available only to unit-actor composition |
| Identity registry | Bind logical unit to expected observed identity/profile | Mismatch fails closed |
| Intent repository | Transactional bounded-intent state and revisions | Manual intent not restored on restart |
| Schedule repository | Versioned timezone-aware schedules | Atomic updates and overlap validation |
| Audit journal | Durable append and correlated queries | Backpressure inhibits new authority |
| Telemetry repository | Current snapshots and configured history | Raw and decoded provenance retained |
| Site-meter port | Fresh import/export and phase observations | Missing required data inhibits constrained control |
| Provider ports | Normalized tariff, weather, PV, load, and forecasts | Advisory only; no actuation capability |
| Secret resolver | Retrieve credentials by reference | Secrets never enter configuration exports or logs |

### 12.2 Required inbound use cases

- Query fleet/unit status and telemetry.
- Submit, replace, or cancel a bounded intent.
- Arm, disarm, emergency-stop, and acknowledge an inhibit.
- Manage versioned schedules.
- Query audit and decision explanations.
- Query provider data and optimization proposals.
- Accept or reject a proposed optimization policy.

No inbound port exposes encoded power, arbitrary register access, sockets, driver instances, authorization construction, or debug modes.

## 13. Inheritance, composition, and reuse

DRY means one authoritative concept, not one universal base class. Deep inheritance would couple protocol details, safety rules, providers, and runtime lifecycle in ways that are difficult to reason about and test.

The design preference is:

- Immutable domain values for facts.
- Small structural ports for substitutable capabilities.
- Composition for safety rules, codecs, allocation policies, and adapters.
- Explicit delegation for cross-cutting concerns such as retries, metrics, and authentication.
- Data-driven protocol profiles only where evidence shows genuine variation.

Inheritance is justified only for a genuine family with a stable behavioral contract and shared lifecycle. Likely examples are optimization strategies or external data providers that share start/refresh/normalize/health semantics. Even there, a shallow abstract strategy/provider contract plus composed helpers is preferred. Unit actors, safety rules, repositories, and protocol layouts do not form inheritance hierarchies merely to maximize reuse.

The Liskov substitution test is mandatory for every subclass: callers must be able to use any subtype without learning subtype-specific safety or lifecycle exceptions.

## 14. Persistence and audit

### 14.1 Storage responsibilities

For the single-container deployment, SQLite in WAL mode on a dedicated persistent volume is the default operational store. It provides transactional configuration metadata, identity bindings, versioned schedules, intent history, lifecycle events, safety decisions, command attempts/results, user actions, and provider/optimizer provenance without introducing a second required service.

The storage adapter is replaceable with PostgreSQL through ports when multi-site scale or external analytics warrants it. High-volume long-term telemetry may be exported to a time-series system without changing the control core.

### 14.2 Durability and backpressure

A dedicated persistence task serializes writes. The control path awaits durable acceptance of authorization and effect records within its deadline budget. If the journal is unavailable, corrupt, out of space, or too slow, the kernel stops issuing nonzero authority. A bounded queue prevents unbounded memory growth.

Telemetry retention uses explicit raw, high-resolution, and downsampled periods. Retention failure does not erase current in-memory quality state, but sustained storage failure inhibits actuation because command auditing can no longer be guaranteed.

### 14.3 Restart semantics

Persisted data may reconstruct history, schedules, identity expectations, policy revisions, and acknowledgement records. It must never reconstruct:

- An armed state.
- A connection epoch or generation.
- A monotonic deadline.
- A manual/agent intent as active.
- A previously issued authorization.
- A prior unit's last nonzero target as desired state.

Every startup creates new epochs and begins at `BOOT`.

### 14.4 Audit shape

Audit records include event ID, UTC time, monotonic offset within the current process, process instance ID, unit, connection epoch, generation, cycle, principal/source, correlation/intent ID, policy and configuration revisions, observation sequences, decision reasons, requested and effective targets, request/response fingerprints, result, and lifecycle state.

Sensitive credentials and raw authentication tokens are never recorded. Access to audit data is itself audited.

## 15. Single-instance and competing-writer controls

The site admits one active controller instance.

- Startup acquires an exclusive operating-system lock in the persistent data directory before opening a gateway socket.
- The database records a unique process-instance ID and unclean prior shutdown for diagnosis; the database record alone is not the lock.
- Container deployment fixes replica count to one and prevents overlapping rolling updates.
- Configuration rejects duplicate unit IDs, endpoints, and expected identities.
- Network policy permits Modbus gateway access only from the controller host/container network where practical.
- The actor monitors validated objective echo, operating mode, and unexpected state changes. Evidence of an external writer increments generation and enters a latched inhibit.
- No second component inside the process receives unit-channel credentials.

Because the gateway protocol does not provide distributed fencing, network exclusivity and external-writer detection are safety requirements. A second controller is not made safe by application-level intent arbitration.

## 16. Authentication and authorization

All mutating APIs require authentication. Startup rejects a network-accessible control configuration with empty or default credentials.

### 16.1 Roles and scopes

| Role | Capabilities |
|---|---|
| Viewer | Read current and historical status |
| Operator | Submit bounded manual intents; arm/disarm; view decisions |
| Safety operator | Emergency stop and acknowledge permitted latched inhibits |
| Schedule manager | Create and activate versioned schedules |
| Automation client | Submit tightly scoped, low-TTL intents within unit and power limits |
| Administrator | Manage identities, policies, users, and provider configuration; cannot bypass safety |
| Auditor | Read audit history and exports |

Roles do not imply raw device access. High-impact operations require recent authentication and explicit confirmation. Emergency stop remains easy to invoke but difficult to clear.

### 16.2 API controls

- OIDC is preferred for people; separately issued, hashed, rotatable service credentials are used for automation.
- Tokens carry audience, expiry, scopes, and site identity.
- Mutations require idempotency keys and optimistic revisions.
- Browser sessions use secure, HTTP-only, same-site cookies and CSRF protection.
- Rate limits apply per principal and operation; repeated requests do not extend TTL beyond policy.
- TLS terminates at a trusted local reverse proxy or ingress. The application trusts forwarded identity only from configured proxies.
- CORS is deny-by-default and management access is limited to a trusted network or VPN.
- Secrets are mounted or resolved at runtime, never baked into the image or returned by configuration APIs.

### 16.3 Future MCP adapter

MCP is read-only by default. A write-enabled deployment requires a separate scoped credential and exposes only intent use cases, never transport or register tools. Tool results explain acceptance, current lifecycle, constraints, and expiry. An agent cannot arm a unit, clear a latched inhibit, alter safety policy, or silently renew an intent unless an explicit policy later authorizes that exact capability.

## 17. Provider and optimization extensibility

External data enters through normalized provider ports:

| Provider family | Normalized data |
|---|---|
| Tariff | Import/export price intervals, currency, market, publication time, quality |
| Weather | Irradiance, cloud, temperature, severe-weather flags, issue time |
| PV forecast | Power/energy quantiles by interval, model and confidence |
| Load forecast | Site and phase demand quantiles by interval, model and confidence |
| Site meter | Current import/export and per-phase power with monotonic freshness |

Provider adapters own authentication, rate limits, retries, caching, source timestamps, and normalization. Cached data retains original age and never becomes fresh merely because it was reread.

Optimization is a strategy family outside the control kernel. A strategy consumes immutable normalized forecasts, prices, battery constraints, operator goals, and uncertainty. It returns a versioned proposal with assumptions, confidence, expected cost, reserve margin, and bounded intent windows. The proposal follows ordinary arbitration and continuous safety checks.

Machine-learning models are versioned artifacts with recorded feature schema, training horizon, validation metrics, drift indicators, and fallback behavior. Forecast failure degrades to a deterministic schedule or idle according to policy; it never produces unrestricted control. Before automatic activation, strategies run in shadow mode and compare proposed actions with actual outcomes.

## 18. API and user-interface boundary

The web interface is a replaceable inbound adapter, not the system of record. React is a suitable default because it supports a mature component ecosystem, accessible data visualization, and a separately testable frontend, but the domain architecture does not depend on it.

The operator experience must make these distinct states visible:

- Connected versus controllable.
- Observed, disarmed, armed, active, inhibited, and stopping.
- Requested site power versus allocated unit power.
- Intent accepted versus authorized versus written versus measured.
- Telemetry age and quality, not merely the last numeric value.
- Current winner and suppressed intents.
- Active constraints, derating, warnings, faults, and inhibit recovery requirements.
- Command expiry and last successful renewal.
- Identity and protocol-profile verification.

Real-time updates use a read-only event stream or WebSocket. Every write remains a normal authenticated, idempotent API request. UI loss has no effect on the control loop, and UI reconnection cannot replay a stale action.

## 19. Runtime supervision and shutdown

Python 3.12 structured concurrency supervises critical tasks. The process contains one top-level runtime supervisor, a fleet coordinator, one unit-actor scope per unit, persistence, API, and provider scopes.

Failure boundaries are explicit:

- Unit transport failure inhibits and isolates that unit.
- Safety kernel, monotonic clock, configuration integrity, audit journal, singleton, or fleet-coordinator failure revokes authority globally and initiates controlled shutdown or global inhibit.
- API failure makes readiness false and may leave already authorized cycles running only until their ordinary intent TTL; no command gains a longer lease because the API failed.
- Optional provider failure invalidates its data and causes optimizer fallback; it does not crash direct observation.

### 19.1 Graceful shutdown

On SIGTERM or administrative shutdown:

1. Readiness becomes false and new mutations are rejected.
2. The runtime revokes all intents and arming state.
3. Every unit generation is incremented before task cancellation.
4. Actors stop issuing nonzero renewals.
5. Each actor independently attempts a validated zero write within a strict bounded deadline.
6. Actors close sockets and record terminal state.
7. Persistence flushes accepted audit events within the remaining grace period.
8. The process releases the singleton lock and exits.

Shutdown does not block indefinitely on an unreachable gateway. SIGKILL, host loss, event-loop stall, or network partition cannot execute this sequence; the commissioned hardware command-expiry behavior is therefore a mandatory independent fail-safe.

## 20. Health, metrics, and operations

Health has separate meanings:

| Signal | Meaning |
|---|---|
| Liveness | Process and supervisor are running; it does not claim batteries are safe to control |
| Service readiness | API, configuration, authentication, database, and singleton are operational |
| Control readiness | Per-unit identity, lifecycle, telemetry quality, timing budget, audit, and site inputs permit arming |
| Unit control health | Current cycle latency, authorization age, write result, and measured response for one unit |

Metrics include cycle and I/O latency histograms, deadline margin, renewal jitter, missed cycles, telemetry ages by field, quality counts, reconnects, CRC/framing/errors, intent winners, authorization/denial counts, derating, lifecycle transitions, audit latency, queue depth, and provider age. Logs are structured and correlated but never substitute for the audit journal.

Alerts distinguish observation loss, control inhibit, unexpected writer, storage pressure, repeated SOC discontinuity, calibration warnings, cell spread, temperature, objective mismatch, and device faults. Alerting cannot issue control commands.

## 21. Container and deployment architecture

The deliverable is one independently versioned image containing the Python service and built immutable web assets. The control process is PID 1 or is managed by an init that forwards signals correctly; the scheduler is not a background shell process.

Production defaults:

- Non-root user, read-only root filesystem, dropped Linux capabilities, no privilege escalation.
- Dedicated writable volume for the database, lock, and controlled exports.
- Configuration and secrets mounted read-only.
- Pinned Python and frontend dependencies with reproducible lockfiles and image digest.
- Bounded local logs and explicit telemetry retention.
- One replica, recreate deployment strategy, and a shutdown grace period exceeding the bounded stop sequence.
- Network policy separating the management API, gateway network, and provider egress.
- No direct public exposure; use trusted LAN/VPN and a TLS reverse proxy.
- Image SBOM, vulnerability scan, signed release artifact, and migration checks.

Container restart policy may restart observation, but it cannot rearm or restore manual control. Readiness remains false until storage, identity, baseline telemetry, and lifecycle checks complete.

## 22. Verification strategy

The architecture is implemented test-first, but tests must independently validate behavior rather than reproduce implementation assumptions.

### 22.1 Contract and property tests

- Signed power encoding rejects overflow and preserves charge/discharge direction.
- RTU-over-TCP frames, CRC, slave, address base, function, lengths, and acknowledgements match captured traces.
- Every decoded field has field-specific word order and scaling tests.
- Intent arbitration is deterministic under order changes, expiry, equal priority, replacement, and cancellation.
- Fleet allocation never exceeds requested, unit, site, phase, ramp, or apparent-power bounds.
- Non-finite, missing, stale, partial, jumping, and contradictory telemetry fails closed.

### 22.2 Stateful and timing tests

- Model-based tests cover every legal and illegal lifecycle transition.
- A fake monotonic clock proves expiry independently of wall-clock changes.
- Authorization is rejected after use, deadline, generation change, reconnect, inhibit, and stop.
- Cancellation is injected before and after every asynchronous boundary; old generations never write.
- Control cycle budget is tested under delayed reads, writes, audit commits, partial frames, exception responses, and reconnect storms.
- An event-loop stall stops renewals rather than extending authority.

### 22.3 Integration and system tests

- An independently authored gateway simulator validates byte-level behavior and fault injection.
- Golden traces come from the vendor application and controlled read-only/device tests, not from the same codec under test.
- One gateway may fail or hang without delaying the other two actors.
- Competing-writer and objective-echo mismatch enter latched inhibit.
- SIGTERM performs ordered revocation and bounded zero; SIGKILL relies on measured device expiry.
- Database full, corrupt, locked, or slow conditions fail closed.
- API authorization, CSRF, idempotency, rate limits, and role boundaries are verified.
- Schedule overlap, site timezone, daylight-saving behavior, cross-midnight windows, and restart semantics are verified.

### 22.4 Deployment gates

1. Protocol facts and safety thresholds have an evidence owner and source.
2. All writes remain disabled in read-only shadow mode.
3. Identity and telemetry are observed on all three live units over a representative period.
4. Exact power sign and watchdog expiry are established with an approved, low-power commissioning procedure.
5. Command expiry, reconnect, process death, and competing-writer behavior are measured.
6. Per-unit control is enabled one unit at a time under supervised limits.
7. Schedules run in shadow mode before automatic control.
8. Optimizers and agents remain advisory until separately accepted.

No test suite can establish “100% correctness.” Release decisions instead require traceable requirements, independent protocol evidence, model/property tests, fault injection, code review, and supervised commissioning.

## 23. Configuration governance

All configuration schemas reject unknown fields. Startup validates:

- Exactly three unique logical units for this deployment.
- Unique endpoints and expected device identities.
- Explicit transport and protocol profiles.
- Complete safety policy and authoritative threshold provenance.
- Valid positive durations and a feasible lease time budget.
- Site timezone and non-overlapping schedules.
- Authentication and trusted-proxy policy.
- Storage paths and available capacity.
- Provider credentials by secret reference.
- No write enablement for an inferred or conflicting protocol field.

Configuration changes are immutable revisions. Safety-affecting changes require privileged authorization, validation, audit, and a transition to `DISARMED`; they cannot alter an active cycle in place. Secrets and credentials are not configuration values exposed to the UI.

## 24. Architectural fitness rules

Automated checks must keep the design honest:

- Domain and application packages cannot import adapter or web modules.
- Transport construction appears only in runtime composition and unit-actor adapter wiring.
- No inbound adapter can import protocol write codecs.
- No class outside the control kernel can create `AuthorizedSetpoint`.
- No class outside a unit actor can hold a unit channel.
- Every mutating API maps to an inbound use case and authorization scope.
- Every safety-relevant observation has age and quality.
- Every nonzero write has an authorization, cycle, generation, intent, observation, and audit correlation.
- Cyclic imports and service locators fail the build.
- Subclass depth is bounded and every inheritance relationship has a documented substitutability reason.

Architecture tests complement review; they do not replace it.

## 25. Deferred decisions requiring separate ADRs

- Exact protocol profile and commissioned lease timing.
- Authoritative battery thresholds and warning classifications.
- Site-meter and per-phase control integration.
- Fleet allocation algorithm after telemetry characterization.
- OIDC provider and TLS deployment topology.
- Long-term telemetry backend and retention.
- Tariff provider, including a future wholesale-price integration.
- Forecast model lifecycle and optimizer objective function.
- Conditions, if any, for write-enabled MCP operation.
- Any maintenance or reactive-power capability.

Until resolved, each deferred capability remains read-only, advisory, or disabled.
