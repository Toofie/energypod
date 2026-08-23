# Energy scorecard — the daily energy account (design)

Prepared: 2026-08-26 (post-`f0b9288`). Accepted design for implementation —
contracts, evidence protocols, red-test families, and the ordered plan. No code,
API, hardware, or test interaction was performed to produce this document; the
src/ greps behind the "what exists" claims are cited inline.

Sources: `docs/PRODUCT_NEXT.md` (2026-08-26 refresh — this feature is its ranked
next), `docs/CONTROL_SURFACE_GAP_ANALYSIS.md` R8, `docs/PROTOCOL_EVIDENCE.md`
§4c/§6 and `docs/evidence/field-mapping-2026-08-22.md` §2.7/§5 A-1 (the counter
evidence), `docs/CONTINUITY.md` (delivered baseline through the schedules
live-test entry), `docs/API_CONTRACTS.md` (snapshot/advisory/composition
doctrines), plus read-only verification of `register_layout.py:143` (the
`0x4101` read is in the plan), `composition.py` read-plan tiers (energy rides
the cold ring; the PCS live block is control-rate while the `excess_charging`
block is present), `observations.py` (advisory field pattern), and
`simulator/pod.py` (`_rebuild_totals_block` serves all six counters;
charge/discharge accumulate, grid/load/PV are seeded statics today).

Operator framing this answers: every control theme the operator has touched is
delivered (per-battery dispatch, honest display, excess-solar present-but-off
awaiting their toggle, schedules live and operator-tested). The one surface
family nobody has built is the one their prior dashboard led with and the
vendor app's home chart showed — the day's energy numbers. This design is R8
made real, evidence-gated where the counters are unproven, with a fully-owned
fallback where they are.

---

## 0. What exists, what this adds

| Fact today | Where | Consequence |
|---|---|---|
| The `0x4101` 12-register cumulative energy block is READ every cold-ring rotation (~108 s period, one slow window per 8th cycle) and discarded — `Observation` carries no energy fields | `register_layout.py:143`, `composition.py` read plan | The data is already flowing; nothing decodes or keeps it |
| Counter decode contract is CONFIRMED: six low-word-first `uint32 × 0.1` kWh pairs in vendor order grid-buy, grid-sell, load, PV, BMS-charge, BMS-discharge | PROTOCOL_EVIDENCE §6; `SysControl.cs:779-784` | Decoding is not evidence-gated |
| Charge/discharge ROLE labels are CONFIRMED (capture 2: only the discharge-labeled counters moved during confirmed discharge); buy/sell ROLE labels are UNCONFIRMED (A-1 remaining) | field-mapping §5 A-1 | The grid pair cannot be labeled "bought/sold" yet — §3 |
| Site PV is NOT wired to the pod inputs: PV counters read 0.0 kWh (mid/rhs) and 6.2 kWh one-off noise (lhs) | field-mapping §2.7, A-12 | No "solar production" number exists to show; §1 handles this honestly |
| Per-pod CT grid power (`0x1000+17`, negative=import/positive=export, control-grade PCS view) and load power (`0x1000+20`) are decoded as advisory fields and refresh at the 1.5 s control rate while the `excess_charging` block is present (it is, present-but-off) | PROTOCOL_EVIDENCE §4c; `composition.py` `_promote_pcs_live_block` | We OWN a 1.5 s per-phase grid sample stream — the fallback source for bought/sold |
| The excess adviser's state projection (`adviser_state`: `active`, `target_unit_id`, `held_intent_id`) is single-writer and snapshot-carried | API_CONTRACTS "Adviser state projection" | Surplus attribution has an authoritative per-tick predicate — §2 |
| SQLite persistence has a schema-version migration discipline and paired memory/sqlite repository adapters | API_CONTRACTS "Operations surface" | A per-day ledger store follows the established pattern |

What this adds: decode of the six counters into advisory `Observation` fields;
an `EnergyAccountant` application component that integrates the CT stream,
deltas the counters, rolls days at the site-timezone midnight, and persists
day records; one read REST route + a snapshot `energy_today` block + one bus
event; a Home "Today" card and the first real Insights content; and the A-1
buy/sell pinning protocol with its evidence record. Advisory only — no new
authority, no writes, no read-plan cadence change (§7).

## 1. The operator model (plain language first)

The card the operator opens the console for, in their language:

> **Today (so far)**
> Bought from grid **8.4 kWh** · Sold to grid **12.9 kWh**
> Charged **6.2 kWh** · Discharged **4.1 kWh** · House load **14.7 kWh**
> Solar-surplus charging moved **3.1 kWh** that would have been exported.
> *Figures 99% coverage, 14:03. Solar panels are not measured by the pods —
> "sold" is the surplus the site exported.*

Rules that make it honest (the operator's own doctrine, applied):

1. Every figure carries its provenance class (§2) and the day's coverage
   fraction; a day below the commissioned coverage threshold renders as a
   **partial day** with the percentage, never as a quiet full number.
2. Nothing is ever zero-filled. An unread counter, a unit that has not yet
   published, or a metric whose source was absent all day renders "not
   available" for that unit/metric.
3. Solar production is explicitly NOT shown as a measurement. The pods' PV
   inputs are unwired at this site (counters read ~0); the scorecard's solar
   story is what IS measured: the surplus the site exported ("sold") and the
   surplus the batteries captured ("charged from surplus"). A footnote says
   exactly this once, on the card and in Insights.
4. Money appears only if the operator commissions tariff keys (§7); otherwise
   the card is kWh-only. kWh figures are facts; a tariff is the operator's
   number, labeled as theirs (`AUD` at `28.0c` import / `9.0c` export).
5. The day boundary is the site timezone (`Australia/Brisbane`), the same zone
   the schedule plan already carries; every stored record names its zone so a
   zone change never silently re-dates history. DST transition days are
   honest 23- or 25-hour days — the record stores `utc_offset_minutes` per day.

## 2. Metric sources and the A-1 gate

One decision per metric, each pinned to its evidence:

| Scorecard metric | v1 source | Why | Provenance label |
|---|---|---|---|
| Battery charged / discharged (per unit, fleet sum) | **Device BMS counters** `0x4101+8..11` (≡ `0x5000+15..18`, byte-identical blocks) — daily delta of the cumulative reads | Role labels CONFIRMED by capture 2; device-side accumulation covers controller downtime (restarts, resyncs — the device kept counting); 0.1 kWh quantization ≈ ≤1% of a typical day | `device_counter` |
| House load (per unit, fleet sum) | **Device load counter** `0x4101+4/5` — daily delta | Vendor decode confirmed; field-mapping verdict OK*; same coverage argument | `device_counter` |
| Bought / sold (per unit, fleet sum) | **Our own integration** of the per-pod CT `grid_power_w` (`0x1000+17`, PCS view only) at the 1.5 s control rate, sign-split: `∫max(0, −P)` = bought, `∫max(0, +P)` = sold | The grid counter pair's ROLES are unpinned (A-1) — presenting vendor-labeled "buy/sell" deltas would risk a silently swapped day-one headline; the CT stream is OUR sample with a live-proven sign contract (§4c), and the PCS block is already control-rate on this deployment | `integrated_ct` |
| Charged from surplus (per target unit, fleet sum) | **Attribution integral**: `∫max(0, −measured_battery_w)` over ticks where `adviser_state.active` and `adviser_state.target_unit_id == unit` | The graduation protocol's criterion 6 ("the operator accepts the economics — kWh shifted vs the autonomy baseline") has no evidence surface today; this is it, computed from measured watts (what physically happened), not authorized watts | `attributed_adviser` |
| Grid counter pair (both, unlabeled) | Decoded and RECORDED from day one into each day record's `counter_cross_check` field (vendor-label deltas + the integrated figures) | This is the A-1 passive pinning evidence stream (§3) — recorded regardless of which grid source is active | `evidence_only` |
| PV cumulative | Decoded into the unit-detail readthrough as the raw cumulative, with the A-12 noise note | Present for completeness; NEVER a scorecard line | `readthrough` |

Two standing metering caveats become design constraints, not surprises:

- **Only the PCS grid view is ever integrated** (`0x1000+17`). The system-block
  view (`0x0137`) diverges from it by ~89 W on mid (field-mapping §5 A-3/A-14);
  §4c's rule "the two views are never merged into one value" extends to "the
  scorecard integrates exactly one view, the control-grade one".
- **Counter decreases are resets, not deltas.** The vendor clear-energy write
  (`0x8001`) stays excluded from every product surface, but an external
  service could clear one counter independently (A-1 hypothesis (a)). A
  cumulative that DECREASES between reads never yields a negative delta: that
  unit-metric-day is recorded from the new baseline and flagged
  `counter_reset_observed` (audit event, `reset` in the record's per-metric
  flags); the day renders the affected figure with a "counter was reset"
  marker.

## 3. The A-1 buy/sell pinning protocol (the evidence gate)

**What is unpinned:** which of the two grid pairs (`0x4101+0/1`,
`0x4101+2/3`) accumulates imports ("buy") and which accumulates exports
("sell"). Field-mapping A-1 eliminated the charge/discharge swap by capture 2
and lists the exact disambiguating read: "`0x4101`×12 at two times spanning a
known import or export interval — whichever grid pair increments identifies
buy vs sell."

**Protocol P-A1-passive (runs from the first day the block is commissioned,
zero operator effort, zero extra frames).** Every day record stores
`counter_cross_check`: the vendor-labeled delta of BOTH grid pairs alongside
the integrated bought/sold for the same day and units. Pinning evidence
accumulates when a day satisfies: coverage ≥ the commissioned threshold, no
`counter_reset_observed` on either pair, and integrated figures ≥ 0.5 kWh on
both sides (a day with no export cannot discriminate). The design's pinning
TEST (reported in the record, never auto-applied): an ordering matches a day
when |Δpair_i − ∫import| ≤ max(0.5 kWh, 5%) AND |Δpair_j − ∫export| ≤
max(0.5 kWh, 5%) for exactly one of the two orderings.

**Protocol P-A1-active (optional, one evening, read-only, operator-authorized
observation window).** The operator names a ~30-minute interval when the
site's state is known by inspection (e.g. 21:00–21:30 after dark, house
importing, no solar): the cold ring already reads `0x4101` every ~108 s, so
the interval's pair deltas land in the record without any new mechanism; the
operator confirms the known state once ("the house was importing, nothing was
exporting"). Whichever pair moved is buy. A midday export interval confirms
the sell side symmetrically. Result recorded as
`energy_counter_roles_pinned` audit fact (the operator's confirmation, the
interval, the measured deltas).

**No self-promotion.** Pinning NEVER flips a source by itself. The record
reports "grid counter roles consistent with vendor labels (4/4 days)" or the
swap verdict; the source change is the operator's explicit config revision
(§7: `grid_counter_roles: unpinned → vendor_labels|swapped`, optionally
`grid_source: integrated → device_counter`), audited at boot like every
commissioning fact. If the two protocols ever disagree, the record says so
and the source stays integrated — the fallback is permanent-safe.

**Why promote to `device_counter` after pinning at all:** the device counters
cover controller downtime (our integration cannot see a window we did not
sample); after pinning they are the strictly-more-complete source for
bought/sold, with the integration continuing as the cross-check. The
recommendation (§11 decision 3) is to promote after P-A1-passive shows ≥ 3
consistent days or P-A1-active lands.

## 4. The fallback math, assessed honestly

The integrated figures are ours, so their accuracy is our claim to state:

- **Sampling.** Zero-order-hold integration over the observation stream:
  `ΔE += P_prev · (t_i − t_prev)` per unit, using each observation's capture
  time, not loop wall-time. At the 1.5 s cadence with household per-phase
  flows (sub-10 kW, seconds-scale dynamics), hold-error is bounded by
  `|dP/dt|·Δt²/2` per interval — in practice well under 1% of a day's energy;
  the golden scenario (§9) pins the arithmetic exactly against a known
  scripted trace rather than estimating it.
- **Gaps.** An inter-sample spacing above `integration_max_gap_s` (default
  10 s) is a GAP: it contributes zero energy and zero coverage. Gaps are
  never interpolated across — a 40-minute outage interpolated at a stale 2 kW
  would invent 1.3 kWh. Per unit per day: `coverage_pct = sampled_seconds /
  elapsed_seconds` (the attribution integral inherits the same windows).
  Fleet coverage is the WORST unit's (the evidence-rollup precedence
  doctrine). Below `min_day_coverage_pct` (default 95) the day renders as a
  partial day.
- **Saturation/quantization.** The CT word is `int16` (±32 767 W) — far above
  per-phase household flows; saturation is not a live risk and is NOT
  specially handled (a saturated word would integrate its clipped value,
  which is the honest reading of the sensor). Integrated figures carry the
  full computed precision; the console renders ≤ 2 decimals per the display
  convention; device-counter figures are exact multiples of 0.1 kWh.
- **Cross-source disagreement is expected, not reconciled.** Integrated
  battery charge vs the BMS charge counter will differ by sampling error and
  metering-chain differences (the same reason the two grid views diverge ~89
  W on mid). The scorecard displays each metric from ITS pinned source and
  does not force a conservation identity; the A-1 cross-check tolerance
  (max(0.5 kWh, 5%)) absorbs exactly this class of difference.

## 5. Contracts (domain, application, observation)

**Observation gains six advisory cumulative fields** — `energy_grid_a_kwh`,
`energy_grid_b_kwh`, `energy_load_kwh`, `energy_pv_kwh`,
`energy_charge_kwh`, `energy_discharge_kwh` (float kWh, `None` when the
energy block was not served this observation; the grid pair keeps NEUTRAL
A/B names until §7's roles key licenses vendor labels — the decode contract
is the vendor's pair ORDER, which is confirmed; the ROLE labels are not).
Quality keys join the existing `ADVISORY_QUALITY_FIELDS` set (the
twelve-key quality-map shape extends to eighteen by the same mechanism; the
fields stay outside every safety completeness set — an unserved energy block
must never refuse power). Decode vectors are the live-capture values
(field-mapping §2.7: e.g. mid 9709.2 / 3187.7 / 3789.4 / 0.0 / 3567.2 /
5678.9).

**Domain: `EnergyDayRecord`** (frozen, per site-day; per-unit maps inside):

```json
{
  "date": "2026-08-26", "timezone": "Australia/Brisbane",
  "utc_offset_minutes": 600, "kind": "complete|partial|in_progress",
  "units": {
    "mid": {
      "grid_import_kwh": 1.2, "grid_export_kwh": 6.8,
      "battery_charged_kwh": 3.4, "battery_discharged_kwh": 0.7,
      "load_kwh": 5.1, "charged_from_surplus_kwh": 3.1,
      "coverage_pct": 99.4, "metric_flags": []
    }, "rhs": { "...": "..." }, "lhs": { "...": "..." }
  },
  "fleet": { "...": "the five sums + charged_from_surplus..." },
  "sources": { "grid": "integrated_ct", "battery": "device_counter",
               "load": "device_counter", "surplus": "attributed_adviser" },
  "counter_cross_check": {
    "grid_a_delta_kwh": 1.1, "grid_b_delta_kwh": 6.9,
    "consistent_with": "vendor_labels", "discriminating": true
  },
  "solar_production_measured": false
}
```

`kind` is `in_progress` for the live day (what the snapshot block serves),
`complete` for a rolled day with coverage ≥ threshold, `partial` below it.
`metric_flags` carries `counter_reset_observed` per affected metric. A
`null` per-unit figure means "source absent all day" — never 0.

**Application: `EnergyAccountant`** — a pure-ish component with one entry
point `observe(observation, *, adviser_active_targets: frozenset[str],
now_mono: float) -> None` and one query `day_summary(local_date) ->
EnergyDayRecord | None`. State: per-unit running integrals for the live day,
per-unit last cumulative counters + capture times (the baseline), the live
day's attribution accumulator, and coverage clocks. Day rollover happens on
the first `observe` whose local date advances (site timezone): the completed
day is finalized, persisted via the repository, published on the bus, and —
exactly the B4 cell-refresh precedent — ONE promoted cold-ring read of the
energy block is requested (`request_energy_refresh()` on the read planner)
so the new day's counter baseline is at most one control period old, not one
ring period (unpromoted slop would be ≤ ~0.06 kWh at 2 kW; promoted, ≤
~0.001 kWh). The accountant READS `adviser_state` (never writes it) for the
attribution predicate; single-writer doctrines are untouched.

**Persistence: `EnergyLedgerRepository` port** — `record_day(record)`,
`get_day(date)`, `latest_days(limit)`, `load_baseline()/save_baseline(...)`
(the live-day baseline is durable so a mid-day restart re-baselines from the
last seen cumulatives instead of losing the day: counter deltas survive
restarts BY DESIGN because the device kept counting; the integration's
coverage clock resumes with a gap counted from the last capture time).
Memory adapter for tests/simulate; SQLite adapter as a schema_version
migration (tables `energy_day` keyed by date, `energy_baseline` keyed by
unit). The audit store is NOT overloaded for this — day records are
projections, not acts.

## 6. REST, snapshot, events, audit, MCP

- **`GET /api/v1/energy/days?limit=N`** (observe scope; N ∈ 1..31, default 8,
  newest-last) → `{"days": [EnergyDayRecord...], "grid_counter_roles":
  "unpinned|vendor_labels|swapped", "solar_production_measured": false}`.
  Answers **409 `energy_scorecard_not_commissioned`** when the config block
  is absent (the schedules precedent verbatim). No mutation exists — there is
  nothing to confirm, nothing to idempotency-key.
- **Snapshot top-level `energy_today`** (feature-detected: the key is absent
  when the block is absent) = the live day's `EnergyDayRecord` plus
  `"as_of"`. Rides the existing 2.5 s console cadence without events (the
  figures are slow-moving; §8).
- **Bus event `energy.day_rolled`** — payload the completed record. A
  TRANSITION event at rollover only (the schedule-window doctrine: never a
  heartbeat); the console refreshes the strip from it.
- **Audit facts**: `energy_day_recorded` (the fleet summary rides the row),
  `energy_counter_reset_observed` (unit + metric), and — when the operator
  lands P-A1-active — `energy_counter_roles_pinned` (interval, deltas, the
  operator's confirmation sentence).
- **MCP `get_energy_days(limit)`** (observe): a read-only ride-along tool;
  no MCP surface can touch sources or roles.

## 7. Config keys and composition semantics

```yaml
energy_scorecard:
  grid_source: integrated        # integrated | device_counter (see below)
  grid_counter_roles: unpinned   # unpinned | vendor_labels | swapped
  integration_max_gap_s: 10.0    # > control_period_s, <= 60
  min_day_coverage_pct: 95.0     # (0, 100]
  tariff:                        # OPTIONAL; absent = kWh only, no money
    currency: "AUD"              # ISO 4217
    import_cents_per_kwh: 28.0   # >= 0
    export_cents_per_kwh: 9.0    # >= 0
```

Validation (config load, before composition): the numeric bounds above;
`grid_source: device_counter` is REFUSED while `grid_counter_roles` is
`unpinned` (the unpinned pair must never become the display source — this is
the A-1 gate made structural); `vendor_labels|swapped` additionally requires
the operator's pinning fact to exist in the durable audit (the
excess-economics boot-load precedent: one keyed existence check) — a roles
key without the recorded evidence is a config error naming the missing fact.

Composition (the established block-presence doctrine): a PRESENT block
composes the accountant into the fleet loop (one bounded tick AFTER the
polls, BESIDE the adviser-projection update — it consumes observations and
never blocks; the same control-interval bounding as every fleet-loop member),
the snapshot key, and the route. An ABSENT block composes nothing —
byte-identical snapshot, 409 on the route, no accountant, no energy decode
(the wire fields are feature-detected on the observation projection only when
the block is present). No read-plan change is needed: the energy block is
already in the cold ring, the PCS live block already rides the core on this
deployment, and the single rollover promotion reuses the B4 mechanism. There
is deliberately NO `enabled` key (the schedules doctrine: a second master
switch is a second way to be silently off; decommissioning is removing the
block).

## 8. Console plan (for the follow-up web agent — NO implementation here)

All additions FEATURE-DETECTED on `energy_today` / the days route's 200;
absent key/route hides everything below. No new controls anywhere — the
scorecard is read-only by construction.

- **W-A. Home "Today" card** (after "What is powering the home?"): the §1
  sentence block, per-unit figures expandable, the coverage/"partial day"
  marker, the solar footnote, and the money line only when tariff keys exist
  (rendered "≈ $X bought / $Y earned" with the operator's rates named once).
- **W-B. Insights view**: the standing "Insights — not available yet"
  placeholder becomes the 7-day strip (one row/bar per rolled day from
  `GET /api/v1/energy/days`; the visual design is the web agent's, per
  UI_CONTRACTS). `energy.day_rolled` appends the newest day.
- **W-C. Batteries detail**: the six cumulative counter readthroughs on each
  unit's detail rows ("lifetime through this pod", the neutral grid A/B
  naming until roles are pinned), plus `charged_from_surplus` on the target
  unit's summary when non-null.

## 9. Red-test families (contract-first; the deferred golden scenario is consumed here)

1. `tests/unit/test_wire_decode.py` — the six-field decode block: live-capture
   vectors verbatim (per unit), low-word-first + ×0.1 arithmetic, absent-block
   `None`s, quality keys, and the neutral A/B naming.
2. **NEW `tests/unit/test_energy_accounting.py`** — the accountant family:
   zero-order-hold integral exactness against hand-computed traces; sign-split
   import/export; gap exclusion at/above `integration_max_gap_s` (a scripted
   40-min hole contributes zero energy and drops coverage, never
   interpolates); coverage arithmetic per unit and worst-unit fleet rollup;
   day rollover at local midnight incl. a DST-boundary day (23 h and 25 h
   honest, `utc_offset_minutes` stored); counter-delta daily math incl. the
   baseline across a simulated restart (repository save/load); counter
   DECREASE → `counter_reset_observed`, no negative delta, flag in record;
   surplus attribution windows (adviser predicate on/off mid-trace, measured
   not authorized watts); never-zero-filled projections; the A-1 consistency
   verdict (matching/crossed/undiscriminating days).
3. `tests/unit/test_composition.py` — block-present composes accountant +
   snapshot key + decode; absent block byte-identical (snapshot, observations,
   route 409); the tick is bounded and post-poll; the rollover energy-block
   promotion fires once.
4. `tests/unit/test_repositories.py` — memory/SQLite parity, `record_day`
   idempotence by date, `latest_days` ordering/bound, baseline round-trip,
   the schema migration from the prior version.
5. `tests/api/test_rest_contract.py` — the days route (shape, limit bounds,
   409 not-commissioned, observe scope), the snapshot block's
   feature-detection, and no new mutation surface.
6. `tests/api/test_event_contract.py` — `energy.day_rolled` payload + the
   no-heartbeat rule (exactly one publication per rollover).
7. `tests/unit/test_config.py` — the §7 validation matrix, including the two
   A-1 gates (`device_counter` refused while unpinned; roles key refused
   without the durable pinning fact).
8. `tests/simulator/` + `tests/golden/` — **this build CONSUMES the deferred
   golden energy/SOC scenario** (DEFERRED_FINDINGS item 3): extend
   `SimulatedEnergyPod` so `_rebuild_totals_block`'s grid/load pairs
   accumulate from the scripted CT words (deterministic, the charge/discharge
   precedent `_accumulate`), then the golden scenario walks a scripted
   import-day / export-day / mixed-day / restart-mid-day / DST-day against
   EXACT expected kWh — the scorecard's reference model. The simulator
   constructor-validation matrix (item 4's open half) rides the same edit.

## 10. Implementation plan (ordered; each slot its own red→green cycle)

1. **E1 — Decode + Observation fields** (1 slot): the six advisory fields,
   quality-map extension, decode vectors. Red: family 1 (+ observation-shape
   pins in the domain family).
2. **E2 — Accountant domain + application** (1–2 slots): `EnergyDayRecord`,
   `EnergyAccountant`, rollover + promotion request, attribution. Red:
   family 2.
3. **E3 — Repository + migration** (1 slot): port, memory adapter, SQLite
   adapter + migration. Red: family 4.
4. **E4 — Composition + snapshot + events** (1 slot): block-presence wiring,
   the fleet-loop tick, `energy_today`, `energy.day_rolled`, audit facts.
   Red: families 3 and 6.
5. **E5 — REST** (1 slot): the days route + 409. Red: family 5.
6. **E6 — Simulator accumulation + golden scenario** (1 slot): the deferred
   golden energy/SOC scenario and the matrices half. Red: family 8.
7. **E7 — Config keys + docs** (1 slot): the §7 block in the live-write
   example (commented, with the A-1 protocol summary), CONTINUITY entry,
   API_CONTRACTS section already landed with this design, PROTOCOL_EVIDENCE
   A-1 note pointing at the protocol. Full suite green.

Web agent, after E4/E5: **W1 Home card** → **W2 Insights strip** → **W3
Batteries readthroughs** (each 1 slot, feature-detected, per §8).

## 11. Operator decisions this package needs (verbatim-ready)

1. **Commission the block:** "Add the `energy_scorecard` block to the
   controller config and restart — the Today card and Insights appear; the
   day rolls at midnight Brisbane time."
2. **A-1 pinning path:** "Either let the passive cross-check run (it reports
   a verdict on the console after ~3 discriminating days) or name one
   half-hour evening window when the house is definitely importing and
   confirm it afterwards — whichever grid pair moved is 'bought'. Both are
   reads-only; nothing is written to the pods."
3. **Source promotion after pinning:** "Once pinned, switch
   `grid_source` to `device_counter` so bought/sold keep counting through
   controller restarts (the integration stays as the cross-check) — or keep
   the integration as the display source. Recommendation: promote."
4. **Tariff:** "Supply import/export rates (and currency) to see money, or
   leave the keys absent for kWh-only. The rates are yours, labeled as
   yours."
5. **Naming check:** "The two grid counters display as 'counter A/B' until
   pinned, then 'bought/sold'. Solar production is never shown as measured —
   the card says the panels aren't wired to the pods. Confirm this wording
   suits."

## 12. Companion dispatch (recommended, same files, NOT part of this design)

The **night-writer between-cycles foreign-objective detector** (CONTINUITY
census queue) touches the same decode/composition surfaces and is the ranked
next companion (see PRODUCT_NEXT 2026-08-26 §3): fold the already-read
served-objective words (`0x1060+17/+18`, already in the cold ring) into
`Observation` as advisory fields, and alert `foreign_objective_observed`
when disarmed/idle — zero extra frames. Dispatch it as its own contract
cycle beside this one; its evidence value (the 7.3 h night blind spot)
compounds with the scorecard's night/day accounting.
