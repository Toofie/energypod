# Desync-resilience audit of the EnergyPod control surface

Status: IMPLEMENTED (2026-08-23) — B1 dce48c4, B2 6e96d60, B3 8da9bde,
B5+cold-ring 27bb50a, B4 997ec49, S1 a1ba210, and the ADD-1 arm blocker
(autonomy-signature discrimination + operator-acknowledged takeover) 6c4254b;
each step contract-first (30 red → 0 across the wave), live-verified on
writemode24. Class-A/C/D rows are unchanged by design. §5's S1 determination:
the real codes are `PCS_Warning0_1` / `DCDC_Warning0_1` (WARNING tier), both
standing-active on this fleet, so the block is documented-but-not-enabled in
the shipped example.
Prepared: 2026-08-24
Trigger: the `soc_disagreement` incident. After a day of heavy cycling our cached
"system SOC" (register 0x0100+17) diverged from the battery's live BMS SOC
(0x5000+9) and blocked charging. The operator's principle:

> "At any time, are there other instances where a desynchronization from the
> actual battery system would cause something to be blocked? If so, it would be
> preferable to rely on the battery — double-check the battery values instead of
> merely stopping and blocking."

This document enumerates EVERY control, threshold, deny reason, and gate in the
controller, classifies each, and designs the fix for every desync-exposed
(class-B) instance. The 2026-08-24 BMS-authoritative SOC remediation (already
landed) is treated as the precedent: remedy pattern R1 below is its
generalization. A second live confirmation arrived during the audit
(2026-08-24 ~04:4xZ): the arm-time sole-writer preflight latched
`external_writer` on `mid` after a restart while the pod was legitimately
self-charging — the same class-B pattern in the guise of stale PROVENANCE
rather than stale data (finding B6, §2.6).

## 0. Method

For every gate four questions are answered:

- **Q1 — input provenance.** What does the gate judge, and where does the input
  come from: a battery register read THIS cycle (control-rate core), a battery
  register read RARELY (tiered/cached — which tier, what staleness is
  structurally possible), or OUR OWN bookkeeping (sequences, epochs, lifecycle,
  generation fences, cached merges, policy constants)?
- **Q2 — desync exposure.** Can the input lag or diverge from the battery's
  ACTUAL state while the battery remains perfectly readable? (The soc incident's
  shape: divergence manufactured by OUR read plan, not by the battery.)
- **Q3 — class.**
  - **[A]** battery-truth-based, correct as-is;
  - **[B]** DESYNC-EXPOSED — our stale/cached/bookkeeping view can block while
    the battery is readable and fine;
  - **[C]** structural authority (keep; not a sync question);
  - **[D]** genuinely-unreadable-battery — fail-closed is CORRECT (if we cannot
    read the battery we must not command it); each D row verifies the check
    really means "unreadable", not merely "stale".
- **Q4 — remedy (class B only).** One of two sanctioned patterns:
  - **R1 — battery-authoritative precedence.** When two sources disagree, the
    battery's authoritative register wins and the divergence becomes a
    non-blocking warning (the soc_disagreement/BMS precedent).
  - **R2 — bounded re-read before deny.** When the blocking input is a
    rarely-served tier or our own bookkeeping, perform one bounded re-poll of
    THAT specific datum (inside the cycle's commissioned I/O budget; if it
    cannot fit, one deferred cycle with an explicit audit reason — never a
    silent block) and re-evaluate with fresh battery data.
  - In every case the class-D semantics are preserved: **two consecutive failed
    reads of the SAME datum = genuinely unreadable = block.**

### 0.1 The read plan, pinned (input provenance for Q1)

`_LiveDecodeTelemetry.read_plan()` (`src/energypod/runtime/composition.py`)
serves, per telemetry cycle at the commissioned 1.5 s cadence:

| Tier | Registers | Cadence | Structurally possible staleness |
|---|---|---|---|
| Probe | 0x5000 (7 w) | every cycle | 0 |
| Core | 0x5000 BMS live (31 w: SOC +9, SOH +10, dyn limits +13/+14, watts +8), 0x1040/0x2040/0x5040 fault words, 0x523C cell temperatures, 0x8106 identity pair, 0x8100 debug readback (+ 0x1000 PCS live when `excess_charging.enabled`) | every cycle | 0 (one cycle ≤ `max_telemetry_age_s` 3.0 s to the kernel) |
| Hot ring | 0x5200 cell voltages | every 3rd cycle (`cycle % 3 == 1`) | ≤ 2 cycles ≈ 3 s fresh, bounded by `max_cell_data_age_s` 15.0 s via its own capture clock |
| Once per process | **0x0100 system overview (61 w: system SOC +17, ctrlMode +1, workMode +2)** | **cycle 1 only** — read once, cached in `_slow_cache` for the process lifetime (it is excluded from the cold ring by `read_plan()`'s cold-set computation) | **UNBOUNDED — hours/days. Proven live 2026-08-24: the boot-resync-then-freeze experiment pinned `mid`'s system SOC at 67 % (its cycle-1 value) with quality GOOD forever while the BMS SOC climbed to 83 % — the definitive reproduction of the soc_disagreement incident's mechanism.** |
| Cold ring | 0x1060, 0x2000, 0x2060, 0x4101, 0x524E, 0x8139, 0x8102 (+ 0x1000 when the excess feature is off) | one window every 8th cycle (~108 s rotation) | ≤ ~108 s (display/advisory only; the arm-time external-writer preflight reads 0x1060+17 directly, fresh) |

Two merge facts load-bearing for this audit:

1. **Slow-cache merge semantics** (`_LiveDecodeTelemetry.decode`): cached blocks
   ride along with their original register words and decode GOOD; only the cell
   block carries an honest separate capture time (`cell_captured_at_mono`,
   `cell_sequence`). Every OTHER cached block — the system block included —
   decodes as if served this cycle. A once-per-process read therefore yields a
   GOOD-quality value that is hours stale.
2. **A failed window read aborts the whole poll** (`EnergyPodActor._poll_owned`
   awaits each window sequentially; any read exception propagates and no
   observation is appended). There is no partial observation: a "transient poll
   hiccup" manifests as a MISSING observation (staleness path, class D), never
   as per-field BAD quality. BAD/SUSPECT quality arises only from
   decode-level contradictions (out-of-domain words, identity/mirror/topology
   verification failure — see the wire decoder's fail-closed contract).

Note: the `read_plan()` docstring says the system overview rides the cold ring;
the code excludes it (cycle-1 only). The docstring is wrong today; B5's plan
fixes the code to match the documented, saner behavior.

## 1. Classification table

Legend: **Source** = where the judged input comes from (Q1). "Core" = read this
cycle; "once" = the once-per-process 0x0100 tier; "3rd" = the every-3rd-cycle
cell tier; "ours" = our own bookkeeping/policy. Class per Q3; remedy per Q4.

### Safety kernel — proposal-shape reasons (`_proposal_reasons`; judged before any telemetry)

| Gate | Source | Desync exposure (Q2) | Class | Remedy / stays fail-closed |
|---|---|---|---|---|
| `no_setpoints` | ours (allocator output) | none — pure plumbing | C | — |
| `invalid_evaluation_time`, `invalid_direction`, `invalid_power`, `direction_power_mismatch`, `duplicate_unit_setpoint`, `unit_policy_missing` | ours (proposal shape, policy maps) | none — deterministic shape validation of our own data | C | — |
| `intent_expired` | ours (intent TTL bookkeeping) | none vs the battery — intent lifetime is authority, not battery state | C | — |
| `zero_dynamic_capability` (all-zero ACTIVE allocation) | BMS dynamic limits (core, fresh) | none — the battery's own 0 W headroom report, read this cycle | A | — |
| `stop_authorized` | ours | none | C | — |

### Safety kernel — per-unit deny reasons (`_deny_reasons`, `_export_evidence_reasons`)

| Gate | Input source & tier | Desync exposure (Q2) | Class | Remedy / stays fail-closed |
|---|---|---|---|---|
| `observation_missing` | no observation at all for the unit | Only when the unit was never polled or every poll failed — genuinely unreadable | D | Keep. Two consecutive failed polls already surface as `telemetry_stale`. |
| `previous_observation_missing` | ours (observation history ≥ 2) | **Yes.** After every controller restart the first tick has no previous pair, so every active proposal is denied one cycle although the battery is readable and fresh. Mirror at mint level: `_eligible` requires `selected <= previous.keys()`. | **B3** | R2 (first-baseline). See §2.3. |
| `quality_system_soc_pct` | **once-per-process 0x0100 tier** via slow-cache merge | **Yes — the soc incident's twin.** A single cycle-1 system-block read that decodes BAD (low byte of +17 in 101..255) or is SUSPECT-downgraded is cached forever and denies EVERY cycle until restart, while the BMS SOC is fresh and GOOD. Also blocks qualification (below). | **B1** | R1 (advisory demotion). See §2.1. |
| `quality_bms_soc_pct`, `quality_soh_pct`, `quality_battery_watts`, `quality_pack_voltage_v`, `quality_pack_current_a`, `quality_dynamic_charge_limit_w`, `quality_dynamic_discharge_limit_w`, `quality_temperatures_c` | core tier, read this cycle | No tier staleness possible. Non-GOOD means the word was served out-of-domain (BAD) or verification failed (SUSPECT) — contradictory or unreadable evidence this cycle | D | Keep. This is the genuine "cannot trust the read" fail-closed path. |
| `lifecycle_not_controllable` | ours (actor lifecycle) | Not a battery-sync question — actuation authority | C | — (availability note in §4) |
| `nonfinite_safety_data` (scalars, cells, temps) | ours/decode | Unreachable from the wire (domain validators); a structural invariant net | C | — |
| `telemetry_stale` (`max_telemetry_age_s` 3.0) | our capture clock vs last successful poll | Fires only when no successful full poll happened within 3 s. A failed window read aborts the whole poll (§0.1), so this really means "could not read the battery" | D | Keep — verified genuinely-unreadable, not merely-stale. |
| `telemetry_from_future` / `cell_data_from_future` / `observation_time_invalid` | ours (clock sanity) | Clock integrity, not battery sync | C | — |
| `cell_data_missing` | ours (cell capture metadata typing) | Unreachable from the wire decoder (defaults fill it) | C | — |
| `cell_data_stale` (`max_cell_age_s` 15.0) | cell tier (every 3rd cycle) with honest own capture clock | Normal steady age is ≤ ~4.5 s; exceeding 15 s requires ≥ 3 consecutive whole-poll failures — genuinely unreadable | D | Keep. The honest per-tier capture clock is exactly right. |
| `observation_order_invalid`, `observation_sequence_invalid`, `observation_epoch_changed`, `cell_sequence_invalid` | ours (per-process decode counters, connection epochs) | A merely-delayed poll appends nothing, so the stored pair stays ordered; sequences are our own monotone counters — regression requires store corruption. Run mode never changes epochs (decoder emits the 0 default); simulate changes them on reconnect and self-heals after one cycle | C | — (B3 covers the only reachable false denial: the missing-pair warmup) |
| `soc_below_discharge_floor`, `soc_above_charge_ceiling` | **BMS SOC 0x5000+9, core, fresh** | None — already the 2026-08-24 battery-authoritative remediation; the stale system word is no longer a second opinion | A | — |
| `soc_jump` (`max_soc_jump_pct` 10.0) | BMS SOC, core, fresh — but compared against OUR previous observation | **Yes, two ways.** (a) Gap-induced: consecutive stored observations may span an abandoned-poll gap (seconds to minutes); a real 10 %+ SOC move across the GAP is flagged as a jump. (b) Battery-induced: this hardware is evidenced to legitimately jump its reported SOC (PROTOCOL_EVIDENCE §13.17, "observed large SOC jumps"); the BMS's own resync then denies one cycle | **B2** | R1 (battery-authoritative re-baseline). See §2.2. |
| `cell_count_invalid` | served cell window vs commissioned count | A served topology that contradicts commissioning is contradictory evidence, not staleness | D | Keep (structural commissioning error must fail closed). |
| `cell_voltage_low` / `cell_voltage_high` (`min_cell_voltage_v` 2.80 / `max_cell_voltage_v` 3.65) | **cell tier — every 3rd cycle, ≤ ~4.5 s old, GOOD quality via slow-cache merge** | **Yes, in the blocking direction.** A violation present in the cached window but recovered in the live battery (e.g. load-sag dip below 2.80 V that recovered when power stopped) denies on stale cells while the fresh battery is fine. (The opposite direction — a fresh violation missed for ≤ 2 cycles — is backstopped by the BMS's own dynamic limit closing at the electrical edge, class A.) | **B4** | R2 (deny-triggered tier promotion). See §2.4. |
| `cell_imbalance` (`max_cell_imbalance_v` 0.500) | cell tier — every 3rd cycle | Same tier exposure as above. (The 2026-08-23 LHS 54 mV incident was a REAL, fresh violation — correctly denied; §2.4 automates the fresh-window confirmation that investigation performed manually.) | **B4** | as above |
| `temperatures_missing`, `temperature_low`, `temperature_high`, `temperature_spread` | 0x523C — core, read this cycle | None | A | — |
| `blocking_fault` (`active_faults ∩ policy.blocking_fault_codes`) | fault words 0x1040/0x2040/0x5040 — core, read this cycle | None — the battery's own fault words, fresh | A | — (side finding S1 in §5: the configured code NAMES never match the decoder's generated codes, so this gate is currently inert — a fail-open misconfiguration, not desync) |
| `blocking_warning` | `active_warnings ∩ policy.blocking_warning_codes` | The composition wires this set EMPTY today | A | — (covered by S1) |
| `soc_disagreement_observed` (informational) | system SOC (once) vs BMS SOC (core) | The divergence itself is manufactured by our tier plan — which is exactly why it is a WARNING and never a denial (2026-08-24 precedent) | A | — (the landed R1) |
| `export_evidence_missing` / `_bad` / `_stale` (fleet-wide, export-bounded proposals only) | PCS 0x1000+17 — core when the feature is armed (tier promotion), cold ring otherwise | Advisory-only: no manual/agent intent is ever export-bounded, so this can never block operator power — positive-evidence doctrine by design. Note: `_stale` judges the OBSERVATION's age as a proxy for the grid word's age; safe only because the tier promotion is composed exactly when the policy triple is armed | A | — (one-sentence coupling note added to API_CONTRACTS in the plan) |
| `power_clamped`, `safety_checks_passed` | ours | plumbing | C | — |

### Control kernel (`control_kernel.py`)

| Gate | Input source | Desync exposure | Class | Remedy |
|---|---|---|---|---|
| `_proposals_match_selection` (identity, direction, per-unit caps, totals) | ours (allocator output vs intents) | none — deterministic self-coherence | C | — |
| `_eligible` structure (selection ⊆ observations, setpoint/intent binding, expiry ≤ winner expiry, per-intent totals) | ours | none — authority minting invariants | C | — |
| `_eligible` previous-observation requirement | ours (history ≥ 2) | same warmup as B3 — blocks the first post-restart mint | **B3** | with B3 |
| `_evidence_is_coherent` (epoch/sequence typing, monotone sequence within epoch) | ours (per-process counters) | Our poll bookkeeping vs a merely-delayed poll: a delayed poll appends nothing, so the pair cannot disorder | C | — |
| Generation fence / publish CAS / `_reconcile_generation` / `authority_generation_changed` | ours | Authority fencing (the 2026-08-23 publish-fence incident class) — not a battery-sync question | C | — |
| `control_cycle_deadline_expired`, `invalid_monotonic_time` | ours (clock) | timing integrity | C | — |
| `emergency_stop` path | ours | authority | C | — |
| `no_active_intent` revoke | ours | authority | C | — |
| Audit-event verification (`_create_audit_event` fact checks) | ours | audit integrity | C | — |

### Allocator (`allocation.py`, `_FleetAllocatorAdapter`)

| Gate | Input source | Desync exposure | Class | Remedy |
|---|---|---|---|---|
| Headroom = min(static, **dynamic**) per unit/direction | BMS dynamic limits — core, fresh | None; a unit with unusable telemetry contributes zero headroom (non-participation) and the kernel independently denies it | A | — |
| Capacity-weighted distribution, per-unit targets, participation floor, concentration | ours (deterministic arithmetic over headroom) | none | C | — |
| Export cap `min()` term (optimizer charges only) | PCS grid words; fail-closed to 0 on any missing/bad/stale | Advisory-only — can only LOWER power; never blocks non-advisory intents | A | — |
| Static unit/fleet limits, apparent/reactive envelopes | policy constants | not a sync question | C | — |
| Ramp limiter (`ramp_limit_w_per_s × heartbeat_interval_s` over measured watts) | `battery_watts` — BMS 0x5000+8, core, fresh; staleness to the kernel bounded by `max_telemetry_age_s` (3.0 s) and structurally re-checked by `telemetry_stale` | Measured watts can be up to one decision-cycle old; the ramp envelope simply includes that bound (1000 W/s × 1.5 s = 1500 W margin ≫ the drift possible in 3 s at these power levels) | A | — |

### Unit actor (`actor.py`)

| Gate | Input source | Desync exposure | Class | Remedy |
|---|---|---|---|---|
| Qualification `_qualifies` (safety-data completeness, quality, identity, profile, cell count) | core tier + **`safety_data_complete` includes `quality_system_soc_pct` GOOD — the once-per-process tier** | **Yes — B1's second face.** A unit whose single cycle-1 system-SOC decode was BAD/SUSPECT can NEVER qualify: `stable_observations` resets every cycle, arming is impossible, all power blocked until process restart — while the BMS SOC reads GOOD every cycle | **B1** | with B1 |
| `stable_samples_to_rearm` warmup (3 samples) | our qualification counter over fresh battery observations | The counter itself judges fresh battery truth; the BLOCK arises only after an inhibit cause (genuine write failure / blocking fault / identity) — recovery availability, not desync. One caveat: recovery lands in DISARMED and needs an explicit operator arm | C | — (availability note in §4) |
| Arm gate (DISARMED + unlatched + stable count) | ours | authority | C | — |
| External-writer preflight (arm time, 0x1060+17/+18) | fresh battery readback — but compared against **our per-process write provenance** (`_applied_objective`; a fresh process holds `None`) | **Yes — proven on hardware 2026-08-24 (~04:4xZ).** After any controller restart while `mid` was autonomously self-charging (its own firmware holding a ~-2.27 kW objective), the fresh process has no provenance, so the pod's OWN legitimate autonomy is classified "foreign": `external_writer` latches INHIBITED, and because the preflight re-latches while the objective persists — and daytime autonomy always persists — the privileged-acknowledge → re-qualify → re-arm path latches AGAIN at arm time. The lockout is absolute for the rest of the daylight window. Not stale DATA — stale PROVENANCE: our bookkeeping diverges from the battery's actual (autonomous, legitimate) state. | **B6** | Combined R1-shaped autonomy discrimination + operator-acknowledged takeover. See §2.6. |
| `objective_readback_unreadable` (arm refused) | fresh read failure | Genuinely unreadable → fail-closed arm refusal is correct (transient, non-latching) | D | Keep |
| `external_writer` latch (out-of-band or discharge-signature objectives) | fresh readback ≠ 0, ≠ our applied objective, and NOT matching the autonomy signature | For genuine foreign writers (the night-writer environment fact) the latch is the designed sole-writer protection — kept exactly | D | Keep (narrowed by B6 to the non-autonomy signature) |
| `identity_mismatch` latch | 0x8106 identity pair — core, fresh every cycle | Structural identity (a different device is speaking); pinned fresh, so no staleness window | C | — |
| `blocking_fault_active` latch | fault words — core, fresh | battery truth | A | — |
| `write_failed` inhibit | transport write failure | We genuinely could not command the battery | D | Keep |
| `authorization_lookup_failed` / `observation_lookup_failed` inhibits | our repositories | In-process stores; a failure is a component failure, not battery desync | C | — |
| Heartbeat consumption gates (unit/generation match, epoch + sequence exact-match to the minted authorization, used-cycles, not-before/expiry) | ours (single-use capability protocol) | Designed sequence-bound consumption; a mismatch rejects (ARMED_IDLE, no latch) and the next cycle re-mints | C | — |
| Connection-epoch change fence/revoke | ours | connection identity; self-heals in one cycle | C | — |

### Facade dispatch gating (`service.py`)

| Gate | Input source | Desync exposure | Class | Remedy |
|---|---|---|---|---|
| `device_debug_mode_active` refusal | 0x8100 debug readback — core, fresh every cycle | None — judged at the control rate (the 2026-08-23 incident-1 fix); positive evidence only, absence never refuses | A | — |
| `device_mode_not_remote` refusal | **ctrlMode 0x0100+1 — the once-per-process system tier, cached forever** | **Yes.** A unit that was in Local (2) at process start refuses EVERY dispatch forever with `device_mode_not_remote`, even after the operator flips it to Remote (1) — our cached word never refreshes. Exactly the soc pattern: our read plan manufactures the block | **B5** | R2 (fresh re-read before refusal). See §2.5. |
| Absent mode evidence refuses nothing | — | Confirmed: `None` (block unserved / unit unpublished) never blocks — the advisory doctrine holds | A | — |
| Arbiter selection, cross-site, scopes, idempotency | ours | authority | C | — |

**Counts (by table row): A = 12, B = 6, C = 15, D = 10.**
Distinct class-B findings: B1, B2, B3, B4, B5, B6 (below).

## 2. Class-B findings, narratives, and fix designs

### 2.1 B1 — the system-SOC quality gate still gives a once-per-process register a permanent veto

**Input.** `quality_system_soc_pct` in `SafetyKernel._required_quality`
(`safety.py`), and `safety_data_complete` / `_SAFETY_QUALITY_FIELDS` in the
actor's qualification path — all require the system controller's SOC word
(0x0100+17) to decode GOOD. That block is read **exactly once per process**
(cycle 1) and merged from the slow cache thereafter (§0.1).

**Divergence scenario (live-incident analog: the soc_disagreement block).** The
2026-08-24 remediation made the BMS SOC authoritative for every BOUND and turned
the divergence into `soc_disagreement_observed` — but the QUALITY gate on the
same stale word was left in the blocking set. Two reachable blocking paths
remain:

1. The single cycle-1 read decodes BAD (the decoder maps the low byte of +17;
  any value in 101..255 is out of the 0-100 domain → `None`/BAD). The bad words
  are cached forever and re-decode BAD every cycle → `quality_system_soc_pct`
  denies every cycle for the process lifetime.
2. Any cycle-1 unit-verification failure (RTU-ID mirror contradiction from the
  cold-ring parameters block, or a fault-block decode error) downgrades every
  GOOD quality to SUSPECT — including the cached system SOC — and because the
  system block never refreshes, a SUSPECT cached system SOC persists even after
  the transient cause clears, permanently resetting the actor's stable-sample
  counter (qualification never passes; the unit can never arm).

In both, the BMS SOC (core, fresh, GOOD) says the battery is readable and fine,
and our once-per-process tier blocks anyway — the operator's exact complaint.

**Live proof (2026-08-24, the boot-resync-then-freeze experiment).** `mid`'s
system SOC was pinned at 67 % — its cycle-1 value — with quality GOOD on every
subsequent cycle while the BMS SOC climbed to 83 % during deep self-charge.
This is the definitive confirmation of the mechanism: 0x0100 is served
cycle-1-only and never again, so the word freezes at boot while the battery
moves on. The already-shipped BMS-authoritative fix (safety.py judges every
SOC bound on `bms_soc_pct`; divergence is the `soc_disagreement_observed`
note) is confirmed correct by the same experiment — no bound moved with the
frozen word.

**Read-plan follow-up (the remaining question).** Two options exist for the
register itself: promote 0x0100+17 to a fresher tier, or stop decoding system
SOC as an input entirely (console-provenance only). This audit recommends
KEEPING the decode and moving the whole 0x0100 block into the cold ring (the
B5 plan, §2.5 — the same block carries ctrlMode): the divergence signal is
operationally valuable (it is exactly how the operator caught the original
incident), so deleting it removes evidence; refreshing it on the ring drops
the staleness from UNBOUNDED to ≤ ~108 s while B1's remedy keeps it out of
every blocking set. Post-B1 the system SOC is console provenance and divergence
telemetry — nothing more — and post-B5 it is honestly timestamped rather than
boot-frozen. "Stop decoding entirely" is rejected only on that evidentiary
ground; it would otherwise be safe.

**Remedy — R1 (battery-authoritative precedence).** The system SOC is already
non-authoritative for every bound; finish the demotion. Move `system_soc_pct`
from the REQUIRED safety-quality set to the advisory tier (alongside
`grid_power_w`): the quality-map key stays (honest inventory; the decoder keeps
emitting it and keeps standing the BMS SOC in when the block is absent), but
neither the kernel's `_required_quality` nor `safety_data_complete` nor the
actor's `_SAFETY_QUALITY_FIELDS` judges it. A BAD/SUSPECT system SOC becomes
part of the divergence signal: when `system_soc_pct` is non-GOOD, decisions
carry the existing informational note (`soc_disagreement_observed` may be
extended or a sibling `system_soc_untrusted` note added) — never a denial.

**What stays fail-closed.** `quality_bms_soc_pct` (the authoritative figure)
non-GOOD still denies and still blocks qualification; the SOC floor/ceiling and
the SOC-jump check still bind on the fresh BMS word; every other required field
is core-rate. Two consecutive failed polls of the BMS block still deny via
`telemetry_stale` (class D untouched).

### 2.2 B2 — `soc_jump` turns the battery's own resync (or our poll gap) into a one-cycle block

**Input.** `abs(bms_soc_pct − previous.bms_soc_pct) > max_soc_jump_pct` — both
figures fresh BMS reads, but the comparison baseline is OUR previous stored
observation, with no notion of the elapsed time between them.

**Divergence scenarios.**
(a) *Gap-induced:* an abandoned poll window (or several) leaves a multi-second
gap between consecutive stored observations; a legitimately fast SOC move across
the gap (heavy cycling at 2.5 kW on these packs) exceeds 10 % and is flagged as
"implausible" although both endpoints are honest battery reads. The
`telemetry_stale` deny already covered the gap; `soc_jump` then adds ONE MORE
denied cycle after telemetry recovers — manufactured purely by our bookkeeping.
(b) *Battery-induced:* this fleet's BMS is evidenced to jump its reported SOC
(PROTOCOL_EVIDENCE §13.17 records the observed large SOC jumps as an open
uncertainty). When the BMS resyncs its estimate, the jump is the battery's own
truthful new word — and we block on it for a cycle.

**Remedy — R1 (battery-authoritative re-baseline).** The fresh BMS SOC stands;
the jump becomes the informational reason `soc_jump_observed` carried ONLY on
authorizing decisions, exactly like `soc_disagreement_observed`, and the next
cycle's baseline is the new figure (automatic re-sync — the operator's words).
The absolute protections are untouched and are the real guards: if the jumped
value crosses the discharge floor or charge ceiling, THAT bound denies (the
safe direction — a battery that jumped to 96 % must not charge, and still will
not).

**What stays fail-closed.** `quality_bms_soc_pct`, `nonfinite_safety_data`, the
floor/ceiling on the fresh figure. The corruption-canary function the jump
check once served (wrong-unit data, garbled decode) is already carried by
identity pinning (per-transport 0x8106, core-rate) and the decoder's 0-100
domain validation, which is why R1 does not weaken real protection here.

### 2.3 B3 — the previous-observation warmup blocks the first cycle after every restart

**Input.** `previous_observation_missing` (kernel deny) plus `_eligible`'s
`selected <= previous.keys()` requirement: a unit needs TWO stored observations
before any authority can be minted.

**Divergence scenario.** Every controller restart (the fleet's most common
operational event — each deploy): boot → first poll (~1.5 s) → first tick finds
no previous pair → every active proposal is REJECTED → second poll → authority.
The battery was readable and fresh the whole time; the missing thing is our
second sample. The same shape bites after a simulated reconnect via
`observation_epoch_changed` (one cycle, self-healing). Cost today: one blocked
cycle per unit per restart, with a standing operator intent visibly rejected.

**Remedy — R2 in its deferred-decision reading, tightened.** The current
behavior is already an explicit, audited one-cycle deferral — the minimal R2
compliant outcome. The design goes one step further because the deferral buys
nothing: treat a unit's FIRST observation as its own baseline. All
pair-derived checks (order, sequence, epoch, cell-sequence, soc_jump) are
vacuous with no baseline; every value-judging check (quality, staleness, SOC
bounds, cells, temperatures, faults, dynamic limits) still runs on the fresh
observation. `_deny_reasons` skips pair checks when `previous is None`;
`_evidence_is_coherent` and `_eligible` accept a unit whose current observation
is its first.

**What stays fail-closed.** Every bound that consumes VALUES still applies to
the first observation exactly as to any other; a unit with NO observation is
still denied (`observation_missing`, class D); `_evidence_is_coherent`'s typing
requirements still apply to the current observation. Only the comparison
against a nonexistent baseline becomes vacuous — it cannot weaken a real check
because there is nothing to compare.

### 2.4 B4 — cell-bound denies judge a ≤ 3-cycle-old tier while the live battery may have recovered

**Input.** `cell_voltage_low`, `cell_voltage_high`, `cell_imbalance` over
`cell_voltages_v` — the 0x5200 window served every 3rd cycle, merged GOOD from
the slow cache, honestly time-stamped but judged as-is.

**Divergence scenarios.** (a) *Stale violation:* load sag pushed a cell below
2.80 V; the intent lapsed and power stopped; cells recovered — but for up to
two further cycles (~3 s, and up to the 15 s age bound under poll jitter) the
cached window still shows the dip and denies a NEW intent while the battery is
fine. (b) The 2026-08-23 LHS imbalance incident is the mirror image: a REAL
54 mV spread, correctly denied — and the operator's investigation manually
confirmed it with fresh cell windows ("stable spread across 9 cell windows,
fresh advancing cell data"). That manual fresh-read-before-concluding is
precisely what the remedy automates.

**Remedy — R2 (deny-triggered tier promotion).** When a control decision
carries a cell-derived deny reason for unit U (`cell_voltage_low`,
`cell_voltage_high`, `cell_imbalance`, `cell_count_invalid`), U's owning
actor's NEXT telemetry cycle includes the 0x5200 window regardless of the
`cycle % 3` phase (a `request_cell_refresh()` flag on the actor, set by the
fleet loop after the tick from the decision's reason codes — the loop already
runs heartbeat → poll → tick in one task, so the flag costs one line of
wiring). The deny therefore lasts at most one cycle and is re-evaluated on
FRESH battery data; a violation that persists in the fresh read is genuine and
keeps denying. Budget: the promotion adds one window only on cycles following
a cell deny (steady 8→9 windows ≈ 1.0 s at the 0.1 s inter-frame gap, inside
the 1.5 s control period and the 1.60 s renewal budget in the commissioned
config; the API_CONTRACTS read-plan line is amended to "≤ 9 windows while a
cell-deny re-verification is active"). Always-core cells was considered and
rejected: it would make the steady plan 9-10 windows permanently, spending the
bootstrap headroom the commissioned cadence budget reserves.

**What stays fail-closed.** `cell_data_stale` (15 s = genuinely unreadable);
`cell_count_invalid` from a fresh read (structural); a fresh-read violation
(persistent = genuine = deny). Two consecutive failed cell-window reads fall
back to the whole-poll failure path (class D).

### 2.5 B5 — the dispatch-time "not Remote" gate judges a once-per-process mode word

**Input.** `device_mode_not_remote` in `submit_intent`/`submit_advisory_intent`
refuses while `ctrl_mode_remote is False`, i.e. while ctrlMode (0x0100+1) reads
2 (Local). The system overview block is the once-per-process tier: the word is
read at cycle 1 and cached forever (§0.1). The debug-mode half of the gate is
NOT exposed (0x8100 rides the core, judged at the control rate — correct).

**Divergence scenario.** A pod that is in Local at controller boot refuses
EVERY dispatch for the process lifetime — `device_mode_not_remote` on every
`POST /api/v1/intents` naming it — even after the operator (or the vendor app,
which touches these units daily) flips it to Remote. The battery is readable
and fine; our cached word blocks. This is the same read-plan shape as the SOC
incident, one register over, and it blocks at the API boundary before the
kernel is ever reached.

**Remedy — R2 (bounded fresh re-read before refusal).** The refusal path — not
the control cycle — performs one bounded fresh read of the system-mode window
(0x0100, 3 words: ctrlMode +1, workMode +2) through the unit's owning actor (a
new serialized mailbox operation, priority below heartbeat, bounded by
`write_timeout_s`), ONLY when the cached word would refuse (positive-evidence
refresh before deny). The cached word alone never refuses. A fresh-confirmed
non-Remote (or a failed refresh read, twice) refuses — genuine class D.
Companion change: move 0x0100 into the cold ring as the `read_plan()` docstring
already (incorrectly) claims, so the display-mode words and system SOC
semi-refresh (~108 s) instead of being pinned to the process start; the ring's
per-cycle window count is unchanged (one cold window per 8th cycle either way),
and after B1 the refreshed system SOC is advisory-only.

**What stays fail-closed.** Fresh-confirmed Local still refuses dispatch;
`device_debug_mode_active` still refuses on its core-rate word; absent evidence
still refuses nothing (advisory doctrine preserved); two consecutive failed
refresh reads refuse (genuinely unreadable).

### 2.6 B6 — the arm-time sole-writer preflight misclassifies the pod's OWN autonomy as a foreign writer (stale provenance, proven on hardware)

**Input.** `_verify_sole_writer_owned` (`actor.py`): at arm time the actor
reads the served PQ objective readback (0x1060+17/+18) — a genuinely FRESH
battery read — and refuses unless it is (0, 0) or equals `_applied_objective`,
the last objective THIS PROCESS wrote. A fresh process holds `None`, so any
nonzero readback is "foreign" by construction. The READ is battery truth; the
COMPARISON is our per-process bookkeeping. This is the class-B pattern in a new
guise: not stale data — stale PROVENANCE.

**Live incident (2026-08-24, ~04:4xZ, `mid`).** After a controller restart
while the pod was autonomously self-charging, the pod's own firmware held a
nonzero PQ objective (~-2.27 kW, charge direction). The fresh process had no
write provenance, so the preflight read a nonzero objective "it did not write"
and latched INHIBITED with cause `external_writer` (privileged acknowledgement
required). The lockout is ABSOLUTE for the daylight window, because the
acknowledgement escape hatch cannot escape: the preflight re-latches while the
foreign objective persists, and daytime autonomy always persists —
acknowledge → 3 stable samples → arm → preflight sees the (still autonomous,
still unprovenanced) objective → LATCHED again.

**Timing analysis — why "just retry" is unwinnable.** The direction-trial
family of measurements plus this incident give the numbers. A bounded
zero-write `[1,0,0]` is reverted by the device in ~1.34 s: the pod's autonomy
re-establishes its objective on roughly that timescale. Our re-qualification
path needs 3 stable observations at the 1.5 s control period (~4.5 s) plus the
arm itself — ~5-6 s end to end. So any remedy of the form "zero first, let
provenance go clean, qualify, then arm" loses the race by ~4 s EVERY time: at
arm time the readback is nonzero again (autonomy) and unprovenanced (fresh
process) → foreign, forever. The race is structural, not a matter of tuning
sample counts.

**Doctrine reconciliation.** CONTINUITY (2026-08-23) pins the environment
facts: "other applications monitor these batteries read-only by day and write
only at night; daytime the pods self-manage solar charging; night writes will
trip our arm-time external-writer preflight by design (coordinate, don't
fight)." The preflight was designed against ANOTHER CONTROL SYSTEM — and for
that writer the latch is correct and stays. What the doctrine did not
anticipate is that during the day the nonzero objective is written by the
battery itself: the pod's CT-following autonomy is not a competing system but
the battery's own desired behavior, which this controller EXPLICITLY preserves
by design (the fail-safe of every non-renewal is "the firmware watchdog returns
the pod to its own CT-following autonomy"; the beat-autonomy doctrine likewise
treats autonomy as the baseline our objective temporarily REPLACES). For the
battery's own autonomy, "coordinate, don't fight" resolves to "take over
cleanly, hand back by non-renewal" — which is precisely the designed interplay.
The preflight conflated two causes of "nonzero and not ours" — foreign
controller vs pod autonomy — into one latched refusal. The fix separates them.

**Remedy — (i) autonomy-signature discrimination as the default, combined with
(ii) operator-acknowledged takeover as the override; (iii) rejected.**

- **(i) AUTONOMY-SIGNATURE DISCRIMINATION (default arm path).** A nonzero
  readback in a fresh-provenance process is classified `pod_autonomy_objective`
  — arm proceeds WITHOUT latch — when ALL of: (a) charge direction (P < 0;
  the evidenced daytime autonomy is self-charge, never discharge); (b) Q == 0;
  (c) |P| within a commissioned per-unit autonomy band (a new policy key, e.g.
  `autonomous_charge_band_w`, defaulting to the unit's static charge limit —
  the incident's -2.27 kW proves the band must cover deep CT surplus, not just
  the ~-520..-560 W trickle the earlier evidence recorded); and (d) the process
  wrote nothing (the existing `_applied_objective is None` condition). The
  classification is audited on the arm (`arm_provenance: pod_autonomy` with the
  served P/Q) and exposed on the facade snapshot so the console shows WHY arming
  was allowed over a nonzero objective. Our first renewed heartbeat then
  replaces the objective exactly as the beat-autonomy doctrine prescribes —
  that replacement is the designed interplay, not a fight.
- **(ii) OPERATOR-ACKNOWLEDGED TAKEOVER (override).** `arm` gains an explicit,
  audited confirmation field (e.g. `takeover_confirmed: true`; `arm` scope +
  interactive principal already required) for objectives that FAIL the
  signature — a deeper surge than the commissioned band, or any case where the
  operator can see (console: battery watts vs objective vs grid CT) that the
  "writer" is the pod itself. This bounds the harm of a too-narrow band: the
  operator is never locked out by band tuning; the band only smooths the common
  case. The existing inhibit-acknowledgement endpoint remains for the latched
  path.
- **(iii) PROVENANCE PERSISTENCE — REJECTED.** Persisting `_applied_objective`
  across restarts would dissolve the ambiguity but is forbidden territory:
  boot is observe-only and nothing that smells of restored authority may be
  read back from persistence (API_CONTRACTS "Runtime composition"; the volatile
  stores start empty on every build). Both (i) and (ii) are provenance-free and
  sufficient; nothing is gained by crossing that line.

**What stays fail-closed.** Positive (discharge) objectives, Q ≠ 0, and
out-of-band magnitudes still latch `external_writer` exactly as today — the
night-writer protection is untouched. An unreadable readback still refuses the
arm (class D, `objective_readback_unreadable`). The takeover override is
interactive, scoped, and audited; it cannot be exercised by automation (the
adviser holds no `arm` scope and is non-interactive). If a genuine foreign
charge writer happens to sit inside the band by day, the exposure is bounded:
our first heartbeat replaces the objective, every authorization remains
short-lived behind the generation fence, and stop/fence/shutdown still dominate
— the design accepts that residual, on the environment fact that external
writers write at night, and records the classification in the audit trail for
after-the-fact detection.

## 3. Implementation plan (for the successor agent)

Ordered by ascending invasiveness; each step lands contract-first with its red
test. No step weakens a class-D path: every remedy removes only the
OUR-STALE-INPUT veto, never the cannot-read-the-battery veto.

**Step 1 — B1 (system SOC demoted to advisory).**
- Contract: API_CONTRACTS "BMS-authoritative SOC (2026-08-24)" gains one
  paragraph: the system SOC word is ADVISORY telemetry — outside the kernel's
  required-quality set and outside `safety_data_complete`; a non-GOOD or
  divergent system SOC surfaces as informational reason codes on authorizing
  decisions only (`soc_disagreement_observed`, optionally
  `system_soc_untrusted`), never a denial or a qualification reset.
- Red tests: `tests/unit/test_safety.py` — (a) an observation with every
  required field GOOD but `quality_system_soc_pct = BAD` AUTHORIZES (today:
  `quality_system_soc_pct` deny); (b) `quality_bms_soc_pct = BAD` still denies.
  `tests/unit/test_domain.py` (or the qualification cases in
  `test_actor.py`) — `safety_data_complete` is True with a BAD system-SOC
  quality; the actor qualifies. `tests/unit/test_wire_decode.py` — the 12-key
  quality-map shape is unchanged.
- Files: `src/energypod/domain/observations.py` (introduce
  `REQUIRED_SAFETY_QUALITY_FIELDS = QUALITY_FIELDS − {"system_soc_pct"}`;
  `safety_data_complete` judges that set), `src/energypod/application/safety.py`
  (`_required_quality` uses the new set), `src/energypod/application/actor.py`
  (`_SAFETY_QUALITY_FIELDS` alias).
- Risk note: a genuinely corrupt SYSTEM SOC no longer blocks — acceptable
  because post-2026-08-24 no bound consumes it; the consumed figure (BMS SOC)
  keeps its full fail-closed gate. Keeping the quality-map KEY (only the
  required-set membership moves) preserves the decoder's pinned 12-key shape
  and every existing wire-decode vector.

**Step 2 — B2 (soc_jump informational re-baseline).**
- Contract: API_CONTRACTS "Safety kernel and arbitration" — the SOC-jump check
  becomes the informational `soc_jump_observed` note (authorizing decisions
  only, exactly the `soc_disagreement_observed` pattern); floor/ceiling on the
  fresh BMS figure remain the enforcing bounds.
- Red tests: `tests/unit/test_safety.py` — previous 40 % → current 55 %
  (jump > 10) AUTHORIZES with `soc_jump_observed` among the reason codes;
  current 96 % against a 95 % ceiling still denies `soc_above_charge_ceiling`
  (the jump note must not mask the bound); the note never appears on a rejected
  decision.
- Files: `src/energypod/application/safety.py` (`_deny_reasons` drop,
  `_soc_jump_observed` alongside `_soc_divergence_observed`; thread through
  `evaluate` like `divergence_observed`).
- Risk note: the implausible-data canary is deliberately ceded to identity
  pinning + decoder domain validation; the dangerous direction (a jumped SOC
  crossing a bound) is still caught by the bound itself, evaluated on the NEW
  figure.

**Step 3 — B3 (first-observation baseline).**
- Contract: API_CONTRACTS "Safety kernel and arbitration" — a unit's first
  observation is its own baseline: pair-derived checks are vacuous (skipped)
  when no previous observation exists; all value checks apply unchanged.
- Red tests: `tests/unit/test_safety.py` — `previous_observations={}`
  AUTHORIZES (today: `previous_observation_missing`); with a previous present,
  the existing order/sequence/epoch cases (the `REASON_CODE_CASES` table)
  still deny. `tests/unit/test_control_kernel.py` — a boot composition with
  exactly one observation per selected unit MINTS a batch (today: no batch,
  `control_decision_not_authorized`).
- Files: `src/energypod/application/safety.py` (`_deny_reasons`,
  `observation is None` vs `previous is None` split),
  `src/energypod/application/control_kernel.py` (`_evidence_is_coherent`,
  `_eligible` previous requirement).
- Risk note: the only removed checks compare against a baseline that does not
  exist on the first observation — vacuous by construction. The
  `observation_missing` (no observation AT ALL) deny and every value check are
  untouched.

**Step 4 — B5 (mode-gate fresh re-read + system block to the cold ring).**
- Contract: API_CONTRACTS "Device-mode telemetry and dispatch gating" — the
  `device_mode_not_remote` refusal is judged on a FRESH bounded read of
  0x0100(+1..+2) through the owning actor at refusal time; the cached word
  never refuses alone; two consecutive failed refresh reads refuse (class D).
  The read-plan tier note is corrected: 0x0100 rides the cold ring (~108 s),
  not a once-per-process read.
- Red tests: `tests/unit/test_service_facade.py` — a unit whose cached
  ctrlMode is Local but whose fresh refresh read returns Remote ACCEPTS the
  intent (today: `device_mode_not_remote` refusal); a fresh-confirmed Local
  still refuses; a failing refresh read twice still refuses.
  `tests/unit/test_live_composition.py` — the composed plan includes 0x0100 on
  some cold-ring cycle after cycle 1 (today: only cycle 1); the steady
  per-cycle window count stays within the amended budget.
- Files: `src/energypod/application/actor.py` (mailbox operation
  `refresh_mode_words`, bounded), `src/energypod/application/service.py`
  (refusal path performs the refresh), `src/energypod/runtime/composition.py`
  (`read_plan()`: move `_SYSTEM_BLOCK_BASE` into the cold ring; fix the
  docstring).
- Risk note: the refresh read runs on the API path inside the actor's
  serialized mailbox (sole transport ownership preserved) and is bounded by
  `write_timeout_s`; it cannot delay a heartbeat (heartbeat priority is
  higher and preempts). Fail-closed is preserved by refusing on repeated
  refresh failures. The cold-ring move adds no per-cycle window (the ring
  serves one window per 8th cycle either way).

**Step 5 — B6 (autonomy-signature arm path + operator-acknowledged takeover).**
May be pulled forward ahead of Step 4 given operational severity: it is the
only finding that produces an ABSOLUTE operator lockout (daylight arming
impossible after every restart during self-charge).
- Contract: API_CONTRACTS "Write-enabled run mode (live control)", bullet 3 is
  amended: the arm-time preflight classifies a fresh-provenance nonzero
  objective matching the autonomy signature (P < 0, Q == 0, |P| within the
  commissioned `autonomous_charge_band_w`) as `pod_autonomy_objective` — the
  arm proceeds, the classification is audited (`arm_provenance: pod_autonomy`
  with the served P/Q) and snapshot-exposed; `arm` additionally accepts an
  explicit audited `takeover_confirmed` confirmation (interactive `arm` scope)
  for out-of-signature objectives; discharge-signature, Q ≠ 0, and
  out-of-band objectives still latch `external_writer` exactly as today.
  Provenance persistence across restarts remains forbidden (boot stays
  observe-only). ControlPolicy/config gain the per-unit autonomy band key.
- Red tests: `tests/unit/test_actor_external_writer.py` — (a) fresh process,
  readback (-2270, 0), band covering it: arm SUCCEEDS, lifecycle ARMED_IDLE,
  audit carries `arm_provenance: pod_autonomy` (today: INHIBITED
  `external_writer`); (b) readback (+1500, 0): still latches; (c) readback
  (-2270, 300) (Q ≠ 0): still latches; (d) readback beyond the band: still
  latches; (e) `takeover_confirmed` on case (d): arm proceeds, audited;
  non-interactive principals cannot set it; (f) unreadable readback: still a
  transient arm refusal. `tests/unit/test_write_enabled_run.py` — the composed
  wiring passes the policy band to the actor. `tests/unit/test_config.py` —
  the new key validates (positive, ≤ static charge limit).
- Files: `src/energypod/application/actor.py` (`_verify_sole_writer_owned`
  signature classification + the confirmation argument; audit + snapshot
  fields), `src/energypod/application/service.py` (arm passthrough of the
  confirmation, interactive-principal enforcement), `src/energypod/domain/models.py`
  (policy key), `src/energypod/runtime/config.py` + `composition.py`
  (configuration + wiring), `config/config.live-write-example.yaml` (band
  commissioned from the 2026-08-24 evidence).
- Risk note: this deliberately narrows the sole-writer latch for charge-direction
  in-band objectives during fresh provenance. The residual (a foreign in-band
  charge writer misread as autonomy) is accepted on the pinned environment fact
  that external writers write at night, bounded by the objective replacement on
  our first heartbeat, short authorization lifetimes, stop/fence dominance, and
  the audit trail that records every autonomy classification. The band is a
  commissioned policy constant — NOT derived from the readback — so it cannot
  be widened by whatever value happens to be on the wire.

**Step 6 — B4 (deny-triggered cell tier promotion).**
- Contract: API_CONTRACTS "Safety kernel and arbitration" +
  "Read-plan tier promotion" — a decision carrying a cell-derived deny reason
  for a unit promotes that unit's next telemetry cycle to include 0x5200;
  the steady plan budget line becomes "≤ 9 windows while a cell-deny
  re-verification is active".
- Red tests: `tests/unit/test_safety.py` — unchanged semantics (fresh
  violations still deny). `tests/unit/test_live_composition.py` /
  `tests/unit/test_actor.py` — after a decision whose reasons include
  `cell_voltage_low` for a unit, that unit's next `read_plan()` includes the
  cell window regardless of `cycle % 3`; a cleared fresh window clears the
  deny on the following tick; a persisting violation keeps denying.
- Files: `src/energypod/application/actor.py` (`request_cell_refresh()`
  flag consumed by the poll), `src/energypod/runtime/composition.py`
  (`_LiveDecodeTelemetry.read_plan()` honors a refresh hint; the fleet loop
  sets it from the tick's decision reasons).
- Risk note: worst-case sustained cell denies keep cells in every cycle (9
  windows ≈ 1.0 s, inside the 1.5 s period / 1.60 s renewal budget — state
  this against the config comments in the PR). The remedy only ever ADDS
  reads of the datum the deny judged; it never bypasses a fresh violation.

**Step 7 — side finding S1 (§5) if the operator confirms intent:** correct the
configured `blocking_fault_codes` to the decoder's real generated codes
(`PCS_WARNING0_1`, `DCDC_WARNING0_1` — the EE-calibration bits are WARNING
bits per PROTOCOL_EVIDENCE §9, and `blocking_warning_codes` is the set the
composition should wire). Configuration-only plus a validator warning for
unknown codes; out of desync scope but fail-open today.

## 4. NOT-CHANGED list (classes A, C, D) — with reasons

- **A — battery-truth, correct as-is:** BMS-SOC floor/ceiling (core-rate, the
  2026-08-24 remediation); dynamic-capability/zero-headroom (the battery's own
  limit words, core-rate); ramp limiter (core-rate watts, staleness bounded by
  `max_telemetry_age_s` and re-checked by `telemetry_stale`); temperature
  gates (0x523C core); blocking faults (core fault words); allocator headroom
  (dynamic limits, core); `device_debug_mode_active` (core-rate word);
  `soc_disagreement_observed` (the landed R1); export-evidence trio
  (advisory-only, can only lower power, never blocks operator intents);
  absent-mode-evidence-refuses-nothing (advisory doctrine confirmed). (The
  external-writer arm preflight was initially classified here — its READ is
  fresh battery truth — but the 2026-08-24 restart-while-autonomous incident
  proved its PROVENANCE comparison is our own per-process bookkeeping: it is
  finding B6, §2.6.)
- **C — structural authority, not a sync question:** all proposal-shape
  reasons; intent expiry; lifecycle gating; observation order/sequence/epoch
  coherence (our monotone per-process counters — a delayed poll appends
  nothing and cannot disorder the stored pair); kernel matcher, `_eligible`
  structure, generation fencing/publish CAS/reconciliation (the 2026-08-23
  publish-fence fix is authority machinery, kept exactly); heartbeat
  single-use consumption; identity-mismatch latch (fresh core-rate identity);
  stable-sample warmup and inhibit-latch/acknowledgement protocol (recovery
  availability is a deliberate operator-in-the-loop choice); arbiter, fleet/
  static limits, emergency stop; audit-fact verification; clock sanity checks.
- **D — genuinely-unreadable, fail-closed verified:** `observation_missing`;
  `telemetry_stale` (a failed window read aborts the whole poll — the check
  really means "could not read the battery within 3 s"); `cell_data_stale`
  (≥ 3 consecutive whole-poll failures); quality gates on core-rate fields
  (BAD = served out-of-domain, SUSPECT = verification failure — contradictory
  evidence, fail-closed correct); `cell_count_invalid` (served topology
  contradicts commissioning); the SUSPECT blanket downgrade on
  unit-verification failure; `objective_readback_unreadable` (arm refusal,
  transient); `write_failed` inhibit (we could not command the battery);
  served-bank/probe mismatch (whole poll fails); export-bound collapse to 0
  (advisory fail-closed).
- **Availability notes (recorded, deliberately not changed):** after any
  transient inhibit the unit recovers only to DISARMED and needs an explicit
  operator arm; and `blocking_warning_codes` is wired empty in the
  composition. Both are authority/availability choices, not desync defects.

## 5. Side observation (not desync, flagged for the operator)

**S1 — the configured blocking fault codes can never match.** The decoder
generates codes as `{prefix}_{bit}` (e.g. `PCS_WARNING0_1`), but
`config.live-write-example.yaml` lists human names
(`PCS_EE_CALIBRATION_OUT_OF_RANGE`, `DCDC_EE_CALIBRATION_OUT_OF_RANGE`) under
`blocking_fault_codes` — and per PROTOCOL_EVIDENCE §9 those EE-calibration
signals are WARNING bits, not fault words, while the composition wires
`blocking_warning_codes` to the empty set. The intersection is therefore
always empty: the operator's intended calibration-fault block is silently
inert (fail-open misconfiguration, the mirror image of this audit's concern).
Fix path in Step 6 above; a config validator warning for unmatched code names
would prevent recurrence.
