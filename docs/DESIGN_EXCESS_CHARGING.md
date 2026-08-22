# Excess-solar accelerated charging — implementation plan

Status: design accepted, pending implementation (2026-08-23)
Owner of contract: `docs/API_CONTRACTS.md` "Excess-solar accelerated charging
(advisory)"; evidence: `docs/PROTOCOL_EVIDENCE.md` section 4c.
Red-phase contract tests: `tests/unit/test_wire_decode.py` (T-UNIT-WIRE-025..028),
`tests/unit/test_safety.py` (export-evidence block), `tests/unit/test_composition.py`
(export-bound block), `tests/unit/test_config.py` (T-UNIT-CONFIG-014..020),
`tests/unit/test_live_composition.py` (tier-promotion test), and the new
`tests/unit/test_excess_charge.py`.

No production code has been changed yet. This document is the ordered work plan
for the implementation agent. NO live-hardware interaction is authorized for the
implementation stage; the live trial at the end requires separate, explicit
operator authorization.

## Design summary

- **Adviser, no new authority.** `ExcessChargeAdviser`
  (`src/energypod/application/excess_charge.py`) submits ordinary short-TTL
  `OPTIMIZER` charge intents through a new internal facade method
  (`submit_advisory_intent`, never on REST/MCP) under the composed automation
  principal `energypod:excess-adviser`. The arbiter, allocator, SafetyKernel,
  and per-unit authority path are unchanged in role; the adviser has no special
  authority anywhere.
- **Deterministic bound (allocator).** For a charge intent with
  `source == OPTIMIZER`:
  `eligible_charge_w = min(max_charge_from_export_w, max(0, floor(Σ_units
  grid_power_w) - export_headroom_margin_w))`, applied as ONE additional
  min() term on the allocation demand. It collapses to 0 unless EVERY fleet
  unit's `grid_power_w` is finite, `GOOD`, and no older than
  `export_telemetry_max_age_s`. It never applies to any other source or
  direction, and the un-armed policy triple (the default) also yields 0.
- **Kernel defense in depth.** A non-zero export-bounded charge proposal is
  rejected with `export_evidence_missing` / `export_evidence_bad` /
  `export_evidence_stale` when the fleet grid evidence is unusable; zero-watt
  proposals never accrue export reasons.
- **Beat-autonomy hysteresis (adviser).** Entry only when
  `achievable_w ≥ assumed_autonomous_charge_w + min_acceleration_w`; continue
  while `> assumed_autonomous_charge_w + exit_hysteresis_w`
  (`exit_hysteresis_w < min_acceleration_w`); otherwise leave the pod's own
  CT-following autonomy untouched. Hand-back is ALWAYS by non-renewal (TTL
  lapse, then the measured ~3.5-4.0 s firmware watchdog): never a stop triple,
  never an idle intent, never a zero-watt submission.
- **Operator precedence.** The arbiter's priority (emergency stop > manual >
  agent > optimizer > schedule > idle) already displaces the adviser
  (live-verified 2026-08-23). The adviser additionally withdraws while ANY
  higher-priority intent is active fleet-wide and re-posts only after it
  expires AND entry hysteresis re-qualifies.
- **Net-billing assumption (OPERATOR-CONFIRMED PENDING).** Energy arbitrage on
  export assumes billing netted across phases. A per-phase-billed site changes
  the economics, never the safety. Confirm before production enablement.

## New configuration keys (defaults)

Under `excess_charging` (absent block == disabled):

| Key | Type | Default |
|---|---|---|
| `enabled` | bool | `false` |
| `export_headroom_margin_w` | int > 0 | `200` |
| `max_charge_from_export_w` | int > 0 | `2500` (must be ≤ `policy.max_unit_charge_w`) |
| `export_telemetry_max_age_s` | float > 0 | `3.0` (must be > `control_period_s + essential_read_timeout_s`) |
| `assumed_autonomous_charge_w` | int > 0 | `520` (evidenced daytime self-charge) |
| `min_acceleration_w` | int > 0 | `100` |
| `exit_hysteresis_w` | int ≥ 0 | `50` (must be < `min_acceleration_w`) |
| `intent_ttl_s` | float > 0 | `10.0` (≤ 300 and > `control_period_s`) |

`ControlPolicy` gains the all-or-none export triple `export_charge_limit_w`,
`export_headroom_margin_w`, `export_telemetry_max_age_s` (all `None` by
default = not armed).

## Ordered implementation steps

Each step lists the exact file, the function/class to change, and the tests
that turn green with it. Run ONLY the named test files per step
(`./.venv/Scripts/python.exe -m pytest tests/unit/test_<file>.py -q -p
no:cacheprovider`); never the full suite while the live controller runs.

1. **Domain: advisory observation fields** —
   `src/energypod/domain/observations.py`.
   - Add `grid_power_w: float | None = None` and `load_power_w: float | None = None`;
     extend the finite-measurement validator to cover them.
   - Add ClassVar `ADVISORY_QUALITY_FIELDS = frozenset({"grid_power_w", "load_power_w"})`;
     relax the quality-map validator to accept exactly `QUALITY_FIELDS` OR
     exactly `QUALITY_FIELDS | ADVISORY_QUALITY_FIELDS`. Leave
     `safety_data_complete` and the ten-field set UNCHANGED (justification: an
     ordinary control decision must not start failing because a deployment's
     read plan does not serve the PCS block; export-bounded control is gated by
     its own fail-closed bound).
   - Tests: `test_wire_decode.py::test_quality_map_carries_the_advisory_field_set`
     (the Observation half; the decode half arrives with step 3).
2. **Domain: policy triple and allocator cap** — `src/energypod/domain/models.py`,
   `src/energypod/domain/allocation.py`.
   - `ControlPolicy`: add the three optional fields; validator: all set or none;
     when set, limit > 0, margin ≥ 0, age > 0.
   - `allocate_fleet_power(intent, headrooms, *, export_cap_w: int | None = None)`:
     the effective demand becomes `min(intent.watts, export_cap_w)`; the
     exact-sum invariants are unchanged.
   - Tests: the policy-construction preamble of the export blocks in
     `test_safety.py` and `test_composition.py` (they stop failing with "the
     ControlPolicy export triple is not implemented").
3. **Wire decoder** — `src/energypod/adapters/modbus/decode.py`.
   - `_PCS_BLOCK_BASE = 0x1000`, `_PCS_GRID_POWER_OFFSET = 17`,
     `_PCS_LOAD_POWER_OFFSET = 20` (PROTOCOL_EVIDENCE 4c; SysControl.cs:500/503;
     system-view cross-check at `0x0100+55`, SysControl.cs:429, stays a cold-ring
     read that is NOT merged into the value).
   - Decode both words through the existing `_measurement` helper (signed,
     unscaled W) and always include the two advisory keys in the emitted
     quality map (MISSING when the block was not served).
   - NOTE: `tests/unit/test_wire_decode.py` already removed `0x1000` from
     `_UNCONSUMED_BLOCKS` in the red phase — do not re-add it.
   - Tests: the four T-UNIT-WIRE-025..028 tests.
4. **Safety kernel export evidence** — `src/energypod/application/safety.py`.
   - In `_deny_reasons` (or a sibling helper invoked only when
     `getattr(proposal, "export_bounded", False)` and `watts > 0`), iterate the
     policy's fleet set (`expected_cell_count_by_unit`) over
     `current_observations` and add `export_evidence_missing` (no observation,
     `grid_power_w is None`/non-finite, or the quality map lacks the key),
     `export_evidence_bad` (quality not GOOD), `export_evidence_stale`
     (`now - captured_at_mono > policy.export_telemetry_max_age_s`), mirroring
     the `telemetry_stale` spelling.
   - Tests: the export-evidence block of `test_safety.py`.
5. **Configuration** — `src/energypod/runtime/config.py`.
   - `ExcessChargingConfig(_FrozenModel)` with the eight keys above
     (`enabled: StrictBool = False`, …) and `ControllerConfig.excess_charging:
     ExcessChargingConfig | None = None`.
   - Cross-validations (model validator on `ControllerConfig`, scoped to
     `enabled=True`): requires `mode == write_enabled`; requires `policy` not
     None; `max_charge_from_export_w <= policy.max_unit_charge_w`;
     `export_telemetry_max_age_s > timing.control_period_s +
     timing.essential_read_timeout_s`; `exit_hysteresis_w < min_acceleration_w`;
     `0 < intent_ttl_s <= 300` and `intent_ttl_s > timing.control_period_s`.
     Every message must name the rule (the tests match on "write_enabled",
     "policy", "hysteresis", "max_unit_charge_w", "control_period_s", "300").
   - Tests: T-UNIT-CONFIG-014..020 in `test_config.py`.
6. **The advisory module** — new `src/energypod/application/excess_charge.py`.
   - `ExcessChargeSettings` (frozen dataclass: the four behavioural keys).
   - `eligible_export_charge_w(observations, policy, now_mono) -> int` — the
     pure bound (see formula above; floor, never round up; 0 when not armed).
   - `ExcessChargeDecision` (frozen dataclass: `action` ∈ {"idle", "propose",
     "renew", "withdraw"}, `target_unit_id`, `eligible_charge_w`,
     `proposed_watts`, `reason_codes`).
   - `ExcessChargeAdviser(*, settings, policy, clock, observations, intents,
     submit)` with `async tick() -> ExcessChargeDecision`. Tick logic:
     1. `observations.all_latest()`; compute the bound.
     2. If any active intent with priority above OPTIMIZER exists
        (`intents.active(now)`, sources EMERGENCY_STOP/MANUAL/AGENT): withdraw
        own intent if held, return `idle` with `yielding_to_higher_priority`.
     3. Target = lowest `system_soc_pct` among controllable
        (ARMED_IDLE/ACTIVE), below `policy.max_soc_pct`, positive charge
        headroom (`dynamic_charge_limit_w` and the static unit cap); ties by
        unit id.
     4. `achievable = min(bound, target headroom, static unit cap)`. Entry
        threshold `assumed + min_acceleration_w`; exit threshold
        `assumed + exit_hysteresis_w` (hysteresis state kept in the adviser).
     5. Participating: remove the previous adviser intent, submit a fresh one
        (`submit(unit_ids=[target], direction=CHARGE, watts=achievable,
        ttl_s=settings.intent_ttl_s)`), return "propose" (first) / "renew".
     6. Not qualifying / bound 0: remove own intent if held ("withdraw"), else
        "idle"; reason codes `no_export_headroom`,
        `no_acceleration_over_autonomy`, `below_exit_hysteresis`,
        `no_eligible_target`. Never submit zero watts or an idle intent.
   - Tests: all of `tests/unit/test_excess_charge.py`.
7. **Facade advisory submission** — `src/energypod/application/service.py`.
   - `submit_advisory_intent(...)` mirroring `submit_intent` (same validation,
     audit event type, idempotency/correlation contract, event publication)
     except `source=IntentSource.OPTIMIZER` and the intent-id prefix. NOT
     routed on REST or MCP; composition-only.
   - Add `grid_power_w` / `load_power_w` to the snapshot telemetry summary and
     the unit-detail projection (nullable readthrough; never zero-filled).
   - Tests: adviser tests via the fake `submit`; the readthrough deserves a
     small addition to `test_service_facade.py` when implementing (not part of
     the red set).
8. **Composition wiring** — `src/energypod/runtime/composition.py` (plus one
   call-site line in `src/energypod/application/control_kernel.py`).
   - `_FleetAllocatorAdapter.allocate(intent, observations, policy, now_mono)`:
     accept `now_mono`; for `OPTIMIZER` charge intents compute
     `eligible_export_charge_w(...)` and pass it as `export_cap_w`; mark the
     resulting `_FleetProposal`s with `export_bounded=True`. Update
     `ControlKernel.tick`'s single call site to pass `now_mono`.
   - `_LiveDecodeTelemetry(..., promote_pcs_live_block: bool = False)`: when
     true, add `_PCS_LIVE_BLOCK_BASE (0x1000)` to `core_bases` in
     `read_plan()`. Budget check stays: promoted steady-state plan ≤ 8 windows
     (+ probe), bootstrap ≤ 10.
   - `_control_policy(config)`: populate the export triple from an enabled
     `config.excess_charging` (mapping
     `max_charge_from_export_w → export_charge_limit_w`, etc.).
   - Compose the adviser when enabled: `ExcessChargeSettings` from the config
     block, the runtime policy, clock, observation port, intent port, and a
     bound `facade.submit_advisory_intent` under an internal principal
     (`energypod:excess-adviser`, scopes {observe, dispatch}, non-interactive,
     site-bound). `ComposedRuntime.excess_adviser: ExcessChargeAdviser | None`
     (None unless enabled).
   - `_Supervision._run_fleet`: after the bounded polls and before the kernel
     tick, one bounded
     `asyncio.wait_for(adviser.tick(), timeout=self._interval_s)` when an
     adviser is composed; failures are suppressed per cycle (advisory) and
     never halt the fleet.
   - Example config: add a commented-out `excess_charging:` block to
     `config/config.live-write-example.yaml` documenting the keys (enabled:
     false spelled explicitly).
   - Tests: the export-bound block and the two composition tests in
     `test_composition.py`; the tier-promotion test in
     `test_live_composition.py`.
9. **Simulator support (for verification)** —
   `src/energypod/simulator/pod.py`.
   - Model deterministic grid/load words in the PCS block (a scripted per-pod
     export scenario hook), so the bound is exercisable end-to-end in
     `energypod simulate` without hardware.
   - Tests: extend `tests/simulator/test_simulated_pod.py` minimally; the
     adviser's e2e behavior is then drivable through the existing simulate
     composition.

## Test-to-implementation mapping

| Red test(s) | Turned green by step |
|---|---|
| `test_wire_decode.py` T-UNIT-WIRE-025/026/027 | 1 + 3 |
| `test_wire_decode.py` T-UNIT-WIRE-028 | 1 + 3 |
| `test_safety.py` export-evidence block (8 tests) | 2 + 4 |
| `test_composition.py` allocator export-bound block (8 tests) | 2 + 8 (allocator adapter) |
| `test_composition.py` adviser-composition tests (2) | 5 + 8 |
| `test_config.py` T-UNIT-CONFIG-014..020 (8 tests) | 5 |
| `test_live_composition.py` tier promotion (1) | 8 |
| `test_excess_charge.py` (16 cases) | 2 + 6 |

## Live-verification protocol (SEPARATE explicit operator authorization only)

Phase 0 — simulator (no hardware): run `energypod simulate` with a
write-enabled config carrying an enabled `excess_charging` block; script the
simulator's per-pod grid words to produce a controllable export sum. Verify
through the console/audit trail: adviser intents accepted under
`energypod:excess-adviser` + `optimizer` (audit distinguishes writers ONLY by
principal + source; local console traffic is `operator:local` + `manual`),
authority minted, PQ writes at the commissioned cadence, measured charge equal
to the formula, withdrawal on export collapse returning the pod to its own
autonomy after the watchdog gap, and manual-intent preemption end to end.

Phase 1 — single low-power daytime trial (explicit operator authorization,
daytime surplus confirmed, other writer apps quiescent for the window):

1. Pre-flight with the operator: confirm the NET-BILLING assumption (pending);
   confirm per-pod export is visible on the console; set a conservative trial
   cap (`max_charge_from_export_w: 500`).
2. Arm exactly ONE unit — the neediest. `POST /api/v1/arm` REQUIRES the body
   `{"unit_ids": ["<unit>"], "confirmation": "ARM"}`; a bare `{"unit_ids":
   [...]}` body is a 422.
3. Start the controller with the feature enabled. Watch `/api/v1/snapshot`,
   the audit trail, and telemetry ages (the promoted read plan adds one window
   per cycle; ages must stay inside `export_telemetry_max_age_s`).
4. Verify: measured battery watts ≈ commanded; site export reduced by the
   charge; the achieved rate ≥ autonomy + margin; withdrawal returns autonomy
   within the ~3.5-4.0 s watchdog window.
5. ABORT criteria — any one: measured charge exceeding eligible + 10% for more
   than two cycles; export evidence stale/bad; any unit latching INHIBITED
   (especially `external_writer`); a fleet halt; the target reaching the SOC
   ceiling; operator command. Abort = disable the feature (let the TTL lapse;
   emergency stop if power persists).
6. Record results in `docs/CONTINUITY.md` and promote the §4c evidence
   classification for the promoted-read-plan timing observation.

## Safety invariants restated for the implementer

- The export bound is a min() term ONLY; it can never raise authorized power.
- Missing/bad/stale grid evidence anywhere in the fleet means ZERO advisory
  charge (never a partial sum).
- Zero-watt and all-zero allocations stay legitimate representations; never
  couple "active intent" to "positive watts" (commits 6abd869, d2163a5).
- The adviser never writes registers, never issues stop triples, never
  submits idle/zero-watt intents; hand-back is by non-renewal.
- Emergency stop, fences, external-writer latching, and shutdown dominate the
  adviser exactly as they dominate any other intent source.
- `debug_modes_enabled` remains false; no maintenance registers are touched.
