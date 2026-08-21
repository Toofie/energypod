# EnergyPod Manager — Product Roadmap

## Purpose

EnergyPod Manager will be a long-lived, locally hosted product for understanding and managing a three-unit, grid-connected home battery system. It will begin as a trustworthy monitoring tool and progress through guarded manual control, deterministic automation, external-data integration, and advisory optimization.

The roadmap deliberately earns control authority in stages. No phase may rely on an unverified protocol assumption, and later intelligence must never weaken the safety guarantees established by the control system.

## Product outcomes

The finished product should let a household:

- See the current condition, availability, and data quality of every battery at a glance.
- Understand energy movement between solar, home loads, batteries, and the grid.
- Safely request charging or discharging without exposing raw registers or maintenance commands.
- Create predictable plans around time-of-use tariffs.
- Incorporate weather, solar-production, and wholesale-price data without making the battery dependent on those services.
- Use forecasts and optimization to propose or execute bounded plans under explicit household policy.
- Offer a constrained MCP interface to a future agent without giving that agent direct access to battery communications.

## Non-negotiable product principles

1. **Observe before controlling.** Real-device reads, field meanings, units, scaling, sign conventions, identities, and data quality must be validated before any production write path is enabled.
2. **Safety remains local and authoritative.** The local control system is the only component allowed to issue or renew a physical battery command.
3. **Unknown means unavailable, not safe.** Missing, stale, contradictory, non-finite, or implausible safety data inhibits non-zero control.
4. **Every command expires.** Manual actions, schedules, optimizers, APIs, and agents submit time-bounded intents. They do not create persistent hardware overrides.
5. **One writer per battery.** All command sources pass through one arbiter and one guarded control path. UI, schedules, providers, optimizers, and agents never write to Modbus directly.
6. **Safe startup and recovery.** The product starts disarmed, does not restore manual commands after restart, and stops renewing commands when supervision is lost.
7. **Evidence is visible.** The UI and audit history explain what was requested, what was allowed, what was sent, and why a request was limited or rejected.
8. **Automation is progressively earned.** A capability advances from simulation to shadow mode, then supervised use, and only then optional automation.
9. **Simple, composable boundaries.** Reuse comes from clear contracts and independently replaceable components. Inheritance is used only where it represents a genuine substitutable family; composition is preferred for safety rules and runtime behaviour.
10. **Local operation does not depend on the cloud.** Loss of weather, tariff, forecasting, MCP, or other advisory services cannot prevent safe monitoring, stopping, or local fallback.

## The permanent safety boundary

The product has two deliberately unequal areas of responsibility.

| Core control and safety | Advisory optimization and integrations |
|---|---|
| Owns each battery connection and is the sole physical writer. | Never receives a transport, register, or driver reference. |
| Validates identity, protocol profile, telemetry freshness, limits, alarms, and command acknowledgements. | Consumes published observations, tariffs, forecasts, household policy, and historical data. |
| Arbitrates competing intents and applies priorities, expiry, ramp limits, site limits, and per-unit limits. | Produces recommendations or bounded, expiring intents. |
| Renews an authorized command only while current conditions remain safe. | Cannot renew a hardware command or override an inhibit. |
| Boots disarmed and owns stop, inhibit, recovery, and shutdown behaviour. | May fail, restart, disconnect, or produce no result without affecting the control system's fail-safe behaviour. |
| Records the authoritative decision and actuation audit trail. | Records inputs, model/version details, confidence, rationale, and proposed plans. |

The control boundary is permanent. Phase 5 does not replace Phase 2 safety; it becomes another untrusted source of intent governed by it.

## Delivery model and promotion gates

Each phase is a shippable product state, not merely a development milestone. A phase is promoted only when its acceptance evidence is retained and reviewable.

Every control-related capability follows this progression:

1. Protocol or behaviour evidence is documented.
2. Deterministic tests and failure cases are defined independently of the implementation.
3. The capability is exercised against a simulator and recorded protocol traces.
4. It runs against the live system in observe-only or shadow mode.
5. A human reviews discrepancies and explicitly approves limited activation.
6. Operational evidence is gathered before limits or autonomy are expanded.

Debug, calibration, fixing-SOC, circulation, capacity-verification, passive-voltage, and other maintenance functions are excluded from the normal product control path. They may be considered later as a separate, locally enabled service procedure only after vendor behaviour, termination conditions, and hazards are independently established.

## Phase 1 — Observe-only validated telemetry

### Goal

Establish a trusted, useful view of all batteries without any ability to write to them.

### Product scope

- Connect independently to each configured WaveShare gateway using an explicitly selected and validated transport profile.
- Pin a human-readable unit name to verified device identity rather than relying on list order or IP address alone.
- Read the protocol conservatively and classify every field as confirmed, provisional, unsupported, or invalid.
- Publish immutable observations with capture time, monotonic age, source unit, sequence, quality, and decoding profile.
- Surface system state, state of charge, state of health, battery-side power, available charge/discharge limits, temperatures, cell voltages, imbalance, alarms, warnings, communications status, and energy counters only where field evidence supports them.
- Detect and clearly flag stale data, gaps, implausible SOC jumps, incomplete cell arrays, conflicting subsystem values, unexpected layouts, duplicate identities, and decoding uncertainty.
- Record raw-read provenance and decoded observations sufficiently to reproduce and investigate discrepancies without recording secrets.
- Keep each battery isolated so a slow or unavailable gateway cannot block the other units.
- Provide separate product health states for application availability, telemetry readiness, and per-unit confidence.
- Package the observe-only product for local Docker deployment with persistent configuration and history.

### Explicit exclusions

- No Modbus writes of any kind.
- No manual control, schedules, optimization, MCP mutation, or maintenance modes.
- No inferred value presented as measured data.
- No claim that a warning is harmless merely because the battery continues to operate.

### Exit criteria

- The production image is technically incapable of issuing a write in observe-only mode.
- Every displayed field has an evidence reference, unit, scaling rule, validity rule, and test coverage.
- Byte-level transport and decoding tests match independent traces or safely captured live reads.
- All three units complete a sustained shadow-observation period with documented availability and discrepancy results.
- SOC jumps, the reported cell-voltage imbalance, EE/EEPROM calibration warnings, stale data, and partial responses are represented accurately in the UI and history.
- Independent review finds no path from an external interface to a battery write.

## Phase 2 — Manually armed, guarded dispatch

### Goal

Allow an authenticated person to request a limited charge, discharge, or stop action while a deterministic local safety system continuously decides whether it may be renewed.

### Product scope

- Add a single authoritative intent arbiter and one supervised control owner for each battery.
- Boot in `DISARMED` and require verified identity, valid protocol profile, fresh stable telemetry, healthy control supervision, and deliberate human arming before non-zero requests are eligible.
- Present control as direction plus positive power, duration, and selected units; signed register encoding remains internal.
- Give every arm session and action a short expiry. Restart, logout, loss of supervision, stale observations, or expiry revokes authority.
- Re-evaluate the full safety policy before every command renewal, including telemetry freshness, SOC plausibility, cell voltage and imbalance, temperature, active faults and warnings, dynamic device limits, static household limits, ramp rates, and apparent-power constraints where applicable.
- Apply per-unit and site-wide import/export limits after allocation and before actuation.
- Enforce command generations so a stopped or replaced request cannot write again.
- Verify acknowledgements and observable response; identify conflicting writers, unexpected modes, or command/readback disagreement and inhibit control.
- Support an emergency stop that outranks every other intent and cannot be overwritten by a later manual or automated request.
- Provide clear reason codes for allowed, reduced, rejected, expired, and inhibited actions.
- Use role-based, scoped authentication and require secure deployment configuration before network-accessible control can start.
- Maintain a durable audit trail of arming, requests, decisions, limits, transmissions, acknowledgements, revocations, and operator identity.

### Explicit exclusions

- No automatic schedules or price-based dispatch.
- No persistent override intended to defeat the battery's native fallback behaviour.
- No raw-register console, public maintenance actions, reactive-power control, or unverified force/debug modes.
- No external service or agent with write authority.

### Exit criteria

- Loss of the UI, API, controller task, process, container, network, or fresh telemetry stops renewal and leaves the device to its validated fail-safe behaviour.
- Stop and request replacement remain correct under cancellation, timeout, reconnect, delayed response, and stale-message races.
- Competing command sources cannot bypass arbitration or create more than one physical writer.
- Limits fail closed for missing, stale, contradictory, impossible, or jumping observations.
- Low-power live acceptance tests confirm direction, magnitude, renewal timing, expiry, acknowledgement, and fallback independently for each unit.
- A documented incident and recovery procedure has been rehearsed before routine use.

## Phase 3 — Deterministic tariffs and schedules

### Goal

Provide predictable, explainable automation for known tariff periods and household rules without machine learning or external data dependencies.

### Product scope

- Model tariff periods, import/export rates, weekdays, holidays, timezone, daylight-saving behaviour, and effective dates explicitly.
- Let a household create charging, discharging, reserve, and idle windows with selected units, limits, priorities, and valid date ranges.
- Validate overlaps, gaps, cross-midnight windows, unavailable units, contradictory objectives, and site-limit consequences before activation.
- Compile schedules into a versioned plan that can be previewed as a day timeline with estimated cost implications.
- Convert each active schedule item into a short-lived intent governed by the same arbiter and live safety kernel as manual control.
- Define deterministic precedence: emergency stop and safety inhibit first, then an active manual session, then the approved schedule, then idle.
- Support pause, one-off skip, temporary override, rollback to a previous schedule version, and a clear next-action display.
- Keep schedule editing separate from activation. Material changes require validation and explicit publication.
- Record planned versus allowed versus delivered power and explain deviations.

### Explicit exclusions

- No weather-driven, PV-driven, wholesale-price, probabilistic, or model-generated control.
- No silent plan rewriting after publication.
- No schedule restoring a manual command or bypassing startup disarm policy.

### Exit criteria

- Timezone, daylight-saving, cross-midnight, overlap, restart, clock-change, and missed-cycle scenarios are deterministic and tested.
- Direction, target units, power, duration, and priority survive the complete path from editor to physical intent.
- A simulated year of calendar cases produces no ambiguous active window.
- Shadow-mode comparisons and supervised operation show that the timeline, audit history, and delivered behaviour agree.

## Phase 4 — Weather, PV, and Amber provider adapters

### Goal

Create a reliable external-data layer that enriches decisions while remaining operationally separate from battery control.

### Product scope

- Introduce provider contracts for weather forecasts, measured and forecast PV production, retail tariffs, and Amber wholesale price forecasts and actuals.
- Keep provider-specific authentication, schemas, rate limits, retries, and licensing concerns inside replaceable adapters.
- Normalize provider data into versioned internal records with source, issue time, applicable interval, retrieval time, quality, confidence, and expiry.
- Preserve raw provider payload references for audit and troubleshooting where terms permit.
- Cache recent valid data, distinguish forecast from actual, detect gaps and revisions, and show provider health without fabricating continuity.
- Allow provider comparison and manual import so the product is not locked to one weather, inverter, PV, or tariff source.
- Add notifications and advisory insights such as an approaching high-price interval or unusually low solar forecast.
- Permit deterministic rules to use external data only after explicit household configuration and only through bounded intents; stale or unavailable provider data selects a documented conservative fallback.

### Explicit exclusions

- Provider adapters cannot call the control driver or renew battery commands.
- External forecasts are never treated as battery safety telemetry.
- No optimizer or agent receives credentials beyond the minimum provider scope it needs.
- No automatic dispatch is enabled merely by connecting a provider.

### Exit criteria

- Contract tests cover provider schema changes, rate limits, authentication failures, stale forecasts, revisions, duplicate intervals, missing intervals, and timezone conversion.
- Loss or corruption of every provider leaves local monitoring, stopping, and guarded control fully functional.
- Users can see exactly which source and issue time informed each insight or deterministic decision.
- Amber and other price feeds can be replayed from recorded datasets without contacting the live provider.

## Phase 5 — Forecasting, optimization, and MCP agent

### Goal

Forecast household energy needs and recommend cost-effective battery plans, with optional bounded execution and a carefully constrained agent interface.

### Product scope

- Forecast household load and PV production with uncertainty bands, horizon-specific accuracy, model version, training-data window, and drift indicators.
- Evaluate plans against tariffs or wholesale prices, battery reserve policy, expected solar, household demand, conversion losses, degradation cost assumptions, export constraints, and uncertainty.
- Use rolling-horizon optimization to propose explicit charge, discharge, reserve, and idle intervals rather than issuing direct real-time writes.
- Compare the proposed plan with deterministic baselines such as idle, fixed tariff scheduling, and self-consumption-first operation.
- Show expected cost, savings range, reserve risk, grid import/export, battery cycling, assumptions, and the reason for each planned action.
- Begin in recommendation-only mode, advance to shadow planning, then require approval per plan. Optional policy-approved automation is considered only after measured performance and safety evidence meet defined thresholds.
- Re-plan when meaningful inputs change, but rate-limit changes, preserve a minimum plan commitment where safe, and expose every revision.
- Provide an MCP server that is read-only by default for status, history, forecasts, plans, prices, and explanations.
- Represent MCP control requests as authenticated, scoped, idempotent, expiring intents. The agent cannot arm the system, access maintenance functions, change hard safety limits, reach the transport, or suppress an inhibit.
- Give agent actions separate permissions, budgets, rate limits, audit identity, and optional human approval requirements.
- Include a deterministic fallback plan whenever forecasting, optimization, MCP, or model services are unavailable.

### Explicit exclusions

- No autonomous learning may alter the safety kernel, protocol profile, hard limits, permissions, or command-renewal behaviour.
- No opaque model output is directly translated into a register write.
- No claim of savings without a measured baseline and uncertainty-aware evaluation.
- No agent may reinterpret missing data as permission to act.

### Exit criteria

- Forecasts are back-tested across seasons and reported against simple baselines using predeclared metrics.
- Optimization constraints are independently checked, and every proposed plan can be reproduced from recorded inputs, policy, and model/version identifiers.
- Shadow-mode evaluation demonstrates savings or another chosen objective without increasing safety events or violating reserve and site constraints.
- Adversarial tests confirm that prompt injection, excessive requests, stale context, replayed requests, compromised providers, and model failure cannot bypass control policy.
- A user can disable optimization and MCP instantly while retaining Phases 1–3.

## Non-technical UI information architecture

The interface should feel like a calm household energy product, not an engineering console. It should lead with plain language, reveal technical detail progressively, and make safe state and data confidence unmistakable.

### Primary navigation

#### Home

The default view answers five questions immediately:

- Is the system safe and connected?
- What is powering the home right now?
- How full are the batteries?
- What will the system do next?
- Is anything limiting normal operation?

The page includes a live energy-flow illustration, combined battery reserve, current solar/home/grid/battery figures, today's cost or savings, the next planned action, and a prominent but non-alarming status banner. An observe-only, disarmed, armed, active, limited, or inhibited badge is always visible.

#### Batteries

A fleet view shows MID, RHS, and LHS as named units without assuming identity from screen order. Each card shows charge level, current direction and power, availability, temperature range, cell spread, data age, and warnings.

Selecting a unit opens:

- **Summary:** condition, power, limits, communications, and recent trend.
- **Cells:** a readable cell-voltage distribution, highest/lowest cells, imbalance trend, temperatures, and data completeness.
- **Events:** active and historical warnings, faults, recoveries, and plain-English explanations.
- **Details:** advanced measurements, evidence status, and protocol diagnostics for expert troubleshooting.

#### Energy Flow

This view explains how energy moves through the property now and over time. It provides a live flow, power and energy charts, daily totals, grid import/export, solar use, and battery contribution. Missing or estimated intervals are visibly different from measured data.

#### Plans

Plans contains the household's control experience:

- **Now:** current request, allowed action, actual response, remaining time, and stop control.
- **Schedule:** a visual day/week timeline for deterministic tariff plans.
- **Upcoming:** the next actions and the conditions that could prevent them.
- **Proposals:** optimizer recommendations awaiting review in Phase 5.
- **Plan history:** published versions, overrides, skips, and outcomes.

Manual controls appear only when Phase 2 is enabled and the user has permission. Arming is a deliberate, time-limited step with a concise readiness checklist. Charge and discharge use separate, unmistakable actions; the interface previews expected grid effect, units involved, power limit, and expiry before confirmation.

#### Insights

Insights explains rather than controls. It includes tariff periods, prices, weather and solar outlook, load/PV forecasts, opportunities, forecast confidence, and plan comparisons. Recommendations link to their evidence and assumptions. Provider or forecast problems appear here without being confused with battery faults.

#### Activity

A unified, filterable timeline records measurements of interest, connectivity changes, warnings and faults, arming, requests, safety decisions, schedules, provider updates, plan revisions, agent activity, and outcomes. Every control entry answers: who or what requested it, what the safety system decided, what happened, and why.

#### Settings

Settings is grouped by household concepts:

- **System:** units, gateway connectivity, identity, and observe-only status.
- **Safety:** visible policy and limits, with hard limits protected from ordinary users and agents.
- **Tariffs:** rates, periods, timezone, and effective dates.
- **Data sources:** weather, solar, inverter, and Amber connections with health and permissions.
- **Automation:** schedule, optimizer, approval, and fallback policies.
- **Access:** users, roles, API credentials, MCP scopes, and sessions.
- **Notifications:** severity, channel, quiet hours, and escalation.
- **Data and support:** retention, export, diagnostics bundle, software version, and evidence status.

### Interaction and visual standards

- Use familiar terms first, with technical units and protocol details available on demand.
- Never rely on colour alone. Combine colour with icons, labels, and concise status text.
- Reserve red for actions or conditions requiring immediate attention; use neutral presentation for normal idle and observe-only states.
- Show the age and confidence of important data near the value, especially when it is stale, estimated, provisional, or incomplete.
- Distinguish **requested**, **allowed**, and **actual** power so a limited action is never presented as fully delivered.
- Make stop available from every active-control screen, but protect arming and material policy changes with deliberate confirmation.
- Avoid celebratory or punitive language around energy use. Explain cost, comfort, resilience, and battery impact neutrally.
- Support desktop, tablet, and mobile layouts; keyboard navigation; screen readers; reduced motion; high contrast; and local date, time, currency, and energy units.
- Preserve a clear empty, loading, disconnected, stale, partial, and error state for every data-driven view.

## Cross-phase product requirements

### Safety and resilience

- Safety decisions are deterministic, versioned, explainable, and independently testable.
- Every asynchronous boundary is tested for cancellation, delay, duplication, reordering, and shutdown.
- One unit's failure degrades that unit explicitly without hiding fleet consequences or blocking healthy units.
- Health reporting distinguishes a responsive web page from a healthy observation loop and from control readiness.
- Container restart never silently resumes a manual action or grants control authority.

### Security and privacy

- Default deployment is local, least-privilege, and deny-by-default for control.
- Credentials are supplied at runtime, scoped, rotatable, and excluded from images, logs, exports, and source control.
- Read, plan, arm, dispatch, administer, and maintain are separate permissions.
- Externally supplied text and agent content are untrusted data and cannot modify policy or executable configuration.
- Audit records identify human, schedule, optimizer, API, and agent actors distinctly.

### Data and explainability

- Measured, decoded, derived, forecast, optimized, and manually entered values remain distinguishable.
- Units, timestamps, timezone, provenance, validity, and retention are defined for every stored data type.
- A decision can be reconstructed from the observation snapshot, active intent, policy version, software version, and advisory inputs used at that moment.
- Model and provider upgrades are evaluated against retained datasets before promotion.

### Quality and lifecycle

- Requirements and safety invariants are reviewed before implementation tests are accepted.
- Test authors, implementers, and reviewers challenge one another's assumptions and use independent evidence where practical.
- Simulators are not treated as protocol truth; captured traces and controlled live observations provide independent oracles.
- Releases include migration, rollback, backup, restore, upgrade, and graceful-shutdown validation.
- Dependency and container updates are pinned, reviewed, scanned, and exercised through the same acceptance suite.

## Scope sequence summary

| Phase | User value | Control authority | External dependency | Promotion evidence |
|---|---|---|---|---|
| 1. Observe | Trusted battery visibility | None | None | Validated telemetry and sustained shadow observation |
| 2. Guarded manual | Safe, deliberate charge/discharge requests | Human-armed, expiring intents | None | Failure testing and low-power live validation |
| 3. Deterministic plans | Predictable tariff scheduling | Published schedules under the same guard | None | Calendar simulation and supervised operation |
| 4. Providers | Weather, PV, and wholesale context | No direct authority; bounded rules only | Replaceable data providers | Replay, outage, schema, and provenance validation |
| 5. Optimize and agent | Forecasted, cost-aware plans and controlled automation | Advisory by default; scoped expiring intents only | Optional models and MCP clients | Back-testing, shadow evaluation, adversarial review, and explicit promotion |

## Definition of long-term success

EnergyPod Manager succeeds when it remains understandable and safe during imperfect conditions: a battery disappears, a cell value is stale, SOC jumps unexpectedly, a warning appears, a provider changes its API, a price forecast is wrong, an optimizer crashes, or an agent makes a poor request. In every case, the product should preserve trustworthy observation, explain the degradation, retain human stopping authority, and ensure that only the local deterministic safety system can authorize continued battery actuation.
