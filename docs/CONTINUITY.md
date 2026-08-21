# EnergyPod continuity and recovery ledger

Last updated: 2026-08-21 (Australia/Brisbane)

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

Most documentation and several test files are intentionally not committed yet;
inspect `git status` before changing anything. Never discard uncommitted work.

## Current work and agent state at this update

The reviewed contract/test baseline is accepted. Production implementation has
not started. Review status is:

- Protocol test authoring completed for codec, register layout, faults, and the
  Waveshare transport. Coverage includes field-specific endian/signed encoding,
  all known blocks, layout detection, 59/60-cell uncertainty and overlap,
  evidence-gated actuation, and PyModbus 3.15 RTU/device-ID calls. These tests
  passed independent adversarial review. Review removed maintenance/debug writes
  from production catalogs, added cancellation/one-attempt and real malformed-
  frame cases, and classified handcrafted frames as E1 reference vectors rather
  than deployed captures. Exact Waveshare framing remains a commissioning item.
- Domain/allocation adversarial review completed.
- Safety/arbiter repair after contract resolution completed; focused collection,
  lint, and formatting passed.
- Actor/control kernel: adversarial review completed; 31 focused tests (17
  actor, 14 kernel), formatting/lint/diff checks passed.

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
4. **In progress:** Commit the accepted documentation/test baseline.
5. **Next:** Dispatch disjoint implementation rounds:
   - domain value objects, observation models, codecs, and fleet allocation;
   - configuration, schedules, and SQLite repositories;
   - arbiter, safety policy, authorization capability, and control kernel;
   - protocol codec, register maps, fault decoding, and transport adapter;
   - per-unit actor and lifecycle supervision.
6. Integrate, run focused/full suites, and repair causes rather than weakening
   assertions.
7. Run independent implementation reviews and mutation/property testing.
8. Specify tests first, then implement REST/API authentication, WebSocket event
   delivery, audit views, and read-only-default MCP.
9. Build the deterministic simulator and golden protocol/integration scenarios.
10. Specify UI behavior/accessibility tests, then build the polished React UI
    and visually inspect all states and responsive layouts.
11. Add bootstrap/configuration UX, Docker image/Compose, health checks,
    migrations, backup/restore, operator documentation, and end-to-end tests.
12. Because Docker is currently unavailable, perform static Docker validation
    and explicitly defer an actual image build to an environment with Docker.

## Environment and restart procedure

Known environment state:

- Windows/PowerShell workspace.
- Python 3.12.10 was installed at
  `C:\Users\vagrant\AppData\Local\Programs\Python\Python312\python.exe` and a
  `.venv` was created, but one isolated reviewer later reported that base path
  missing. Verify before use; recreate the virtual environment if broken.
- Dependencies were installed and resolved with Starlette 1.6.0.
- PyModbus 3.15 uses `device_id=`, not legacy `slave=`, on requests.
- Node 24.19.0 exists; npm is broken due a missing user-level npm CLI. Corepack
  works and should invoke pinned pnpm.
- Docker is not installed in this environment.

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
