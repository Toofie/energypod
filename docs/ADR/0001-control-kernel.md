# ADR 0001: Monotonic, Continuously Revalidated Control Kernel

**Status:** Accepted for initial implementation  
**Date:** 2026-08-21  
**Decision owners:** EnergyPod Manager maintainers  
**Scope:** Control authority, per-unit concurrency, and actuation boundary

## Context

The site has three grid-connected EnergyPods reached through independent Waveshare Ethernet gateways. Historical applications repeatedly transmit an active-power request, which is consistent with a short-lived device command or watchdog. The precise expiry timing and fallback behavior have not yet been established by controlled measurement.

Prior implementations and draft designs had several unsafe properties:

- Safety was checked when a command was requested but not necessarily before each renewal.
- A driver-owned heartbeat could continue after the strategy, guard, UI, or scheduler failed.
- Polling, writes, reconnects, manual control, and scheduling could compete for the same device.
- Locks around shared clients admitted cancellation races and reconnect deadlocks.
- Missing telemetry was sometimes converted to zero and therefore treated as safe.
- Fleet requests had ambiguous semantics and could be multiplied across three units.
- API, scheduler, phase-balancing, and future agent paths could become competing writers.
- Process restart could resume automatic work without identity revalidation or explicit arming.

The architecture must support future tariff, weather, PV/load forecast, optimization, and MCP integrations. These increase the number of intent sources but must not increase the number of actuation paths.

The three most important failure cases are:

1. The decision-making task dies while a heartbeat task remains alive.
2. A stop or replacement races with an older in-flight renewal.
3. Telemetry becomes stale or implausible while an unchanged intent remains active.

## Decision drivers

- Nonzero output must stop naturally when current safety reasoning is unavailable.
- Each gateway needs an isolated failure, timing, and connection boundary.
- Intent sources must be extensible without receiving device authority.
- Timing must remain correct across wall-clock adjustments.
- Stop, cancellation, reconnect, and replacement must have deterministic ordering.
- Protocol uncertainty must not leak into domain or API semantics.
- The core must be testable with a fake clock and without network or database infrastructure.
- A single Docker container should be sufficient for the initial site deployment.
- The design must be understandable and auditable by future maintainers.

## Decision

Adopt a ports-and-adapters architecture centered on one deterministic safety/control kernel and one actor per EnergyPod.

The central invariant is:

> A per-unit actor is the sole owner of its gateway socket and may renew nonzero output only when it holds a fresh, monotonic `AuthorizedSetpoint` produced for that unit, connection epoch, command generation, and current control cycle after revalidation against current telemetry and policy.

### 1. One actor is the sole socket owner

Each configured unit has one actor that exclusively opens, reads, writes, reconnects, and closes its gateway connection. No API handler, scheduler, strategy, provider, repository, or fleet service receives the channel or a raw driver.

The actor serializes its protocol operations through its event loop and mailbox. A shared client protected by a lock is not an accepted substitute because ownership, cancellation, deadline scheduling, and generation fencing would remain ambiguous.

A unit actor maintains its own:

- Connection epoch.
- Command generation.
- Polling plan and cycle deadline.
- Connection and reconnect state.
- Request/response validation.
- Lifecycle state.

One unit's latency or failure cannot consume another actor's deadline budget.

### 2. Intents are requests, not authority

Operator, schedule, optimizer, site adapter, and future MCP clients submit bounded domain intents. An intent names direction and positive watts, target scope, principal/source, creation, expiry, revision, and reason. It cannot contain a register address, encoded signed value, command generation, internal authorization, or client-selected priority.

Safety constraints are gates and cannot be overridden by priority. Among otherwise eligible intents, fixed policy uses this source order:

1. Latched emergency stop, which can request idle only.
2. Authenticated bounded operator override.
3. Site/grid constraint, reduction-only by default.
4. Approved bounded automation or MCP intent.
5. Optimization strategy proposal.
6. Static schedule proposal.
7. Default idle.

Arbitration is deterministic and audited. A fleet intent is allocated into explicit per-unit targets; it is never sent independently in full to every selected unit.

### 3. Every nonzero renewal requires a new authorization

The safety kernel is the only component permitted to create `AuthorizedSetpoint`. It is deterministic and side-effect free. Every control cycle evaluates:

- Current lifecycle and arming state.
- Current winning intent and expiry.
- Unit identity, profile, connection epoch, and generation.
- Required telemetry age, completeness, quality, plausibility, and consistency.
- Current faults and warnings.
- SOC, cell voltage/spread, temperature, and dynamic power limits.
- Ramp, reversal, and static commissioning bounds.
- Fresh site import/export and phase constraints when enabled.
- The remaining cycle and device-renewal deadline budget.

The resulting authorization is bound to one unit, connection epoch, generation, control cycle, observation sequence, policy version, target, and monotonic deadline. It is single-use and never persisted.

An unchanged intent does not renew itself. The kernel must issue a new authorization from current facts on each cycle. If the kernel, arbiter, fleet coordinator, audit journal, clock, or supervision path cannot complete before the deadline, the actor does not send another nonzero request.

### 4. Monotonic time governs authority

All freshness, intent TTL, cycle, and authorization deadlines use an injected monotonic clock. UTC wall time is used only for schedule interpretation, records, and display.

Persisted monotonic values are never reused after process restart. Schedules are re-evaluated against current zoned wall time; manual and automation intents are not restored as active.

The exact control period and renewal margin are derived from a controlled measurement of the hardware expiry behavior. Write-enabled startup is refused when the configured worst-case essential-read, kernel, audit, write, and acknowledgement budget cannot fit inside that window with a conservative margin.

### 5. Generation fencing orders stop and replacement

Each event that invalidates control increments the unit generation before cancelling outstanding work:

- Winning-intent replacement or cancellation.
- Explicit idle, emergency stop, or disarm.
- Safety inhibit.
- Disconnect, reconnect, or identity/profile change.
- Actor replacement or process shutdown.

The actor verifies generation before encoding, immediately before a socket write, and before accepting the acknowledgement. An old operation that completes late cannot restore lifecycle or authority. Its outcome is diagnostic only.

Stop therefore follows this order:

1. Revoke intent and arming state.
2. Increment generation.
3. Prevent old mailbox work from becoming writable.
4. Stop nonzero renewals.
5. Attempt a zero write within a bounded deadline.
6. Close the socket and rely on commissioned device expiry if the zero cannot be confirmed.

The zero attempt improves shutdown behavior but is not the final safety mechanism.

### 6. Telemetry quality fails closed

Every safety-relevant observation carries a connection epoch, sequence, monotonic sample time, raw provenance, completeness, quality, and reason. Missing values are never replaced with zero. Stale, bad, missing, suspect, non-finite, partial, contradictory, or implausible required data prevents nonzero authorization.

SOC jumps, including the observed class of 20% to 90% changes, enter a quarantine/inhibit path until a configured stable-sample window and consistency checks pass. Cell spread and EE/EEPROM calibration warnings are first-class inputs. Their numerical thresholds and latching policy must be supported by authoritative evidence or documented commissioning; this ADR invents none.

### 7. Lifecycle gates actuation

Every unit uses this lifecycle:

```text
BOOT -> OBSERVE_ONLY -> DISARMED -> ARMED_IDLE -> ACTIVE
          |               |            |          |
          +---------------+------------+----------+--> INHIBITED
                                                         |
                          qualified recovery + ack -------+

Any nonterminal state ------------------------------> STOPPING
```

- `BOOT`: validate configuration and singleton ownership; no writes.
- `OBSERVE_ONLY`: connect read-only, verify identity/profile, and establish stable telemetry.
- `DISARMED`: healthy observation but no nonzero authority.
- `ARMED_IDLE`: explicit arming is current; no winning safe intent yet.
- `ACTIVE`: the actor has just transmitted under a current authorization and continues only through revalidation.
- `INHIBITED`: generation revoked; bounded zero attempt; no nonzero renewal.
- `STOPPING`: terminal revocation, bounded zero, close, and audit flush.

Startup, actor restart, and reconnect cannot return directly to `ARMED_IDLE` or `ACTIVE`. Recovery from `INHIBITED` returns to `DISARMED`; latching causes also require privileged acknowledgement.

### 8. Audit participates in authority

Authorization and write effects are correlated with unit, epoch, generation, cycle, intent, observation, policy, principal, and response. A transactional audit adapter durably accepts control records within the cycle budget. Storage backpressure, corruption, or exhaustion prevents further nonzero authorizations.

The initial single-container implementation uses SQLite/WAL on persistent storage behind an audit/repository port. The core is not coupled to SQLite and may move to PostgreSQL later.

### 9. One process instance controls the site

The runtime acquires an exclusive operating-system lock in the persistent data directory before opening gateway sockets. Deployment fixes replicas to one and avoids overlapping updates. Unit IDs, endpoints, and expected identities are unique and validated.

Where possible, network policy prevents other hosts from writing to the gateways. Validated objective echo or unexpected operating-mode changes are monitored as competing-writer signals and trigger a latched inhibit. Application-level locking cannot compensate for an independently connected second writer.

### 10. External providers and agents remain advisory

Tariff, weather, PV, load, and forecast adapters normalize timestamped, quality-bearing observations. Optimization strategies consume them and emit proposals or bounded intents. They do not receive transport, actor, kernel-construction, or arming capabilities.

MCP is read-only by default. A separately authorized write-enabled MCP adapter may submit narrowly scoped, low-TTL intents only. It cannot clear inhibits, arm units, modify policy, call maintenance modes, or renew physical commands directly.

### 11. Prefer composition over deep inheritance

The domain uses immutable values and small structural ports. Safety policies are composed ordered rules; unit behavior is an explicit state machine; transport framing and protocol layout are composed profiles/adapters.

Inheritance is reserved for genuine strategy or provider families that share a stable lifecycle and substitutable contract. Even those hierarchies remain shallow. There will be no universal device base class, safety-rule subclass tree, repository superclass with hidden behavior, or inheritance added solely to claim reuse.

This decision interprets DRY as one authoritative representation of each policy and contract. It does not merge concepts with different reasons to change.

## Architectural boundaries

```text
Inbound adapters
  web / REST / future MCP
          |
       intents
          v
Application core
  arbiter -> fleet allocation -> safety kernel
                                |
                       AuthorizedSetpoint
                                v
Unit actor A     Unit actor B     Unit actor C
    |                |                |
RTU/TCP A        RTU/TCP B        RTU/TCP C

Provider adapters -> normalized data -> strategy -> intent
Persistence adapters <- observations, decisions, effects
```

The dependency direction is inward. The application core defines ports; adapters implement them. Runtime composition is the only place that constructs concrete transports and provides them to actors.

## Alternatives considered

### A. Keep and improve the previous manager

**Rejected.** It contains common-mode safety assumptions, competing writers, fail-open telemetry, ambiguous scheduling semantics, and transport/concurrency defects. Retaining its classes would make historical accidents part of the new contract. Its protocol observations remain evidence and tests, not architecture.

### B. Request-time guard followed by a driver heartbeat

**Rejected.** This separates safety approval from renewal. The driver can continue a stale command after telemetry, strategy, or guard failure and creates a time-of-check/time-of-use gap.

### C. Shared Modbus client protected by a lock

**Rejected.** A lock serializes calls but does not establish one lifecycle owner, deadline priority, generation fencing, or safe reconnect/cancellation behavior. Nested connection paths can deadlock, and a slow poll can consume the heartbeat window.

### D. Direct API or scheduler writes

**Rejected.** Multiple writers defeat deterministic priority, audit correlation, continuous safety, and emergency-stop ordering. Every source submits an intent through one path.

### E. Persistent force/debug mode

**Rejected.** The firmware semantics, limits, termination, and calibration effects are unverified, and persistent control would defeat the known short-lived-command safety characteristic. Debug and maintenance modes remain outside production.

### F. One fleet loop with three clients

**Rejected.** One slow or disconnected gateway would delay the other units and complicate independent deadlines. Per-unit actors provide isolation; the fleet coordinator handles only deterministic allocation.

### G. Microservices and a distributed event bus

**Rejected for the initial site.** They add network partitions, distributed leases, operational dependencies, and more failure modes without improving a three-unit single-site safety boundary. Ports allow later extraction if measured scaling needs justify it.

### H. Actor decides safety internally

**Rejected.** It would mix protocol timing, connection lifecycle, policy, fleet constraints, and decision logic. The chosen actor performs last-mile authorization validation but receives decisions from a deterministic, independently testable kernel.

### I. Deep object-oriented inheritance hierarchy

**Rejected.** Device, safety, protocol, repository, and strategy concerns vary independently. Deep inheritance obscures dependencies and weakens substitutability. Composition plus narrow contracts is more extensible and testable; inheritance remains available where a real strategy/provider family proves it useful.

## Consequences

### Positive

- Loss of current reasoning naturally stops nonzero renewal.
- Socket ownership and cancellation ordering are unambiguous.
- One unit cannot monopolize another's transport deadline.
- Safety decisions are deterministic, explainable, and replayable from recorded inputs.
- New UI, schedule, tariff, optimizer, and agent sources reuse one bounded intent path.
- Protocol encoding and sign conventions remain at the adapter edge.
- Startup and reconnect behavior are conservative and testable.
- The core can be tested with fake clocks, pure snapshots, golden protocol traces, and state models.
- The initial deployment remains operationally simple: one process and one container.

### Negative and costs

- The control loop is more explicit and requires careful deadline budgeting.
- Durable audit acceptance adds latency and requires storage-health handling.
- Commissioning must measure watchdog timing and validate protocol facts before writes.
- Operators must explicitly arm after restart, reconnect, or inhibit recovery.
- A transient missing required telemetry value can stop control instead of guessing.
- Fleet allocation and site constraints require a reliable site meter before advanced control.
- The actor/kernel cycle protocol and generation tests demand substantial verification effort.
- SQLite write latency and retention need measurement; an external database may later be justified.

These costs are accepted because they reduce uncontrolled energy flow and make failure behavior explicit.

## Rejected shortcuts

- Hard-code a two-second lease because earlier software refreshed near that interval.
- Consider a successful socket call to be a successful device command.
- Convert missing SOC or temperature to zero.
- Reuse the last good telemetry indefinitely.
- Mask out-of-range integers into signed registers.
- Copy one fleet watt request to all three units.
- Infer unit identity from configured order.
- Auto-clear a safety inhibit when one sample returns to range.
- Auto-rearm after container restart.
- Let an agent or UI keep a heartbeat alive.
- Expose maintenance registers behind an “advanced” endpoint.

## Verification obligations

The decision is not considered implemented until tests demonstrate:

1. Only a unit actor can obtain a unit channel, and one channel exists per unit epoch.
2. Every nonzero write correlates to one unused, unexpired authorization for the same unit, epoch, generation, and cycle.
3. Wall-clock jumps do not extend telemetry, intent, or authorization freshness.
4. Kernel, audit, fleet, API, actor, and event-loop failures stop renewals within the commissioned device window.
5. An older generation cannot write after stop, replacement, reconnect, or inhibit at any injected cancellation point.
6. Missing, stale, malformed, non-finite, jumping, contradictory, and incomplete required telemetry denies authorization.
7. SOC jump, EE/EEPROM warning, cell data, temperature, dynamic limit, and objective mismatch paths are covered.
8. One gateway can hang, return partial frames, reject writes, or reconnect repeatedly without delaying the other two actors.
9. Fleet allocation respects request, unit, dynamic, site, phase, and ramp constraints and never multiplies power.
10. Intent priority, tie-breaking, expiry, emergency stop, and idempotency are deterministic.
11. SIGTERM orders revocation before cancellation and uses bounded zero attempts; SIGKILL behavior matches measured device expiry.
12. Restart creates new epochs, remains disarmed, and does not restore manual or agent intents.
13. Storage unavailable/full/slow behavior fails closed within the timing budget.
14. Objective acknowledgement and, where validated, readback mismatches inhibit control.
15. RTU-over-TCP frames match independent captured traffic at byte level.

Tests use an injected monotonic clock, model-based lifecycle testing, property testing, fault injection, and an independently authored gateway simulator. Simulator agreement alone is insufficient; golden captures and supervised live commissioning are required.

## Operational rollout

1. Implement read-only protocol profiles with evidence labels.
2. Run all three actors in `OBSERVE_ONLY` and verify identity, topology, telemetry quality, scaling, and timing.
3. Capture and compare byte-level traffic from the vendor application where safe.
4. Measure command sign, acknowledgement, objective echo, expiry distribution, and fallback using an approved low-power commissioning plan.
5. Set a conservative control-cycle budget and prove it under latency and failure injection.
6. Enable one unit at minimum bounded power under local supervision.
7. Validate stop, disconnect, restart, external-writer, and stale-telemetry behavior.
8. Commission the remaining units independently.
9. Run schedules, optimization, and agents in shadow mode before granting bounded intent authority.

Any mismatch between the deployed device and the protocol evidence returns the affected unit to `OBSERVE_ONLY` or `INHIBITED`; it is not patched around in the control kernel.

## Follow-up decisions

Separate ADRs are required for:

- Commissioned transport profile, register semantics, power sign, and watchdog timing.
- Battery safety thresholds and warning/fault recovery policy.
- Site meter, export limit, and per-phase constraint source.
- Fleet allocation strategy.
- Authentication provider and network/TLS topology.
- Persistence retention and migration beyond SQLite.
- Tariff/wholesale integration.
- Forecast and optimization strategy governance.
- Any write-enabled MCP capability.
- Any maintenance/debug or reactive-power capability.

Until each decision is accepted and commissioned, the related capability remains disabled or advisory.
