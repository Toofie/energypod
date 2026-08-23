# EnergyPod continuity and recovery ledger

Last updated: 2026-08-24 (Australia/Brisbane)

## Purpose

This is the canonical, continuously maintained handoff for the EnergyPod
rewrite. It is deliberately sufficient for a new Codex task or engineer to
resume work if all conversational context is lost. Read this file completely
before acting, then follow the referenced authoritative documents. Update it
whenever reality changes; do not merely append optimistic status.

## Mission and authorized scope

Build a clean-slate, production-quality, Docker-hosted platform for monitoring
and safely controlling three BYD Energy Pod systems through Waveshare Ethernet
gateways. The system will provide a polished non-technical web interface, a
guarded REST API, and eventually a guarded MCP surface. It must support future
tariff, weather, solar, load-forecasting, and optimization providers without
placing advisory or AI components inside the safety authority.

Earlier applications have no compatibility status. They are evidence only.
The user explicitly requested a ground-up redesign and authorized replacing the
earlier project contents. Do not import old architecture merely to save time.
Challenge every design decision. Prefer composition and small explicit
interfaces; use inheritance only where a stable substitutable family genuinely
exists. “More inheritance” is not itself a quality target.

No live hardware interaction is authorized by the build request. Do not scan,
connect, poll, or write the real units. Build and test against deterministic
fakes and a simulator. Any later live commissioning requires a separate,
explicit authorization and starts observe-only.

## Evidence locations and provenance

- Current clean project: `C:\Users\vagrant\Downloads\EnergyPod\pod-manager`
- Vendor executable, resources, and logs: `C:\Users\vagrant\Downloads\EnergyPod`
- Decompiled application source: `C:\Users\vagrant\Downloads\EnergyPod_RE\src`
  (the previously suggested `EnergyPod\_RE` path does not exist)
- Prior user attempts: `C:\Users\vagrant\Downloads\modbus`, especially
  `C:\Users\vagrant\Downloads\modbus\byd`
- BYD product evidence:
  `https://cdn.enfsolar.com/z/pp/rs5tv0cf91/60015260b3443.pdf`

The hardware is clearly BYD Energy Pod. The decompiled program is strongly
BYD-specific: it contains BYD user roles, `MiniESapp`, and EnergyPod/MINI2
concepts, and the BYD literature says Energy Pod derives from first-generation
MiniES. The binary is unsigned and its assembly publisher metadata is not
credible proof of origin. Describe it as “BYD-specific service software of
unverified publisher provenance,” not as authenticated BYD source code.

## Authoritative design documents

Read these in this order when resuming:

1. `docs/CONTINUITY.md` — current state and exact next work.
2. `docs/ARCHITECTURE.md` — system decomposition and operational model.
3. `docs/ADR/0001-control-kernel.md` — safety authority and actor decision.
4. `docs/API_CONTRACTS.md` — domain and application contracts.
5. `docs/PROTOCOL_EVIDENCE.md` — facts, inferences, unknowns, and provenance.
6. `docs/TEST_STRATEGY.md` — verification taxonomy and quality gates.
7. `docs/ADR/0002-runtime-and-frameworks.md` — selected technology versions.
8. `docs/PRODUCT_ROADMAP.md` — staged product capabilities and UI direction.

If code, tests, and documents disagree, stop and classify the conflict. Safety
and protocol behavior must not be guessed. Update the contract/evidence first,
then tests, then implementation.

## Non-negotiable safety invariants

- One per-unit actor is the sole owner of each socket and serializes all I/O.
- Boot and restart are observe-only and disarmed. Active commands are never
  restored from persistence.
- Every nonzero heartbeat consumes one fresh, single-use, monotonic
  `AuthorizedSetpoint` that is continuously revalidated immediately before I/O.
- Authorization is generation-fenced. Late or cancelled work cannot regain
  authority after revocation, shutdown, reconnect, or actor replacement.
- Missing, stale, contradictory, malformed, or non-finite safety inputs fail
  closed to zero.
- Emergency revocation fences authority before any potentially blocking audit
  operation. Audit durability participates in granting authority, not in
  delaying revocation.
- Shutdown fences first, makes one bounded zero-command attempt, then closes the
  transport. It is idempotent and dominates queued renewal.
- Debug/service modes are excluded from ordinary product control.
- MCP is read-only by default. Any future mutation uses the same intent,
  authorization, auditing, arming, and policy path as the UI/API.
- Advisory systems (optimization, ML, prices, weather, agents) can propose
  intents but can never bypass deterministic safety authority.
- No secret is stored in source, logs, tests, or this ledger.

## Protocol facts and unresolved unknowns

Evidence-backed facts:

- RTU settings: 19200 baud, 8N1, slave/device ID 4, functions 03 and 16.
- User’s Waveshare integration uses RTU-over-TCP on port 4196.
- Vendor Ethernet mode is a different reverse-connected Modbus TCP/MBAP design:
  the PC listens on `192.168.1.10:507`.
- Handshake reads seven registers at `0x5000`; register 0 greater than 10 selects
  IoT layout. IoT string-enable/BIC counts are offsets 4/5; legacy are 2/6.
- Active/reactive command writes `[1, P, Q]` at `0x0200`; the vendor application
  refreshes nominally once per second and stops with `[1, 0, 0]`.
- Debug mode writes at `0x8000` and reads at `0x8100`, values 0 through 6. Labels
  exist, but their actual battery behavior is unproven and must not be exposed.
- Multi-register word order varies by field; see the evidence matrix. Never
  introduce a global 32-bit endian rule.
- PCS/DCDC currents scale by 0.01 A; system/BMS/BECU currents by 0.1 A.
- IoT cell packing is only safe for six BICs/60 cells; larger topology overlaps
  fixed addresses. Prior operational code suggests 59 cells, but topology must
  be commissioned and validated per unit.

Unknown or not sufficiently proven:

- The device’s exact watchdog timeout. Two-second command renewal is an
  operational observation, not a proved firmware constant. Timing is a
  commissioned setting constrained by an end-to-end timing budget.
- Positive/negative power direction is corroborated by prior operational code,
  not proved by the vendor UI. Public APIs use direction plus positive watts;
  signed register conversion lives only in the adapter.
- Engineering units for system-history counters; the vendor applies no visible
  scaling.
- Semantics and safety of all debug/service modes.

## Completed work

- The inherited rewrite was removed and `pod-manager` was initialized cleanly.
- Architecture, protocol evidence, product roadmap, test strategy, API
  contracts, and two ADRs were written.
- Runtime selected: Python 3.12, one process with structured async tasks,
  FastAPI 0.141.1, FastMCP 3.4.7, PyModbus 3.15.0, SQLite WAL, React 19.2.8,
  Vite 8.2.1, and pnpm 11.22.0. Simulator is a distinct deployment target.
- Package/test scaffolding and pinned `pyproject.toml` exist.
- Contract-first tests have been authored for domain models, fleet allocation,
  safety, intent arbitration, configuration, schedules, repositories, actor,
  control kernel, protocol codecs, register layouts, faults, and transport.
- At least 430 tests collected before the latest adversarial additions.
- Safety/arbiter review exposed contract contradictions. Contracts now specify
  server-assigned monotonic acceptance/revision, observation-owned monotonic
  capture/sequence, a separate fleet allocator, explicit current/previous
  observations, deterministic arbitration, and exact emergency-stop
  acknowledgement semantics.
- Configuration/schedule/repository review added full timing-budget checks,
  non-hardcoded topology, single-use/generation fencing, DST/cross-midnight and
  overlap cases, and SQLite transaction/CAS pressure tests.
- Actor/control-kernel adversarial review added tests for revocation-before-
  audit, cancellation-resistant late work, distinct equivalent capability
  reuse, expiry during lookup, mailbox ownership, cancellation propagation,
  shutdown races, and read preemption before heartbeat writes.
- Domain/allocation adversarial review removed signed-wire concepts and
  undocumented fairness from the domain, strengthened strictness and
  immutability coverage, and repaired property tests to fail cleanly in the red
  phase. Domain and authorization setpoints now use direction plus unsigned
  watts; signing and signed-16 validation exist only in the protocol adapter.
- Safety/arbiter contract repair now separates allocation from deterministic
  safety evaluation, passes current/previous observations explicitly, uses
  `DataQuality.BAD`, orders equal-priority intents by server acceptance
  revision, and requires scoped exact-ID emergency-stop acknowledgement plus
  repository removal.

Git commits currently known:

- `3381c1f test: define safety kernel and arbiter contracts`
- `f989dd7 test: define actor and control kernel safety contracts`
- `1552202 test: accept reviewed architecture and contract baseline`
- `89f483a feat: implement reviewed production round with adversarial hardening`

Most documentation and several test files are intentionally not committed yet;
inspect `git status` before changing anything. Never discard uncommitted work.

## Current work and agent state at this update

The first production implementation round is complete, independently reviewed,
and repaired. All five disjoint streams delivered working code (domain and
fleet allocation; configuration, schedules, and SQLite/memory repositories;
arbiter, safety kernel, authorization capability, control kernel, generation
coordinator, and audit factory; protocol codec, register maps, fault decoding,
and Waveshare transport; sole-owner actor), and the guarded REST/WebSocket/MCP
boundary with idempotency was delivered with its contract tests. Status:

- Full suite: 729 tests pass. Ruff lint and format checks pass. Mypy strict
  passes on all 32 source files. No test contacts hardware.
- Independent adversarial implementation review completed as a 36-agent
  workflow: eight reviewers (five disjoint streams plus cross-cutting
  docs-vs-code coherence and test-gaming detection), each P0/P1 finding then
  attacked by two independent skeptic lenses. Result: 0 P0, 11 confirmed P1,
  3 properly refuted, 18 P2 notes.
- All 11 confirmed P1 findings were repaired at the cause without weakening
  assertions; every repair carries a new regression test:
  1. control_kernel: audit-event creation crashed with KeyError when a
     selected unit had no observation. The audited observation basis is now
     every observation the kernel held for the cycle.
  2. control_kernel: the canonical audit-event factory is required at
     construction; authority can never be granted on a degraded audit trail.
  3. control_kernel: a cycle fenced between minting and publication now
     appends a durable zero-authorized audit record after revocation, so
     fencing never produces an audit gap.
  4. safety: cell-sequence monotonicity is non-decreasing. An unchanged cell
     sequence between control cycles is permitted (cells poll slower than the
     control rate); a regressed sequence still fails closed.
  5. protocol evidence: the vendor byte-cast of the enable mask was verified
     first-hand (MiniESapp.cs:1289,1299; SysControl.cs:729,1150) and recorded
     in PROTOCOL_EVIDENCE.md section 4. Implementation already matched the
     vendor and is unchanged; the legacy 8-BECU cap is evidence-backed.
  6. actor: INHIBITED was a dead-end clearable only by bad telemetry. Bad
     observations now preserve the inhibit; recovery requires the configured
     count of stable qualifying observations and returns to DISARMED, never
     directly to ACTIVE. Latched-class acknowledgement is not modeled yet.
  7. actor: shutdown makes its bounded zero attempt even when the mailbox
     owner task is already dead.
  8. rest: latched emergency stops are no longer evicted by the idempotency
     capacity bound; exact-id acknowledgement remains possible.
  9. rest: a failure after WebSocket accept sends the structured error
     envelope and a clean 1011 close.
  10. rest and mcp: audit reads require both observe and audit:read.
  11. rest/tests: the production testserver origin bypass was removed; the
      configured-origin path is now genuinely tested for accept and reject.
- Mutation testing (mutmut 2.4.5; mutmut 3.x fails on Windows because it
  imports the Unix-only resource module): allocation reached 6 survivors, all
  proved equivalent (pydantic tolerates missing @classmethod on validators,
  arbitrary_types_allowed is unused, one idle branch is unreachable behind
  prior invariants). Protocol codec survivors are enum-value strings, error
  message text, the slots flag, and an equivalent scale shortcut; no core
  encode/sign/word-order mutant survived. Safety kernel: 60 survivors remain,
  dominated by reason-code string vocabulary and isclose-guarded boundary
  equivalents; every decision/clamp/fleet-distribution/ramp/reactive/expiry/
  capability-floor logic mutant is killed. A reason-code pinning pass is the
  natural follow-up.
- 18 P2 findings from the review are deferred without safety impact; see the
  update log entry for the list.

Do not assume an agent is still running based on this list. Inspect current
agent/task status where available and inspect file diffs. Agent output is input
to the primary review, not automatic acceptance.

## Required development workflow

For each bounded component, follow this sequence:

1. Gather primary evidence and identify facts, inferences, and unknowns.
2. Define or amend the explicit contract and acceptance criteria.
3. Assign a test author with a bounded file scope and fresh context.
4. Assign a different adversarial reviewer to attack test gaps, races,
   vacuous assertions, false protocol assumptions, and contract conflicts.
5. Resolve conflicts in authoritative documents before accepting tests.
6. Have a different implementation agent implement only against accepted tests
   and contracts, with no real-hardware access.
7. Have another reviewer inspect implementation for correctness, concurrency,
   failure behavior, security, maintainability, and test gaming.
8. The primary agent performs an integration review and runs focused tests,
   then the full suite, static checks, type checks, and mutation tests where
   applicable.
9. Commit one coherent milestone and update this ledger with evidence, results,
   open risks, and the exact next step.

Tests are evidence, not proof of “100% correctness.” Never claim mathematical
certainty for the integrated system. The quality target is explicit contracts,
defense in depth, adversarial tests, deterministic simulation, static analysis,
mutation testing, and cautious observe-only commissioning.

Parallel work is allowed only for disjoint ownership. The primary agent owns
shared contracts and resolves integration conflicts. Never permit two agents to
edit the same production module concurrently.

## Exact next-work sequence

1. **Completed:** Collect and close the remaining test/review agents; inspect every diff.
2. **Completed:** Run collection again and run the intentionally red suite. Catalog failures
   by missing module versus malformed/contradictory contract.
3. **Completed:** Perform a primary coherence review across all tests and authoritative docs;
   resolve every remaining P0/P1 finding before implementation.
4. **Completed:** Commit the accepted documentation/test baseline.
5. **Completed:** Disjoint implementation rounds delivered for domain/allocation,
   configuration/schedules/repositories, arbiter/safety/authorization/kernel,
   protocol codec/register maps/faults/transport, and the sole-owner actor, plus
   the guarded REST/WebSocket/MCP boundary.
6. **Completed:** Integration: full suite green; every failure repaired at the cause.
7. **Completed:** Independent adversarial implementation review (36 agents, two-lens
   verification) and mutation testing; all 11 confirmed P1 findings repaired with
   regression tests; contracts updated to match.
8. **Completed:** REST/API authentication, WebSocket event delivery, audit views, and
   read-only-default MCP implemented against their contract tests and hardened by the
   review (scope composition, idempotency, origin policy, error envelope).
9. **Next:** Build the deterministic simulator and golden protocol/integration
   scenarios. Also run the reason-code pinning pass identified by mutation testing,
   and decide the latched-inhibit acknowledgement model for the actor.
10. Specify UI behavior/accessibility tests, then build the polished React UI
    and visually inspect all states and responsive layouts.
11. Add bootstrap/configuration UX (including `energypod.main`, which pyproject
    already declares but which does not exist yet), Docker image/Compose, health
    checks, migrations, backup/restore, operator documentation, and end-to-end tests.
12. Because Docker is currently unavailable, perform static Docker validation
    and explicitly defer an actual image build to an environment with Docker.

## Environment and restart procedure

Known environment state:

- Windows/PowerShell workspace.
- Python 3.12.10 was installed at
  `C:\Users\vagrant\AppData\Local\Programs\Python\Python312\python.exe` and a
  `.venv` was created. On 2026-08-21 the `.venv` was re-verified working
  (`.\.venv\Scripts\python.exe --version` -> 3.12.10); the earlier "missing
  base path" report did not reproduce. Still verify before use.
- Dependencies were installed and resolved with Starlette 1.6.0. Package index
  network access is available (pip installs succeed).
- PyModbus 3.15 uses `device_id=`, not legacy `slave=`, on requests.
- Node 24.19.0 exists; npm is broken due a missing user-level npm CLI. Corepack
  works and should invoke pinned pnpm.
- Docker is not installed in this environment.
- The repository is owned by a different Windows account (Codex sandbox). Git
  requires `git config --global --add safe.directory
  C:/Users/vagrant/Downloads/EnergyPod/pod-manager` before any git command.
- Mutation testing: mutmut 3.x fails on Windows (it imports the Unix-only
  `resource` module). Use mutmut 2.4.5 via
  `python -m mutmut run --paths-to-mutate=<module> --runner='<runner>'` with
  backslash paths in the runner, and set `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8`
  so console emoji do not crash the cp1252 codec. Results land in
  `.mutmut-cache` (git-ignored).

Safe fresh-context checks from the project root:

```powershell
Get-Content docs\CONTINUITY.md
git status --short
git log --oneline -10
Test-Path .venv\Scripts\python.exe
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pytest --collect-only -q
.\.venv\Scripts\python.exe -m ruff check .
corepack pnpm --version
```

In the Codex sandbox, launching the virtual-environment Python may require an
approved elevated command because it resolves to the base interpreter outside
the writable workspace. If the interpreter is genuinely absent, do not patch
the launcher. Reinstall/locate Python, recreate `.venv`, and reinstall from the
pinned project configuration.

## Completion gates

A component is not complete until contracts and tests agree, focused and full
tests pass, lint and types pass, reviewer findings are resolved, unsafe failures
are tested, and this ledger is current. The product is not ready for live
control until simulator/e2e suites pass, Docker is built in a capable
environment, security and recovery procedures are reviewed, and a separately
authorized observe-only commissioning validates register topology, scaling,
direction, freshness, and watchdog timing per physical unit.

## Update log

- 2026-08-24 (recovery): THE SELF-HEALING AWARENESS LAYER LANDED (b20058d,
  8f840ea, 6da8541, 7b491b3 + docs) — detection and honest surfacing of the
  fleet's self-recovery states, per the accepted research ladder (R4/R5
  "buildable now" + promoted P1 vi/iii; docs/POD_RECOVERY_RESEARCH.md).  The
  operator's principle implemented verbatim: the batteries self-heal
  (watchdog reversion, autonomy resumption, balancing, SOC re-estimation,
  stable-sample requalification — all trusted); the system's job is DETECTION
  of self-healing in progress (quiet), ambiguous behavior (evidence), and
  self-healing FAILURE (the firmware-wedge class that needed a physical
  power cycle).  PASSIVE by construction — audit facts + bus events only, no
  new write paths (0x8000/standby/write-scope excluded, separate
  authorization per the research).  Components:
  (1) ACTUATION-COHERENCE WATCHDOG (P1 vi): supervision peeks each unit's
  about-to-be-consumed authority before the heartbeats and compares the
  polled measured watts against the PRE-COMMAND baseline of the
  authorization episode; movement >= max(50% of authorized, the 150 W floor)
  is confidently actuating, below min(...) confidently still, the dead zone
  between refuses to conclude (tiny setpoints never false-trigger; a silent
  pod still alarms).  4 consecutive still cycles -> ONE actuation_incoherent
  audit + actuation.incoherent bus event per episode (re-arm: a coherent
  cycle or the authorization ending).  This catches the 22:11Z silent-loss
  and publish-fence signatures in ~6 s at the 1.5 s cadence.  Known pinned
  limit: mid-flight loss under an UNCHANGED command (pinned at a delivered
  level) is movement-indistinguishable from delivery and is NOT this
  watchdog's case.
  (2) OBJECTIVE ECHO READ-BACK (P1 iii): on the coherence trigger ONLY, one
  bounded fresh read of the served objective (0x1060+17/+18) through the
  owning actor's mailbox, classified against our last applied objective:
  echo_matches_write (pod-side wedge -> the R5 "physical restart required"
  hint), objective_not_served (mode/autonomy conflict -> the vendor-app
  Normal-Mode/Remote checklist hint), external_writer (rides the existing
  vocabulary), echo_unreadable (honest).  Audited as objective_echo with the
  read value.
  (3) UNRESPONSIVENESS CLASSIFIER (R4): derived per-unit health_state
  (NO latching; boot healthy-by-observation) with precedence unreachable
  (TCP connect fails, gateway class) > not_responding (K=3 consecutive read
  timeouts while the path connects — the wedge signature, carries the R5
  physical-restart remediation_hint) > foreign_writer (existing latch) >
  inhibited > actuation_incoherent > self_healing (requalifying_after_inhibit
  / cell_balancing >50 mV / autonomous_self_charge in band — quiet,
  informational) > healthy.  Transitions publish unit.health_changed.
  Snapshot units carry health_state/health_reasons/remediation_hint; /health
  carries a per-unit units block and control_readiness gains
  "{unit}:actuation_incoherent".
  (4) UNEXPECTED-AUTONOMY EVIDENCE RECORDER: measured watts outside
  expected_autonomy_band_w [-2600, 300] while NO intent claims the unit ->
  one unexpected_autonomy audit + unit.unexpected_autonomy bus payload per
  unit per 60 s carrying measured watts + SOC + the four mode words.  Pure
  evidence (mid's ±1.2 kHz oscillation is now timestamped); NOT a block, NOT
  an alarm tier, state stays healthy/self_healing.
  Config keys (policy, defaulted): actuation_coherence_cycles 4,
  actuation_coherence_min_movement_w 150, expected_autonomy_band_w
  [-2600, 300] (validated to span the self-charge region); live-write
  example documents all three.  Bus vocabulary additions:
  unit.health_changed, actuation.incoherent, unit.unexpected_autonomy.
  Contract-first, stepwise: config 4 red -> green; monitor family NEW (20
  red -> green); facade 5 red -> green (120 family); write-enabled replay 5
  red scenarios + 1 composition lifespan supervision-driving scenario ->
  green (19 family, 47 composition); the replay double models the wedge
  class (writes ACK, battery power never moves; readback reflect/zero/
  foreign).  Monitor semantics fixed en route (steady delivery must read
  coherent forever — baseline is pre-command for the whole episode).
  Families + neighbors green (~430 across recovery/composition/write-
  enabled/actor/facade/config/golden/simulator/e2e/event-bus/REST boundary);
  ruff + ruff format + MYPYPATH=src mypy strict clean (46 files).  CONSOLE
  FOLLOW-UP (web agent, feature-detected): consume unit.health_changed +
  the snapshot/health health_state fields; render remediation_hint where
  present; unexpected_autonomy is quiet-tier evidence, not an alarm.

- 2026-08-24 (backend polish): THREE SMALL ITEMS LANDED (f49c522, e5856cc,
  f7f1750). (1) SNAPSHOT PER-UNIT INTENT FIGURES (the console's precise
  proposal; closes the cold-load structural gap the web hook documents in
  web/src/app/useUnitIntentFigures.ts): the /api/v1/snapshot response now
  carries top-level `intent` = {requested_watts_by_unit: Record[str,int]|null,
  authorized_watts_by_unit: Record[str,int]|null, directions_by_unit:
  Record[str,str]|null} — null when no live intent claims any unit — composed
  across ALL active intents with the same per-unit winner-set arbitration a
  kernel cycle uses (fresh throwaway IntentArbiter per read; facade projects,
  never latches): each unit's requested figure is ITS winner's target
  (watts_by_unit) or its exact largest-remainder integer share of the winner's
  scalar total over its surviving scope; directions are per-unit winners';
  authorized mirrors the FRESHEST control_decision audit row (one bounded
  newest-first scan, limit 25; first decision in the window decides) filtered
  to live-claimed units so ended requests never linger — null on no-batch /
  no-decision / unreadable audit, never fabricated. Per-unit `requested_power`
  scalars unchanged (compatibility). Contract-first: 7 red facade-family
  tests (KeyError 'intent') → green; API_CONTRACTS snapshot section amended
  with the exact shape. Field names match the audit-row maps the web already
  parses. Also repaired 7 pre-existing red tests in test_facade_audit_content
  (the 2026-08-24 directions_by_unit field never reached that family's
  expected dicts). (2) The observe-only compose default
  max_cell_imbalance_v 0.050 → 0.500 (composition.py _control_policy) now
  matches the operator-directed live tier (a7297bf, config rev 4), pinned by
  a new composition test; absolute per-cell bounds unchanged. (3) The two
  forever-hanging startup tests in test_main_entry.py RETIMED off the hang:
  the start-report rework completes the lifespan before the first kernel
  tick, so the exploding-first-tick injection landed after
  lifespan.startup.complete; the harness now delivers the failure through the
  start-report path (one actor's start() raises in-window). Same pinned
  contract (nonzero exit, startup.failed, 'serving failed'/'supervision
  failed during startup'); both pass in ~0.6 s, the whole family green in
  2.3 s — THE FULL SUITE IS UNBLOCKED for future runs. Checks per change:
  scoped tests only, ruff + format + MYPYPATH=src mypy strict clean (45
  files).

- 2026-08-24 (concurrent per-unit operation): THE OPERATOR'S CONCURRENCY
  REQUIREMENT IMPLEMENTED — "I instructed MID to charge at 2,000 watts and RHS
  to discharge at 1,000 watts. Only one operation functions at a time. I
  require both to function concurrently whenever a battery request is made."
  The arbiter now selects a PER-UNIT WINNER SET (`arbitrate()` →
  CycleArbitration; `select()` stays as the single-winner equivalence anchor):
  each unit is claimed by the highest-priority live intent naming it, an
  intent's effective scope is its selection minus higher-priority claims, and
  a fully-claimed intent is simply not represented that cycle. One kernel tick
  composes every per-unit winner into ONE cycle: the allocator runs once per
  represented intent over its SURVIVING scope (`allocate_fleet_power` gained a
  `unit_ids` scope argument), the matcher binds every proposal to its unit's
  winning intent (identity AND direction) with per-intent watt bounds, one
  cycle_id/decision_id, one audit row (new optional `directions_by_unit`;
  multi-intent rows are cycle-level — intent_id null, correlation
  `cycle:<cycle_id>`, principals joined, dominant source), and one
  AuthorizationBatch whose capabilities carry their own unit's intent,
  revision, and direction. Safety: the fleet-wide `mixed_directions` rejection
  is REPLACED by per-unit coherence; fleet limits apply PER DIRECTION
  (charge subtotal under fleet_charge_limit_w, discharge under
  fleet_discharge_limit_w); every per-unit denial zeroes ONLY its unit (a
  denied unit is a zero-watt non-participant for its direction — the
  6abd869/d2163a5 doctrine extended to concurrency) while the other units
  still run; no-participant cycles still fail closed to whole-cycle rejection.
  Single-intent cycles are byte-identical to before (rows, fingerprints,
  behavior). Excess-charge adviser yield is now per unit (a claim on its own
  target withdraws it; a claim on a different battery no longer does; any live
  stop always does). Bus payloads extended: authorization.granted carries
  watts_by_unit + directions_by_unit; audit.appended carries directions_by_unit.
  REST schema unchanged (multiple POST /intents coexist and now run
  concurrently on disjoint scopes). API_CONTRACTS.md: arbiter section
  rewritten + new "Concurrent per-unit operation" subsection. Scoped families
  green (arbiter, safety, control_kernel, control_audit, composition,
  write_enabled_run incl. a two-gateway concurrent replay, service_facade,
  excess_charge, allocation, domain, rest + event contracts); ruff, format,
  and mypy strict clean. Commits 00ad829..edf5881.

- 2026-08-23 (excess-solar design): EXCESS-SOLAR ACCELERATED CHARGING DESIGNED,
  ACCEPTED PENDING IMPLEMENTATION. Contracts + red-phase tests + implementation
  plan landed for the operator's scenario (single-phase empty battery charging
  faster from fleet-wide surplus PV; billing net across phases). Authority
  posture: a new ADVISORY component only — `ExcessChargeAdviser`
  (src/energypod/application/excess_charge.py, to be built) submits ordinary
  short-TTL OPTIMIZER charge intents through an internal facade path; arbiter →
  allocator → SafetyKernel → per-unit authority are unchanged in role.
  Deterministic bound (allocator, one additional min() term on OPTIMIZER charge
  intents only): eligible = min(max_charge_from_export_w, max(0, floor(Σ
  grid_power_w) − export_headroom_margin_w)), collapsing to 0 unless EVERY
  fleet unit's grid evidence is finite, GOOD, and fresh; the kernel adds
  export_evidence_missing/bad/stale denials as defense in depth. BEAT-AUTONOMY
  HYSTERESIS pinned: intervene only above autonomy+min_acceleration_w (520+100
  W default), continue above autonomy+exit_hysteresis_w (570 W), otherwise
  leave the pod's own CT self-consumption alone — commanding less would slow
  charging. Hand-back is ALWAYS by non-renewal (TTL ≤ 300 s, default 10 s; the
  measured ~3.5-4 s watchdog returns autonomy); the adviser never writes
  registers, stop triples, or idle intents. OPERATOR PRECEDENCE PINNED from the
  2026-08-23 live verification (a manual console intent superseded an in-flight
  agent intent mid-window): the arbiter's manual > optimizer priority already
  displaces the adviser, and the adviser additionally withdraws while any
  higher-priority intent is live and re-posts only after expiry AND hysteresis
  re-qualifies. Audit attribution note: the trail distinguishes writers only by
  principal + source — the adviser is energypod:excess-adviser + optimizer vs
  operator:local + manual. NET-BILLING assumption is OPERATOR-CONFIRMED
  PENDING: a per-phase-billed site changes the economics, never the safety.
  Deliverables: API_CONTRACTS.md advisory section; PROTOCOL_EVIDENCE.md §4c
  (grid/load CT registers 0x1000+17, 0x1000+20, 0x0100+55 with vendor cites
  SysControl.cs:500/503/429 and live-capture values; §7's external-grid-sign
  "Unknown" row superseded by the live-proven negative=import); red tests in
  test_wire_decode.py (T-UNIT-WIRE-025..028; 0x1000 moved out of the unconsumed
  set), test_safety.py, test_composition.py, test_config.py
  (T-UNIT-CONFIG-014..020), test_live_composition.py, and the new
  test_excess_charge.py — 54 red cases, all failing for the intended missing
  contract (Observation advisory fields, ControlPolicy export triple,
  allocator now_mono port + export_bounded flag, config block, adviser module,
  tier promotion), 292 pre-existing tests in those files still green; ruff
  clean, scoped mypy clean on the added lines. docs/DESIGN_EXCESS_CHARGING.md
  is the ordered implementation plan (9 steps, test mapping, live protocol).
  Feature is config-gated OFF by default (`excess_charging` absent block).
  NEXT: implement per the plan (simulator first), then the separately
  authorized single-unit daytime trial — note POST /api/v1/arm requires
  {"unit_ids": [...], "confirmation": "ARM"} (bare unit_ids is 422).
- 2026-08-21: Created this durable ledger after architecture/protocol audits and
  the first contract-test/review cycles. Recorded the completed actor/kernel
  adversarial review and the virtual-environment discrepancy.
- 2026-08-21: Protocol contract-test authoring completed; independent review
  remains pending. No tests contacted hardware.
- 2026-08-21: Domain/allocation review and safety/arbiter repair completed.
  Reconciled the repository on `DataQuality.BAD` and direction plus unsigned
  domain watts. Full collection reached 547 tests after review additions.
- 2026-08-21: Independent protocol review completed. Added confirmed evidence
  for the prohibited `0x8001 / 0xFF00` clear-energy maintenance write while
  keeping it absent from production interfaces and tests.
- 2026-08-21: Accepted the reviewed red-phase baseline: 567 tests collect;
  execution fails cleanly on absent production modules; Ruff lint and format
  checks pass. Implementation may now begin in disjoint modules.
- 2026-08-21: Committed the baseline as `1552202` and dispatched five disjoint
  implementation streams: domain/allocation; config/schedule/persistence;
  arbiter/safety/kernel; protocol/transport; and sole-owner actor.
- 2026-08-23: PARTIAL ELIGIBILITY FIXED END TO END (6abd869). Every console
  fleet dispatch (charge AND discharge, all three pods) was rejected every
  1.5 s cycle with "Allowed: None yet" — the fleet never halted but never
  acted, which reads as a stall in the console. Root cause was NOT load or
  time-of-day: any fleet-wide intent is PARTIALLY eligible in practice (MID
  sits at the 10% discharge floor; earlier, two pods sat above the 95%
  charge ceiling), and the allocator correctly proposed zero watts for
  those units — but four layers coupled "active direction" to "positive
  watts" and each rejected the WHOLE batch: the domain UnitSetpoint
  validator (could not even REPRESENT a non-participating setpoint), the
  safety kernel (direction_power_mismatch, then zero_dynamic_capability,
  then per-unit deny reasons like soc_below_discharge_floor vetoing
  everyone), and the control kernel matcher (fixed first, cc2b58e) and
  mint gate (_eligible required positive watts for EVERY selected unit).
  Resolution: a zero-watt setpoint with an active direction is explicit
  NON-participation — no authority is ever minted for it, so representing
  and accepting it is safe. Domain now forbids only IDLE-with-nonzero;
  safety skips per-unit deny reasons for zero-watt proposals (their
  telemetry cannot endanger actuation; the kernel's evidence-coherence
  gate still requires observations for every selected unit before minting
  anything); the kernel mints authority per participating unit, and an
  intent participating nowhere still mints nothing (fail-closed).
  Single-unit verification (2026-08-22 evening) never tripped this because
  single-unit intents are either fully eligible or honestly refused — only
  fleet-wide dispatches with a mixed fleet do. Also fixed en route
  (f788701): a pymodbus client closed by resync never reconnects with
  reconnect_delay=0 — the transport now rebuilds its client from the
  factory, ending the multi-hour LHS telemetry-stale incident.
  Verification sweep dispatched (agent-driven, per unit: discharge
  500 W x 300 s then charge 500 W x 60 s); results to be appended.
  Process note from the operator: long test harness runs must never block
  the live development loop — full suites go to background/subagents.

- 2026-08-23 (later): FALSE STALL + SILENT-WEDGE HARDENING (f20602c). The
  operator reported data age ~160 s and climbing; an agent investigation
  (audit trail + py-spy + socket census) proved the running controller never
  stalled: the age spike was the 6abd869 deploy — the previous process was
  terminated at 21:33:51Z, the new one booted 56 s later, and the console
  kept counting age across the dark window. Telemetry has been continuous
  since boot (per-unit sequences in lockstep, ages 0.05-2.45 s), and the
  audit trail showed the per-unit authority path working live: mid holding
  an authorized 500 W discharge while rhs/lhs (97-98% SOC, above the charge
  ceiling) sat as zero-watt non-participants with safety_checks_passed.
  NOTE: rhs/lhs near-full means their CHARGE phases in verification sweeps
  will be correctly REFUSED (soc_above_charge_ceiling) — expected, not a
  defect. Real gap found and fixed: _run_fleet bounded polls but NOT
  actor.heartbeat_once() or kernel.tick() — a never-resolving write would
  wedge the fleet loop silently (no exception, watcher never fires, clean
  log, ages climb forever). Both now bounded by the control interval;
  /api/v1/health liveness carries process_instance_id + uptime_s so a
  console can distinguish deployment from stall (console-side use queued).
  Also killed a full-suite run wedged since 2026-08-22 13:44 that was
  competing with live ops (the earlier "stalled at 59%" incident); a
  CLEAN full suite passed in the background (exit 0) with 6abd869.

- 2026-08-23 (final): HALT FIX LIVE-PROVEN; FLEET VERIFIED. The all-zero
  allocation fix (d2163a5) was deployed by restart and PROVEN on hardware:
  the kernel survived TWO intent expiries — the exact phase transition that
  wedged the fleet at 21:50 — with 63 continuous control decisions, zero
  halts, clean log, single process throughout. mid's charge is verified by
  combined evidence: a 500 W charge intent authorized for 5 kernel cycles
  (audit-proven, safety_checks_passed) before the OPERATOR'S OWN console
  session superseded it with a manual 1000 W charge (measured -876 W), then
  clean return to autonomous PV self-charge (-637..-697 W; the operator
  confirmed pods self-charge from their own CTs whenever idle by day —
  daytime "idle" is NOT zero watts). The operator's concurrent console
  session also ran its own rhs discharge (+1121/+1205 W) successfully.
  Environment facts pinned (operator-confirmed): other applications monitor
  these batteries read-only by day and write only at night; daytime the pods
  self-manage solar charging; night writes will trip our arm-time
  external_writer preflight by design (coordinate, don't fight).
  Verification scorecard vs the operator's request — mid: discharge 5 min
  @ ~500 W VERIFIED, charge VERIFIED (authorized + measured under manual
  follow-up); rhs: discharge @ 1000 W VERIFIED (twice), charge correctly
  REFUSED at 97% SOC; lhs: discharge @ 1000 W VERIFIED, charge refused by
  BMS 0 W limit (the case that used to halt the fleet — now safe).
  API quirk recorded: POST /api/v1/arm requires {"unit_ids": [...],
  "confirmation": "ARM"} (bare unit_ids is 422). Console follow-ups queued:
  surface process_instance_id/uptime_s (instance swap vs stall), reconnect
  banner. Excess-solar accelerated charging: design stage dispatched
  (contracts + red tests; advisory-only, export-bounded, beat-autonomy
  hysteresis, operator-precedence pinned).

- 2026-08-23 (census): FOREIGN-WRITER INVESTIGATION CLOSED — day shift is
  clean. Zero external_writer inhibits across the entire durable audit
  (1,722 events, 6 process instances); exactly one socket per gateway, all
  owned by the controller; Docker is NOT INSTALLED on this host (the
  historic "−2500 W container" was a misattribution — it traces to
  repro_es_fence.py, an offline harness with fake transports that never
  touches hardware); the vendor app last ran 2024. Every anomaly
  reconciled: daytime charging-with-no-command = the pods' own CT/PV
  self-consumption (confirmed live: rhs flipped +1172 W discharging to
  −672 W charging the instant our objective cleared); discharge overshoot
  (+172 W on a +1000 W command) = pod firmware serving local load on top
  of our battery-power objective; the 2026-08-22 "undiagnosed" first-charge
  halt is now PINNED — two latched e-stops reason bounded_zero_failed:mid
  during shutdown; and the "no impact" evening was three stacked internal
  causes, all since fixed (245x direction_power_mismatch 0 W authorizations,
  stale single-use authorizations c75ec13, legitimate SOC refusals).
  OPEN BLIND SPOTS, honestly bounded: (1) this host is NAT'd — foreign
  LAN-host connections to the gateways are invisible locally; (2) the audit
  has a 7.3 h overnight gap (23:30→06:48 local) — night writing is
  UNVERIFIED either way. QUEUED: between-cycles foreign-objective detector
  (fold a 0x1060+17/+18 readback into the existing poll tiers, zero extra
  frames; alert foreign_objective_observed when disarmed/idle, escalate per
  policy); one deliberate overnight observe run to characterize the night
  window; day/night control partition agreement with the night-writing
  systems; dedicated VLAN for 192.168.1.11-13:4196 as the structural fix.

- 2026-08-23 (actuation): SILENT ACTUATION LOSS DIAGNOSED — vendor mode
  registers implicated. The operator's rhs+lhs discharge "stall" (22:11:28Z
  onward) was NOT a kernel halt, refusal, arbitration, or proxy fault: the
  kernel decided every ~2.3 s and AUTHORIZED the requested power for 6+
  minutes across three intents, but the authorized watts never physically
  happened — measured battery power never moved, with zero errors, zero
  transport faults, zero epoch changes. Two compounding defects:
  (1) GREEDY ALLOCATOR STARVATION (by design, needs fixing): a fleet intent
  over [lhs, rhs] fills lhs first (sorted order, lhs headroom >= request),
  so lhs takes everything and rhs receives a 0 W proposal — the operator's
  "both batteries" discharge silently halved. (2) SILENT WRITE/ APPLY
  FAILURE: every actor write is wrapped in suppress(Exception) + bounded
  wait_for (the f20602c hardening) — fail-closed but INVISIBLE BY
  CONSTRUCTION; a total actuation loss leaves no log, audit, or error.
  VENDOR RE CROSS-CHECK (the operator's directive paid off immediately):
  SysControl.cs:1442-1458 SendPQPower is byte-identical to our encoder,
  BUT the vendor refuses to send unless device debugMode == 0 "Nomal Mode"
  (status 0x8100, command 0x8000; MiniESapp.cs:2178) and SysControlMode is
  Remote (ctrlMode, GlobalFun.cs:204-212). Our controller NEVER writes
  0x8000 — a pod latched into debug/local mode silently ignores 0x0200
  objectives: the exact observed signature. Vendor renewal = re-send every
  1000 ms, NO vendor ramp (our first-cycle ~800/1580 W clamp is OUR ramp
  limiter from the pod's self-charge/float baseline — working correctly),
  no PQ readback. Post-restart anomaly: mid oscillates ±1.2 kW completely
  uncommanded (not the steady −520..−700 W self-charge) — consistent with
  device-local control active on mid; EXCLUDE mid from fleet intents until
  explained. Controller restarted cleanly (facade-1bfbb2c8, writemode13,
  src/ verified clean first; py-spy had shown no OS-level hang — per-cycle
  suppressed I/O failure, not a wedged thread). OPERATOR DISCRIMINATOR on
  re-dispatch: first cycle clamps (~750 rhs / ~1580 lhs), then either
  CLIMBS to setpoint within 2-4 cycles (controller-side, fixed by restart)
  or PINS (pods ignoring remote objectives — check each pod in the vendor
  MiniES app: Debug Mode "Nomal Mode", SysControlMode "Remote"; also check
  mid). Per-unit intents get full power to each unit; fleet intents
  starve the alphabetically-later unit until the allocator is fixed.
  FIX QUEUE (sequenced after the excess-charging implementation lands, to
  avoid two agents in src/): (i) audit+log every suppressed write failure
  (composition.py ~1440); (ii) per-unit attribution on control_decision
  rows; (iii) objective echo readback after 0x0200 writes (vendor
  precedent: DebugModeRead after SendSysCtrl); (iv) poll debugMode
  0x8100/ctrlMode in telemetry, expose in /units, refuse intents with an
  explicit reason when mode != Normal/Remote; (v) allocator starvation fix
  (capacity-aware split or per-unit targets + allocated_zero reason code);
  (vi) actuation-coherence watchdog (N cycles authorized>0 with no
  measured movement => alarm); (vii) distinct arm-refusal reason for
  already-armed vs actor_failure.

- 2026-08-23 (UX): REQUEST-CARD STALENESS + MISSING CANCEL. Operator
  reports a "requested discharge 2000 W" card that never clears; server
  truth at snapshot 2631: all units armed_idle, EMPTY request state — the
  intent had EXPIRED and the UI never applied the transition. Root
  suspicion (being traced field-by-field): the backend publishes NOTHING
  explicit at intent expiry (kernel decisions simply stop when the arbiter
  has no winner — silence, not an event), and the request panels neither
  poll nor bind, so the card can only ever be replaced by the NEXT
  request. Related known gap: raw telemetry ticks on Batteries but
  request/authorization info (requested/allowed/actual) is stale on ALL
  views during live intents — one defect family. QUEUED FEATURES (next
  contract cycle, after the excess-charging implementation lands):
  (1) POST /api/v1/intents/cancel (cancel the active intent by id or
  current: repository remove -> kernel no-winner -> revoke -> non-renewal
  stops power via device watchdog ~3.5-4 s; audit event required; per
  repo rules stopping is safety-positive so scope = dispatch, no
  interactive requirement) + a Stop/✕ button on the UI request card;
  (2) explicit intent lifecycle events (accepted/expired/cancelled) on
  the event bus so UIs never infer state from silence; (3) the
  request-panel live binding (console agent, in flight). Until cancel
  exists, the honest stop paths remain: TTL expiry (≤300 s), emergency
  stop (immediate, fleet-wide, latched + acknowledge), or disarm.

- 2026-08-23 (events): REQUEST-OBSERVABILITY ROOT CAUSE PINNED (tracer,
  field-level matrix). The WS event stream carries almost nothing the UI
  needs: observation.published = {unit_id, connection_epoch, sequence} ONLY
  (no readings); authority GRANT is store-only (composition.py ~574);
  intent EXPIRY is total silence (no intent.expired type exists — the
  kernel's no-winner revoke publishes only when `held` is non-empty, and
  held is always empty at expiry because capabilities are consumed/expired
  by then); audit.appended is trimmed to identity+result+reasons (no
  watts); intent.accepted IS published but was ignored by the views. REST
  /snapshot is always fresh; views read it once per connection plus a few
  event-triggered refetches. Hence: only ages/sequences moved on screen
  while requested/allowed/actual froze everywhere, and expired request
  cards linger forever. BACKEND FIX (queued behind the excess-charging
  implementation — same files): (1) publish `intent.expired` (fix the
  empty-held expiry path: publish from the kernel no-winner tick or the
  facade's intent TTL sweep); (2) publish an authority-grant event
  (symmetry with authorization.revoked); (3) decide: enrich
  observation.published with the reading vs republish snapshot frames on
  material change — the UI architecture assumes "snapshot once + events
  carry state", so events must carry the power figures. WEB FIX (console
  agent, in flight): consume intent.accepted now; refetchSnapshot must
  force a wire read (plane.refresh) and the plane must republish refreshed
  snapshots to subscribers; interim 2-3 s snapshot cadence-poll while
  live. Served bundle verified current — hard refresh is NOT the fix.
  Dispatch itself confirmed healthy through all of this: the operator's
  2000 W intent drove authorized 2000 W / measured 2197-2239 W before
  their own 22:58:50 disarm.

- 2026-08-23 (stop): EMERGENCY-STOP INCIDENT CLOSED — and a deeper bug
  found. The "unable to charge mid / stopped authorized" episode was a
  LATCHED EMERGENCY STOP pressed MANUALLY by the operator at 23:14:24Z
  (stop-5-3753.297000, 1.4 s after their own -2000 W charge intent went
  active — an accidental/experimental press), holding every cycle at
  stop_authorized until acknowledged. Released 23:20:37Z via the
  documented acknowledge (needs an Idempotency-Key header). CHARGING
  VERIFIED on release: the operator's still-live 2000 W intent immediately
  won arbitration — authorized -2000 W, measured -1824 W vs the ~-900 W
  self-charge baseline (~900 W deeper, tracking), clean TTL expiry back
  to armed_idle. First-ever stop_acknowledged audit event (Activity
  Acknowledgements section now has its first real entry). UX DEFECTS
  confirmed (release affordance renders ONLY in the browser session that
  pressed the stop — stopOutcome is session state; the latch banner is
  event-driven only, so a console loaded after the latch shows NO banner;
  Activity renders stop cycles as "Power allowed" instead of naming the
  stop): fix = expose latch state in snapshot/health (queue item already
  present), show banner + inline type-it-back acknowledge wherever the
  latch is announced, disable arm/disarm with the stop id named.
  PRIORITY BUG REOPENED — publish-fence generation desync: after the
  acknowledgment, a NEW intent (300 W probe) arbitrated authorized EVERY
  cycle while its watts NEVER dispatched: each cycle minted authority
  then publish was fenced 2-5 ms later (StaleGenerationError-at-publish
  path; memory.py fence rejects generation <= revoked_through), leaving
  two audit rows per cycle (granted + zero-authorized fenced record) and
  snapshot authorized_power null. Counterexample: intent-1->intent-2 at
  23:00Z published cleanly at generation 0 — failure is specific to the
  post-ack generation state (gen 4). This is the SAME authorized-but-
  nothing-happens signature as the 22:11Z "silent actuation loss" (which
  the restart "fixed" — a restart also resets generation state), so the
  mode-word theory is demoted to co-suspect: REPRO — new intent after a
  prior intent's expiry in the post-ack generation state; inspect the
  generation coordinator vs repository revoked_through reconciliation
  (why does the coordinator keep minting at a fenced generation instead
  of advancing?). Fix owner: src/ (queued at the head of the fix queue,
  behind the in-flight excess-charging implementation).

- 2026-08-23 (queue): DISPLAY PRECISION PASS (operator request, queued for
  the web agent slot — dispatch when the emergency-stop release-UX agent
  lands to avoid two agents in web/): every numeric metric in the console
  renders at most TWO decimal places — watts, SOC, voltages, temperatures,
  currents, ages, percentages; integers render as integers (500 W, not
  500.00 W); trailing zeros trimmed (46.5, not 46.50); display formatting
  only, underlying data untouched; shared formatter helper + per-view sweep
  + vitest pins.

- 2026-08-23 (P2 pass): DEFERRED-FINDINGS BACKLOG WORKED (branch
  worktree-agent-a1fdd73fb81836d8a, 9 commits, MERGE PENDING until the
  backend wave lands to avoid interleaving). FIXED: facade audit/publish
  content pins (~86 mutation survivors), EventBus payload aliasing,
  future/negative cursor handling, abandoned-subscription reclamation
  (weakrefs), non-mapping payload rejection, resync_required vocabulary
  collision, types-PyYAML pin, ControlPolicy zero SOC tolerances,
  sqlite set determinism. RESOLVED-BY: audit REST cursor, credential
  store, entry point, snapshot telemetry fields. DEFERRED-CONFLICT
  (deliberately untouched — overlap with main-tree rework): battery
  current signed decode + PCS status coherence (decode.py, excess CT
  rework), allocator signature drift, auth-repo revoke reason,
  kernel revoke masking. PROMOTED to P1 (adopt into the next contract
  cycle): (1) Impl-10 commit-then-raise — an arm that mutates the actor
  but fails audit leaves an armed unit with the caller told it failed
  and nothing published (unaudited state change in an audit-first
  system; fix with the facade rework); (2) simulator literal register
  image (~90 survivors — the simulator is now the reference model and a
  misplaced word would pass silently); (3) simulator blanket GOOD
  quality masking sentinel-decode gaps (62,535 W headroom artifact).
  docs/DEFERRED_FINDINGS.md is now a live status ledger per entry.

- 2026-08-23 (wave): THE FULL FIX QUEUE LANDED AND IS LIVE. The backend
  agent completed 14 commits, and the controller restarted onto them
  (writemode15, PID 10052). Publish-fence desync ROOT-CAUSED AND FIXED:
  the kernel's _revoke revoked only through the repository, never the
  coordinator — after any publish-then-revoke sequence the repository's
  revoked_through equaled the coordinator epoch and every later mint was
  fenced forever (granted+fenced audit pair per cycle, watts never
  dispatched, zero errors — the 22:11Z silent-loss signature). Fix:
  coordinator.advance_past() + repository revoked_through read; the kernel
  reconciles after every revocation AND before every mint; fencing itself
  untouched; red test reproduces the live failure exactly. Also landed:
  snapshot active_stops (always present, ISO latched_at) + per-unit
  inhibit fields — the console latch banner is now LIVE; intent.expired +
  authorization.granted events + watt figures on audit.appended (the
  console's feature-detected hooks light up); POST /api/v1/intents/cancel
  (audited, idempotent, bus transition; console Cancel button UI still to
  build); device-mode words (debug_mode 0x8100+0 promoted to control-rate
  core, ctrl/work/run modes) decoded + exposed + dispatch gating with
  device_debug_mode_active / device_mode_not_remote — positive evidence
  only, read-only; per-unit attribution on single-unit control_decision
  rows + emergency_stop:{stop_id} correlation on stop-held rows; every
  suppressed heartbeat failure audited (heartbeat_failed) and logged.
  All scoped families green (407 across the wave); ruff/mypy clean.
  MERGE of the P2 worktree branch dispatched (conflicts expected in
  rest.py/events.py). REMAINING NEXT: console Cancel button + per-request
  panels consuming the new events (drop the 2.5 s interim poll); excess-
  solar activation (fence fix + restart clear the technical gate — still
  awaiting the operator's net-billing confirmation and the 500 W trial
  cap); promoted-P1 contract cycle (Impl-10 commit-then-raise, simulator
  register image, blanket GOOD quality).

- 2026-08-23 (imbalance): CELL-IMBALANCE INCIDENT CLOSED — real drift, not
  phantom. The operator's blocked fleet discharges were LHS alone at 54 mV
  spread (4 mV over the 0.050 V gate) vetoing every fleet decision via the
  reason union; mechanism is top-of-charge balancing at 99% SOC after a day
  of heavy cycling (rhs/lhs charged to full; LHS BMS charge limit closed).
  Phantom ruled out four ways (stable spread across 9 cell windows, fresh
  advancing cell data, all-GOOD quality, kernel judges the exact projected
  tuple). Spreads: mid 22 / rhs 31 / lhs 54 mV, easing to 45 mV after the
  verification discharge. OPERATOR-DIRECTED POLICY (a7297bf, config rev 4):
  maximum_cell_imbalance_v 0.050 -> 0.500 (10x) — imbalance demoted to an
  early-warning tier; the ABSOLUTE per-cell bounds (2.80/3.65 V) remain the
  hard over/under-charge gates; console warning tier >50 mV landed (5453c6a:
  Batteries amber line + Home "what's limiting" factor). VERIFIED LIVE: 900 W
  fleet discharge, 24/24 cycles authorized safety_checks_passed, LHS went
  ACTIVE at ~900 W, clean TTL return; zero imbalance rejections on rev 4.
  The verification again exposed the GREEDY-ALLOCATOR starvation in fleet
  intents (lhs absorbed the entire 900 W; mid/rhs correctly drew 0 W —
  per-unit intents remain the workaround; the capacity-aware allocation fix
  stays at the head of the promoted queue). POST-MERGE NOTES queued: align
  the observe-only hardcoded default max_cell_imbalance_v (composition.py
  ~1881) with the relaxed live policy; extend per-unit attribution to
  fleet-level control_decision rows. Controller: PID 2860, writemode16,
  all units armed_idle, no faults, mid's ±1.2 kW self-charge oscillation
  continues (pre-existing, unexplained, day-scope observation).

- 2026-08-23 (per-unit watts): THE OPERATOR'S RULING IMPLEMENTED — "I asked
  for each setting to be one thousand, not a total of 1,000." One dispatch
  can now name a different watt target per battery, end to end
  (074bc7b, 1b1816b, a943478, 1145de3, bf771f0). THE CONTRACT: (1)
  PowerIntent.watts_by_unit — frozen map, keys == selected units, positive
  ints, sum == watts (the fleet total), None on IDLE; scalar watts remains
  fully supported and byte-identical in behavior. (2) Allocator: each target
  is that unit's own CAP — capacity-weighted share clamped to the target,
  clamping-released watts (chiefly targets above headroom) redistributed to
  units still below their own targets, bounded by gap+headroom+total;
  allocated == min(demand, sum of min(target, headroom)) exactly; every
  prior invariant kept (unit-set equality, zero-watt non-participation,
  export-cap composition, permutation-invariant ties, concentration
  boundary now per-target); over-static-cap targets clamp, never error —
  same doctrine as an over-cap scalar request. (3) Kernel matcher: a
  proposal may never exceed the unit's own target (total bound kept).
  (4) REST POST /api/v1/intents: exactly one of scalar watts or
  watts_by_unit (key set == unit_ids, ints > 0); 422 on both/neither/
  key-mismatch; facade derives watts = sum and carries the breakdown into
  the stored intent, the intent_accepted audit fact set, the
  intent.accepted payload, and the acceptance view. (5) Audit: every
  control_decision row now carries authorized_watts_by_unit (per-unit
  decision watts; null when no batch) and requested_watts_by_unit (the
  intent's targets; null for scalar) — the fleet-row opacity fix; both are
  optional fields, and the SQLite decode accepts pre-existing durable rows
  missing them (unknown keys still refused); audit.appended bus payloads
  carry both maps. SIMULATOR PROOF (deterministic, run twice identical;
  real kernel+allocator+safety+actors over 3 simulated pods): {lhs:1200,
  mid:400, rhs:800} with 2500 W static caps → authorized and measured
  exactly 1200/400/800 after the ramp settles (400→800→1200 on lhs);
  same targets with lhs capped at 1000 W → lhs 1000, others at targets,
  authorized_active_w 2200 of requested 2400 (200 unallocated, never
  forced); scalar 2400 → 800/800/800 capacity-weighted (unchanged);
  {2400,100,100} → exactly 2400/100/100 (no unit exceeds its own setting).
  Scoped families green: domain 132, allocation 60, kernel 27,
  control-audit 29, rest-contract 73, facade 104, facade-audit-content 7,
  repositories 23, composition 44, write-enabled-run 12, excess-charge 16
  — 550 passed across the scoped set (ruff + ruff format + mypy strict
  clean on every changed file). CONSOLE MIGRATION (web agent):
  the dispatch form should now POST watts_by_unit {unit: perBatteryW for
  each ticked unit} with NO watts key (mutually exclusive on the wire);
  scalar watts stays valid. NOTE: tests/golden
  test_healthy_dispatch_applies_exactly_the_authorized_setpoint fails at
  HEAD BEFORE this work (single-unit control_decision rows now carry
  unit_id per the earlier attribution feature; the golden pin still
  expects None) — pre-existing, untouched here, needs its own small fix.

- 2026-08-23 (allocator): GREEDY-ALLOCATION STARVATION FIXED AND
  LIVE-VERIFIED (4c41ed7, 58a03e6, d717deb). Root cause of "only one
  device powers": the operator's dispatches were single FLEET intents
  whose watts field is the fleet TOTAL — decoded from request
  fingerprints, all four were [lhs,mid,rhs] at 900-1000 W total, and the
  greedy first-fit gave lhs everything (2500 W headroom) with legal
  zero-watt proposals for mid/rhs. The operator believed watts was PER
  UNIT — a semantics mismatch now addressed two ways: (1) allocate_
  fleet_power is now capacity-weighted with a one-watt participation
  floor, exact integer largest-remainder split, permutation-invariant
  ties, and a pinned concentration boundary (demand below the
  participating-unit count concentrates; 300 W over 3 units splits
  ~100/100/100); ineligible units keep zero-watt non-participation;
  export-cap composes; exact-sum invariant kept; 11 new tests + a 250-
  case property test, family 290 green. (2) LIVE-VERIFIED: one 3000 W
  fleet discharge = 1000/1000/1000 per unit, 22 consecutive authorized
  cycles (one first-cycle ramp clamp), all three ACTIVE simultaneously,
  clean TTL return; controller PID 9036 / writemode17. PER-UNIT WATTS
  design sketch delivered (watts_by_unit on IntentRequest/PowerIntent,
  per-unit caps in the allocator, arbiter/safety unchanged; five open
  contract questions incl. console form shape and audit per-unit
  breakdown) — queued as its own contract cycle. ALSO queued (small):
  the console dispatch form must label watts as the FLEET TOTAL
  unmistakably (the operator's misread is the UX defect); fleet-level
  control_decision rows should carry per-unit allocation breakdowns
  (Phase 1 needed fingerprint inference to reconstruct requests).

- 2026-08-23 (merge-verified): P2 MERGE d097dcd DEFINITIVELY VERIFIED (582
  scoped tests green at HEAD; zero textual conflicts; no branch fix
  superseded — the wave's event work was in callers, not the bus).
  Worktree and branch cleaned up after merge. FOUND (pre-existing, proven
  at merge base): tests/unit/test_main_entry.py has TWO tests that hang
  forever (test_supervision_failure_through_the_lifespan_exits_nonzero,
  test_default_runner_exits_nonzero_when_supervision_fails_during_startup)
  — the start-report startup rework completes the lifespan before the
  first kernel tick, so their exploding-first-tick never lands in the
  startup window and their parked receive() never returns. These explain
  the earlier "full-suite stalls at 59%/wedges under load" reports (the
  families themselves run in ~3 s isolated). QUEUED FIX: retime the
  startup-window test harness (tick-once-before-ready or inject the
  failure via the start-report path). Cosmetic: d097dcd lacks the
  Co-Authored-By trailer (auto-committed clean merge; amending would
  rewrite 8 descendant commits from concurrent agents — left intact).

- 2026-08-23 (concurrency): CONCURRENT PER-BATTERY OPERATIONS DELIVERED
  AND LIVE-VERIFIED (00ad829..a0a81a7, 766 tests across 12 families). The
  single-winner arbiter became a PER-UNIT winner set: each battery runs
  its highest-priority active claimant (priority order unchanged; same-
  priority ties per unit by newest revision); one kernel cycle composes
  all winners into ONE batch/audit row (multi-intent rows are cycle-level
  with directions_by_unit; single-intent rows byte-identical to legacy);
  safety's fleet-wide mixed_directions rejection is replaced by per-unit
  coherence with PER-DIRECTION fleet limits, and every per-unit deny
  reason zeroes only its own unit (non-participation doctrine extended) —
  fail-closed when no unit can participate. The excess adviser yields per
  unit. LIVE-VERIFIED with the operator's exact scenario: mid charge 2000
  authorized/measured (~-1810) WHILE rhs discharge 1000 authorized/
  measured (~+1180) simultaneously for the full 60 s, 23 composed
  multi-direction decision rows, clean TTL return, no hazards. Simulator
  deterministic (SHA-pinned, twice) across overlap/stop/yield/regression
  scenarios. Controller restarted on the build (writemode20); boot
  disarmed — re-arm required. CONSOLE FOLLOW-UP QUEUED: N coexisting
  request cards (no card supersedes another's units), per-unit directions
  in cards from authorization.granted/audit maps, Activity rows with
  intent_id null correlate via cycle correlation id. Note: during
  verification the operator's own standing 1000 W/unit discharge was
  outranked per-unit by the newer verification intents — per-unit
  revision precedence working as designed.

- 2026-08-24 (matrix): COMBINATION MATRIX — DEFINITIVE LIVE RECORD: PASS
  11/11 (13 runs, 15 intents, 02:49-03:00Z, ~10.5 min, zero hazards/
  restarts/faults/inhibits; all cancels clean, watchdog return 4-8 s).
  Every 1000/1500/2000 W charge/discharge combination across 0/1/2/3
  units verified: baseline idle; single charge (mid) and single discharge
  x3 permutations; 2- and 3-unit charge (mid delivers, rhs/lhs honest
  per-unit soc_above_charge_ceiling refusals WITHOUT blocking mid —
  EXPECTED-REFUSED-PARTIAL, correct physics); 2- and 3-unit concurrent
  discharge; MIXED 1C+1D+1I and 2D+1C (opposite directions
  simultaneously in one decision row with per-unit directions);
  2000 W rate boundary; per-intent cancellation granularity (cancel one
  of two concurrent discharges -> that battery stops in ~4-8 s while the
  other continues untouched). Ramp limiter honestly audited as
  first-cycle power_clamped on large step-ups (resolves full within one
  cycle). Known-benign observations re-confirmed: discharge runs
  +150-250 W over command (firmware serving local load); mid charge
  magnitude 75-87% of command (solar self-charge offsetting within its
  uncommanded band); arm-while-armed returns the conflated actor_failure
  reason (pending item); snapshot requested_power still showed intent
  aggregate during the run — the per-unit snapshot figures build was
  held for this run and is now LIVE (controller restarted writemode22).
  Live SOC at run: mid 67 (both directions eligible), rhs/lhs 98.

- 2026-08-24 (console-complete): CONSOLE AT PARITY WITH THE BACKEND
  (0b81471, 57cda5f, c96dcfb; 272/272 web tests). Now view: ONE CARD PER
  ACTIVE REQUEST (keyed by intent id) with independent countdowns, per-
  card direction and per-battery figures, take-over display when a newer
  request claims one battery (older card's other batteries continue),
  and a live CANCEL button per card (POST /api/v1/intents/cancel
  {intent_id}; bus intent.cancelled retires the card). Batteries summary
  and the fleet banner render per-battery truth from the shared tracker
  maps — no surface anywhere stamps a fleet total on a per-battery card
  or derives Limited from a fleet-vs-unit comparison. The snapshot intent
  block is LIVE on the controller (verified serving; null when no active
  claimant) so cold loads are exact. QUEUED SMALL BACKEND NICETIES:
  intent.accepted payloads should carry expires_in_s (other operators'
  cards currently show "Remaining time: Not available"); audit.appended
  bus summaries should carry correlation_id so cards can join cycles by
  id instead of unit membership. Full test suite: 1473 PASSED in 17.6 s
  (exit 0) — the first complete clean run (unit+api+e2e+integration+golden
  +simulator); the previous era stalled at 59% for hours on the two since-
  fixed hanging startup tests.

- 2026-08-24 (soc): SOC-DISAGREEMENT STALESSNESS PROVEN; BMS-AUTHORITY
  LANDED AND LIVE A/B-VERIFIED (23494d8, f0967c7; 21 red -> 0, 1263 unit
  tests green). Mechanism definitively pinned: the system block 0x0100
  (SOC at +17) is served on CYCLE 1 ONLY (excluded even from the cold
  ring) and re-decodes from cache as quality GOOD forever — mid frozen
  at 67.0 across 112 sequences while BMS climbed 73->83; boot resync
  then freeze reproduced it exactly. Fix per operator direction: every
  SOC bound (floor/ceiling/jump) evaluates bms_soc_pct; soc_disagreement
  removed from deny reasons; informational soc_divergence_observed on
  the authorizing path only; Observation.authoritative_soc_pct = BMS
  (observations.py); the excess adviser follows it. Live A/B: pre-fix
  binary rejected at divergence 6.0; post-fix binary at divergence 8.0
  shows no soc_disagreement. READ-PLAN FOLLOW-UP QUEUED: promote
  0x0100+17 or stop decoding system SOC as an input entirely (console
  provenance only). NEW BLOCKER EXPOSED (armed into the running desync
  audit): the ARM-TIME SOLE-WRITER PREFLIGHT latches external_writer
  INHIBITED after any restart while a pod is AUTONOMOUSLY self-charging
  (its own firmware holds a nonzero PQ objective ~-2.27 kW; fresh
  process has no provenance). Race unwinnable: watchdog reverts a
  bounded zero in ~1.34 s vs ~5-6 s re-qualification; ~40 arm attempts
  failed over 25 min while mid self-charged at -2.2 kW throughout. The
  battery's OWN daytime autonomy is being misclassified as a foreign
  writer — remedy directions (autonomy-signature discrimination /
  operator-acknowledged takeover) under design in the audit. INTERIM:
  console control of mid may be blocked during deep self-charge until
  the remedy lands; rhs/lhs unaffected; mid itself is physically fine
  (charging via its own firmware).

- 2026-08-23 (afternoon): DESYNC-RESILIENCE WAVE IMPLEMENTED AND LIVE-
  VERIFIED — every class-B finding of docs/SYNC_RESILIENCE_AUDIT.md plus
  S1 and the ADD-1 arm blocker (B1 dce48c4, B2 6e96d60, B3 8da9bde,
  B5+cold-ring 27bb50a, B4 997ec49, S1 a1ba210, ADD-1 6c4254b; 30 red
  -> 0 across the wave, 1288 unit tests green, ruff/format/mypy clean
  each step; API_CONTRACTS amended per finding). B1: system SOC fully
  advisory (out of _required_quality/safety_data_complete/_SAFETY_
  QUALITY_FIELDS; informational system_soc_untrusted note; 12-key wire
  shape unchanged; quality_bms_soc_pct keeps the hard gate). B2: soc_jump
  deny removed -> informational soc_jump_observed on authorizing
  decisions only; floor/ceiling still bind the FRESH BMS figure so a
  jump across a bound still denies, safe direction. B3: first observation
  is its own baseline (previous_observation_missing gone; _eligible/
  _evidence_is_coherent accept a first observation; value checks
  unchanged) — no more one-cycle block per restart. B5: device_mode_
  not_remote performs ONE bounded fresh 0x0100 read through the owning
  actor at refusal time (cached word alone never refuses; fresh-confirmed
  non-Remote or two failed reads still refuse) AND 0x0100 moved to the
  cold ring first slot (cycle 8) with the read-plan docstring fixed —
  also the SOC-incident read-plan follow-up (system SOC semi-refreshes;
  BMS stands in as authoritative while unserved). B4: cell-derived denies
  (cell_voltage_low/high, cell_imbalance, cell_count_invalid) promote the
  0x5200 window into the next poll regardless of cycle%3 (one promoted
  cycle, 8->9 windows ~1.0 s inside the 1.5 s period/1.60 s renewal
  budget; config comment amended) — the 2026-08-23 LHS manual
  fresh-window confirmation automated; a persistent fresh violation
  re-denies and re-promotes. S1: the EE-calibration codes DETERMINED —
  the decoder generates {prefix}_{bit}, and the two signals are WARNING
  bits PCS_Warning0_1 / DCDC_Warning0_1 (PROTOCOL_EVIDENCE 9); the old
  human-name entries could never match AND the composition hard-wired
  blocking_warning_codes empty — doubly inert. Now configurable and
  wired (default empty). NOT ENABLED: both bits are STANDING-ACTIVE on
  all three pods (re-confirmed live this run) and vendor evidence does
  not establish severity, so enabling would deny every dispatch; the
  example config documents both codes with the format and the decision
  left to the operator. ADD-1 (the morning's ~40-failed-arm blocker),
  both layers: (a) autonomy-signature discrimination — policy key
  autonomous_charge_signature_max_w: 2500 (live-write example; rationale
  commented) classifies a nonzero not-written readback with negative P
  in band, Q zero as POD AUTONOMY: arm proceeds, our renewal replaces
  the pod's own (beat-autonomy); discharge/beyond-band/reactive still
  latch external_writer; (b) operator-acknowledged takeover — POST
  /api/v1/arm {"confirmation":"ARM","takeover":"ACKNOWLEDGE"} arms
  beyond-band objectives, audited (unit_armed reason arm_takeover_
  acknowledged / arm_pod_autonomy + objective_classification in the
  outcome and unit.armed payload); never persisted, boot observe-only.
  LIVE VERIFICATION (writemode24, PID 3432, one intent mid/charge/
  1000 W/60 s exactly): boot -> first snapshot mid DISARMED-qualified in
  ~8 s with ctrl_mode honestly absent (cycle 1 no longer pins 0x0100),
  then ctrl_mode_w=1/work_mode_w=6 served after the ring rotated (B5/
  ADD-2 live-confirmed); POST arm WHILE mid's firmware holds its charge
  objective -> 200 {"status":"armed","objective_classification":
  "pod_autonomy"} FIRST ATTEMPT (audit row ['armed','arm_pod_autonomy'])
  — the exact case that failed ~40 times this morning; intent accepted
  202; every one of this process's 25 control decisions rejected ONLY
  on zero_dynamic_capability — the pods are at 99% SOC with their BMS
  dynamic charge limit reading 0 W, the battery's own fresh word (the
  ~1000 W deeper trajectory was not physically available at 99%; no
  desync-class block appeared: 0 previous_observation_missing/
  soc_disagreement/soc_jump/quality_system_soc_pct this process, the
  newest soc_disagreement row is pre-fix-era); clean TTL return
  (requested -> idle/0); mid disarmed at handoff, all three pods
  disarmed + idle, controller left running on the new build. Residual:
  web/src/test/wire.ts still lists the retired reason codes (display
  vocabulary only; web untouched per scope); controller.simulate.yaml
  still carries the inert human-name blocking codes (S1 fixed only the
  live-write example per scope).

- 2026-08-22 (evening): CONTROL VERIFIED END TO END. Two live defects were
  found and fixed by in-process stall diagnostics with halt-evidence
  instrumentation: (1) the kernel treated a publish-time
  StaleGenerationError (the repository CAS legitimately fencing a cycle
  minted just before an actor fence) as a component failure, halting the
  fleet — now handled as a fenced cycle (5a27796); (2) independent
  kernel/actor/poll timers meant a fresh poll landed between mint and
  heartbeat with near certainty, so the sequence-bound single-use
  authorization was stale at consumption and no write ever happened —
  replaced by one fleet cycle (heartbeat -> bounded concurrent polls ->
  kernel tick) making consumption deterministic (c75ec13). The console's
  vite proxy also needed a restart after backend restarts (stale keep-alive
  sockets produced 502s). VERIFICATION SWEEP (operator-requested): each
  battery 500 W charge and discharge for one minute — mid/rhs/lhs charge
  flowed continuously (-346..-481 / -378..-477 / -216..-354 W measured),
  rhs/lhs discharge flowed continuously (+558..+690 / +608..+728 W), MID
  discharge correctly REFUSED at the 10% SOC floor (armed_idle, kernel
  rejection, house baseline continued) — the safety system working as
  designed. Follow-ups: the stalled-at-59% full-suite run under live load
  (0 failures; targeted families 100/100 green in isolation) should be
  re-run without the live controller competing; halt evidence should be
  surfaced through /api/v1/health.

- 2026-08-22: LIVE CONTROL COMMISSIONED. Write-enabled run mode landed
  contract-first (ea62398: policy qualification threshold, config gates for
  live-trial expiry evidence and the 1.5 s renewal envelope, arm-time
  external-writer preflight reading the served PQ objective — a foreign
  nonzero objective latches INHIBITED instead of fighting) and was then made
  live-capable by the tiered telemetry plan (fafc47c: the full 17-window
  read takes ~3.4 s over the 0.1 s gateway gap, longer than the renewal
  envelope, so refresh is tiered — control-rate core every cycle, cells on
  their own capture clock, cold ring with cache merging). Two controlled
  charges on MID verified end to end: dispatch -> kernel authority -> PQ
  writes at the commissioned cadence -> measured -423 W charging -> TTL
  expiry -> clean return to resting state. OPEN INCIDENT (und diagnose):
  the FIRST live charge attempt (120 s TTL) ended in a fleet-wide halt
  ~16 s in — all actors STOPPING, polls stopped, serving stayed up, no
  traceback in the log; the battery was verified SAFE by direct probe
  (objective P=0, watchdog had cleared it). Did not reproduce on the
  second trial (45 s TTL, clean). Follow-up: surface the halt reason
  through /api/v1/health or a diagnostics field, and watch the durable
  SQLite audit path under write load. The console (localhost:5173) shows
  live control: requested/authorized/measured separately, arm/disarm,
  dispatch with preview, emergency stop with acknowledgement.

- 2026-08-22: LIVE COMMISSIONING (observe-only) + telemetry surface. With the
  operator's explicit direct-hookup authorization: first live reads ever
  through the production transport verified the RTU-over-TCP framing,
  probed all three units (MID 6 BIC / RHS 5 BIC / LHS 6 BIC), captured the
  full IoT plan + follow-up blocks as the first E1 hardware vectors, and
  produced the validated field mapping (docs/evidence/). Run-mode observe-only
  then landed contract-first: wire decoder, FileCredentialStore, live
  composition with structural observe-only (stable-sample threshold 2^63-1 —
  no telemetry count can ever qualify a live unit), proven gateway timing
  (3.0 s timeout, 0.1 s inter-frame gap — back-to-back reads made the
  gateway answer out of order). The telemetry surface (69132b6) completes
  the picture: snapshot units carry the nullable 16-field telemetry summary
  and GET /api/v1/units/{id} serves the full observation projection; the
  console renders SOC/pack/cells/temperatures live. State: 1203 Python +
  184 web tests green; live controller verified serving real values
  (SOC 9/68/47%, 60/50/60 cells, quality good). REMAINING for control:
  power-direction sign validation, watchdog timing experiment, then a
  SEPARATE explicit authorization for any write-enabled composition; MCP
  get_unit_detail queued (facade method exists).

- 2026-08-22: MILESTONES B AND C COMPLETE — the product ships. Milestone B
  (2ce9194, 392310e): the React operator console (shell with token gate,
  four-fact connection indicator, live stream with resync recovery; Home,
  Batteries, Now control surface, Activity timeline) built against twice-
  reviewed behavior suites, hardened by a 25-agent composed-app review that
  caught the isolated suites' blind seams (dead control surface from prop
  drift, phantom wire events, frozen fleet badge) and repaired with a
  composed-app integration test layer; 172/172 web tests, tsc strict clean,
  production build verified. Server-side additions: audit cursor route,
  disarm route, single-use browser event-stream ticket via subprotocol, and
  the frozen-mapping serialization fix (real AuditEvents 500'd through the
  audit boundary). Milestone C (f99bda1): energypod db migrate/backup/
  restore with day-one schema versioning and backup-API snapshots; /healthz
  liveness-only endpoint; simulate-mode development principal (fail-closed
  preserved everywhere else); multi-stage Dockerfile + compose + nginx SPA
  config + sample configs + docs/OPERATIONS.md; end-to-end simulate test
  driving a dispatch through the public REST surface to the simulated pod.
  Final state: 1088 Python tests + 172 web tests green; ruff, mypy strict,
  tsc, vite build all clean. REMAINING before live use: build the container
  image in a Docker-capable environment (step 12 — Docker absent here);
  human visual inspection of every console state (corepack pnpm dev against
  energypod simulate); the deferred-findings queue (docs/DEFERRED_FINDINGS.md)
  including mutation-test gaps and the queued P2 inventory; and the
  separately authorized observe-only commissioning that validates per-unit
  topology, scaling, direction, freshness, and watchdog timing before any
  hardware write path is enabled. The dev token is simulator-only; run mode
  without a credential store refuses every bearer by design.
- 2026-08-22: MILESTONE A COMPLETE — the application runs. Implementation
  (7 agents, commits 8a57706/e2ca540), adversarial implementation review
  (32 agents, two-lens verification: 24 confirmed findings incl. 3 P0,
  1 refuted, 30 P2 notes), and full repair (5 agents + primary seams,
  commit 858caf9). Every repair carries a regression test and was
  mutation-verified by defect re-introduction. 998 tests pass; ruff and
  mypy strict clean. The product now composes and serves end to end:
  build_runtime (observe-only boot, lifespan supervision, lazy production
  transport), EnergyServiceFacade, EventBus, deterministic simulator,
  energypod main CLI (check-config/run/simulate), inhibit acknowledgement
  (actor classes + facade + REST endpoint), reason-code vocabulary pinned
  (safety mutation survivors 60 -> 23, all non-vocabulary). Key accepted
  limitations recorded: run-mode telemetry decode unwired until identity
  evidence is commissioned (observe-only); repeated-timing-failure and
  external-writer latched causes deferred; latched-cause golden scenario
  and store-side cursor for the shipped SQLite audit class are follow-ups.
  EXACT NEXT STEP: Milestone B — pin the web toolchain via corepack pnpm,
  author UI behavior/accessibility tests first, then build the React
  operator console; then Milestone C packaging/ops/e2e.
- 2026-08-22: Milestone A contracts and red-phase test baseline accepted.
  ADR-0003 fixed the composition/facade/event-bus/simulator/inhibit design;
  API_CONTRACTS.md gained the service-facade, event-bus, composition,
  simulator, and inhibit-acknowledgement sections (plus granted ports:
  peek, actor bounded-zero request, fence revocation, main server_runner
  seam). Six author agents wrote tests/unit/test_service_facade.py (66),
  test_event_bus.py, tests/simulator/test_simulated_pod.py,
  test_composition.py, test_main_entry.py, and
  tests/golden/test_golden_scenarios.py; six adversarial reviewers produced
  19 P1 + 25 P2 findings; all were resolved (grants vs repairs) and the
  repairs applied by four agents (one interrupted run was restarted
  cleanly after two agents died). Committed as 1fc9ed3, 816be76, 2536ed6.
  State: 916 tests collect; the 791 pre-existing tests pass; the new
  Milestone A suites fail cleanly on absent production modules (correct
  red phase). EXACT NEXT STEP: dispatch the Milestone A implementation
  workflow — disjoint implementers for energypod/application/service.py,
  energypod/application/events.py, energypod/simulator/{pod,transport}.py,
  energypod/runtime/composition.py, and energypod/main.py against the
  accepted tests — then implementation review, integration (full suite
  green), mutation testing on the new critical modules, and the Milestone
  A commit. Then reason-code pinning + latched inhibit (already contracted
  in D5), then Milestone B (UI) and Milestone C (packaging/ops/e2e).
  REST/WebSocket/MCP boundary delivered; 729 tests pass; Ruff lint/format and
  mypy strict pass. Ran the independent adversarial implementation review as a
  36-agent workflow (8 reviewers + two-lens verification of each P0/P1): 0 P0,
  11 confirmed P1 (all repaired with regression tests, contracts updated), 3
  refuted, 18 P2 deferred. Ran mutation testing on allocation, protocol codec,
  and the safety kernel; equivalent survivors are classified in this ledger.
  Deferred P2 inventory (no safety impact; address opportunistically):
  ControlPolicy accepts zero `max_soc_jump_pct`/`max_soc_disagreement_pct`;
  `allocate_fleet_power` signature drift vs API_CONTRACTS.md wording;
  in-memory authorization repository discards revoke reason;
  SQLite `_json_value` non-determinism for set/frozenset; policy-timing
  cross-validation skipped when timing validation fails first; SQLite
  `open` creates parent directories without path validation; O(n^2) schedule
  overlap detection; untyped Any in in-memory repositories;
  `_revoke_after_failure` can mask the original exception; no audit event when
  no intent is selected; FaultTracker prefix type guard; Waveshare `close`
  failure handling; shutdown transport leak if the owner dies mid-check; stale
  read-preemption start time; redundant `_used_cycles.clear()` in `fence()`;
  duplicated idempotency-key parsing in rest mutation lambdas; missing
  ttl_s<=0 negative cases in REST/MCP tests; one tautological prompt-injection
  assertion. Open risks and follow-ups: reason-code vocabulary pinning pass
  (mutation survivors); latched-inhibit acknowledgement model for the actor;
  cell-capture-time ordering relative to previous capture is unspecified;
  `energypod.main` entry point absent though declared in pyproject; simulator
  and golden scenarios are the next major milestone.
