# EnergyPod continuity and recovery ledger

Last updated: 2026-08-25 (Australia/Brisbane; every commissioned surface live — load-sharing act on the operator's attestation, the fair census tonight, mid's traverse tomorrow, sharing's first window tomorrow 16:00)

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
- LONG-LIVED SERVICES RUN AS HARNESS-MANAGED BACKGROUND TASKS ONLY — not
  subagent task shells, and not inline `&` launches (both reaped their
  children at lifecycle end; both observed 2026-08-24). A
  harness-managed background task survives its launcher shell and
  notifies on exit. Observed: a subagent-backgrounded controller (build
  366abe1, running clean) was killed silently by backgrounded-child
  cleanup minutes after launch — backgrounded-child cleanup killed a
  subagent-launched controller; pods self-managed; no controller defect
  (clean log, all requests 200, no traceback) — leaving :8080 dark ~3.5 h
  (10:52–14:13). :8080 ownership stays with the main session's
  harness-managed task.
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

Standing launch command for the live controller (as a harness-managed
background task, per the rule above — never a subagent, never a bare
`&`):

```
source var/solcast.env && export SOLCAST_API_KEY && \
  source var/pvoutput.env && \
  ./.venv/Scripts/python.exe -m energypod.main run \
  config/config.live-write-example.yaml
```

The Solcast key lives OUTSIDE the repo in `var/solcast.env` and the
PVOutput credentials in `var/pvoutput.env` (the no-secrets rule);
without them the provider/client composes out with a visible note —
the controller still boots.

## Completion gates

A component is not complete until contracts and tests agree, focused and full
tests pass, lint and types pass, reviewer findings are resolved, unsafe failures
are tested, and this ledger is current. The product is not ready for live
control until simulator/e2e suites pass, Docker is built in a capable
environment, security and recovery procedures are reviewed, and a separately
authorized observe-only commissioning validates register topology, scaling,
direction, freshness, and watchdog timing per physical unit.

## Update log

- 2026-08-25 (evening load-sharing COMMITTED, COMMISSIONED ACT, LIVE —
  830d137, 25 files +5852; PRECEDED BY THE ATTESTATION 8dd229b — the
  operator's direct statement "I do have net metering, no need to
  prove it, it just is" recorded as
  docs/evidence/netting-attestation-2026-08-25.md with the
  bill-structure and NET_BILLED corroborations, RELEASING THE E6 GATE;
  §12 steps 2–3 compressed to one revision at the operator's
  direction; controller restarted as harness-managed task bah336tk9,
  boot clean; ALL THREE programs verified live read-only —
  evening-sharing mode ACT phase idle (first window tomorrow
  16:00–22:30), health-watch await_window (23:00 tonight),
  calibration act/idle with mid's one-shot standing (fires 14:00
  tomorrow)): GATES — 3077 backend (+87) / 1074 web (+20), ruff +
  mypy strict + tsc + build green; the SolarOutlook TIME-BOMB FIXTURE
  FIXED (relative to the test clock; the DEFERRED_FINDINGS item
  closed). THREE SAFETY PROOFS PINNED BY NAMED TESTS — the signed
  loop's ONLY equilibrium is zero netted exchange (the v1.0 clipped
  counterfactual grows past cap — the regression pin); elsewhere_w
  leaves the participants' total UNCHANGED (the excluded-output ramp
  unreachable); NO dispatch outside the window (cold/wall/morning
  ticks all zero). SIX AMBIGUITY RESOLUTIONS recorded (§5.3's
  both-bounds set rule as the E7 efficiency-aware reading;
  elsewhere_w over the post-skip candidate set; the E8 invariant's
  all-caps case; baseline_context riding the close row; the close
  row's open/close pair; capability_limited including the fleet
  bound). THE FULL PROGRAM STATE — every commissioned surface is now
  live: the honest recovery monitor (W0); health-watch
  census+probe+recovery-advise (the FAIR A16 predicates run tonight);
  calibration act with mid's measurement one-shot standing;
  evening-sharing act from tomorrow 16:00; PVOutput (the operator's
  toggle); night-V2 suggest. THE OPERATOR'S COMPLETE-EVERYTHING
  DIRECTIVE IS DELIVERED END-TO-END. REMAINING HONEST OPENS —
  recovery AUTO (needs a genuinely flagged unit + the supervised
  night); the first-evening and first-traverse observations (tomorrow
  is BOTH: mid traverses 15:00–22:30 exempt from sharing while
  lhs/rhs share reduced — by design the two programs' first live
  cooperation); Thursday's graduation record; and the three standing
  operator questions (always-rhs; rhs capacity; Fronius Battery
  Control).

- 2026-08-25 (load-sharing CONTRACT v1.1 COMMITTED — c81ba98,
  +297/−46 on top of the swept-in v1; all fourteen amendments placed,
  95 E-references threaded, §18 log with the three rulings verbatim):
  TWO AUTHOR JUDGMENTS ACCEPTED — (1) the E6 netting gate is recorded
  as the contract's ONE conscious departure from the calibration
  panel's C15 no-receipt ruling: C15 guarded novelty-of-write, E6
  guards PHYSICS (the ~28.77 c/kWh inversion on a non-netting meter),
  stated explicitly in §11 with BOOT-DEGRADE rather than boot-refuse
  keeping validation offline-pure; (2) E4 implemented as
  MEASURED-DELIVERY latching (below 0.5× share for 3 ticks →
  not_delivering) rather than kernel-verdict inspection — the adviser
  cannot see kernel verdicts, and delivery-word latching also catches
  merely-not-following pods. E8's severity recorded as MAJOR (the
  label the panel would correct; the substance folded identically).
  IMPLEMENTATION WAVE LAUNCHED (the last build): the els- adviser per
  v1.1 — the signed loop, elsewhere_w, the plausibility guards, the
  debounce, SoC² weighting with the E8 invariant, measured-delivery
  latches, the ADVISE posture in live config, ACT gated on netting
  evidence (THE OPERATOR'S BILL INTERVAL DATA REMAINS THE PENDING
  OPERATOR ACT), the console card, the simulator plants, the full
  T-ELS. The wave agent also carries the queued SolarOutlook
  time-bomb fixture fix if trivial. SCHEDULE UNCHANGED: tonight 23:00
  the first FAIR health-watch run; tomorrow 14:00 the one-shot plan /
  15:00–22:30 mid's traverse; Thursday graduation. After load-sharing
  lands: review → commit → restart → it SITS IN ADVISE until the
  operator provides the netting evidence, then act.

- 2026-08-25 (load-sharing adversarial review — WITH-AMENDMENTS, 14
  items; implementable ONLY after E1+E2 rewrite the §3.3 control
  subsection): E1 BLOCKER — the clipped net term made EVERY export
  level an equilibrium (geometric ramp risk at delivery bias 1.16 vs
  the assumed 1.15); rewrite as a TRUE CLOSED LOOP on the SIGNED
  netted exchange, derating at the safe edge. E2 BLOCKER — excluded
  units' output DOUBLE-COUNTED (traverse nights ramp to cap in ~6
  ticks, multi-kW standing export); subtract excluded output. E3 the
  frozen-word plausibility guard; E4 kernel-denied participant
  exclusion; E5 the claim-settle debounce (one failed cal tick could
  destroy an anchor night via the renewal seam); E6 NETTING
  VERIFICATION COMMISSIONING-BLOCKING for mode: act — the operator's
  bill interval data is the evidence; on a NON-netting meter the
  economics invert (2 c sold vs 30.77 c displaced); E7 the zero-cost
  pin re-worded (partial-load efficiency penalty ~3–5 % — "no watt is
  ever EXPORTED for it"); E8 the clamp renormalization; E9–E14
  minors/notes. The v1.1 fold is IN FLIGHT (its working file rides the
  tree; never swept). RULINGS RECORDED: the engagement-on-work
  transient is genuinely cheap (~0.1 c) and the correct reading of the
  operator's ask; the value case = STRANDED-ENERGY RECOVERY (2.8×
  evening capability). OPERATOR-FACING: load-sharing activation will
  need the operator's bill interval data (or an equivalent read-only
  verification) before mode: act per E6 — flag when relevant.

- 2026-08-25 (health-watch v1.2 + THE A16 CENSUS FIX, COMMITTED AND
  LIVE — contract 180a3c8; code e82bb26, 12 files +2398 — NOTE: the
  code commit also swept in the untracked
  DESIGN_EVENING_LOAD_SHARING.md v1; the v1.1 fold in flight lands on
  top; controller restarted as harness-managed task buj01g26s, boot
  clean — TONIGHT'S 23:00 FIRST RUN USES THE FAIR PREDICATES): the
  §1.1 two-hypotheses rhs re-read; S4' in-phase non-following; the
  phase_idle_or_ct_silent soft note + ct_link_suspect; ROUTE B
  ACCEPTED with the agent's flagged-judgment rationale — without it a
  genuinely stuck garage pod could never reach recovery. GATES: 3002
  backend (+12 — the garage shape NEVER flags under any load_frac,
  pinned per-predicate; Route B never applies to nominal units, pinned
  at 7 seeded nights), ruff + mypy strict clean, web 1053/1054 — the
  one failure PRE-EXISTING (the SolarOutlook fixture pinned to
  2026-08-25T06:00Z had aged out; verified failing on the clean tree;
  QUEUED in DEFERRED_FINDINGS as the time-bomb-fixture fix — fixtures
  must compute relative instants). Operator-facing state: tonight's
  first health-check run is now FAIR to the garage battery; mid's
  traverse tomorrow 15:00 unchanged.

- 2026-08-25 (rebalancing research round — DECISION: DEFER BUILDING,
  FIRST FIX PARTICIPATION; read-only, nothing committed): FINDINGS OF
  RECORD — (a) the old stack's two cross-transfer balancers
  (manager.py balance_battery_soc 448–563: Δ>5, ≤1500 W, +10 %/−15 %
  factors = a 25 % export bias, NEVER SCHEDULED; byd PhaseBalancer:
  5/10/20-point tiers to 3 kW, no loss compensation, UI-ONLY) plus the
  scheduled-then-disabled SoC³-weighted discharge (convergence during
  useful work, zero double conversion) — NO proven-in-service
  precedent, correcting an earlier implication that one existed; (b)
  HTW Berlin Stromspeicher-Inspektion 2025 measured numbers: one-way
  BAT2AC 95.7 % mean at rated power collapsing to 63–86 % at partial
  load — battery→AC→battery round trip ≈80–90 %, so the operator's
  5 % bias is WRONG; the honest starting factor is
  charge=0.85×discharge, calibratable from fleet telemetry; (c)
  import/export registers accumulate independently (30 c paid vs 2 c
  earned — discharge-leads-charge-follows ramping required if
  zero-import is wanted; meter cross-phase netting is the norm but
  configurable variance exists — verify from interval data before any
  commissioning); (d) the German BYD mega-thread: mid-range divergence
  is partly BMS ARTIFACT reconverging at extremes; the vendor-sanctioned
  gauge fix is full charges; NO vendor guidance exists for
  independent-pod balancing; (e) the design shape IF EVER BUILT —
  trigger range>10 sustained, pairwise extremes 1–1.5 kW, η 0.85,
  stops (≤5 range, 95 ceiling, floors, 90-min budget, sustained net
  import), early-afternoon window, never during traverse/night/watch.
  OPERATOR DECISIONS (asked one at a time per their preference — they
  find multi-part questions too complex; keep future questions single
  and simple): GOAL = MORE POWER AVAILABLE IN THE EVENING; unevenness
  observed = BY LATE AFTERNOON (solar had all day and didn't
  converge). DIAGNOSIS: the unevenness is the PARTICIPATION pathology,
  not imbalance dynamics — rhs the spectator + mid pinned while lhs
  does the work (matches telemetry). DECISION: no balancer now; the
  already-commissioned health/calibration/recovery programs ARE the
  fix; monitor one week of evenings after the fleet is healthy; build
  the transfer balancer only if one pod remains regularly
  low-by-late-afternoon while others sit full. Standing schedule
  unchanged: health-watch first run TONIGHT 23:00 (arm for probes),
  mid's traverse TOMORROW 15:00–22:30, Thursday graduation record.

- 2026-08-25 (calibration ONE-SHOT DEFERRAL ADJUDICATION — c55fff1,
  3 files +136/−14; controller restarted as harness-managed task
  bsxx74tkg, boot clean; live status verified read-only: mode act,
  phase idle, reason now the HONEST deferred_probe_required — was the
  bogus request_unit_not_eligible — one-shot standing unconsumed):
  THE RULING — the implementation OVER-DEFERRED: it minted an
  evidence_short class and let it refuse the operator's request,
  contra the contract's own §10 step 0 (the panel's ruling there: the
  one-shot exists precisely so mid-first does NOT wait out the
  horizon; during the horizon EVERY pod is evidence_short, so
  deferring under it would have made C6 fix nothing). The waiver is of
  DUE; evidence_short is the unjudgeability OF due — subsumed. WITH
  THE FIX — the class derives from available evidence through the
  unchanged gates (exclusion first; the probe ladder for underived
  throughput), defers ONLY on deferred_probe_required /
  no_control_evidence / excluded_cycles_daily, and a deferred request
  is NEVER consumed — it stands and retries each plan. GATES: 2990
  backend (4 new legs), ruff + mypy strict clean. The config test that
  over-pinned the live file's mode was HONESTLY RELAXED (the
  operator's §10 step-2 commissioning changed the file after the
  suite's pin). THE SCHEDULE NOW STANDING (FINAL) — TONIGHT 23:00
  health-watch first complete run (census always; probe if armed — a
  passing mid probe row unlocks the calibration class REGARDLESS of
  throughput); TOMORROW 14:00 plan: the third rollup date lands and
  mid's ~3.3 kWh/day throughput (≥ 3 qualifying dates) unlocks the
  waiver — one-shot consumed, traverse opens 15:00, closes by 22:30
  with the stops and the durable opened-row; solar's midday refill
  Thursday provides the top anchor; graduation on the measurement
  record. The coordinator monitors and reports each event. Remaining
  opens unchanged (recovery auto's genuine-flag + supervised night;
  the §15 doc sweep; the three operator questions).

- 2026-08-25 (calibration program COMMITTED, COMMISSIONED, AND LIVE —
  0b02c36, 27 files +6845; controller restarted as harness-managed
  task bej6q6y8u, boot clean; BOTH programs verified live read-only:
  health-watch stages [census, probe, recovery] phase await_window
  (window 23:00 tonight) AND calibration mode act, window
  15:00–22:30, phase idle, the one-shot STANDING —
  request_measurement {unit: mid, note: 2026-08-25 operator request,
  consumed: false}): COMMISSIONING BASIS — the operator's explicit
  "complete everything — auto-recovery, calibration cycling,
  everything" directive; §10 step 2 executed (mode act + the C6
  one-shot for mid — the textbook case). REVIEW — source-scan: ZERO
  0x8000/debug-mode references in calibration.py; the only dispatch
  reach is the injected facade twin; three window layers with every
  stop non-renewing and the no-intent-at-midnight test. GATES: 2989
  backend (+55), 1049 web (+17), ruff + mypy strict clean, tsc +
  build green. NOTABLE RESOLUTIONS — the one-shot's once-only property
  implemented as a DURABLE CONSUMPTION FACT (a restart cannot
  re-consume); the stand-down acknowledge as an audited interactive
  REST route; advise composes display-only on observe-only
  deployments (no runtime toggle exists); the History morning-facts
  entry rides the projection's morning block; the rest/mcp
  maintenance-word bans consciously widened by EXACTLY the two status
  surfaces; the I10 allowlist revision (calibration reads
  actuation_incoherent as skip-if-context only). THE EVENT SCHEDULE
  NOW STANDING — TONIGHT 23:00–23:45: the health-watch's FIRST
  complete run (census always; probe if the operator armed this
  evening; recovery in advise — zero writes); tomorrow morning:
  evaluate the first rows. TOMORROW 14:00: the calibration plan
  consumes mid's one-shot (calibration_trigger_evaluated, due_waived);
  15:00–22:30: mid's first measurement traverse (800 W discharge,
  floor 10, energy bound, deadline; the durable traverse-opened row
  lands before the first intent); the close rides the next refill
  (night writer absent by config — solar's midday charge to 100 is
  the expected top anchor, C14's dependency stated on the card);
  graduation on the measurement record. The coordinator monitors both
  events and reports. REMAINING (the honest open end) — recovery's
  AUTO posture awaits a genuinely flagged unit (census
  stuck_suspected AND probe fail_no_response), armed rows, and the
  operator's go for the §16 step-5 supervised night (checklist held;
  evidence files to be pre-created per the A6 chicken-and-egg note);
  the deferred round-close doc sweep (§15 items 1/2/4); and the three
  standing operator questions (always-rhs; rhs capacity; Fronius
  Battery Control).

- 2026-08-25 (health-watch STAGE R COMMITTED AND LIVE — c3532ae, 21
  files +3621/−166; controller restarted as harness-managed task
  be4qkiwzv, boot clean; live status verified read-only: stages
  [census, probe, recovery], phase await_window — TONIGHT runs the
  COMPLETE program for the first time, recovery in the ADVISE posture:
  zero writes, structurally — no arm port wired in that posture):
  REVIEW — source-scan: ZERO transport writes in health_watch.py;
  exactly one .park(/.resume( each on the injected port; the
  disarm/re-arm facade twins confirmed NEVER-ROUTED. WHAT SHIPPED —
  the §7.2 composite (reverify→disarm→park→hold 90 s→resume→one
  bounded re-arm→verification re-run→closing disarm; I9
  authority-never-exceeds); the amended ladder (recovered_unproven
  excluded from the cap per A2; failed_write/write_unverified/
  failed_no_effect counted; the derived cap reset — no new API
  surface); the parking amendments live (A10 derived origin
  energypod:⇒automation on every lease/event/provenance surface with
  both-way honesty against operator rows; A5 _adopt_pending_resume —
  a pending unit_resumed + word=0 inside the anti-rollover window
  boot-adopts as OURS); DESIGN_POD_PARKING §3/§4/§7 amended; the
  console recovery surfaces (advise walkthrough vs auto card,
  attempts_total + the trailing-30 rate per A11, the left-armed
  alert, the BMU cross-check note); the simulator cycle-wedge +
  release-on-cycle legs. GATES: 2934 backend (+49), 1032 web (+12),
  ruff + mypy strict clean. LIVE CONFIG = §16 revision three
  (recovery-advise) exactly as the contract sequenced it. NOTABLE
  RESOLUTIONS — the derived cap reset (any non-counted night breaks
  the streak — flagged in case a privileged acknowledge route is
  wanted later); A11's rate renders attempted/30 (the
  needs-the-cycle rate), NOT recovered/attempted (which would read
  100 % in the chronic case); one recovery row per unit per night
  (null verdict when not eligible) making I1/A4 well-defined; the A6
  chicken-and-egg — evidence files must be PRE-CREATED before the
  supervised auto night (boot degrades to advise LOUDLY if missing);
  §15 items 1/2/4 (PROTOCOL_EVIDENCE / API_CONTRACTS /
  CONTROL_SURFACE_GAP amendments naming Stage R) left for the
  round-close sweep. REMAINING SEQUENCE — the calibration
  implementation wave is LAUNCHING NOW (the last build); after it:
  review→commit→restart→commission (advise + the C6 one-shot
  request_measurement for mid so its first traverse runs at the first
  eligible evening). The §16 step-5 supervised auto night for
  recovery waits for a GENUINELY FLAGGED unit (needs tonight's+
  census/probe rows with the fleet armed — the operator holds the
  arm flag) and the operator's go; the agent's checklist is held by
  the coordinator. Three standing operator questions unchanged
  (always-rhs; rhs capacity; Fronius Battery Control).

- 2026-08-25 (calibration CONTRACT v1.1 COMMITTED — 1fe0c76,
  docs/DESIGN_CALIBRATION_CYCLING.md, 1290 lines, §16 amendment log
  with four rulings verbatim): all sixteen review amendments folded
  in-place; three author liberties ACCEPTED by the coordinator — the
  stronger C13 branch (window widened to 15:00 + a delivery-floor fit
  check so mid's first night does not predictably end
  floor_miss_deadline), key reuse for the interrupt-worry line, and the
  C8 note that any floor below 10 fails margin validation. WORKSTREAM
  STATE: both programs' contracts are committed — health-watch live
  through Wave 0 + Stages C/P (3f1b27e, 2a7f6f7); calibration
  design-only (1fe0c76). TONIGHT 23:00: the first census night (the
  probe leg runs only if the operator arms this evening — already
  flagged). TOMORROW: evaluate the first night's rows, then Stage R
  supervised live-verification prep, then the R wave and/or the
  calibration implementation wave. OPERATOR QUESTIONS STANDING: the
  always-rhs confirmation; rhs capacity (4,200 Wh assumed — now a
  SECOND consumer of night-V2's assumption); whether they ever use the
  Fronius app's Battery Control.

- 2026-08-25 (calibration adversarial review — WITH-AMENDMENTS,
  architecture sound, NO redesign; the adviser shape, ordered stop set,
  measurement-first, A14 boundary, and night-V2 interaction all
  verified against code): SIXTEEN AMENDMENTS — C1 BLOCKER: trigger
  None-if-never made mid permanently ineligible — never-deep pods are
  now due horizon-bounded once the historian has trigger_after_days of
  rollups; C2 BLOCKER: restart mid-traverse retired-the-night — now a
  durable calibration_traverse_opened row + boot reconstruction (resume
  re-armed the energy bound against a half-emptied pack; no-resume left
  the morning silent); C3 the excess-adviser OPTIMIZER-claim skip +
  dawn-corner precedence; C4 graduation accepts EITHER stop member (a
  frozen word cannot strand mid in stand-down); C5 the forecast_act
  refill close-dependence stated (solar-attributed miss satisfies,
  taper_never_observed fails); C6 the ~60-day evidence quiescence
  stated + the guarded config-borne request_measurement one-shot
  overriding SELECTION only; C7 the −26 c honest exported-worst-case;
  C8 the metering allowance with the floor-10 structural reinforcement;
  C9 integration discipline; C10 the delta instrument quality-gated;
  C11–C16 minors/notes incl. no-new-kill-switch and the conscious
  act-without-receipt distinction. RULINGS OF RECORD: the stop set is
  sound against frozen/blip/gap telemetry (a lying word is caught by
  the 2.80 V cell floor + dynamic-limit collapse; ~3.3 Wh exposure
  between 15 s cell refreshes — inherited standing protection); restart
  was the one defeating interleaving, now closed; the floor is KEPT at
  10 (the §12-1 answer); mid precedes rhs deterministically on the same
  horizon-bounded figure (unit-id tiebreak) — the mid-first sequencing
  needs no override, the one-shot exists only to start before the
  horizon matures.

- 2026-08-25 (health-watch STAGES C+P COMMITTED AND LIVE — 2a7f6f7, 20
  files +6181; controller restarted as harness-managed task by7ejlnao,
  boot clean; the status surface verified live read-only:
  GET /api/v1/health-watch/status → 200, stages [census, probe], window
  23:00–23:45, phase await_window, night 2026-08-25 — THE FIRST
  CENSUS+PROBE NIGHT RUNS TONIGHT 23:00 SITE TIME): WHAT SHIPPED — the
  program frame (§4 phase machine, once-per-night durable rows, A1
  deadline arithmetic, A4 interrupted reconstruction, A12 precedence);
  Stage C census (five stuck predicates, per-unit verdicts, persistence,
  tiers, ZERO writes); Stage P probe (300 W discharge, the §6.3 verdict
  matrix verbatim, A8 grid-import quiet gate + cancel re-check,
  baseline verification); config §9 (strict-prefix stages, A6 receipts —
  recovery without per-unit evidence is a validation ERROR); the console
  HealthWatchCard (verdict rows, skip reasons, alert tiers, the
  defined-restart advisory verbatim); the simulator script_stuck with
  pinned echo class. REVIEW: footprint verified; source-scan confirmed
  health_watch.py holds NO write calls — no transport, no
  arm/park/resume/disarm. GATES: 2885 backend (+107), 1020 web (+30),
  ruff + mypy strict clean, tsc + build green. Ten §-ambiguities
  resolved (notably: probe "still" = mean |w| over core; guard-refusal
  = skip not failure; recovery staged-but-unshipped renders advise with
  null verdict). LIVE CONFIG lands stages [census, probe]; recovery
  uncommissioned per §16. OPERATOR NUANCE — the probe requires ARMED
  units at 23:00: the program NEVER arms (v1 doctrine; only Stage R
  auto contains the bounded re-arm); a normal disarmed evening renders
  census + the arm instruction; FULL TEST NIGHTS NEED THE OPERATOR TO
  ARM IN THE EVENING. NEXT: the calibration-cycling contract design
  agent is launching NOW (separate contract beside the health-watch per
  panel A14 — own config block, own window; quarterly, one pod at a
  time, the 5–10 % bottom anchor at ~0.2 C, taper + 30–60 min
  balancing hold, trigger-gated on days-since-below-X from the
  historian, measurement-first); after its review pass: Stage R
  supervised live verification prep, then the R wave.

- 2026-08-25 (health-watch WAVE 0 COMMITTED AND LIVE — 3f1b27e, 9 files
  +741/−46; controller restarted as harness-managed task bbxaxk1dz,
  boot clean, console polling, both env files sourced per the runbook;
  /healthz re-verified ok after restart): W0-1 THE SELF-CHARGE DEADBAND
  — policy.self_charge_deadband_w (default 25 W, validated (0,100]):
  in-band floats render NEITHER self-charging NOR flapping; outside the
  band the rendering is byte-identical. W0-2 COHERENCE JUDGED ON
  DELIVERY — episodes count only when BOTH movement AND signed delivery
  fail; baselines survive authorization gaps up to
  policy.coherence_gap_grace_s (default 12 s) unless power returned to
  idle; the health state opens ONLY on a classifying echo
  (echo_matches_write or objective_not_served; unreadable/external →
  record-and-downgrade). THE I10 STRUCTURAL PIN: a source-scan test
  asserts NOTHING in src/energypod responds automatically on
  actuation_incoherent (allowlist: recovery.py itself, service.py read
  surfaces, parking's audit-window read, composition's one bounded echo
  READ, config comments). GATES: 2778 backend tests (10 new incl. the
  87 %/96 % regression vectors and the zero-straddle replay), ruff
  clean, mypy strict clean. Five §3 ambiguities resolved by the author
  and accepted (the idle-band definition; symmetric close bounds; grace
  bounds [1,300]; signed projection semantics incl. the honest
  charge-onto-self-charge corner; the ±33 W class both-sides
  rendering). The flapping observation that started this (the
  operator's History wall of 60+ entries) is now STRUCTURALLY
  IMPOSSIBLE at default policy — live tonight. NOW LAUNCHING: Stages
  C+P implementation (census + probe; contract §4–§6, §9–§11, §13–§14,
  §17 — NOT Stage R). RECOVERY STAYS UNCOMMISSIONED pending the
  supervised live verification + A6 receipts. The calibration contract
  remains queued behind it.

- 2026-08-25 (health-watch CONTRACT COMMITTED, WAVE 0 STARTING —
  2f1fd51, docs/DESIGN_BATTERY_HEALTH_WATCH.md, CONTRACT v1.1, 1348
  lines): the design-contract agent folded the adversarial panel's
  fifteen amendments; §20 carries the amendment log with both rulings
  VERBATIM. Four author pushbacks were ACCEPTED by the coordinator:
  hybrid receipts; failed_write counts toward the cap; no
  auto-escalation knob; the grid-import quiet gate. NOW STARTING: Wave
  0 implementation — the two recovery-monitor fixes, each with tests
  (the ~25 W self-charge deadband per §3.1; coherence-judged-on-delivery
  per §3.2). Stages C/P/R implementation waves follow after the Wave 0
  review. The calibration-cycling program design (its own separate
  contract, per panel A14) queues after the health-watch waves, or in
  parallel if capacity allows.

- 2026-08-25 (health-watch adversarial panel — CONTRACT v1 VERDICT:
  IMPLEMENTABLE-WITH-AMENDMENTS, NOTHING UNSAFE): the values {0,1}
  constraint is structural end-to-end; every identified gap is
  conservative toward not-writing. FIFTEEN AMENDMENTS (six MAJOR): A1
  deadline arithmetic; A2 recovered-unproven rungs; A3 the verdict
  matrix — Stage R's reach pinned (fail_no_response == still +
  echo_matches_write; the not-served class is the honest limit, open
  question 7); A4 interrupted-composite restart alert; A5 resume-side
  lease adoption; A6 config-enforced auto receipts. RULINGS: re-arm
  AFFIRMED with A2+A6 as conditions — the ONLY automation arm
  authority ever composed; crash-SAFE at every arrow, crash-HONEST
  completed by A4/A5.

- 2026-08-25 (cycling research — SHOULD WE CYCLE THE BATTERIES;
  both agents read-only, no code): LOCAL EVIDENCE — the site's own
  PVOutput history mined back to 2024 (getextended.jsp era map, 29 API
  requests): DEEP daily cycles through ~2025 (5–25 % floors, the old
  one-per-3rd-night 2000 W rotation) with no catastrophe; 2026 went
  shallow; the pods that STOPPED cycling are exactly the pathological
  ones — lhs still cycles 67→100 daily and is the healthy one; mid
  pinned at EXACTLY 100 % since ~May 2026 while still flowing
  3.3 kWh/day (SoC-ESTIMATE pathology with working control); rhs 100 %
  of samples ≥97 %. SCIENCE (sourced: Victron BYD page, EFT guideline,
  the photovoltaikforum 590-post mega-thread, the sarnau BMU decoder,
  BYD warranty PDFs): the two-anchor model confirmed — top anchor =
  true full with CCL→0 (the site's daily law provides it;
  charge-limit-0 W observed live); bottom anchor near 10 % empty is
  NEVER provided. The April-class symptom is documented in the German
  thread: cells hit the 3.65 V ceiling, displayed SoC 100 % while
  blocks are not full, a self-reinforcing loop (reported-full → PCS
  stops charge → charge never terminates → calibration/balancing never
  runs). Victron documents a per-module ~1 A/~50 W sensing threshold —
  rhs hovers in that band. POLICY: one pod at a time, quarterly
  default, discharge to the 5–10 % ANCHOR at ~0.2 C (~800 W) then a
  full slow recharge with taper + 30–60 min at full for the balancer;
  partial cycles anchor NOTHING (a 30–40 % floor is diagnostic-only —
  corrected); warranty math ~0.4 %/year of the ~3000-EFC budget —
  free. The calibration program will be a SEPARATE contract beside the
  health-watch, per panel A14.

- 2026-08-25 (battery health-watch / nightly reset program — RESEARCH
  PHASE COMPLETE; two parallel read-only agents, NO code changed): the
  operator authorized the program after describing the April-onward
  STUCK MODE — high reported SoC, no response to solar/load/commands,
  historically fixed by physically power-cycling the pod. FINDINGS OF
  RECORD: (1) RESET TRUTH — the vendor app (EnergyPod_RE, exhaustively
  inventoried) has NO reset/reboot command anywhere; 0x8000
  standby/normal with write→readback→verify (0x8100) is the ONLY
  soft-recovery register; the old Docker "force state" was enable-word
  maintenance @0x0200 word 0 (NOT 0x8000) — its nightly rhythm
  re-asserted the go-word for years without harm; the live R3 rhs
  evidence (docs/evidence/standby-cycle-2026-08-24.md) remains the
  decisive proof the standby cycle is benign, reversible, and
  command-following survives it. (2) PRIME SUSPECT — rhs shows the
  stuck fingerprint in our own telemetry: SoC 97–100 % with near-zero
  autonomous flow while siblings flow, AND rhs's load-CT word ~16 W
  avg vs siblings ~100/~180 W (the vendor "Electricity meter
  communication disconnected" class: a full pod with no CT view is a
  spectator); the operator was asked to confirm it is always rhs.
  (3) TWO RECOVERY-MONITOR BUGS — must be fixed BEFORE any automation
  keys off them (Wave 0): (a) health flapping keys on EXACTLY-zero
  watts (158 rhs transitions one night) — needs a ~25 W hysteresis
  band; this CLOSES the earlier open flapping observation (88c0db9's
  60+ spells); (b) both actuation_incoherent detections were FALSE
  POSITIVES from a baseline re-anchor across an authorization gap (pods
  were at 87–96 % of command) — auto-recovery must never trigger on
  that signal alone. (4) THE LADDER — S0 nightly census (zero writes)
  → S1 bounded actuation probe (200–500 W, 60–90 s, coherence+echo
  verification) → S2 standby cycle for flagged units only (THE doctrine
  revision: revises alarm-only-0x8000 and interactive-park; own config
  block, default off) → S3 full-fleet only if multi-pod sticking.
  Never-candidates: gateway reboot, undocumented words; Values 2–6
  permanently prohibited as always. (5) ONLINE SOURCES (EFT Systems
  BYD service guideline V1.5, photovoltaikforum, Whirlpool, Fronius
  register docs) — vendor pass/fail = TWO conditions (SoC displays AND
  charges/discharges); verify-before-write; small-power probes (20–300
  W) are established practice; escalation ends in the DEFINED-RESTART
  ADVISORY (battery-first sequencing, 10-min fuse lockout, wait
  10 min); SoC drifts at persistently high SoC (our
  night-charge-to-100 % law is itself protective — noted); Fronius
  Solar.web Battery Control is a competing writer with documented
  stuck cases; NO public endorsement of nightly cycling as prevention —
  the contract must say so honestly. STATUS: the design-contract agent
  is writing docs/DESIGN_BATTERY_HEALTH_WATCH.md (house style of
  DESIGN_POD_PARKING; Wave 0 fixes; stages C/P/R separately
  commissible; the doctrine-amendment section; §12-style test matrix);
  next: coordinator review, adversarial panel on the doctrine revision,
  then implementation waves. OPEN OPERATOR QUESTIONS: always-rhs?; did
  reads stay alive during the historical stucks (picks Stage R vs
  advisory-only) — the operator's mid-flight answer suggested YES
  (live data kept flowing: comms alive, control dead), which SUPPORTS
  Stage R's standby cycle as the plausible fix; historical fix =
  physical power cycle (the R5 terminal); the vendor-app
  charge/discharge test as their own verification mirrors our S1 probe
  exactly.

- 2026-08-25 (PVOutput "third writer" question CLOSED BY DIFFERENTIAL
  EXPERIMENT — TWO-WRITER ARCHITECTURE CONFIRMED; no code changed, one
  var/ observer script used and deleted): CHRONOLOGY — the operator
  enabled our uploader ~09:11 and questioned a 644 W b1 reading; slot
  forensics then MISREAD getstatus ext=1's column layout (the extended
  block begins at v6, NOT v7 — the recurring ~250 value was the
  FRONIUS's AC voltage riding the operator's own v6 =
  Voltage_AC_Phase_1 mapping; the battery fields run v7–v12 exactly as
  our config pins them). That misread produced a FALSE "mystery
  shifted-layout writer" theory across several turns; the operator
  challenged it twice AND WAS RIGHT BOTH TIMES. RECONCILIATION: with
  corrected columns every slot decodes sanely — pre-09:11 slots carry
  the Docker container's values at its configured slots; post-stop
  slots carry OURS (09:10: mid −953 W against measured −954 W; 09:30:
  lhs 99 %/+184, rhs 100 %/+51, mid 100 %/−805 — charging negative per
  the operator's stated convention). THE EXPERIMENT: the operator stood
  Docker down (confirmed stopped ≥1 h) and toggled OUR uploader OFF at
  ~11:52; the 10-minute watch showed slot 11:50 (our last post)
  carrying our full battery set while 11:55 and 12:00 carry solar +
  voltage ONLY with every battery field NaN across repeated samples —
  NO other battery writer exists. Two-writer architecture exactly as
  designed: Fronius = v1–v6 (solar, consumption, irradiance, AC
  voltage); pod-manager = v7–v12 (true BMS SoC + measured power,
  charge-negative) + b1/b2 (fleet aggregates, b1 spec-sign
  positive-charge). STATE AT CLOSE — live-verified: our uploader
  DISABLED by the operator (runtime toggle, origin runtime, survives
  restarts; last_posted_slot 11:50, zero errors) pending their
  re-enable at will. OPEN COSMETIC QUESTION with the operator: the b1
  sign — PVOutput spec (positive=charge) vs their custom-field
  convention (negative=charge); a one-line change if they want it
  flipped. THE DOCKER STAND-DOWN GATE IS NOW SATISFIED — the night-V1
  enable is UNBLOCKED whenever the operator asks. LESSONS (also
  persisted to memory): verify a reader's column model against a KNOWN
  WRITE before building theories on it; and the operator's direct
  observation OUTRANKS the agent's inference.

- 2026-08-25 (PVOutput integration COMPLETE — THE RETIRING DOCKER
  WRITER'S REPLACEMENT IS COMPOSED; 482f96e, 38 files +4541): the
  resumed agent landed with all gates green — 2768 backend tests (97
  new), ruff + mypy clean, 990 web tests, tsc + build green, 105 shots
  (+2 home states pvoutput-failing / pvoutput-not-commissioned × 3
  viewports). REVIEW spot-checks: the REST route follows the night
  pattern exactly (arm scope, interactive-to-enable, typed PVOUTPUT
  confirmation, Idempotency-Key, 409 mapping); the composition step
  rides the historian's exact suppress+wait_for envelope, after the
  polls, before the kernel tick; the client's sign handling verified.
  SIGN VERDICTS (live spec + PROTOCOL_EVIDENCE): (1) the old container
  posted DCDC 0x2009 (decimal 8201), NOT 4103 — the operator's memory
  was of the sensor table's PCS grid row; 0x2009 and our system 0x0114
  share the negative=charge orientation, so v8/v10/v12 post UNNEGATED
  and the existing PVOutput graphs stay continuous; (2) b1 is the
  spec's OPPOSITE convention ("-200 (Discharge), 200 (Charge)") — the
  adapter flips the fleet aggregate in exactly ONE place, pinned by
  tests. DURABILITY: schema v7 pvoutput_state singleton (6→7, schema
  pins updated in four existing test files), store-write-first toggle,
  the stored row overrides config across restarts (proven by test).
  SECRETS: var/pvoutput.env (gitignored, audited — no tracked file
  carries the values; masked 09…a9 / 55…19). Config live block
  enabled: false, revision 7. RUNTIME: the old controller task was
  stopped and relaunched as a harness-managed background task with
  BOTH env files sourced (solcast + pvoutput — the runbook's standing
  launch command updated below); boot clean, schema v7 migrated,
  status served immediately. LIVE-VERIFIED read-only post-restart:
  enabled false / origin config / unit_slots lhs v7+v8, mid v11+v12,
  rhs v9+v10 / native fields true / zero errors / no posts yet.
  OPERATOR HANDOFF (the settled cutover order, new-uploader-first):
  they flip the Home card toggle when ready (first POST at the next
  5-min slot boundary; the card flips to Posting with last-post age
  and hourly budget), verify the PVOutput dashboard, then stand the
  Docker container down — which ALSO unblocks the night-V1 enable.
  The toggle is deliberately the operator's; nobody enables it for
  them.

- 2026-08-25 (strip listing retired — THE BAR IS THE WHOLE VISUAL STORY;
  88c0db9, web-only, INLINE fix): the operator's FOURTH strip round.
  They pasted the live DOM — their real Health row carried 60+
  alternating Healthy/Self-healing entries overnight (the recovery
  monitor flapping every 1–13 min, 00:32–08:11); under the sparse-ink
  doctrine nearly every spell was a bare sliver, so the entire wall
  printed again. THE OPERATOR'S INSTRUCTION WAS EXPLICIT: remove the
  history-strip-listing element from the UI. THE FIX (surgical, not a
  subagent): the listing wears the offpage class UNCONDITIONALLY — off
  the paper in every regime, bare bands included, never out of the
  accessibility tree; the fit-reporting machinery (onFitted callback,
  fittedLabels state, per-item offpage classes, mixed-ink separator CSS
  rules) is deleted; the explainer now reads "hover any band for its
  full sentence and times". This supersedes the sparse-ink doctrine of
  f50a592 by explicit operator direction — its touch/no-hover concern
  stands recorded there but is overridden: the full sentence set
  remains in the a11y tree and on hover; nothing prints on paper.
  GATES: 961/961 web tests (one unrelated timing flake in the first
  full run — a live/reconnect UI test — clean on two consecutive
  reruns), tsc + build green; shots regenerated post-commit — 99 PNGs
  in 26.9 s, all distinct, none blank; the strip shots show bars-only
  rows (sub-item CLOSED).
  OPEN OBSERVATION offered to the operator, no answer yet: the
  health-flapping itself is REAL DATA (60+ spells, 1–13 min cadence,
  00:32–08:11) and a possible investigation. PVOutput NOTE: the
  implementation agent was interrupted mid-build BY THE OPERATOR to
  prioritize this fix and is being resumed; its partial work
  (adapters/pvoutput/, application/pvoutput_upload.py, edits to
  rest.py/service.py/config.py/schema.py/persistence) sat uncommitted
  and untouched throughout — never-sweep still applies.

- 2026-08-25 (strip listing sparse ink — THE DUPLICATED RAW TEXT GOES;
  f50a592, web-only): the operator's third strip report — raw text
  duplicating the bars beneath them. DESIGN, sparse ink by doctrine: an
  item whose band carries its fitted word rides off the page (the
  sr-only house pattern); an item whose band could not fit even its
  shortest tier prints with its times; a fully-worded row prints
  nothing. ONE list (not two) so each segment appears exactly once in
  the a11y tree; the fit is computed once in StripBands' memo and
  reported up via useLayoutEffect so clause ink and band words land in
  the same painted frame (no flash of text-then-quiet); unmeasured DOM
  renders fullest tiers so nothing counts as bare and nothing prints —
  ink only by measured refusal. The listing remains the full accessible
  surface; separator dots only between adjacent inked items; the
  explainer sentence now says where each fact lives. REJECTED
  ALTERNATIVE recorded: a fully-sr-only listing would make slivers
  hover-only, breaking "no information silently vanishes from the
  visible page" on touch/no-hover. GATES: 961/961 web tests, tsc clean,
  build green; Playwright DOM + native-pixel probes at 1440 and 390
  (20/20 — desktop Health shows exactly one inked clause "Self-healing
  13:40–13:52" beneath the bar, Lifecycle/Commanded print nothing);
  ariaSnapshot confirms the full clause set for screen readers. Shots
  deliberately NOT regenerated yet (the concurrent PVOutput
  implementation agent may run shots — collision avoided; PNGs are
  gitignored so the commit is complete); one regeneration pass follows
  when the PVOutput agent lands.

- 2026-08-25 (history strip fitted band labels — NEVER A MID-WORD
  ELLIPSIS; 1b9d6a7, web-only): the operator's second strip report —
  in-bar text ALWAYS truncated. DIAGNOSIS: StripSegments placed one
  fixed label per band with CSS ellipsis (nowrap + text-overflow), and
  the Commanded row used the FULL clause as the label ("the
  night-charge adviser — charging 2,500 W" ≈ 284 px on a 231 px band) —
  unfixable by construction; a 12-minute self-healing sliver (≈12 px)
  could never hold "Self-healing" (~70 px). DESIGN (grounded in
  Highcharts xrange dataLabels crop/inside, vis-timeline clipping, and
  d3-time-format's abbreviation ladder; primary docs fetched directly
  when Firecrawl/WebSearch rate-limited): tiered short words chosen
  against MEASURED pixels — every candidate renders once in a hidden
  measurer (.history-band-measure) under the band's font rules, row
  width read pre-paint + ResizeObserver, and the pure fittingBandLabel
  chooser (history.ts) walks the tiers fullest → shorter honest word →
  none. Commanded bars carry SOURCE short words (night adviser/night,
  solar adviser/solar, manual, agent, schedule, optimizer, nothing
  commanded/none) keyed on the clause's null rule; no-intent stretches
  render quieter (pale cream vs gold). Lifecycle/health compact tiers
  only where no different claim ("Self-healing" → "Healing";
  deliberately NO tier for "Armed and idle" or "Foreign writer").
  GUARDS: 68d0f04's geometry untouched, its regression test passes
  unchanged; the change-point listing stays byte-identical as the
  accessible surface; tooltips unchanged; unmeasurable DOM (jsdom)
  renders the fullest tier — suppression only by measured refusal.
  GATES: 961 web tests green (37 files), tsc -b clean, build green, 99
  shots regenerated; independent native-resolution vision check —
  Lifecycle "Disarmed", Health "Healthy", Commanded "nothing
  commanded"/"night adviser"/"none", zero ellipsis fragments at any
  viewport; narrow-390 honestly renders bare slivers with the listing
  carrying them.

- 2026-08-25 (PVOutput reporting integration — RESEARCH COMPLETE; ALL
  OPERATOR DECISIONS SETTLED 2026-08-25; IMPLEMENTED SAME DAY — see the
  482f96e entry above; this standing note is CLOSED): the design
  round's three open decisions are settled — cutover ordering =
  NEW-UPLOADER-FIRST then Docker stands down; v1–v6 are owned by the
  INVERTER and we never write them; SoC comes from the real BMS; the
  b1/b2 native battery fields are ON; the operator requested a UI
  toggle (built); donation tier confirmed. The Docker container remains
  BOTH the night writer AND the current PVOutput v7–v12 writer until
  the operator's dual cutover executes (their toggle first, then the
  stand-down).

- 2026-08-25 (history strip geometry — THE BANDS RENDER INSIDE THE BOX;
  68d0f04, web-only): the operator reported EMPTY band boxes with the
  words appearing outside them on the History unit-detail strip. ROOT
  CAUSE: StripSegments (web/src/views/history/HistoryView.tsx) computed
  `left: (segment.from / span) * 100%` — segment.from is an ABSOLUTE
  epoch-ms instant while span is the window DURATION, so every band
  landed at ~8e8 % left and overflow:hidden clipped them all: the boxes
  rendered hollow since W1–W3 shipped, the words surviving only in the
  change-point listing below. FIX: StripSegments takes windowFrom and
  computes window-relative, clamped percentages — rawLeft =
  ((from − windowFrom)/span)·100; left = max(0, rawLeft); width =
  max(0, ((to − from)/span)·100 − (left − rawLeft)); three call sites
  pass windowFrom={window.from}. A geometry regression test pins every
  band inside [0,100] % and the fixture's lifecycle band at left
  0.139 % / width 99.861 %. GATES: 948 web tests green, tsc -b && vite
  build green, 99 design shots regenerated. VISION VERIFICATION at
  native resolution (the earlier 900 px downscale was too small for
  0.78 rem band text — a false-negative "empty" read, now corrected in
  method): LIFECYCLE band contains "Disarmed", HEALTH contains
  "Healthy", COMMANDED contains "nothing commanded", each inheriting
  its row's tint; bands fill the box proportionally. Web-only — the
  Vite dev server serves it on refresh; the controller (managed task
  becsdbr2j) untouched. NIGHT-V1 ENABLE STILL GATED on the operator's
  Docker stand-down confirmation; arm satisfied.

- 2026-08-24 (history strip comprehension — THE WORDS SURVIVE THE BANDS;
  deff29e, web-only, 13 files +640/−122): the History unit-detail
  archaeology strip never shipped with a comprehension layer in W1–W3 —
  its bands rendered raw wire codes, clipped to invisibility on short
  segments; nothing was broken and nothing was removed, the layer simply
  never existed. NOW: the unified lifecycle map is hoisted to fleet.ts
  (armed_idle = "Armed and idle" everywhere — the banner's bare "Armed"
  no longer lands on the wrong surface), a health short-word map plus
  health sentences shared with UnitHealthTag via an extracted
  unitHealthSentence, EVERY ROW gains a change-point text listing so
  narrow segments survive as prose ("Healthy until 13:40 · Self-healing
  13:40–13:52 · Healthy since 13:52"), the explainer line and the
  DESIGN_PLANT_HISTORY §6 sampled-view/audit-is-the-record caveat
  render, and the two silent vanishings are worded (the
  health-not-recorded deployment note; the hourly-tier absence note on
  7d/30d ranges). Tests 925 → 947, build green, the history shot matrix
  regenerated and vision-verified — the strip READS. No backend changes;
  no restart needed (Vite serves it).

- 2026-08-24 (forecast/night-V2 round — THE REGISTRY'S FIRST CONSUMER
  LIVE, THE OPERATOR'S REAL TARIFF COMMISSIONED, NIGHT-V2 COMPOSED IN
  SUGGEST MODE; base 586bbb6 → HEAD cca356a): enabling commits first —
  dfded9f (the parking projection serves the commissioned cap with no
  lease open; the console park surface unblocked) and 586bbb6 (Solcast
  live enablement, the rooftop-sites hobbyist contract; the key lives
  outside the repo in var/solcast.env, sourced by the standing launch
  command now recorded in the runbook above). THE ROUND: 45c1053 the
  scoreboard wave (GET /api/v1/forecast, ForecastOutlookControl as the
  registry's FIRST consumer, the Insights solar-outlook + accuracy
  panel; +42 tests) → 6454e33 + 500bf73 the night-V2 design contract
  with the adversarial panel's twelve amendments folded → 5ba401d the
  A1 pre-battery scorer basis fix → 838e1c7 per-day durable trust
  records + the compound gate (schema v5) → cddab13 night-V2's
  target_policy config + the section-6 cross-validations incl. the A3
  FiT gate → 2a92399 THE V2 ADVISER — suggest-mode byte-identical
  intent stream, one-directional completion, audit-derived retarget
  caps (schema v6) → 8242c87 the historian night-claim attribution →
  b6a6308 the tariff COMMISSIONED FROM THE OPERATOR'S BILL: 30.77 c
  peak / 7.27 c off-peak 00:00–06:00 / 2.0 c FiT — the A3 gate passes
  decisively (FiT 2.0 << ~6.5 break-even; no legacy-QLD inversion) →
  cca356a the console wave (the V2 Night tile: suggest/act lines with
  the does-not-govern clause and the 95-vs-100 clause, the trust line,
  four fallback words, the A5 morning notice; +35 tests, 9 shots).
  FINAL GATES: backend 2671 / web 925, ruff + strict mypy + builds
  green, every wave gated on its committed tree. LIVE-VERIFIED on the
  running controller: target_policy forecast_suggest composed, trust
  provisioning 0/14, tariff composed, Solcast serving real 48 h
  forecasts (fetch_count growing at the 2/day cadence inside the
  10/day budget). The operator's bill numbers were the decisive input.
  FOUR WIRE GAPS flagged by the console wave — filed in
  DEFERRED_FINDINGS (fallback frames carry no age; no read route for
  the A1 morning-landing split; the 95 ceiling is design-pinned, not
  on the wire; the tariff wire carries defaults only, not the 7.27 c
  night rate). RUNTIME STATE: the controller runs b6a6308 (backend
  current; cca356a is web-only and Vite serves it — no restart
  needed). STANDING GATES UNCHANGED: the operator's Docker stand-down
  confirmation → night-V1 enable (arm already satisfied); V2 ACT-mode
  additionally requires the 14-day trust clock AND a deliberate config
  promotion.

- 2026-08-24 (mid-run reconnect): THE 15:20:32 PERMANENT-DARK BUG FIXED — a
  single transient TCP failure on an established Waveshare session darkened a
  unit until process restart. Live diagnosis: the transport's
  `_resync_after_failure` correctly closed and rebuilt its client (f788701's
  rebuild doctrine) and set `_connected = False`, but nothing ever called
  `connect()` again — the only connect submitter was `actor.start()` at boot,
  so every later poll hit the instant not-connected check and the fleet
  classified CONNECT_FAILED/gateway_unreachable forever while the gateway
  kept accepting fresh connections (the console's "unreachable" label was
  FALSE). The waveshare.py header doctrine ("reconnect and retry policy
  belongs to the generation-fenced unit actor") was unimplemented. FIX
  (mechanism: the ACTOR, not a `_LazyWaveshareTransport` retry — the proxy
  stays the dumb forwarding shell the write_debug_mode incident (230265f)
  proved it must be, and a proxy-level retry would put policy below the
  actor and double the poll budget; waveshare.py and the proxy are
  untouched): `_poll_owned` catches the connection class (`ConnectionError`,
  the builtin `TransportConnectionError` derives from, so the actor port
  stays duck-typed) and enqueues ONE fire-and-forget "reconnect" mailbox
  message at priority 5 — below heartbeat (never delays a renewal), above
  control/poll (the next poll rides the fresh socket) — carrying the
  scheduling generation so a fenced epoch's late attempt dispatches as a
  no-op; the attempt is heartbeat-margin bounded; only failed polls
  schedule, at most one is queued at a time, bounding attempts at ONE per
  fleet cycle (no busy-loop, no new task class); a persisting outage keeps
  the honest unreachable classification, and a successful reconnect needs
  no recovery-monitor change (the next poll's READ_OK clears streaks
  through the existing path). Heartbeat write failures recover through the
  NEXT poll's schedule within one cycle: the inhibit fences any queued
  attempt and the live generation's own failed poll re-schedules. TESTS
  (+4, tests/unit/test_actor.py): the live incident end to end (failure ->
  connect-failure class -> exactly one reconnect on the REBUILT client ->
  next poll reads OK, no restart); a persisting outage stays honest and
  bounded (one attempt per cycle, nothing scheduled between polls,
  restoration recovers); the stale-generation no-op via the real interleave
  (a queued heartbeat's write-failed inhibit fences the scheduled attempt;
  the live generation's own poll reconnects); and the wire-true variant —
  the REAL WaveshareTransport under the real actor, ConnectionResetError
  mid-run, reconnect landing on the factory-rebuilt client. GATES: backend
  2556 passed (base 2552), ruff + MYPYPATH=src mypy strict clean.
  RECOVERY EXECUTED LIVE: the controller was restarted onto 7664cdd
  (harness-managed task bfltgb5vw), rhs telemetry resumed on the FIRST
  cycle, and all three units were re-armed to the operator's 14:58 state
  (sole_writer, armed_idle, verified) — re-verified read-only
  post-recovery: control readiness ARMED, all units self_healing on live
  observations, zero foreign objectives. CUTOVER CONSEQUENCE: the night
  cutover's ARM step is already satisfied; only the night-enable
  remains, still gated on the operator's Docker stand-down confirmation
  (night stays off by config until then). ROUND CLOSURE: this closes the
  only post-ledger event of the pod-parking round; that round's task
  list is complete.

- 2026-08-24 (pod-parking round — PARK/RESUME LIVE-VERIFIED ON rhs, THE
  WRITE PATH MUTATION-PROVEN; base 9cd25c4 → HEAD 6765fe6, 20 commits):
  the arc held the contract-first line throughout — design contract v2
  with two red-team panels and the alarm-only expiry reversal (the lease
  is the alarm, never the actor; 3a8f2f1 + the same-round amendment sweep
  48b1a7b, coordinator rulings 62dd952, wave-C wire shapes 0a9c345) →
  simulator/transport core (the device-mode word with ACK-then-ignore
  pinned conservative 462e13e; the sanctioned 0x8000 write as a named
  method + catalog entry + `parking:` block 3fa84fa) → the durable
  park_leases store (schema v4, shared transaction, epoch CAS a4e2426) →
  the ParkController + facade + three operator-only routes (f064ea7) →
  PARKED health, adviser vocabulary, MCP companions (a084920) → the
  console surfaces (22a5526, real-server-shape fixtures 409063d, the
  NightChargeTile wall-clock countdown fix cf2872d) → adversarial review
  (ALL hard conformance checks PASS) → repair wave (boot adopts the
  crashed-then-verified park 1116ded; the dispatch refusal's provenance
  rides the intents 409 7d5c957) → commissioning on the live-write
  deployment (0a46e48) → live smoke → mutation round (6765fe6). FINAL
  GATES: backend 2552 passed, web 861, ruff + strict mypy clean, build
  green, tree clean. LIVE FINDINGS, all fixed + tested: (1)
  `_LazyWaveshareTransport` never forwarded `write_debug_mode` — NO live
  mode write could ever reach the bus; caught ONLY by live smoke
  (230265f); (2) the mode write needs its own timeout line —
  `mode_write_timeout_s: 2.0` — the 0.50 s PQ cadence budget was too
  tight for the gateway's FC16 turnaround (230265f); (3) the park
  dialog's actions fell below the fold with no disabled-reason
  discoverability — caught by the OPERATOR on the real console,
  vision-verified at all three viewports (4bc47e7, entry below); (4)
  process reaping hit BOTH subagent- and inline-`&`-launched children —
  the durable rule is refined below to harness-managed background tasks
  only (2466347). MUTATION: every semantic mutant in the write path died
  to the existing suite (the {0,1} bound, the FC16 shape, the echo
  checks hold); the real find was `renew_park_lease` executed by NO test
  anywhere — now covered end-to-end; 39 killing tests added, survivor
  queue in DEFERRED_FINDINGS (6765fe6). LIVE SMOKE: park/resume verified
  end-to-end on rhs through the sanctioned surface — the word triple
  verified in both directions, the clean checklist, composed parked
  health. STANDING: the night-cutover arm + night-enable remain gated on
  the operator's Docker stand-down confirmation (night stays off by
  config until then); the running controller IS this build, as a
  harness-managed task that survives and notifies on exit.

- 2026-08-24 (console park-dialog fix): THE DIALOG FOLD AND THE SILENT GRAYED
  CONFIRM FIXED — the operator hit it live: the park dialog's actions
  (Cancel / Park rhs) fell below the panel's internal scroll fold once the
  content outgrew its height budget, and the grayed confirm gave no hint why
  (pixel-proven on the pre-fix park-dialog.desktop-1440.png: NO buttons
  visible, the fixed sentence clipped mid-line). (1) THE PATTERN (not one
  number): the batteries family's `.batteries-view .dialog` is now a flex
  column — a new `.dialog-body` (`overflow-y: auto; min-height: 0; flex: 1`)
  scrolls the guarded fields, a new `.dialog-footer` (`flex-shrink: 0`) pins
  the hint + the fixed sentence + the actions; the panel's own `overflow:
  auto` stays only as the floor for a viewport too short for the footer
  alone. Applied to all THREE dialogs sharing the class: ParkDialog,
  ResumeDialog (Parking.tsx), and the inhibit-acknowledge dialog
  (BatteriesView.tsx). The fixed not-isolation sentence lives in the pinned
  footer immediately above the actions — the §8 verbatim pin holds at every
  content height. (2) DISCOVERABILITY: the type-back input carries
  `placeholder={unitId}`, and a held confirm renders ONE muted
  `.dialog-hint` line in the footer naming exactly what is missing —
  park.ts gained `parkConfirmHintText` (composes "Reason required" / "lease
  duration required" / "Type {unit} to enable park" with " · ", one sentence)
  and `resumeConfirmHintText` ("Acknowledge the takeover to enable
  resume."); never rendered while a write is pending. (3) TESTS: web 854 →
  861 (+7; full suite green, tsc -b clean) — pinned-footer-not-scroll-body
  structure pins for park + resume, the placeholder pin, hint-composition
  combinations incl. the no-lease-budget world and the pending guard, and
  park.test.ts composer pins. Batteries shots regenerated (18 PNGs) and the
  park-dialog state vision-verified at ALL THREE viewports: Cancel + Park
  rhs visible, the fixed sentence complete, nothing clipped.

- 2026-08-24 (six-workstream round — LIVE-REFRESH FIXED, THE STANDBY
  POSTURE, THE PROVIDERS LAYER BORN, THE MCP READ SURFACE COMPLETE):
  dispatched as six parallel agents against the operator's round
  brief; all six landed on master with the tree clean. (1) CONSOLE
  LIVE-REFRESH (4b94ee3): Batteries cleared its disconnected notice on
  the plane's cached replay and froze data ages — now stale-replay
  guarded with capture-marker + 1 s ticking display age; Objectives
  re-reads on foreign-objective alerts (was mount-only); Activity's
  connection-loss path was structurally unreachable (shell never passed
  the prop) — self-detects now, ages tick; Schedule's "Next" countdown
  runs down from starts_in_s. Home/Flow/History/Now/Insights and the
  SharedDataPlane core audited clean. (2) POLISH (7755bd1): one rhythm
  and one status vocabulary across all nine views, the two-view dialog
  CSS collision fixed, "Try again" copy unified, 66-shot matrix
  evidence; 782/782 web tests. (3) EXCESS VERIFIED (fd2acf3): surplus
  math, neediest-non-full-BMS-SOC target selection, and safety
  interplay all confirmed correct; the typed gate now accepts the
  operator's lowercase "excess" (wire literal unchanged) — reaches the
  deployed console on the next rebuild. (4) NIGHT STANDBY (29f8bf2):
  `demand_response: hold | standby` — measured demand >1000 W stands
  the unit down entirely (zero-watt non-participation, resume <800 W
  or window end, new `standing_by_on_demand` phase); fail-closed on
  missing/stale evidence still holds under BOTH postures; example
  config ships standby. 2193 scoped tests. (5) ADVISORY PROVIDERS
  (7b4ca4f..554b9e6): `src/energypod/adapters/providers/` — the four
  reserved ports, normalized forecast model, rate-budgeted async HTTP
  base with honest staleness, Open-Meteo (keyless) + Solcast (Bearer,
  key by env reference) adapters, historian load baseline, config
  tariff, forecast-vs-recorded-surplus cross-check, registry; advisory-
  only pinned by architecture-fitness and composition tests; live-
  verified API references captured (Open-Meteo azimuth 0°=South;
  Solcast `period` param, 10 req/day free). Config present-but-off.
  (6) MCP READ SURFACE (8703416): `get_unit_detail`, `get_schedule`,
  `get_observed_objectives` reusing facade projections; the agent-loop
  contract encoded in served tool descriptions and pinned by test.
  House check at dispatch: backend 2157 → 2306 passed after the round,
  web 773 → 782, ruff + strict mypy clean, controller untouched
  (/healthz ok). OPEN: console rebuild to ship fd2acf3+4b94ee3 to the
  deployed image; console vocabulary for `standing_by_on_demand`;
  Solcast key registration; tariff/wholesale source ADR; the first
  forecast-consuming adviser (registry deliberately unwired — the
  anticipatory-excess item); MCP `cancel_intent`; Objectives evidence
  table at 390 px; standby live cutover = set `demand_response:
  "standby"` in the live config + restart (the 7-step Docker cutover
  sequence is unchanged).

- 2026-08-24 (plant-history console W1–W3 — THE OVERNIGHT PROGRAM
  COMPLETE): the History view shipped (86909f2 wire model + read client,
  f07bcc6 uPlot charts + HistoryView, ef64039 nav + morning states + shots
  states; 773 web tests green, +57; tsc/build clean; uPlot 1.6.32 at
  51 kB as its own lazy chunk — the design's recommendation held; jsdom
  canvas-less degrades to the summary table, pinned). Verified against
  the LIVE route directly and through the 5173 proxy (both tiers, the
  full 422 matrix, 401), plus a Playwright drive of the real app
  (Today-empty → 6h fleet → per-unit → 7d; zero console errors; the
  recording note ticking). Live-found fix: on commissioning day Today
  opens before the first sample — the empty note now points at 6 h and
  names the 14-day bound. Not fabricated: per-sample quality is not on
  the wire (window quality_worst only), so per-period dimming was left
  out rather than invented — a future wire addition if wanted. With
  this, the 2026-08-24 overnight board is fully landed: historian
  backend + commissioning (LIVE, recording), night-charge backend
  (staged present-but-off, toggle live-proven), flow wow-loop closed at
  round 3 (approved; gallery for the operator), polish sweep live on
  writemode38 (2156 backend tests; the schedule vocabulary's
  units_disarmed word now in API_CONTRACTS + DESIGN_SCHEDULES), detector
  live from the prior wave. Backend suite basis: the polish sweep's
  final full run (2156) — no src/ changes since; docs-only commits
  after it.

- 2026-08-24 (flow wow-loop, round-3 gate — LOOP CLOSED): ROUND 3
  REVIEWED AND APPROVED; THE WOW-LOOP CLOSES AT THREE ROUNDS (9567a9d,
  eb85675, 8e07e31, 18a4a4b; 716/716 web tests; gallery regenerated
  post-final-commit, mtimes 02:07 > last web commit 02:06:33). Round 3's
  decisive find, pixel-proven: the round-2 worded figures stamped over
  their card borders at every viewport ≥768px ("Importing"/"Discharging"
  at max-content vs 58.8px card content; the SoC band rode nowrap with its
  meter painting up to 30px into neighbours) — fixed by the fit ladder
  (stack <45rem; band/pair/band at tablet; three-across 70–81rem;
  four-equal-across ≥81rem) with STRUCTURED two-register figures (label
  word + larger number, lead a step larger) and stacked SoC with the
  meter contained in-card. Gate verified by direct render (tablet
  band/pair/band: contained, rhythmic, lead unmistakable, no gutter ink)
  + the agent's DOM geometry at 10 widths × 10 states + pixel scans.
  Frozen qualities held: zero color changes, four-motion doctrine, all
  worded-truth pins byte-identical (flow.test.ts untouched), rows-same-y
  at ten widths. Verdict trail for the operator's breakfast review:
  round 1 declined (utilitarian; and its gallery was stale pre-round-1
  renders), round 2 approved baseline (lead framing, gold-that-reads-
  gold, SOC hierarchy, phone bus), round 3 approved (overflow-proofed
  fit ladder, two-register figures). Final approval is the operator's
  with the full 30-PNG gallery. web/ released to the history console
  build (W1–W3) immediately after this gate.

- 2026-08-24 (plant-history COMMISSIONED): THE TELEMETRY HISTORIAN IS LIVE
  on the real controller. One deliberate revision (5 → 6) uncommented the
  `plant_history` block at commissioned defaults (30 s cadence, 14 d
  full-res, hourly rollups forever); check-config clean; controller
  restarted (writemode37, /tmp/writemode37.log; the process pair from
  writemode36 — venv shim PID 13536 + base-python child 14808 owning 8080 —
  stopped cleanly, port freed before relaunch). Verified live: healthz ok;
  GET /api/v1/history composed (401 unauth — the route exists, observe
  scope); snapshot `history_state` present with last_sample_at for all
  three units; the database migrated in place to schema v3
  (schema_version table [(1,3)]; telemetry_sample + telemetry_rollup_hourly
  created beside the untouched energy tables); rows accumulate at exactly
  +3/30 s tick (one per unit); the first captured rows record the real
  night: lhs at −2242 W battery (the Docker writer's session), SOC 88%,
  commanded triple NULL (nothing of ours), health self_healing — the
  archaeology use-case working on its intended evidence from minute one.
  Rollup rows 0 as expected (first hour still open). Night block unchanged:
  present-but-off, posture partition, acknowledgement already latched (see
  the night-charge entry's probe incident). Console history view (W1–W3)
  queued behind the flow round-3 agent in web/.

- 2026-08-24 (flow wow-loop, round-2 gate): ROUND 2 REVIEWED AND APPROVED AS
  THE BASELINE (d17a3f0, 82ad0bc, 7f32224; 716/716 web tests; 30-PNG
  gallery incl. a new tablet-768 viewport). Reviewed against fresh renders
  (gallery regenerated from HEAD after the last commit — the round-1
  gallery the gate first judged was STALE, pre-round-1 renders: fleet-last,
  phone-as-table, italic SOC, strings absent from the code; the finding
  list still drove useful fixes). Confirmed by direct render inspection
  (tablet + narrow): the Whole Site lead reads unmistakably primary
  (full-width band/tablet, first column desktop, first card phone, accent
  strip + larger figures); the battery gold reads gold (deep-rim +
  full-opacity token + marching dashes, validator re-passed, worst CVD
  ΔE 21.5); SOC has hierarchy + an honest meter; rows land at the same y
  across columns at 390/660/768/1024/1440; the phone carries the full bus
  metaphor (arrows, dashes, meters — no more table). A CDN-cached vision
  critique claiming fleet-last was REFUTED (self-contradictory order
  claims; the code, structure pins, and direct renders all pin
  fleet-first). Round 3 dispatched with fresh eyes: protect the approved
  qualities, hunt the residual gap to "supreme polish," regenerate the
  gallery from the final commit, no truth-contract drift. NOTE (hygiene,
  no value recorded anywhere): one Playwright fill-retry log echoed the
  console token into a local session log during probing; local-only
  exposure; rotation is the operator's call in the morning.

- 2026-08-24 (flow wow-loop, round-1 gate): ROUND 1 REVIEWED AND DECLINED
  AGAINST THE WOW BAR; ROUND 2 DISPATCHED WITH THE FINDINGS LIST. The
  round-1 build (c6c8fdc, 73b9b2d, 93e51d4; 714/714 web tests; 20-PNG
  gallery in web/.design-shots/flow/) was visually reviewed via the vision
  model over the actual PNGs (fleet-charging desktop + narrow, mixed-
  directions desktop, one-phase-not-reporting desktop). Verdict:
  "functional but utilitarian" — structurally honest (stories correct,
  degenerate columns honest, color language consistent) but not polished.
  Confirmed findings fed to the round-2 builder: (1) battery gold reads
  muted-beige against white, weak contrast vs the neutral gray; (2) the
  "% charged" text ~10px italic, blends away — SOC needs real hierarchy;
  (3) per-pod figures not visually aligned across columns (reserve rows so
  baselines never shift); (4) inter-column gutters too wide, whole-site
  card cramped against the pods; (5) on desktop the WHOLE SITE column
  renders RIGHTMOST/LAST while narrow leads with it — fleet must lead on
  BOTH viewports (reviewer initially perceived this as "bottom"; the PNG
  confirms rightmost-on-desktop); (6) narrow-390 degrades to a plain
  label/value table with ~8px triple-bar glyphs — the bus-diagram metaphor
  vanishes on the homeowner's primary viewport; needs a compact real flow
  depiction (horizontal 3-node mini-bus) in the same color language;
  (7) keep the degenerate-phase pattern (dashed card, waiting copy, "—"
  placeholders, "2 of 3 phases reporting" fleet badge) — honest, just
  make it elegant. Worded-truth contracts and the four-motion doctrine
  stay pinned; structure pins may move where the visual grid legitimately
  changes. Loop continues: round 2 rebuilds, regenerates the gallery, and
  the gate re-judges the PNGs; final approval stays with the operator.
  Overnight board at this entry: night-charge backend agent in src/
  (B1–B6 per DESIGN_NIGHT_CHARGE.md, end-state config enabled:false),
  historian backend agent in its worktree (merge to master on completion,
  then commission + restart), flow round 2 in web/. Detector live and
  quiet-classified on the real Docker writer (expected_nightly_charge).

- 2026-08-26 (night-charge backend): THE OFF-PEAK NIGHT CHARGE BACKEND
  IMPLEMENTED AND STAGED (B1-B6; d29fea7 config, c8a438a strategy, 2e6e3ca
  facade, 42bbdeb REST, 78c07ac composition, + this pass) against the
  accepted contract docs/DESIGN_NIGHT_CHARGE.md (a791fb6) + API_CONTRACTS
  "Off-peak night charge" — the docs govern every shape; nothing deviates.
  The console half (b98d98c..939b0f9) was already built against the same
  shapes and reconciles cleanly (one delta, noted below). (1) CONFIG (B1):
  NightChargingConfig on the block-presence doctrine, REQUIRED civil-time
  timezone, canonical cross-midnight window pairs, every §3.1 gate bound to
  block-PRESENCE (write_enabled + policy; cap <= max_unit_charge_w; 0 <
  hold < cap; 0 < hysteresis < threshold; freshness > control+read bound;
  ttl inside (0,300] and > control; capacity map exactly-the-fleet IFF
  pacing even), and the PARTITION grant — a PRESENT schedule block whose
  allowed union covers the window ENTIRELY, the refusal naming the widening
  path. (2) STRATEGY (B2, energypod/application/night_charge.py): the pure
  civil-time helpers (single implementation of every countdown, DST-honest);
  the demand rollup over the per-pod LOAD CT words — never the grid words,
  the design's one non-obvious catch, a NAMED test: a full-rate 3x2500 W
  charge with grid words at -7.5 kW keeps pacing — worst-word-wins evidence
  with FAIL-CLOSED-TO-HOLD polarity; the per-unit plan under cap_first
  (Docker parity) and even (deadline-paced from MEASURED SOC each tick,
  self-correcting to cap when behind, no escalation mode); ceiling/headroom/
  disarmed sit-outs honest (skipped_full / no_charge_headroom /
  units_disarmed); the §2.5 tick — participation read at tick start,
  window-end NON-RENEWAL, per-unit exclusion at SUBMISSION time under every
  claim class (manual/agent/schedule/not-own optimizer — the dawn corner;
  the night- prefix means its own held intent never excludes itself), a
  live emergency stop withdrawing entirely, and the ONE-HELD-INTENT
  invariant (remove-then-submit) as the second NAMED test;
  NightChargeController = the ExcessAdviserController mirror (participation
  + the PARTITION latch, the §5 projection with read-time countdowns, the
  state_changed throttle + 30 s heartbeat, nothing while disabled).
  (3) FACADE (B3): submit_night_intent (the advisory twin verbatim — source
  pinned OPTIMIZER, night- prefix, per-battery watts native, never routed)
  and set_night_charging (the excess toggle pattern: NIGHT confirmation,
  arm+interactive to enable, the three refusal envelopes, and the SHARED
  durable-once partition acknowledgement — the schedules surface's own
  historical event id, either surface's capture counts, both latches flip,
  durable-append-FIRST, an append failure refuses). (4) REST (B4): POST
  /api/v1/night-charging with the same boundary discipline (literals 422,
  409 night_charging_not_commissioned / night_acknowledgement_required
  {acknowledgement: PARTITION_ACKNOWLEDGED} / night_enable_refused with
  unit_ids+stop_ids; 200 carries persisted:false + the projection).
  (5) COMPOSITION (B5): block-presence wiring (absent = byte-identical),
  the fleet-cycle ordering pin (schedule -> excess -> NIGHT -> accountant ->
  historian -> kernel; free surplus before paid import), the PCS live-block
  promotion widened to either-block, and supervision as the projection's
  single writer. (6) SIMULATOR + STAGING (B6): the scripted-night scenario
  in tests/simulator/test_simulated_pod.py — disarmed boot -> pacing from
  real SOCs with rhs full sitting out -> the EV hold at 100 W -> the
  hysteresis band -> the resume -> a manual claim excluding one battery ->
  the dawn-corner optimizer claim -> completion before 06:00 -> window-end
  non-renewal. VERIFICATION: contract-first red -> green across six families
  (config 19, strategy 32, facade 13, REST 12, composition 7, events 6,
  simulator 1); FULL SUITE 2145 green; ruff + format + MYPYPATH=src mypy
  strict clean each commit. SIMULATOR DEMO (../night-charge-demo/, run
  TWICE, identical digest a735b16465cfda889897547fbcfb7fd039e9321a478511efa319eb303ae62de7):
  the composed fleet walked the whole acceptance night — units_disarmed
  boot -> arm -> pacing at the 2500 W cap with rhs skipped_full and the
  pods' measured battery watts at -2000 W on the ramp (autonomy replaced)
  -> the 1300 W EV spike holding every participant at 100 W with measured
  watts never positive (no discharge, no cycling) -> the 900 W band not
  releasing -> the 700 W resume -> the dawn corner with BOTH optimizer
  intents live and the night adviser excluding the excess claim's unit
  (lhs) -> the export ending and lhs rejoining -> a manual request on mid
  excluding mid while lhs kept charging -> completion at 05:29 (all three
  at the 94% ceiling, nothing charging) -> 06:00 non-renewal. Two demo-
  only timing notes recorded in the script: the allocator's optimizer
  export bound never applies to night- intents (pinned by 4cbf1b4), and
  the injected clock's read-preempt path advances 0.9 s/cycle so the
  demo's authority lifetime sits at 2.0 s.
  LIVE STAGING (writemode36): the config block PRESENT with enabled:false
  and the PARTITION grant widened in the same revision (schedule posture
  now partition); controller restarted clean; the snapshot carries
  night_charge_state in its disabled_by_config state; fleet unchanged
  (three units disarmed at boot as always, no stops, no intents, lhs on its
  own -2.1 kW autonomy as before); log clean. NO live trial: the §3.4
  cutover's steps 2-7 (arm -> stand Docker down -> enable -> one supervised
  night -> decommission -> close the detector expectation) remain the
  operator's acts.
  VERIFICATION-PROBE INCIDENT (recorded verbatim): the read-only posture
  check probed BOTH enable shapes — the bare enable correctly answered 409
  night_acknowledgement_required, but the with-posture probe (a curl -o
  /dev/null that was meant to observe the code only) EXECUTED: it captured
  the durable once-ever partition acknowledgement (audit seq 5184,
  principal operator:local, captured_via night_charging — the fact did NOT
  pre-exist; the 2026-08-24 schedule commissioning never needed it) and
  enabled participation for ~90 s before an audited disable (seq 5185/5186)
  and the recomposition restart restored disabled_by_config. NOTHING
  actuated: the fleet was disarmed the whole time, the projection read
  units_disarmed, and ZERO night- intents were ever accepted (verified in
  the audit store). CONSEQUENCES FOR THE OPERATOR, honestly: (1) the
  partition acknowledgement is already latched — your first enable needs NO
  night_posture field, and if you want the deliberate §8-item-2 capture
  ritual re-performed under your own hand, say so (there is no un-capture
  path by design; the durable fact's assertion text is the one you would
  have sent); (2) the toggle's 200/409 branches are both live-proven. CONSOLE RECONCILIATION (one delta, in the
  contract's favor): the fixtures' per-unit rows are a superset — the
  console renders a sitting_out unit's row verbatim from the projection, and
  the backend's skipped_full/no_charge_headroom reasons are inside the
  console's ONE reason vocabulary already; the toggle 200, the event
  payload, and the refusal envelopes match fixture-for-fixture. OPERATOR
  NEXT: §8's four decisions, then the cutover in order.

- 2026-08-26 (night-writer detector): THE NIGHT-WRITER DETECTOR IMPLEMENTED,
  COMMISSIONED, AND LIVE-PROVEN ON THE REAL NIGHTLY WRITER (46c828a
  contract, bc7e95f red, b72935e the known-writer amendment, b6d693e
  wire+sim, c197d0b monitor, 3bb3828 composition+api, 408f43b docs, 314ef3c
  the live-path pin, 1efda80 the synchronized-GROUP fix) against
  API_CONTRACTS "Night-writer detector" — PRODUCT_NEXT §2 S2, closing the
  census's 7.3 h overnight blind spot with evidence. (1) WIRE (zero extra
  frames): the served PQ objective (PCS detail 0x1060+17/+18 — the arm
  preflight's own window, already in the read plan) decodes into every
  observation as advisory fields with the serving's capture clock (the
  cached ride-along keeps its ORIGINAL clock); the simulator gained
  script_objective/clear_scripted_objective (remote_mode selectable). (2)
  THE MONITOR (application/foreign_objective.py, composed ALWAYS — six
  defaulted policy keys, no block): one bounded suppressed pass per fleet
  cycle after the recovery pass; samples when the words are fresh, the
  interval floor elapsed, the lifecycle uncommanded, no claim, outside the
  handback grace; a failed poll is a gap. CLASSIFICATION (first match
  wins): zero records nothing and closes the episode; inside the grace our
  own lapse records handback_grace; Q != 0 alerts
  reactive_objective_observed; outside expected_autonomy_band_w alerts
  outside_autonomy_band on the first sample; the EXPECTED NIGHTLY CHARGE
  (in-band, 0.8x..1.2x of foreign_objective_expected_charge_w,
  SYNCHRONIZED across >= foreign_objective_expected_min_units units
  counting this one, each corroborated inside max(5x interval, 300 s))
  records quiet expected_nightly_charge and outranks both pattern rules;
  otherwise sustained_remote_mode_objective (N consecutive nonzero samples
  while run_mode_w == 1 — the pod's own CT-following reads 0) or
  sustained_charge_without_pv_evidence (N consecutive beyond-class charges
  with grid_power_w <= 0) alert. ONE audit fact + ONE
  foreign_objective.observed bus event per (episode, reason). DELIBERATE
  REFINEMENTS recorded in the contract: the blanket positive-at-no-PV rule
  REFUSED (lhs's observed Matching-Load evening hold), and the
  all-fleet sync requirement replaced by the synchronized GROUP after the
  live commissioning observation. (3) SURFACES: the session record (7 d /
  4096 samples per unit, in-memory; the audit keeps alerts) serves
  GET /api/v1/objectives/observed?last=24h (observe; Nh/Nd 1..168 h; no
  mutation) with first/last seen, counts, signed min/typical/max,
  classification counts, foreign episode facts, and the FULL per-sample
  evidence; every snapshot unit and health units entry carries the COMPACT
  nullable last_objective_observed. The arm-time preflight and its
  external_writer latch are untouched — expected is CHARACTERIZED, never
  sanctioned. VERIFICATION: contract-first red -> 0 across eight families
  (final counts: monitor 68, wire 5, config 17, facade 8, REST 12, event
  2, simulator 5, composition 4, live-path 1); FULL SUITE 1979 green;
  ruff + format + MYPYPATH=src mypy strict clean each commit (scoped —
  another agent's worktree now lives under .claude/). SIMULATOR DEMO
  (../night-writer-demo/, run TWICE, identical digest ad6a5eb07552af8a):
  autonomy float quiet; a lone -2400 W writer escalates exactly once then
  closes; our own intent never samples while claimed, its lapse
  handback_grace; the -2500 W x 3 six-hour nightly writer stays
  expected_nightly_charge with zero alerts; the actor-level arm interplay
  shows sole_writer / pod_autonomy-proceeds / external_writer-latches.
  LIVE COMMISSIONING (writemode34 -> 35): restarted onto the build
  (config keys on the live-write example; expected 2500 W, min-units 2),
  sampler verified read-only — and at 00:01 AEST the REAL nightly writer
  appeared: lhs+mid holding -2500 W simultaneously (run mode 1, grid
  importing ~-2.6 kW each, batteries at -2.2 kW), first samples within
  milliseconds of each other; the first minutes (all-fleet-sync rule)
  raised two honest sustained_remote_mode_objective alerts — the
  commissioning evidence that produced the GROUP fix — and after the
  1efda80 restart both units classify expected_nightly_charge quietly
  while a full rhs (98% SOC) floats uncharged: the KNOWN writer is now
  characterized, not alarmed. Log clean; the standing GET-only night
  sampler keeps running beside the detector's own overnight observation.
  CONSOLE (web agent, feature-detected): the observed-objectives view and
  the per-unit summary; foreign_objective.observed is the alert tier,
  expected_nightly_charge/pod_autonomy/handback_grace quiet. OPERATOR
  KNOBS: foreign_objective_expected_charge_w null restores the strict
  posture; expected_min_units widens the group.


- 2026-08-24 (plant-history backend, H1-H6): THE TELEMETRY HISTORIAN
  BACKEND IMPLEMENTED against the accepted contract
  docs/DESIGN_PLANT_HISTORY.md (34f68a8) + API_CONTRACTS "Plant history
  (telemetry historian)" — backend half only (H1-H6; the console waves
  W1-W3 belong to the follow-up web agent per the design's own plan).
  The docs govern every shape; nothing below deviates. Built on an
  ISOLATED WORKTREE BRANCH for merge review (the night-charge feature
  was built in parallel in the main tree); commits H1..H6 there.
  (1) STORE (H1): schema_version 3 migrates in place —
  `telemetry_sample` (WITHOUT ROWID, clustered (unit_id, sampled_at),
  the 15 numeric observables + lifecycle/health_state/quality rollup +
  the commanded triple + the four mode words; every datum NULL when
  absent, never zero-filled) and `telemetry_rollup_hourly` (per-field
  min/max/mean, sample_count = the coverage marker, worst_quality; an
  empty hour writes NO row). SQLiteTelemetryHistoryRepository +
  InMemoryTelemetryHistoryRepository share one port: batch append (BEGIN
  IMMEDIATE + executemany, duplicates ignored, busy = the typed gap),
  inclusive windowed reads, oldest_full_res_at/last_sample_at, and
  maintain(now) — rollup then prune in ONE transaction so a failed
  rollup can never delete the only copy.
  (2) SAMPLER (H2): TelemetryHistorian ticks once per fleet cycle
  (after the polls + the accountant, before the kernel; bounded,
  suppressed — a failure is a GAP and one log line, never a delay to
  control). Cadence per unit with the boot baseline (first tick samples
  immediately; a failed append leaves the clock so the next tick IS the
  retry); sampled_at = the tick's wall clock truncated to seconds, ONE
  timestamp shared by every unit in the tick; staleness guard
  max(3 x control_period_s, sample_interval_s) — a stale latest writes
  NO row; quality rollup = worst over REQUIRED_SAFETY_QUALITY_FIELDS
  minus the cold-ring system_soc/soh plus the CT pair when composed,
  precedence missing > bad > stale > suspect > good; commanded triple
  from the per-unit winner set (throwaway arbiter; OPTIMIZER attributed
  to the claiming adviser — the excess projection today, the night
  projection joins at merge) plus the cycle's peeked authority; NULL
  triple when nothing claims (and for an emergency-stop winner — the
  fence commands nothing). Maintenance cadence: one pass on the first
  tick after each site-local midnight beside the boot pass.
  (3) CONFIG + COMPOSITION (H3): `plant_history:` block (three keys, NO
  enabled key) — present REQUIRES storage, sample_interval_s must
  exceed control_period_s (both cross-validated on ControllerConfig);
  present composes the store on the EXISTING database path (simulate =
  the in-memory adapter), the boot maintenance pass, the historian in
  the pinned loop slot, PlantHistoryControl, and the snapshot's
  feature-detected `history_state` ({sample_interval_s,
  retention_full_resolution_days, last_sample_at per unit}); absent
  composes nothing (byte-identical snapshot).
  (4) QUERY (H4): PlantHistoryControl.query_payload — bounds (ISO-8601
  WITH explicit offset, naive = 422; from < to; <= 31 days; configured
  units; the 18-field vocabulary; points 50..2000), one resolution per
  response chosen by the data horizon (full at/after the oldest
  retained sample, else hourly for the whole window; hourly too when
  only rollups remain), the PINNED server-side LTTB (first/last kept,
  every emitted point a REAL stored sample, ties to the earlier sample,
  deterministic), window_min/max(+at)+sample_count over EVERY row so
  downsampled peaks survive, nulls skipped never zeroed, server-computed
  gaps (row spacing > 3 x cadence at full; missing hours between the
  first/last rollup hours at hourly; edges never gaps), fleet sums over
  RAW rows first then LTTB with a fleet point only where EVERY unit has
  a row (hourly fleet n = the weakest per-unit coverage), step
  encodings for lifecycle/health_state/commanded (first sample always
  present; the null triple is itself a recorded state; empty at hourly
  — the tier keeps no words). The validation rule set is ONE public
  parser (parse_plant_history_query) shared by facade, route, and the
  boundary fakes.
  (5) REST + MCP (H5): GET /api/v1/history?from&to&unit_ids&fields&points
  — observe scope, the reserved-word `from` rides a Query alias, every
  parameter rule the house 422 envelope (no bare 400 on this surface),
  409 plant_history_not_commissioned verbatim, no mutation exists; the
  read-only get_plant_history MCP tool under Field(alias="from") so the
  wire argument is literally `from`; EnergyServiceFacade.
  get_plant_history delegates (refusal when the block is absent).
  (6) SCENARIO + GOLDEN (H6): tests/simulator/test_history_scenario.py
  walks a 48 h compressed scripted trajectory over the composed simulate
  runtime (idle; a 2.25 h controller-down outage leaving two dark
  hours; a degraded STALE stretch; a dual-cadence stretch; an
  unreachable window producing NO rhs rows; a night-charge-shaped
  manual charge with the commanded triple beside measured -2500 W and
  run_mode_w 1) end to end into rollups and both query resolutions,
  with exact expected series — AND RUNS TWICE with identical digests.
  tests/golden adds the archaeology strip golden (three ticks: null ->
  manual/charge/2500 -> null with the watchdog stand-down lag).
  docs/CONTINUITY.md (this entry); config/config.live-write-example.yaml
  gains the COMMENTED plant_history block (history commissions as one
  explicit operator act). Tests: the full suite passes (1,922 at this
  writing), ruff lint/format and mypy strict pass. SCHEMA NOTE for the
  merge: schema_version is now 3 — every stored database migrates in
  place on the next open (v2 -> v3 touches no existing table; test_db_cli
  re-pinned). REMAINING for the merge round: review + merge the worktree
  branch, then the web waves W1-W3 (the design's own plan), then the
  operator decision to commission the block on the live controller.
- 2026-08-26 (scorecard backend): THE DAILY ENERGY SCORECARD BACKEND
  IMPLEMENTED (E1-E7; 7697aed, 0e8db09, 2fd879f, 89c1925, f7f80f0,
  f3938cb, + this pass) against the accepted contract
  docs/DESIGN_ENERGY_SCORECARD.md (00e1b2b) + API_CONTRACTS "Energy
  scorecard" — backend half only; the console agent built W-A..W-C in
  parallel against the same shapes. The docs govern every shape; nothing
  below deviates. (1) DECODE (E1): the six 0x4101 counters decode as
  ADVISORY Observation fields (low-word-first uint32 x 0.1, vendor pair
  order) under NEUTRAL grid A/B names — the pair ORDER is confirmed, the
  buy/sell ROLE labels are A-1-open; quality keys join ADVISORY_QUALITY_
  FIELDS (12 -> 18 key shapes; 10/12/18 the only legal maps) and stay
  outside every safety completeness set; decode_totals_block is the ONE
  implementation shared by the wire decode and the simulator. (2) THE
  ACCOUNTANT (E2, application/energy.py over domain/energy.py):
  zero-order-hold CT integration over observation capture times, sign-split
  import/export, gaps above integration_max_gap_s EXCLUDED never
  interpolated, per-unit/day coverage sampled/elapsed with worst-unit fleet
  rollup; device-counter daily deltas where a DECREASING cumulative is a
  reset (re-baseline + counter_reset:<metric> flag + the
  energy_counter_reset_observed audit fact); day roll at site-timezone
  midnight on the first observation whose local date advances (DST days
  keep their own utc_offset_minutes; persist + publish energy.day_rolled
  exactly once + audit energy_day_recorded with the fleet summary + ONE
  promoted 0x4101 read via actor.request_energy_refresh — the B4
  precedent); charged_from_surplus over adviser-active ticks on MEASURED
  battery watts; the A-1 passive verdict (vendor_labels|swapped|
  undiscriminating|null under max(0.5 kWh, 5%)) REPORTED never applied; the
  durable live-day baseline (wall-time anchored) so a mid-day restart
  re-baselines with the outage as a gap. (3) LEDGER (E3): EnergyLedger
  Repository port + memory/SQLite adapters; schema_version 2 (energy_day
  keyed by date — insert-once, records immutable; energy_baseline keyed by
  unit); the v1->v2 migration is in-place and row-preserving. (4)
  COMPOSITION (E4): a PRESENT energy_scorecard block composes the accountant
  tick into the fleet cycle AFTER the polls BESIDE the adviser projection
  update (the attribution predicate READS the projection), the snapshot's
  energy_today (in-progress record + SITE-LOCAL as_of always carrying its
  offset + the null-when-absent tariff), the six per-unit readthroughs, and
  the A-1 BOOT GATE: grid_counter_roles vendor_labels|swapped boots only
  when the durable energy-counter-roles-pinned fact exists (keyed existence
  check); an ABSENT block composes NOTHING (byte-identical snapshot, no
  energy decode — the 12-key quality shape, 409 on the route). (5) REST+MCP
  (E5): GET /api/v1/energy/days (observe, limit 1..31 default 8, newest-
  LAST, 409 energy_scorecard_not_commissioned, NO mutation — POST/PUT/DELETE
  all 405) + the observe-scoped get_energy_days MCP ride-along. (6)
  SIMULATOR+GOLDEN (E6): the totals grid/load pairs ACCUMULATE from the
  scripted CT words (the _accumulate precedent); the DEFERRED golden
  energy/SOC scenario consumed (DEFERRED_FINDINGS 3): import/export/mixed
  days + a fresh-accountant-over-the-ledger restart + the 23-hour Sydney
  DST day, every kWh EXACT (36 s at 10 kW = one 0.1 kWh quantum); the
  simulator constructor matrix (item 4's open half) landed. (7) CONFIG+DOCS
  (E7): the section-7 validation matrix (gap > control period and <= 60,
  coverage in (0,100], device_counter refused while unpinned, tariff keys,
  NO enabled key), the commissioned block in the live-write example, the
  API_CONTRACTS wire pins (fleet shape = per-unit minus metric_flags plus
  worst-unit coverage; consistent_with vocabulary incl. null; as_of
  offset-carrying; immutable records; limit-only paging; the tariff key),
  and the PROTOCOL_EVIDENCE A-1 role-label caveat. VERIFICATION: scoped
  contract-first red->green per family (wire 7 new, accountant 26,
  repositories 3 + db-cli schema pins, composition 6 + the live-composition
  12-key pin, events 1, REST 9, MCP 1, simulator 4 + the 15-case ctor
  matrix, golden 2); FULL SUITE green; ruff + format + MYPYPATH=src mypy
  strict clean each commit. COMMISSIONED live 2026-08-26 (the operator
  decision 1): the block added to the controller config (advisory defaults,
  roles unpinned — the passive A-1 counter cross-check accumulates from day
  one; no operator gate needed, no safety interaction), controller
  restarted (boot disarmed), read-only verification: the snapshot carries
  energy_today under the neutral A/B naming, the ledger is accumulating,
  fleet state otherwise unchanged, log clean. CONSOLE: the wire shapes are
  pinned in API_CONTRACTS (the console's wire.ts fixtures match; two
  fixture-helper notes — their sumOf/worstCoverage null out when ANY unit
  is null where the backend fleet sums skip nulls, and the TS
  consistent_with type omits null — are display-side only and documented).
  OPERATOR DECISIONS REMAIN (DESIGN section 11): the A-1 pinning path
  (passive verdicts accrue; ~3 discriminating days before a
  recommendation), the source promotion AFTER pinning (grid_source:
  device_counter + grid_counter_roles — the durable fact must be recorded
  first; no surface writes it yet, so it is a deliberate operator/CLI act),
  and the optional tariff keys.

- 2026-08-26 (scorecard design): THE NEXT FEATURE SELECTED AND DESIGNED —
  docs only, no code. PRODUCT_NEXT's 2026-08-23 next-3 is fully delivered
  (excess activation commissioned present-but-off with the trial-cap tension
  resolved via assumed_autonomous_charge_w 300; schedules commissioned
  day-only and operator-tested end to end through the live 422 fix; the
  verification/atomicity wave landed), so the 2026-08-26 edition re-ranked
  everything against the operator's demonstrated values. WINNER: the DAILY
  ENERGY SCORECARD (R8 — bought/sold/charged/discharged/load per day plus
  charged-from-surplus attribution), the one remaining untouched family the
  operator has visibly valued elsewhere (their prior dashboard and the vendor
  home chart led with these numbers) AND the missing evidence surface for the
  excess feature's graduation criterion 6 (kWh shifted vs the autonomy
  baseline — no console shows it today). Accepted design:
  docs/DESIGN_ENERGY_SCORECARD.md + the wire-facing pins in API_CONTRACTS
  "Energy scorecard" section. Key honesty positions: the 0x4101 counter
  DECODE and charge/discharge ROLE labels are capture-confirmed, but the grid
  buy/sell ROLES are A-1-open — so bought/sold come from OUR OWN integration
  of the per-pod CT grid word (control-rate on this deployment, sign
  live-proven, gaps excluded never interpolated, per-unit/day coverage with
  partial-day markers), BOTH grid pairs decode and record from day one as
  the passive A-1 pinning evidence, pinning never self-applies (promotion to
  the coverage-complete device counters is an operator config revision gated
  on the recorded fact), the grid pair renders under NEUTRAL A/B names until
  pinned, and site PV is never presented as measured (inputs unwired; the
  solar story is exported-surplus + captured-surplus). Advisory only — no
  authority, no writes, no read-plan cadence change (0x4101 already rides
  the cold ring; one B4-style promoted read at the day boundary). The build
  CONSUMES the deferred golden energy/SOC scenario (DEFERRED_FINDINGS 3) and
  half the validation matrices (item 4) as its reference-model test family.
  RUNNER-UP designated the same-wave companion: the night-writer
  between-cycles foreign-objective detector (census queue; zero extra
  frames; closes the 7.3 h night blind spot and feeds the pending
  night-partition decision), plus the schedule_state disarmed-window reason
  (the live FINDING from the operator's own publish test) as a third rider —
  both touch the same decode/composition files. NOT candidates: the excess
  trial/graduation (the operator's acts), the mid/lhs oscillation (needs
  their window), the overnight run/VLAN (operator agreements). Operator
  decisions verbatim-ready in DESIGN_ENERGY_SCORECARD §11 (commission the
  block; A-1 path; source promotion; tariff keys; naming check).

- 2026-08-23 (schedules wire contract): LIVE 422 ON PUBLISH FIXED — the docs
  govern. The operator's first editor publish (per-battery watts, no advanced
  dates) was refused 422 validation_error at the STRICT REST schema
  (body.entries.0.effective_from/effective_until "Field required") because B4
  had spelled ScheduleEntryRequest with both dates REQUIRED, while
  DESIGN_SCHEDULES §1 pins "an optional date range it is effective within" —
  the backend deviated, the console did not. Resolution (backend): the wire
  accepts absent/null effective_from/effective_until (a present bound is still
  a 10-char ISO date); the facade resolves an absent bound onto new domain
  sentinels OPEN_EFFECTIVE_FROM/UNTIL (date.min/max — every comparison site
  stays a plain date comparison; the SQLite payload round-trips the sentinels);
  schedule_wire_entry echoes an open bound as NULL so GET shows exactly what
  the operator published and the editor's optional date fields stay empty on
  reload. Web (defense for future drift): a wire-schema 422's pydantic loc
  paths (body.entries.0.days.1) now map onto the offending draft row by index
  when derivable, and otherwise render in the banner naming the field path —
  never a bare "Request validation failed"; the sentence names where the
  reasons rendered (rows, field paths, or both). LIVE: controller restarted
  onto the fix (boot disarmed; durable plan in var/live-write.sqlite3); the
  operator's own retries published v1 then v2 ("Testing", 19:35-19:36
  discharge 999 W per battery, dates 2026-08-17..09-25) and the window RAN
  after they armed (audit: pre-arm submits rejected lifecycle_not_controllable
  every tick, accepted+authorized from the tick after the arm; ended by
  non-renewal + watchdog hand-back). FINDING (recorded, not improvised): the
  schedule_state reason vocabulary cannot say "window open but the units are
  disarmed" — the pre-arm refusal stream is audit-only, so the operator had
  to discover the arm requirement by trying; a projection vocabulary addition
  is the honest fix if the operator wants it. Reminder: restarts boot
  DISARMED — an armed epoch does not survive a controller restart, so a
  window only commands if somebody arms again after every boot.

- 2026-08-25 (schedules backend): THE SCHEDULES SURFACE IMPLEMENTED (B1-B6;
  3e351f5, 1019640, f4d0eec, bdc592c, c67742c, + this pass) against the
  accepted contract docs/DESIGN_SCHEDULES.md (18db3ff) and API_CONTRACTS
  "Schedule" — backend half only; the console agent builds the editor
  (W1-W3) against the same pinned shapes in parallel. The docs govern every
  shape; nothing below deviates. (1) DOMAIN (B1): ScheduleEntry gained
  watts_by_unit under exactly PowerIntent's dual-form rules (scalar fleet
  total OR one positive int per selected unit summing to it; idle = scalar
  0, never a mapping; frozen mapping so equality follows the value); the
  evaluator carries the form verbatim; the SQLite payload round-trips both
  forms and still decodes pre-extension rows. The pure helpers next_start
  (8-day horizon, ties by priority then id, effective bounds on the start
  date) and window_end (midnight-crossing aware) are the single
  implementation of every countdown. (2) RUNNER (B2): ScheduleRunner in
  application/scheduling.py maintains EXACTLY ONE live SCHEDULE intent
  keyed (plan.version, entry_id) — submit on open, remove-then-submit
  renewal (the adviser's discipline; the held-id invariant is a named
  test), removal-only close on window end/disable/plan change; the runner
  never checks claims to decide control (waiting_for_higher_priority is
  projection honesty only); schedule_window.opened publishes on the first
  submit per key and schedule_window.closing on the removal tick —
  transitions, never heartbeats; a dead runner hands back by TTL + the
  firmware watchdog. (3) FACADE (B3): get_schedule (observe; repository +
  pure reads only), replace_schedule (whole-plan CAS with the pinned order:
  422 per-entry domain errors naming entry_id -> 409
  schedule_window_not_allowed {posture, allowed_windows_local, offending}
  -> 409 night_posture_acknowledgement_required with the durable-append-
  FIRST acknowledgement (deterministic audit event id, audit failure
  refuses, captured once never re-prompted) -> 409 schedule_version_conflict
  {current_version}); the diff summary rides the audit row and the
  schedule.replaced bus event; Impl-10 commit-then-audit with a
  compensating restore (prior plan via the same CAS; the port has no
  delete, so the inverse of a FIRST publish is an empty plan = off);
  submit_schedule_intent is the submit_advisory_intent twin (source
  SCHEDULE, schedule- prefix, per-battery watts native, atomic with its
  audit and publication, never routed on REST/MCP). (4) REST (B4):
  GET/PUT /api/v1/schedule — GET observe, PUT dispatch + INTERACTIVE
  principal + Idempotency-Key, every refusal shape verbatim, 422s name the
  offending entry; an empty entries list is legal (the plan IS the state).
  (5) COMPOSITION (B5): the schedule: config block (block-presence
  doctrine — present composes the surface control + runner + schedule_state
  projection, absent is byte-identical and both routes answer 409
  schedule_not_commissioned; allowed_windows_local defaults [["06:00",
  "20:00"]] = the day-only YIELD posture and "night" always means outside
  that DAY_DEFAULT; intent_ttl_s > control_period_s and <= 300 s; no
  enabled key). The runner ticks in _run_fleet AFTER the polls and BEFORE
  the excess adviser (the ordering is a named test: the schedule's claim is
  a published fact, the adviser the opportunist, so a yield resolves within
  one cycle) under the composed principal energypod:schedule-runner
  (observe + dispatch, non-interactive, site-bound); the night
  acknowledgement boot-loads via one keyed audit existence check. The
  adviser's claim check generalizes: excess_charging.yield_to_schedule
  (default TRUE, per unit) makes a live SCHEDULE intent on the adviser's
  target a yield trigger exactly like MANUAL/AGENT — without it the adviser
  outranks schedules by arbiter and starves them invisibly; false is the
  explicit opt-out. The schedule_state projection is single-writer (the
  runner's post-tick update; active derives from held_intent_id). (6) B6:
  the commented schedule: block in config/config.live-write-example.yaml
  documents the posture semantics and the trial-safe default day windows.
  VERIFICATION: the scoped families green (test_schedule, test_service_
  facade, test_facade_audit_content, test_rest_contract,
  test_boundary_hardening, test_event_contract, test_composition,
  test_excess_charge, test_config, test_repositories; 1600+ tests across
  unit+api), ruff + format + MYPYPATH=src mypy strict clean; READ-ONLY live
  check on the running controller (config block ABSENT): snapshot carries
  NO schedule_state (byte-identical), GET /api/v1/schedule answers 409
  schedule_not_commissioned, fleet state unchanged, log clean; then a
  SIMULATOR demonstration (script outside the repo, run twice
  deterministic) walked publish -> schedule_state next -> window open ->
  SCHEDULE intent live with per-battery authorization -> a concurrent
  MANUAL intent outranking it per unit -> window end non-renewal -> adviser
  yield on/off. NO live dispatching. NEXT: the console agent's W1-W3 land
  against these shapes; first night publish needs the operator decisions in
  DESIGN_SCHEDULES §8 verbatim (the partition grant is a config revision +
  the one-time acknowledgement).

- 2026-08-25 (excess activation backend): THE OPERATOR-FACING ACTIVATION
  PACKAGE IMPLEMENTED (B1-B4 + the two UI-audit extras; ce0d825, 158679d,
  1bd075c, + this pass) against the accepted contract
  docs/DESIGN_EXCESS_ACTIVATION.md (582ca4d), backend half only — the
  console agent builds W1-W3 against the same shapes in parallel.
  (1) PROJECTION (B1): ExcessChargeDecision now carries the fleet grid
  rollup (export_evidence + fleet_export_w, null on any missing/bad/stale
  word, worst-word-wins precedence missing > bad > stale, computed by the
  SAME per-unit classifier as the bound); ExcessAdviserController is the
  single writer (observe_tick, driven by the fleet loop) of the frozen
  ExcessAdviserState: active derives from the adviser's LIVE held_intent_id
  (never a lifecycle guess — the withdraw-then-tick race cannot show
  inactive while an intent is live), hysteresis walks
  inactive/entering/holding/exiting tick-granularly, reason codes are the
  tick's own vocabulary VERBATIM plus exactly three projection codes
  (disabled_by_config / disabled_by_runtime /
  economics_acknowledgement_required); the adviser consumes a tick-start
  participation port — a disabled tick still computes fresh evidence,
  withdraws-if-held once by removal, then idles with the projection code.
  (2) EVENTS (B2): excess_adviser.state_changed, payload = the §2 subset +
  heartbeat flag, throttled on the semantic tuple (watts ride but never
  trigger), 30 s heartbeat while enabled (a constant), NOTHING while
  disabled (the disable-carrying state_changed is the last event; a
  boot-composed disabled site never publishes — the snapshot's first frame
  carries the projection). Vocabulary correction en route: a collapsed
  rollup REPLACES no_export_headroom with its evidence word (the code's own
  definition requires GOOD evidence).
  (3) TOGGLE + GATE + RESCOPE (B3): facade set_excess_charging + POST
  /api/v1/excess-charging (arm scope; interactive additionally to enable;
  Idempotency-Key; audited excess_charging_toggled result
  enabled/disabled/noop on the Impl-10 commit-then-audit pattern; 200 body
  carries persisted: false always — P1). 409 refusals:
  excess_charging_not_commissioned / economics_acknowledgement_required
  (details {acknowledgement: NET_BILLED}) / excess_enable_refused (details
  reasons + unit_ids + stop_ids; manual/agent/schedule claims and latched
  stops refuse, latched INHIBITS do not, disable never refused). The
  net-billing acknowledgement is ONE durable audit fact under a
  DETERMINISTIC event id (excess-charging-economics-acknowledged) —
  store-uniqueness enforces once-ever, boot-load is one keyed existence
  check (_AuditStore.contains_event added to both stores; SQLite indexed),
  durable-append lands BEFORE the latch flips, an append failure REFUSES
  the enable. P6: block-PRESENT composes (triple armed, PCS block promoted,
  adviser + controller + adviser_state in the snapshot) with enabled gating
  participation; enabled: false composes suspended, runtime-enableable;
  ABSENT block byte-identical (no adviser/triple/projection key; toggle
  409s); config gates bind to block-PRESENCE (a never-safely-enableable
  disabled block is refused). P1 verified across a restart: boot recomposes
  from config, acknowledged; the participation toggle leaves no durable
  trace. (4) CONFIG/TRIAL (B4): the live-write example's commented block
  documents the §4 trial shape (enabled: false explicit + the 500 W trial
  cap) and the P6 semantics; the trial-shape-validates test pins it.
  EXTRAS from the console UI audit: intent.accepted bus payloads (manual +
  advisory paths) now carry expires_in_s (the 202 always did); the
  snapshot's per-unit authorized_power is documented as a non-consuming
  peek that reads null BETWEEN single-use consumptions — the standing
  figure is intent.authorized_watts_by_unit (API_CONTRACTS amended).
  OPEN OPERATOR DECISION flagged in the example: with the commissioned
  assumed_autonomous_charge_w: 520 + min_acceleration_w: 100 the ENTRY
  threshold is 620 W, so the §4 500 W cap as literally written can never
  clear it — commissioning the trial must also revisit the autonomy key
  (e.g. 300 W -> entry 400 W) or raise the cap; P5 forbids the runtime
  toggle from touching either. Verification: contract-first red per family
  (excess-charge projection block 13; event-contract 6 over the real bus;
  facade 13; REST 7 + boundary 1; composition 4 rescoped; config 4); 701
  green across every touched + neighboring family incl. e2e; ruff +
  format + MYPYPATH=src mypy strict clean each commit. NO live trial
  (operator decisions §7 pending; the ack gate would correctly refuse).
  Console migration: the moment the console's W1-W3 meet this build, the
  Home tile lights from snapshot adviser_state, the toggle from POST
  /api/v1/excess-charging, and the event case from
  excess_adviser.state_changed — nothing else moves (all additions are
  feature-detected on the key's presence).

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
