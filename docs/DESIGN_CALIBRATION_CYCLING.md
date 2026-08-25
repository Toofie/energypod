# Battery calibration cycling — the periodic bottom-anchor traverse

Design date 2026-08-25, panel-folded the same day. Status: CONTRACT v1.1 —
the adversarial review's verdict was WITH-AMENDMENTS, architecture sound,
no redesign: the adviser shape, the ordered stop set, the measurement-first
doctrine, the A14 boundary, and the night-V2 interaction were all verified
against the code. All sixteen amendments (C1–C16) are folded into the
sections where they landed, and the four reviewer rulings are recorded
verbatim in the amendment log (§16).
Parents: `docs/DESIGN_BATTERY_HEALTH_WATCH.md` §19/A14 (the boundary ruling
this design EXISTS as the first tenant of — a sibling maintenance program
with its OWN top-level config block and its OWN window, never a stage
inside that contract's enumeration or window machinery),
`docs/DESIGN_NIGHT_CHARGE_V2.md` (the `even_rate_w` self-correcting
deadline-rate shape §2.2, the one-capacity-truth ruling §2.1, the reserve
floor and midday finish-line vocabulary, the tariff arithmetic §2.4),
`docs/DESIGN_PLANT_HISTORY.md` (the historian this program's trigger and
measurement read), `docs/VENDOR_CONTROL_COMPARISON.md` (the old stack's
one-battery-per-night evening rotation, manager.py:113-149 — the site's own
cycling precedent), and the two operator-commissioned research rounds whose
findings §1 carries.

This document is DESIGN ONLY. It touches no `src/`, `tests/`, `config/`, or
`web/` file, authorizes no live-hardware interaction, and grants no new
authority of any kind: the program is a strategy ADVISER in the exact
night-charge class — it submits ordinary short-TTL `OPTIMIZER` DISCHARGE
intents under a composed automation principal through the internal facade
twin, judged by the arbiter, allocator, SafetyKernel, and actor exactly as
the night adviser's charge intents are. No new write primitive, no mode
register (values 2–6 of `0x8000` stay PERMANENTLY PROHIBITED — the vendor's
own "Fixing SOC" mode 5 among them; this program provides the anchor with
ORDINARY DISPATCH, or it does not provide it), no arming, no schedule
publication. Every actuation is gated behind its own config block that is
ABSENT by default.

## 0. What this is — and is not

The operator asked whether the batteries should be cycled periodically. Two
research rounds answered YES, and answered it with enough precision that the
answer is not "cycling" at all — it is ANCHORS. LFP SoC is coulomb-counted
on a voltage curve too flat to recalibrate against, so the BMS re-anchors at
exactly two voltage-defined points: true full (charge terminating with a
current taper, the dynamic charge limit collapsing to 0 W) and
near-10%-empty. This site performs the TOP anchor daily — charge to 100% by
midday is the standing strategy, and the live telemetry shows the charge
limit hitting 0 W at full. The BOTTOM anchor has never been provided. The
pods that stopped crossing their range are exactly the pathological ones:
lhs cycles toward deep floors daily and is healthy; mid has sat pinned at
exactly 100% since ~May 2026 while still flowing 3.3 kWh/day (an SoC-estimate
pathology with working control); rhs shows 100% of samples at or above 97%.
The program is the missing half: ONE pod per cycle-night, selected by NEED
and not by calendar, discharged slowly across the evening to the 5–10%
bottom anchor — never beneath it — then refilled by the same night's
off-peak charge and finished to a REAL taper by the morning autonomy that
already owns the top anchor, with the at-full hold the balancer needs,
observed and recorded. A partial cycle anchors nothing: a 30–40% excursion
is diagnostic energy, and this program does not spend it.

The program is measurement-first. A pod's FIRST traverse is a MEASUREMENT:
before/after system-vs-BMS SoC delta, cell-voltage spread, the SoC word's
behavior across the traverse, energy through the pack — recorded, compared
against the expected re-anchoring signature, and only on that evidence does
the pod enter routine rotation. A first cycle that shows no re-anchoring is
an ALERT, not a schedule: the pod stands down from rotation until an
operator acknowledgement, because a traverse that does not re-anchor is
either the wrong medicine or a deeper fault, and the program's answer to
both is the advisory, not persistence.

**What this is not.** It is not a health-watch stage — A14 drew that line
and this design lands beside that contract, sharing nothing but the fleet
and the audit store (the interlock it does read is a durable ROW, §3.2). It
is not a schedule entry: depth-by-watts-x-duration is exactly the arithmetic
this program refuses, because the stop condition is a STATE (SoC floor), not
a time (§2). It is not a calendar service — no pod is cycled because the
quarter turned; a pod is cycled when its own history says its anchors have
not both run in N days. It is not a wedge-fighter: every standing guard
(foreign writer, latched stop, park, manual/agent claim) judges these
intents, and a refusal is a SKIP with the guard's reason, never a retry. It
is not a vendor-mode driver — the vendor's "Fixing SOC" (mode 5) and "Verify
Capacity" (mode 6) exist behind the same `{0,1}`-bound register parking
uses, and this program writes NONE of it, ever, under any configuration.
And it is not economical heroism: the traverse is approximately free
(warranty arithmetic §1.2) and mildly cash-positive (§5.1), but the anchor
is the point; the money lines exist so the morning surface can be honest,
not so the program can claim savings.

One pinned sentence rides every surface this feature adds, verbatim:
*"Partial cycles anchor nothing — the floor is the anchor, and the anchor is
a floor, not a finish line: the traverse stops at it and never beneath it."*

## 1. The evidence base

Two research rounds, one sourced and one local; each finding carries its
evidence class, because thresholds built on the wrong class are how Wave 0
bugs happen (the health-watch §1 doctrine, inherited).

### 1.1 Sourced findings (vendor documentation, service guideline, community)

| Finding | Source class | Consequence in this design |
|---|---|---|
| LFP SoC is coulomb-counted; the flat voltage curve means mid-range cycling recalibrates NOTHING. The BMS re-anchors at exactly TWO voltage-defined points: true full (charge terminates with current taper, CCL→0) and near-10%-empty | Victron's official BYD documentation; EFT Systems BYD service guideline V1.5 §2.5; photovoltaikforum thread 229480 | The program's shape IS this row: a traverse to the bottom anchor plus a real top taper (§4, §5); partial excursions are diagnostic-only and never scheduled (§0) |
| Partial cycles do NOT anchor; to recalibrate, the discharge must reach the 5–10% bottom anchor — and NEVER below (documented cell-undervoltage risk at REPORTED low SoC) | Vendor service guideline + community consensus | `floor_pct ∈ [5, 10]`, the floor is a STOP (§4.3), the kernel's cell-voltage floor is the last line beneath the lying-word guard (§4.3) |
| The April-class stuck symptom is drift + per-cell protection interacting: cells hit the 3.65 V ceiling, the displayed SoC (an average) reads 100% while blocks are not full, and the loop self-reinforces (reported-full → PCS stops charging → charge never terminates → calibration/balancing never runs) | Community/vendor thread evidence, corroborated by this fleet's own mid/rhs behavior | The trigger is "has not been below X% in N days", not "is showing a fault" (§3.1); mid-class pods are FIRST in the order, not last (§3.2) |
| Victron documents a per-module ~1 A (~50 W) sensing threshold: sub-threshold currents report as 0 W | Victron BYD documentation | The traverse's minimum commanded rate clears 3 modules × 50 W = 150 W; `min_discharge_w` default 200 (§4.2) — rhs hovers in exactly that band |
| BYD prices its warranty at ~3,000 EFC over 10 years; the warranty-test protocol itself uses ~0.2 C constant current with 10-minute rests | Vendor warranty terms + vendor test protocol | One deep cycle/month ≈ 12 EFC/year ≈ 0.4%/year of the throughput budget — effectively free; the site's shallow ~0.3 EFC/day regime (~1,095 EFC/decade) never approaches the cap; the protocol's 0.2 C is the rate class §4.2 defaults inside |
| The balancer starts 10–30 min AFTER charge ends — hold at full 30–60 min | Vendor documentation + community practice | The at-full hold window, OBSERVED not enforced, default 45 min bounded [30, 60] (§5.3) |
| One module/tower at a time is the community/vendor practice (Pylontech/BYD precedent); all-at-once has no advantage | Community practice | ONE target pod per cycle-night, strictly (§3.3) |

### 1.2 Local archaeology (this fleet's own history, our telemetry, the old stack)

| Finding | Class | Consequence |
|---|---|---|
| The site ran deep daily cycles (5–25% floors) through 2024–2025 without catastrophe; 2026 went shallow; the pods that STOPPED crossing the range are the pathological ones (lhs cycles daily and is healthy; mid pinned at exactly 100% since ~May 2026 while flowing 3.3 kWh/day — SoC-estimate pathology with working control; rhs 100% of samples ≥97%) | Observed in this site's records | The whole program: the bottom anchor is the missing half of a regime the site already proved survivable, aimed by need at the pods that lost it |
| The old stack's one-battery-per-night evening rotation (manager.py:113-149; load-compensated charge `max(200, 2000 − load)`, SOC³-weighted discharge split) — the site precedent; the successor stack dropped ALL cycling | Confirmed by prior-integration code | The one-pod-per-night shape and the evening slot are precedented site behavior, not novel doctrine (§3.3, §4.1) |
| The top anchor runs daily on this site: charge-to-100%-by-midday, live telemetry shows the dynamic charge limit hitting 0 W at full (rhs reads 0 W when full — observed) | Live-observed | The program OBSERVES the top anchor rather than commanding it; the taper signature is already in the read plan (§5.2) |
| The kernel's `minimum_soc_pct` = 10.0 denies DISCHARGE at or below 10% SoC; `soc_above_charge_ceiling` refuses our own charges above 95; the fleet SoC-difference policy (5%) is informational only — a one-pod deep cycle is NOT vetoed; the SoC-jump check is out of the deny set (`soc_jump_observed` informational) | Confirmed by our code | The floor/kernel ordering rule (§4.3); the cycled pod's 8→95 night jump rides as an informational note, not a refusal (§5.1) |
| The historian is commissioned: per-row `bms_soc_pct`, `system_soc_pct`, `cell_spread_mv`, `dynamic_charge_limit_w`, `battery_watts`; hourly rollups (low/high/mean) retained forever | Confirmed by our code | The trigger (days-since-below-X over any horizon), the measurement record, and the before/after deltas are all historian-backed through injected ports (§3.1, §6) |
| The BMS SoC word is the AUTHORITATIVE word for every policy bound; the system SoC word is informational, may be hours stale on a cycled unit, and its divergence is recorded as an informational note (`_soc_divergence_observed`) | Confirmed by our code + operator ruling 2026-08-24 | The floor stop reads the BMS word; the system-vs-BMS DELTA is the measurement's primary instrument (§6.1) |
| Our Modbus map exposes NO SoC-calibration event register: the vendor logs' "calibration" warnings (PCS_Warning0_1 / DCDC_Warning0_1, EE-calibration parameter out of range) are a different animal, and the BMU event log the vendor documents has no registers in the read inventory | Established by search (PROTOCOL_EVIDENCE §9, open items 12/16; the health-watch §19 note) | The honesty clause §6.2: the program canNOT read a calibration event, says so on every surface, and instruments the observable deltas instead |
| ScheduleEntry supports one-shot evening entries (action discharge, `watts_by_unit`, `days`, `effective_from/until`) and the publish gate allows windows inside 00:00–20:00; the actor renews bounded objectives for a runner; REST intents cap `ttl_s ≤ 300` | Confirmed by our code | The REJECTED alternative (§2): depth-by-duration arithmetic with no state stop; the adviser shape uses the same renewal discipline instead |
| Off-peak import 7.27 c (00:00–06:00), general usage 30.77 c, feed-in 2.0 c; the legacy 44–52 c QLD FiT inversion does NOT apply to this site (night-V2 A3 gate passes decisively) | Confirmed by the commissioned tariff block | The economics of §5.1, stated not waved at |

## 2. Doctrine decisions (each with its reason)

- **The floor is the ANCHOR, and depth is not a dial.** The science says two
  voltage-defined points recalibrate and nothing between them does; so a
  calibration cycle that turns back early is diagnostic energy spent for no
  anchor, and a cycle that blows through the floor trades calibration for
  documented cell-undervoltage risk. The config therefore exposes `floor_pct`
  bounded `[5, 10]`, the traverse STOPS on it (§4.3), and nothing in the
  block can order a shallower "gentler" cycle — a partial-cycle mode is the
  one posture deliberately absent, because it would be a button that spends
  wear buying nothing.
- **An ADVISER, not a schedule entry.** The mission could be carried either
  way; the adviser wins on five counts, and §4/§5 are written to it. (1) The
  stop condition is a STATE — SoC at floor — and `ScheduleEntry` is
  watts-x-duration arithmetic: bolting a per-objective SoC stop onto the
  evaluator would turn a versioned operator-published plan into a feedback
  controller living in the wrong home, with its state machine split across
  the schedule store and the runner. (2) The night adviser's `even_rate_w`
  is ALREADY the self-correcting deadline-rate over a SoC target, renewed
  per fleet cycle from measured SoC — a discharge-to-floor is that same
  function with the direction flipped and the target set to the floor; the
  pattern is proven on this site nightly. (3) Crash-safety by construction:
  a schedule entry outlives the crash that orphaned it; an adviser's
  short-TTL intent dies in ≤10 s and the firmware watchdog returns the pod
  to autonomy — the standing fail-safe, inherited whole. (4) The
  measurement bookkeeping (§6) is adviser-shaped: before/after deltas and
  verdicts are audit rows beside the intents they explain. (5) A15 already
  settled the window question: advisers are window-gated by their OWN
  blocks, never by the schedule union — this program's evening window is
  its own fact, validated against its siblings (§7), not a schedule
  publication.
- **Trigger-gated by NEED, ordered by NEED, one pod at a time.** The
  trigger is "this pod's BMS SoC has not been below X% in N days"
  (defaults X=30, N=60 — a quarterly-class cadence that falls out of the
  numbers rather than being nailed to them); the eligible pod with the
  LONGEST days-since-deep goes tonight. Calendar rotation would cycle lhs
  (healthy, anchoring daily) on schedule and miss mid (pinned since May) —
  exactly backwards. One target per night, strictly: the one-at-a-time
  practice is vendor/community precedent, and it keeps the fleet's reserve
  shape intact while one pod crosses.
- **Measurement-first, with graduation by evidence and stand-down by
  absence.** The first cycle per pod carries the full before/after record
  and the re-anchor signature test (§6); only a passing signature admits
  the pod to routine rotation, and the admission is derived from durable
  rows, never a config flag an operator could set before the evidence
  exists. A first cycle whose signature is absent STANDS DOWN that pod from
  rotation until an operator acknowledgement — the acknowledge-inhibit
  pattern — because the second-most-dangerous program is one that repeats
  an act that did nothing.
- **The stop set has three members and the SoC floor is checked
  PRE-SUBMISSION every tick.** Floor-reached, energy-bound, deadline (§4.3).
  The pre-submission ordering is load-bearing: the adviser stops renewing
  the tick the authoritative word touches the floor, so the kernel's
  `soc_below_discharge_floor` never sees an intent to refuse — our stop and
  the backstop are ordered by construction, not by luck (§4.3). The stop is
  NON-RENEWAL, the standing doctrine: no stop triples, no idle intents,
  ever.
- **The lying word has its own guard: the ENERGY BOUND.** mid's pathology
  is precisely a pod whose SoC word may not move while the pack empties —
  and the kernel reads the SAME word, so the SoC floor and the kernel's
  backstop would BOTH be blind on that pod. The traverse therefore
  integrates its own delivered energy from `battery_watts` and stops at the
  co-computed bound `(start_soc − floor)/100 × capacity + margin` (§4.3).
  Beneath that guard sits the kernel's absolute per-cell floor (2.80 V),
  which no SoC word can lie its way past — the last line, named as such.
- **The top anchor is OBSERVED, never enforced.** The night charge stops at
  the 95% ceiling our own kernel enforces; the span 95→100% by midday
  belongs to the pods' own autonomy and the morning sun (night-V2 §5.3,
  restated and inherited). The taper signature — at or above 99% with the
  dynamic charge limit collapsed to 0 W, sustained — is already in the read
  plan; the at-full hold is a 30–60 min OBSERVATION window recorded, with
  honest interruption evidence when the house draws it down (§5.3). Nothing
  in this program writes a watt during the close.
- **Economics stated, not claimed.** The evening traverse displaces 30.77 c
  imports when the house absorbs it (net ≈ +99 c against the ~35 c
  off-peak refill at the ceiling posture); the FIT inversion that killed
  night-V2-A3-on-other-sites does not apply here (2.0 c feed-in against
  7.27 c off-peak). The morning surface carries BOTH branches — the
  house-absorbed figure and the fully-exported quiet-house worst case
  (~8.7 c received, net ≈ −26 c — trivial, but a real minus carried
  honestly, C7) — so nobody is surprised by either meter (§5.1, §9).
- **The health watch is a SIBLING and an INTERLOCK, never a stage.** A14's
  boundary, honored structurally: this block owns its own window, its own
  vocabulary, its own projection. The one thing it reads from the sibling
  is a durable `health_probe_completed` row — the rhs-class gate (§3.2).
  The two windows are disjoint by validation (§7), the traverse ending
  before the watch opens, so both programs run the same civil night without
  ever meeting on the wire.
- **MCP observes; it does not cycle.** No mutation tool; read surfaces gain
  the calibration projection. The agent-loop text gains: "calibration
  cycling is a commissioned program, not an agent act; recommend the
  operator surfaces."

## 3. The program — trigger, eligibility, selection

### 3.1 The trigger (historian-derived, fail-deferred)

Once per day, at the first tick after the traverse window's planning
instant (`plan_local`, default `"14:00"` — after the morning's taper
deadline has resolved, before the window opens), the program computes, per
unit, from the telemetry historian through an injected port (the
load-baseline pattern; the application layer imports no adapter):

```
horizon_days(u)   = today − earliest date with a judgeable rollup
last_deep_date(u) = latest civil date with min(bms_soc_pct over the
                    date's hourly rollups) <= trigger_floor_pct
days_since_deep(u) = today − last_deep_date(u)   if a deep date exists
                     else horizon_days(u)        # HORIZON-BOUNDED, flagged
due(u) = horizon_days(u) >= trigger_after_days AND days_since_deep(u) >= trigger_after_days
evidence_short(u) = horizon_days(u) < trigger_after_days   # defer, no verdict
```

Defaults `trigger_floor_pct: 30`, `trigger_after_days: 60`. The C1 shape,
and its reason, pinned: a NEVER-deep pod is the trigger's designed
population — mid has not been below 30% since the historian's first rollup
and never will produce a `last_deep_date`, so a v1 reading that required
one made the program's own first target permanently ineligible. Once the
horizon is judgeable (`horizon_days >= trigger_after_days`), a never-deep
pod is DUE with the HORIZON AGE as its days-since figure, flagged
`horizon_bounded: true` on every surface that carries the number — an
honest lower bound on a pod that is AT LEAST this many days unanchored,
likely more. `None` exists nowhere in the due arithmetic; the
`evidence_short` deferral (the historian horizon younger than N — the
entire fleet sits here for ~N days after historian commissioning, the C6
quiescence §10 owns) is the ONLY unjudgeable state, and it defers the
night rather than minting a verdict from a short window dressed as a long
one. The proxy semantics, stated honestly: X=30 measures EXERCISE, not
anchoring — a pod oscillating 28–31% resets its clock without ever
anchoring. That is deliberate (the proxy's job is to find pods that have
not crossed their range AT ALL; the anchor itself is the traverse's job),
and X is config so the operator can tighten it to 20 if mid-range
oscillation proves common. The computation uses the HOURLY ROLLUPS
(low/high/mean, retained forever), so the horizon is not bounded by the
14-day full-resolution window; a date whose rollups are missing or
degraded is EXCLUDED from the scan, never interpolated. The
`calibration_trigger_evaluated` row (§8) records the full vector per unit,
`horizon_bounded` included.

### 3.2 Eligibility — classes derived from data, not unit ids

Per unit, in evaluation order, all over the trailing
`eligibility_window_days` (default 14) of hourly rollups and the health
watch's durable rows:

| Class | Derivation | Disposition |
|---|---|---|
| `excluded_cycles_daily` | BMS SoC went below `cycles_daily_floor_pct` (default 25) on ≥ `cycles_daily_min_days` (default 5) distinct dates in the window | EXCLUDED by rule — the pod's own regime provides the bottom anchor naturally and often (the lhs class); rendered on the projection with its figures, never silently |
| `eligible` | Not excluded; `due` per §3.1; AND control-works evidence: trailing mean daily \|battery_watts\|-integrated throughput ≥ `min_daily_throughput_wh` (default 1000) over ≥ `throughput_min_days` (default 3) dates | The mid class — pinned SoC, working control. First in the order; the FIRST traverse is a measurement (§6) |
| `deferred_probe_required` | Not excluded; due; but throughput evidence ABSENT — and no `health_probe_completed` row with verdict `pass` for this unit within `probe_pass_window_days` (default 7) | The rhs class. The discharge is deferred until a passing probe exists; the interlock is the sibling's own verdict, read as a durable row (§2, the A14 boundary kept: one row, not a stage) |
| `no_control_evidence` | Not excluded; due; throughput absent; and the `battery_health_watch:` block is ABSENT or staged below `probe` | Deferred honestly — the program will not deep-discharge a pod whose command-following it cannot evidence, and it says which evidence is missing |
| `measurement_pending` | Eligible, but the pod has no completed first-cycle record | Eligible tonight — as a MEASUREMENT (§6); the distinction rides the record, not the gate |

The probe interlock, stated once: a pod that flows no energy and has no
passing probe is exactly the pod a deep discharge would be gambling on —
the health watch's Stage P exists to prove command-following under direct
command, and this program leans on that proof rather than duplicating it.
A probe verdict of `pass` within the trailing window unlocks the class; a
`fail_*` or inconclusive verdict keeps it deferred, with the verdict
referenced on the projection. If the health watch is uncommissioned, the
`deferred_probe_required` path is simply unavailable and its population
falls to `no_control_evidence` — both defer; neither guesses.

### 3.3 Selection and the skip-if set

At window open (`window_local`, default `"15:00"`, C13), among due units
not excluded/deferred: the target is the one with the GREATEST
`days_since_deep` (horizon-bounded figures compete on their honest lower
bounds); ties break by unit id (deterministic). A standing
`request_measurement` one-shot (C6, §7) names the target instead,
overriding SELECTION ONLY — the named unit still passes every class gate
(excluded and probe-deferred still defer), and the waiver of the `due`
requirement is recorded as `due_waived: request_measurement` on the row.
At most ONE target per civil night, derived from durable rows — a restart
never re-budgets the night (the night-V2 retarget rule, and C2's retire,
§4.4). A night with no due, eligible unit renders idle with its reason on
the projection (`not_due`, `evidence_short`, `excluded_cycles_daily`,
`deferred_probe_required`, …) — never silence.

**Skip-if vocabulary (the standing guards, all reused):** parked / disarmed
/ latched stop / inhibited (`external_writer` or otherwise) / vendor mode
word ≠ 0 / unreachable / not_responding / telemetry stale / foreign
objective evidence / a live MANUAL or AGENT claim on the unit / a live
NOT-OWN `OPTIMIZER` claim on the unit (C3 — the excess adviser charges
from surplus whenever export exists, and the 15:00 open sits squarely in
its hours; two equal-priority opposite-direction OPTIMIZER intents is
exactly the tie-flap the night adviser eliminates by exclusion at
submission time, `night_charge.py`'s own `_CLAIMING_SOURCES` discipline;
the precedence is the dawn-corner rule, FREE SURPLUS outranks the anchor,
one direction only — the anchor waits, it never contests) / a published
SCHEDULE claim on the unit (the standing adviser yield — the operator's
published plan beats the opportunist, the night-charge doctrine, no
opt-out flag) / the health watch flagged this unit's LATEST census
`stuck_suspected` with no passing probe since (the §3.2 gate re-checked at
window open). The adviser's own held intent (a pinned `cal-` id prefix)
is never a foreign claim to skip on — the night adviser's `_OWN_INTENT_PREFIX`
pattern verbatim. A guard refusal mid-traverse ends the leg with the guard's
reason as an ABORT verdict, never a failure of the battery and never a
retry that night (§4.4). The site holds no grid-outage word; the
telemetry-staleness guard is the standing proxy, and nothing here models
outage further. Forecast-poor evenings are NOT a skip: the traverse is
discharge (helped, not hurt, by evening demand) and the refill is grid
off-peak; only the TOP anchor is weather-dependent, and that dependence is
carried by the §5.4 attribution, not by cancelling the traverse.

## 4. The traverse — the bottom anchor

### 4.1 Window and arithmetic

`window_local` (default `"15:00"`, C13) to `traverse_end_local` (default
`"22:30"`). The end is the no-new-renewal boundary: no intent is submitted
at or after it, and the last submitted intent dies by TTL (≤10 s) long
before any sibling window opens. Validation (§7) checks, naming its
arithmetic on refusal: (a) the worst-case traverse FITS, at the DELIVERY
floor — commanded watts are not delivered watts, and this fleet's charge
delivery legitimately sits at 75–96% of command with mid's DISCHARGE
delivery the unmeasured one (`delivery_bias.py` exists to measure it;
until it has, `assumed_delivery_frac` default 0.8 is the planning figure),
so the check divides by `discharge_w × assumed_delivery_frac`: from 100%
SoC at window open to `floor_pct` at 640 delivered W takes
`0.90 × 5000 / 640 ≈ 7.0 h` on a 5,000 Wh pod, against the default 7.5 h
of window (5.9 h on rhs's 4,200); (b) `traverse_end_local + intent_ttl_s`
lands at or before `battery_health_watch.window_local` (23:00) when that
block is present, and before `night_charging`'s window open (00:00) always
— the program may not bleed into either sibling's watch or charge. The
evening slot is the old stack's own precedent (manager.py:113-149) and the
tariff's: the house draws in the evening, so the traverse displaces
30.77 c imports exactly when they would have been bought.

### 4.2 The rate — deadline-paced, slow-class, sensing-clear

Per tick, from MEASURED authoritative SoC (the night adviser's own
self-correcting deadline rule, direction flipped):

```
required_w = ceil((soc_pct − floor_pct)/100 × capacity_wh × 3600
                  / remaining_s_to(traverse_end_local))
rate_w = clamp(required_w, min_discharge_w, discharge_w)
```

Defaults: `discharge_w: 800` (the ~0.2 C class — 0.16 C on a 5,000 Wh pod,
0.19 C on rhs's 4,200; BYD's own warranty protocol paces 0.2 C),
`min_discharge_w: 200` (clears the documented ~1 A / ~50 W per-module
sensing threshold with three modules' margin — a commanded rate inside
that band can meter as 0 W, and rhs lives near it), `intent_ttl_s: 10`
(renewed every fleet cycle, the schedule/night discipline, well inside the
300 s REST cap and the ~3.5–4.0 s watchdog only by RENEWAL — one held
intent, ever). The plan recomputes from measured SoC every tick, so an
evening-autonomy discharge before window open (the pod already below
midday's landing), a lost preemption interval, or a wrong capacity
assumption all self-correct; `at_risk` (required > cap) renders on the
projection and, if it stands at the deadline, the honest `floor_miss`
verdict of §4.4 — the deadline is never extended.

### 4.3 The stop set — three members, ordered by construction

Checked BEFORE each tick's submission, in this order:

1. **FLOOR (the anchor).** `bms_soc_pct <= floor_pct` → phase
   `floor_reached`, non-renewal, the anchor is had. Because this fires
   pre-submission, the kernel's `soc_below_discharge_floor` (policy
   `minimum_soc_pct`, commissioned at 10.0) never receives an intent at or
   below its own line — the adviser's stop and the backstop are ordered,
   and the ordering is a validated config fact: `floor_pct >=
   policy.minimum_soc_pct` (§7). At the commissioned policy the only
   commissionable floor is EXACTLY 10.0 — the "near-10%-empty" anchor of
   the sourced table, and this design is content with it: opening the 5–8%
   half of the band would require LOWERING the global backstop, this
   contract neither requests nor recommends that (documented
   cell-undervoltage risk at reported low SoC argues for keeping 10), and
   the operator question is preserved (§13). Tick granularity overshoots
   the floor by at most one tick's energy (~0.3 Wh at 800 W × 1.5 s) —
   noise, stated so nobody hunts it.
2. **ENERGY BOUND (the lying-word guard).** The traverse integrates its
   own delivered energy from the LIVE `battery_watts` word at tick cadence
   (C9: the control-rate observation, never a historian re-read); the
   integration discipline is the scorecard's — a sample gap beyond
   `integration_max_gap_s` (default 10 s) is EXCLUDED, never interpolated,
   and because such a gap is by construction a telemetry-staleness event
   that DENIES the intent at the kernel, the gap ENDS the leg
   (`aborted:telemetry_lost`) rather than being interpolated past — the
   bound is never evaluated across a gap, and the fail direction (an
   undercounting integrator firing late) is structurally unreachable
   because the intent dies first. At `energy_wh >= (start_soc_pct −
   floor_pct)/100 × assumed_capacity_wh + energy_margin_wh` (margin
   default 100 Wh) the traverse stops with verdict `floor_miss_energy_bound`
   — the SoC word did not reach the floor and the coulomb book says the
   anchor's energy has been delivered. This is the mid-pathology guard: on
   a pod whose SoC word is frozen at 100% while the pack empties, the FLOOR
   member never fires (and neither does the kernel — it reads the same
   frozen word); the energy bound fires instead, near the physical anchor,
   and the measurement record carries the frozen-word finding as its
   headline. The margin is SIZED against METERING, not just the band edge
   (C8): an integrator that undercounts by ~2% across a ~4.7 kWh leg loses
   ~94 Wh of truth, so the stop's physical depth is
   `floor_pct − (margin + undercount)/capacity × 100`, and validation
   bounds the SUM — `energy_margin_wh + metering_allowance_wh ≤
   (floor_pct − 5)/100 × min(assumed_capacity_wh)`, with
   `metering_allowance_wh` default 100 Wh — so a frozen-word stop behind a
   2%-undercounting meter still lands inside the science's band (at the
   defaults: 100 + 100 = 200 Wh against the 210 Wh edge on the smallest
   pack → no deeper than ~5.2% even when the allowance is fully spent). The
   refusal message names the whole sizing. The bound's honesty: it assumes
   the START SoC word was true at traverse open; beneath it sits the
   kernel's absolute `cell_voltage_low` (2.80 V) denial, which no SoC word
   can bypass — the last line, named. One ordering note (C4): on a pod
   whose word STEPS rather than freezes, a discontinuity can land the word
   below the floor BETWEEN ticks — the floor member fires on the next
   tick's check and the traverse stops AT, not through, the step; and on a
   pod whose word steps only AFTER the energy bound already stopped the
   traverse, the anchor was physically delivered and the word caught up
   late — §6.3(c) admits exactly that evidence, which is why the two stop
   members are one anchor's two witnesses, not rival verdicts.
3. **DEADLINE.** `traverse_end_local` reached with the floor unmet and the
   energy bound unmet → `floor_miss_deadline`, non-renewal, the leg closes
   honestly at whatever depth it reached (a partial traverse anchors
   nothing — the pinned sentence — and the record says so rather than
   pretending otherwise).

Every stop is NON-RENEWAL (remove-then-lapse, the adviser doctrine); no
stop triples, no idle intents, no zero-watt submissions, ever. A MANUAL or
AGENT claim arriving mid-traverse preempts it instantly, as does the
emergency stop (supreme over everything): verdict `inconclusive_preempted`
— no depth credit, no re-run that night, evidence recorded.

### 4.4 The traverse verdicts, and restart retires the night

`floor_reached` (the anchor, with the floor-crossing SoC, the integrated
energy, the rate figures); `floor_miss_energy_bound` (ALERT — the lying
word; the pod's measurement record headlines the finding and the pod does
NOT rotate routinely until re-anchored, §6.3); `floor_miss_deadline`
(ALERT — depth unmet, the arithmetic named); `inconclusive_preempted`
(notice — the operator's own act ended it); `inconclusive_interrupted`
(C2 — a restart ended it; see below); `skipped:<reason>` (the §3.3 set,
verbatim). Guard refusals mid-leg render the guard's reason with the
`skipped:`/`aborted:` prefix (a mid-leg guard abort included — the leg
closes with the guard's word and the figures it had reached, never a
battery failure), and each such close writes the completed row with the
energy accumulator's state: there is no un-witnessed end. There is no
retry and no resume-the-next-night continuation: each night's traverse
starts from wherever the pod stands, selected afresh by §3.3.

**Restart retires the night (C2, the one interleaving that defeated the
v1 stop set).** A same-night resume would reset the energy-bound
accumulator and re-derive `start_soc` from a half-emptied pack — arming a
full second traverse (~4,700 Wh more) against a pod already near its
anchor with only the 2.80 V cell floor beneath it; and a silent no-resume
would leave no completed row at all, the crash-dishonest shape. The
resolution is the health-watch A4 pattern, calibrated for a LONG act: at
traverse start the adviser writes the durable `calibration_traverse_opened`
row (target unit, `start_soc`, the computed energy bound, the window
figures) BEFORE the first intent; a boot that finds an open row for last
night (or tonight) reconstructs the night as `inconclusive_interrupted`,
writes the completing row from whatever figures the open row and the
historian hold, raises the MORNING alert naming the state the pod was
actually left in, and NEVER resumes — the pod's next chance is the next
eligible selection, starting from wherever it stands. One crash retires
the night unambiguously, the same-word rule as every once-per-night budget
on this controller; and because the open row is durable, the
reconstruction needs no runtime state at all. The morning alert is notice
tier unless the historian shows the pod below `floor_pct +
reanchor_delta_pct` at the interruption (deeper than a graceful stop
would leave it — then alert tier, the honest worry line).

## 5. The close — refill, taper, hold

### 5.1 The same night's refill (night-V2 interaction, verified)

The traverse ends by 22:30 with the pod at 8–10%; the health watch runs
23:00–23:45 (the cycled pod's census reads `nominal` — a deeply discharged
pod is not stuck; its probe, if it would run at exactly the 10% floor, is
refused by the kernel's floor — an honest `skipped:soc_floor`, never a
fail, named here so the morning card is not a surprise); the night window
opens at 00:00 and the night adviser refills the cycled pod as an ordinary
below-target unit — it needs NOTHING from this contract, and no exclusion
is wanted: the refill IS the close. The arithmetic, verified against both
commissioned pacings for a 5,000 Wh pod at 8% at 23:59: ceiling-targeted
(`forecast_suggest`/`full`, target 95): 4,350 Wh, at `cap_first` 2,500 W
≈ 1.9 h (lands ~01:56), at `even` ≈ 725 W required — on plan the whole
window; floor-targeted (`forecast_act`, floor 50): 2,100 Wh, ~17 c. Worst
case ≈ 4,830 Wh bought at η 0.9 ≈ 35 c off-peak. The forecast_act
close-dependence, stated because its arithmetic is inverted from a naive
read (C5): a SUNNY forecast drives the night target LOW (big E_credit →
target → the 50 floor), so a sunny-forecast cycle night refills the cycled
pod only 8→~50 and the top anchor rides on the SAME under-priced morning
surplus the forecast says will arrive — the close is a forecast, and when
that forecast busts the taper misses with the SOLAR attribution (which
§6.3(d) deliberately accepts); a rainy forecast targets the 95 ceiling and
the close is grid-certain. The pod's 8→95 jump in one window rides as the
informational `soc_jump_observed` note (the SoC-jump check left the deny
set in the 2026-08-24 audit — both endpoints are honest fresh BMS reads),
and the fleet SoC-spread policy is informational only — a one-pod deep
cycle is not vetoed, and the morning spread is recorded as measurement
data. One standing dependency named (C14): `night_charging.enabled` is
presently FALSE on this site and the retiring Docker writer does the
refill — the close holds only while ONE of them runs. If neither does, the
cycled pod sits at 8–10% until the morning sun and the taper observation
reports `top_anchor_missed_solar` with the morning's actual refill
figures beside it; §10 step 2 makes the refill writer's existence a named
precondition of the supervised measurement, and the projection's morning
line names the refill source actually observed. The economics line for
§9, both branches carried (C7): the traverse discharged ~4.35 kWh into the
evening; house-absorbed, it displaces ~$1.34 of 30.77 c imports against a
~35 c refill — net ≈ +99 c; fully exported on a silent house, it receives
~8.7 c at the 2.0 c feed-in against the same ~35 c refill — net ≈ −26 c,
a trivial-but-real worst case, carried honestly rather than waved at.
The anchor is the point, and every figure is on the record so neither
meter surprises anyone.

### 5.2 The taper observation (the top anchor's proof)

From floor-reached until `taper_deadline_local` (default `"12:00"` — the
operator's declared midday finish line, night-V2 §2.3, the same civil
fact), the program OBSERVES for the signature, all words already in the
read plan: `bms_soc_pct >= taper_soc_pct` (default 99) AND
`dynamic_charge_limit_w <= taper_limit_w` (default 0) sustained
`taper_sustain_s` (default 600 s — the vendor protocol's own 10-minute
rest class). On signature: the `calibration_taper_observed` row with the
figures (when the taper landed, the CCL collapse, the sustained window),
and the at-full hold observation (§5.3) opens. No write of any kind
occurs in this phase — the span 95→100% is the pods' own autonomy plus
morning surplus (night-V2 §5.3 inherited verbatim; this program stops at
the same ceiling the kernel enforces on everyone).

### 5.3 The at-full hold (observed, honestly interruptible)

`hold_min_s` default 2,700 (45 min), bounded [1,800, 3,600] (30–60 min —
the balancer starts 10–30 min after charge ends; the window brackets it).
The hold is an OBSERVATION: the pod remains at or above `taper_soc_pct`
with CCL at 0 and battery watts inside the float band (`hold_float_w`,
default 100 W — the Wave 0 deadband family). If house load draws the pod
down inside the window, the hold records `hold_interrupted` with the
evidence (the draw, the SoC dip, the elapsed fraction) — an honest partial,
never a fabricated completion, and never a write to "protect" the hold:
the anchor was had at the taper; the hold is the balancer's courtesy
window. The program ends all observation at `taper_deadline_local`; the
question of extending holds into solar hours is the operator's (§13), and
the default declines.

### 5.4 The attribution split (a night-V2 lesson, applied)

A taper that never lands by the deadline splits by cause exactly as
night-V2's A1 split landing misses: the recorded morning surplus (the
pre-battery basis the trust scoreboard already reconstructs) below
`poor_surplus_kwh` (default 3.0) renders `top_anchor_missed_solar` — a
NOTICE, the sky's account, retry is the next eligible night's ordinary
traverse-to-full; good surplus with the pod still below full and CCL > 0
renders `taper_never_observed` — an ALERT, the pod's account (the refused
charge class), and the health watch's stuck-signature predicates are the
named next diagnostic. The attribution figures ride both the audit row and
the morning surface.

## 6. Measurement-first bookkeeping

### 6.1 The record (every cycle; the FIRST cycle is the measurement)

`calibration_cycle_completed` carries, per cycle: kind (`measurement` |
`routine`); start/end instants; start and end `bms_soc_pct` AND
`system_soc_pct` (the delta pair is the instrument); the SoC word's trace
class across the traverse (`monotone` | `stepped` | `frozen` — the frozen
class is §4.3's energy-bound headline); integrated discharge Wh and the
rate figures; floor verdict; every `soc_jump_observed`-class discontinuity
in the window; `cell_spread_mv` and cell min/max BEFORE (traverse open)
and AFTER (post-refill, at the taper observation); the taper and hold
figures or their honest absence; the attribution word when the top anchor
missed. Before/after snapshots read the historian through the injected
port — no reads of the control plan, no writes, ever.

### 6.2 The calibration-event honesty clause

The research asked whether the pod's own calibration-event vocabulary is
readable via our map. It is NOT: the Modbus inventory exposes no
SoC-calibration event register — the vendor logs' "calibration" warnings
(PCS_Warning0_1 / DCDC_Warning0_1) are EE-parameter warnings, a different
animal, and the BMU event log the vendor documents (where force-state and
calibration events live) has no registers in our read inventory. This
contract says so on every surface that would otherwise imply it, and
instruments the OBSERVABLE DELTAS instead: the system-vs-BMS delta's
change across the traverse (a re-anchor typically re-steps it), the SoC
word's discontinuities (the `soc_jump_observed` informational class is
precisely the resync signature), and the cell spread's behavior at the
anchors. The BMU event log remains the operator's on-device cross-check
after a first cycle — the same standing note the health watch's §11
carries — and an automated read of it is a documented future extension
(§14), evidence first.

### 6.3 Graduation and stand-down

Expected re-anchoring signature, ALL of: (a) the SoC word moved plausibly
across the traverse (`monotone` or `stepped`, never `frozen`); (b) after
the close, at least one discontinuity/resync event OR a material change in
the system-vs-BMS delta (absolute change ≥ `reanchor_delta_pct`, default
2.0) — and the delta instrument is QUALITY-GATED (C10, the panel's
REQUIRED ruling): both endpoints' system-word samples must carry GOOD
quality and fresh capture before a delta change can satisfy (b), because
the system word "may be hours stale on a cycled unit" (the kernel's own
comment) and a staleness artifact alone must never read as a re-anchor;
(c) the anchor was DELIVERED — satisfied by EITHER stop member (C4):
`floor_reached`, OR `floor_miss_energy_bound` followed within the close by
a word discontinuity landing at or below `floor_pct + floor_epsilon_pct`
(default 2.0) — the frozen-word pod's energy-bound stop plus the word's
late catch-up step is the anchor's TWO-WITNESS proof, and criterion (c)
was unsatisfiable on exactly that pod in v1 (the floor member reads the
frozen word; the designed first target would have stood down on its best
night); (d) the taper landed — semantics explicit (C5):
`top_anchor_missed_solar` SATISFIES (d) (the sky's account, the close was
a forecast, the anchor itself was had at the floor), `taper_never_observed`
FAILS it (the pod's account). ALL hold → the durable `calibration_anchored`
fact (derived from the completed record, the pattern of a durable
once-fact) and the pod enters ROUTINE rotation — subsequent cycles are the
same traverse with kind `routine` and the same record (the measurement
never stops being taken; it stops being the gate). ANY missing →
`reanchor_not_observed`, ALERT: the pod stands down
from rotation until an operator acknowledgement resets it
(acknowledge-inhibit, privileged) — the program does not spend a second
deep cycle proving the first one's point, and the alert's named exit is
the operator's decision tree: re-run the measurement after a physical
restart advisory, or escalate to the installer with the record. The
stand-down is durable-row-derived: a restart neither grants nor lifts it.

## 7. Config — the `battery_calibration:` block

Absent block = nothing composes (no adviser, no snapshot key, no events,
no REST surface — byte-identical behavior). Present block composes the
`CalibrationAdviser` (one tick per fleet cycle inside the existing
supervision pass, no new task class) under principal
`energypod:calibration-adviser`.

```yaml
battery_calibration:
  timezone: "Australia/Brisbane"   # REQUIRED; must equal site.timezone
  mode: "advise"                   # advise | act — advise computes and
                                   #   displays trigger/eligibility and
                                   #   submits NOTHING, ever; act is the
                                   #   operator's later revision
  window_local: "15:00"            # traverse window open (C13: wide
                                   #   enough at the 0.8 delivery floor)
  traverse_end_local: "22:30"      # no-new-renewal boundary; + ttl must
                                   #   land before the health-watch window
                                   #   and the night window (validated)
  plan_local: "14:00"              # the daily trigger/eligibility instant
  # C6: the guarded one-shot — a config-borne, audited operator request
  # that the named unit's NEXT plan_local select it as target, waiving the
  # `due` requirement ONLY (every class gate still applies). Consumed at
  # the plan tick it names; the waiver rides the trigger row. Absent key
  # = no request stands. This is SELECTION authority only: it never flips
  # mode, never bypasses a guard, never outlives one consumption.
  # request_measurement: {unit: mid, note: "2026-08-25 operator request"}
  trigger:
    trigger_floor_pct: 30.0        # X — "has not been below X% in N days";
                                   #   >= floor_pct (a completed anchor
                                   #   must reset the trigger)
    trigger_after_days: 60         # N — the quarterly-class cadence
    eligibility_window_days: 14    # the class-derivation lookback; >=
                                   #   cycles_daily_min_days
    cycles_daily_floor_pct: 25.0   # lhs-class exclusion: below this on...
    cycles_daily_min_days: 5       #   ...>= this many dates => excluded
    min_daily_throughput_wh: 1000  # mid-class control-works evidence
    throughput_min_days: 3
    probe_pass_window_days: 7      # rhs-class: a passing probe this fresh
                                   #   unlocks the deferred class
  traverse:
    floor_pct: 10.0                # [5, 10] AND >= policy.minimum_soc_pct
    discharge_w: 800               # [200, policy.max_unit_discharge_w]
    min_discharge_w: 200           # > 3 modules x the ~50 W sensing floor
    intent_ttl_s: 10.0             # (0, 300]; > timing.control_period_s
    assumed_delivery_frac: 0.8     # (0, 1] — the fit check's delivery
                                   #   floor; delivery_bias.py graduates it
    integration_max_gap_s: 10.0    # > timing.control_period_s, <= 60 (the
                                   #   scorecard's gap precedent, C9)
    energy_margin_wh: 100.0        # the lying-word bound's slack
    metering_allowance_wh: 100.0   # the integrator undercount allowance;
                                   #   margin + allowance must keep the
                                   #   frozen-word stop inside the band
    assumed_capacity_wh: {lhs: 5000, mid: 5000, rhs: 4200}  # MUST equal
                                   #   night_charging's map when present
  top_anchor:
    taper_deadline_local: "12:00"  # the operator's midday finish line
    taper_soc_pct: 99.0            # [50, 100)
    taper_limit_w: 0               # CCL collapse threshold, >= 0
    taper_sustain_s: 600           # [60, 3600] — the 10-minute rest class
    hold_min_s: 2700               # [1800, 3600] = 30-60 min
    hold_float_w: 100              # > 0, the Wave 0 deadband family
    poor_surplus_kwh: 3.0          # > 0 — the solar/pod attribution split
  measurement:
    reanchor_delta_pct: 2.0        # (0, 20] — the system-vs-BMS delta
                                   #   change that counts as a re-anchor
                                   #   step (quality-gated, C10)
    floor_epsilon_pct: 2.0         # (0, 5] — the late-step tolerance that
                                   #   lets an energy-bound stop satisfy
                                   #   graduation (c) (C4)
```

**Cross-validations (each refusal names its rule):**

- `mode ∈ {advise, act}`; `advise` submits no intent on any tick under any
  input (a named test) — the standing posture until the operator's
  revision, exactly as `forecast_suggest` was for night-V2. There is
  deliberately NO receipt gate on `mode: act` (C15, a conscious distinction
  from the health watch's A6): the act is ordinary `OPTIMIZER` dispatch —
  the night-charge precedent, no novel write class, no mode register, no
  arm — and the staging that matters (measurement-first, C6's one-shot,
  the supervised first night) is durable-row and sequencing discipline,
  not authority. The panel saw the first act is a ~7 h deep discharge of
  the pathological pod and endorsed the distinction; §12-4 records it.
- `request_measurement`, when present: `unit` in the fleet, and the pair
  is consumed at the next `plan_local` — a second request for the same
  unit while one stands unconsumed is a validation error naming it (C6).
- `floor_pct ∈ [5, 10]` AND `floor_pct >= policy.minimum_soc_pct` — the
  §4.3 ordering; the refusal names both values and the science band.
- `trigger_floor_pct >= floor_pct` (C12 — the predicate is `<=`, so
  equality still lets a completed anchor reset the trigger; below it, a
  cycle ending AT the floor would leave the pod immediately due again);
  `eligibility_window_days >= cycles_daily_min_days` (C12).
- `discharge_w ∈ [min_discharge_w, policy.max_unit_discharge_w]`;
  `min_discharge_w >= 3 × 50` (the sensing floor, named); `0 <
  intent_ttl_s <= 300` and `> timing.control_period_s`;
  `assumed_delivery_frac ∈ (0, 1]`; `integration_max_gap_s >
  timing.control_period_s` and `<= 60` (C9);
  `energy_margin_wh + metering_allowance_wh <= (floor_pct − 5)/100 ×
  min(assumed_capacity_wh)` — the SUM rule (C8): the frozen-word stop must
  not cross the science band's 5% edge even behind an undercounting
  meter; the refusal names the whole sizing.
- `taper_sustain_s ∈ [60, 3600]`; `taper_soc_pct ∈ [50, 100)`;
  `taper_limit_w >= 0`; `hold_min_s ∈ [1800, 3600]`; `hold_float_w > 0`;
  `poor_surplus_kwh > 0`; `reanchor_delta_pct ∈ (0, 20]`;
  `floor_epsilon_pct ∈ (0, 5]` (C12).
- Window arithmetic, both checks naming their numbers (§4.1):
  worst-case-traverse FIT at the delivery floor inside `[window_local,
  traverse_end_local]` — the refusal carries the divide
  `0.90 × min(capacity) / (discharge_w × assumed_delivery_frac)`;
  `traverse_end_local + intent_ttl_s <= battery_health_watch.window_local`
  (when present) and `<= night_charging` window open (always). The block's
  `timezone` must equal `site.timezone` (A9's one-civil-time rule).
- `traverse_end_local < plan_local + 24h` sanity, `plan_local` strictly
  before `window_local`; `taper_deadline_local` after the night window's
  end wall (the taper is a MORNING fact).
- `assumed_capacity_wh` present, key set exactly the fleet, every value >
  0, and EQUAL to `night_charging.assumed_capacity_wh` when that block
  carries one — one physical fact, two keys would drift (night-V2 §2.1's
  ruling, extended to this consumer).
- The `plant_history:` block must be present (the trigger and the
  measurement are historian-backed; no degrade-to-single-instant path —
  the health-watch §9 precedent for evidence-backed features).
- The rhs-class path additionally requires `battery_health_watch:` present
  with `probe` in `stages` — but its ABSENCE is a REFUSAL of that path
  only, never of the block: the affected units defer
  `no_control_evidence` and the projection says so (§3.2).
- No runtime toggle for `mode`: the authority ladder is climbed by config
  revision plus restart, the health-watch §9 rule. The console can never
  flip a traverse into existence.
- Runtime state does not persist (the standing doctrine): boot recomposes
  from the file; per-pod measurement/graduation/stand-down status is
  durable-row-derived, never boot state — and a boot that finds an open
  `calibration_traverse_opened` row reconstructs per §4.4, never resumes.

## 8. Audit, events, projection — additive vocabulary only

**Audit rows** (advisory class, the accountant pattern):

- `calibration_trigger_evaluated` — once per civil day at `plan_local`:
  per-unit vector (days_since_deep with its `horizon_bounded` flag,
  last_deep_date, horizon_days, class, throughput figures, probe-verdict
  reference), the selected target or the honest none-reason, a standing
  `request_measurement` consumption if one, and the mode.
- `calibration_traverse_opened` — at traverse start, BEFORE the first
  intent (C2): the target unit, `start_soc` (both words), the computed
  energy bound, the window figures. The restart-reconstruction anchor —
  a boot that finds one uncompleted retires the night per §4.4.
- `calibration_cycle_completed` — per traverse: the FULL §6.1 record with
  the verdict, kind, and both stop members' states. The reconstruction
  row — any later question replays from it.
- `calibration_taper_observed` — per close: the taper signature figures,
  the hold window or its interruption, the attribution word on a miss, and
  the before/after measurement deltas that land at the close (spread,
  delta pair, jumps, with both endpoints' quality words — C10). Separate
  because it lands up to a civil day later
  than the traverse that opened it.

**Durable facts:** `calibration_anchored` (once per unit, on graduation,
§6.3) — the routine-rotation receipt, derived from completed rows.

**Bus events:** `calibration.cycle` (notice on `floor_reached`,
`inconclusive_preempted`, and `inconclusive_interrupted`; alert on every
`floor_miss_*`, `taper_never_observed`, `reanchor_not_observed`; resolved
tier on a routine `floor_reached` + observed taper + observed hold). Typed
payloads mirroring the rows; no control path subscribes.

**Projection** — the `calibration_state` snapshot key (absent when
uncommissioned), the example on the C1 arithmetic (a never-deep mid is due
on its HORIZON AGE, flagged) and the C11 fix (a passing probe inside the
window UNLOCKS rhs to eligible/measurement_pending — never a deferral
beside its own unlock):

```json
"calibration_state": {
  "mode": "act",
  "window": {"opens_local": "15:00", "ends_local": "22:30"},
  "phase": "idle",
  "as_of": "2026-10-24T14:00:12+10:00",
  "units": [
    {"unit_id": "mid", "class": "eligible", "days_since_deep": 65,
     "horizon_bounded": true, "last_deep_date": null, "due": true,
     "kind": "measurement", "anchored": false, "standdown": false,
     "throughput_wh_mean": 3300},
    {"unit_id": "lhs", "class": "excluded_cycles_daily", "due": false,
     "sub_floor_dates_14d": 13},
    {"unit_id": "rhs", "class": "eligible", "days_since_deep": 65,
     "horizon_bounded": true, "due": true, "kind": "measurement_pending",
     "probe_verdict": "pass", "probe_at": "2026-10-23T23:04:00+10:00",
     "anchored": false, "standdown": false}
  ],
  "last_cycle": null,
  "request_measurement": null
}
```

Skips and deferrals render their reason verbatim; `mode: advise` renders
the whole projection with `"submits": "never"` named beside it — a
displayed plan that does not act MUST say so beside itself (the
invisible-state rule). Uncommissioned renders nothing at all.

## 9. Alerting and the morning surface

| Tier | Trigger | Surface |
|---|---|---|
| notice | trigger due computed (a pod's N days elapsed) | the card's due line — the operator sees the traverse coming a day ahead |
| notice | `inconclusive_preempted`; `top_anchor_missed_solar` | the card's morning line with the reason |
| notice/alert | `inconclusive_interrupted` (C2) — a restart retired the traverse night; alert tier iff the pod was left deeper than a graceful stop would leave it | the morning alert names the state the pod was actually left in, with the open row's figures; never a same-night resume |
| alert | any `floor_miss_*` — the anchor was not had, and the arithmetic or the lying word says why | alert-tier event; the card promotes the unit row with the energy/SoC figures and the trace class |
| alert | `taper_never_observed` (pod attribution) | alert tier; names the health watch's stuck predicates as the next diagnostic |
| alert | `reanchor_not_observed` after a first cycle | alert tier; the stand-down is stated, the operator decision tree named (§6.3); BMU-log cross-check note rides it |
| resolved (info) | routine cycle: `floor_reached` + taper + hold observed | the morning line: before/after deltas, spread, energy, and the honest done-what sentence |

Every alert carries its evidence the vendor's own tooling would (the
Be-Connect pattern, inherited from the health watch §11): the audit row
reference plus the figures excerpt, so the operator can act without
opening a log directory.

**Console — recommendation: its own compact card, BESIDE the health-watch
card, not a line on it.** Reasons: A14 drew the sibling line for programs,
and a surface grafted onto the watch's card extends that contract in
spirit while its code stays separate — the vocabulary mismatch would show
(the watch speaks verdict-per-unit; calibration speaks class, due-date,
and cycle-state); the two programs share the maintenance ROW on the
console (one visual neighborhood, one alert styling family) and nothing
else. The card is small and mostly quiet: a per-unit row (class chip, due
date or excluded/deferred reason), the active traverse's live line on
cycle nights (target, rate, SoC vs floor, energy vs bound), and the
morning-after line the day following a cycle — which also lands on the
shipped morning-facts surface (the History console's morning states) as
one entry: unit, verdict, the delta pair, the spread change. Wherever the
traverse's live line renders, the card NAMES the standing stop route
(C16): "to stop tonight's traverse, claim the pod — any manual command
preempts instantly" — no new kill switch exists or is wanted, and the
surface says how the operator's existing authority reaches the act. The
pinned §0 sentence rides the card's traverse state wherever it renders.
Shot matrix (states × viewports) per the polish bar; one rhythm/one
vocabulary/live-refresh per the standing bar.

## 10. Commissioning sequence

0. **THE QUIESCENCE, STATED (C6):** the historian was commissioned days
   ago, and the trigger is honest about it — for the first
   `trigger_after_days` after commissioning, EVERY pod renders
   `evidence_short` and no night is due, because a 5-day horizon cannot
   prove a 60-day absence. Waiting the full N days for mid's first
   measurement is one operator choice; the design's recommendation is not
   to wait, because the one-shot exists: `request_measurement: {unit:
   mid}` in the SAME revision that flips `mode: act` names mid at the next
   `plan_local` with the waiver recorded — selection only, every gate
   intact, consumed once. The panel's ruling: mid-first commissioning is
   blocked only until this one-shot exists; it now does.
1. **ADVISE** (config revision one): the block present at `mode: advise`
   with the §7 defaults. Runs the trigger and eligibility daily, writes
   rows, renders the card, submits nothing (a standing `request_measurement`
   under advise plans the named unit's traverse and says so — still no
   intent). Minimum run: enough days to
   see the trigger's honest edge — the fleet's `evidence_short` clearing
   into due flags (or the one-shot naming mid through it), lhs excluded on
   its figures, rhs's class resolved by whatever the health watch's probes
   actually return. This step calibrates the operator's trust in the
   CLASS DERIVATION before any watt moves.
2. **THE SUPERVISED MEASUREMENT** (config revision two: `mode: act`, plus
   the C6 one-shot for mid unless the quiescence has already cleared): the
   operator names the evening; mid is the designed first target (the
   eligible mid class, the pod the program exists for). PRECONDITION,
   named (C14): exactly one refill writer must be live that night — the
   night adviser enabled, or Docker still standing — or the close is
   solar-dependent from the floor, and the morning says so. One supervised
   traverse — supervised reads of the card, not an attended event: the
   traverse is 800 W of ordinary dispatch under every standing guard, and
   the card names the manual-claim stop route throughout. The
   §6 record lands; the morning's taper and hold are observed; the
   re-anchor signature is evaluated. Evidence file in `docs/evidence/` is
   recommended (the standby-cycle precedent) though not config-enforced —
   the act is ordinary dispatch, the night-charge precedent, not a novel
   write class needing a receipt gate (C15 records the distinction as
   conscious; §7's validation note carries it).
3. **GRADUATION REVIEW:** the operator reads the first-cycle record. On
   the expected signature, mid enters routine rotation (the durable
   `calibration_anchored` fact, automatic). On its absence, the stand-down
   holds and the §6.3 decision tree is the operator's.
4. **rhs, PROBE-GATED:** rhs's first measurement waits for a passing
   health-watch probe within `probe_pass_window_days` — the interlock is
   live from step 1's advise run, so the answer is already on the card.
5. **STEADY STATE:** the quarterly-class rhythm runs itself; the morning
   line is the operator's daily read; thresholds are re-examined after the
   first season with the trigger rows as the evidence base.

## 11. Test matrix (author FIRST, per repo doctrine)

- **T-CAL-CONFIG** — absent block composes nothing (byte-identical
  snapshot); every §7 bound and gate with its named-message refusal; the
  floor-band AND kernel-ordering rule (a floor of 8 against a policy floor
  of 10 refused naming both); the C12 set — `trigger_floor_pct >=
  floor_pct` (with the completed-anchor-resets edge at equality),
  `eligibility_window_days >= cycles_daily_min_days`, and every taper/
  hold/measurement bound (`taper_sustain_s`, `taper_soc_pct`,
  `poor_surplus_kwh`, `reanchor_delta_pct`, `floor_epsilon_pct`,
  `hold_float_w`); the C8 SUM rule (`energy_margin_wh +
  metering_allowance_wh` vs the band edge, refusal naming the sizing);
  the C6 one-shot's shape (unit in fleet; duplicate-standing refusal);
  the capacity-map equality with `night_charging`'s; historian-block
  prerequisite; BOTH window checks naming their arithmetic — the fit
  check at the DELIVERY floor (C13: the divide carries
  `discharge_w × assumed_delivery_frac`; a 0.8 floor against a too-narrow
  window refused with the ~7.0 h figure); end+ttl vs the health-watch
  window and the night window; timezone equality; no runtime toggle for
  mode exists.
- **T-CAL-TRIGGER** — days-since from hourly rollup minima (a date's
  minimum below X resets the clock; above does not); X/N edge values; the
  C1 shape: a NEVER-deep pod is due on its HORIZON AGE with
  `horizon_bounded` flagged once the horizon is judgeable — never `None`,
  never ineligible-by-absence; the evidence-short deferral (a historian
  horizon younger than N renders unjudgeable, never a fake-long window)
  and its CLEARING into a horizon-bounded due; gap exclusion never
  interpolation; the full per-unit vector on the row; the C6 one-shot's
  consumption at plan_local with the `due_waived` record.
- **T-CAL-ELIGIBILITY** — the lhs-class exclusion (sub-25% on ≥5 of 14
  dates); mid-class throughput evidence; the rhs-class interlock: deferred
  with no passing probe in the window, unlocked by a `pass` row, kept
  deferred by `fail_*`/inconclusive; health-watch absent →
  `no_control_evidence`, never a guess; selection by greatest
  days-since-deep (horizon-bounded figures competing honestly) with
  deterministic ties; one target per civil night across restart
  (durable-row derivation).
- **T-CAL-TRAVERSE** — the deadline-rate math (self-correction from
  measured SoC; cap and min clamps; at-risk rendering); the STOP SET
  ordering: the floor checked pre-submission — the tick that touches the
  floor submits nothing and the kernel never sees an at-or-below-floor
  intent (the named invariant); non-renewal stops only (no stop triple,
  no idle intent, ever — the architectural pin); the energy bound's
  co-computation from the START SoC and margin; C9's integration
  discipline (live word at tick cadence; a gap beyond
  `integration_max_gap_s` ends the leg `aborted:telemetry_lost`, never
  interpolates, the bound never evaluated across a gap); deadline
  floor-miss; preemption by MANUAL/AGENT/e-stop → `inconclusive_preempted`,
  no re-run; the C3 skip — a live not-own OPTIMIZER claim (the excess
  adviser at the 15:00 open) excludes the unit at submission, the anchor
  waits and never contests, and the adviser's own `cal-` intent is never
  a foreign claim; the full skip-if set each rendering its skip verbatim;
  C2's restart legs — a mid-traverse restart writes
  `inconclusive_interrupted` from the open row at boot, retires the night,
  raises the morning alert (notice tier normally; alert tier when the
  historian shows the pod deeper than floor + `reanchor_delta_pct`), and
  NO same-night resume exists under any input (property-style); a
  mid-leg GUARD abort writes the completed row with the guard's word and
  the figures reached; advise mode submits nothing on any tick.
- **T-CAL-FROZEN-WORD** — the measurement-first failure leg end-to-end: a
  scripted pod whose SoC word never moves while watts flow → the floor
  member never fires, the energy bound stops the traverse,
  `floor_miss_energy_bound`, the alert, the trace class `frozen` on the
  record, NO graduation, stand-down until acknowledgement; the kernel's
  cell floor never reached (the bound sits above it by construction); and
  C4's companion leg — a pod whose word steps BELOW the floor only after
  the energy-bound stop, inside the close, within
  `floor_epsilon_pct` → criterion (c) SATISFIED, graduation proceeds on
  the other members.
- **T-CAL-NIGHT-INTERPLAY** — the cycled pod refills the same night under
  both pacings with the §5.1 arithmetic named; the C5 forecast_act
  close-dependence (a sunny-forecast night refills 8→~50 and the taper's
  miss carries the SOLAR attribution, which satisfies §6.3(d)); the C14
  no-refill-writer case (neither the night adviser nor Docker live → the
  pod sits at the floor to sunrise and the morning line names the
  observed refill source); no calibration intent alive
  at 00:00 (end+ttl < window open, proven); the cycled pod's 23:00 probe
  skip at the floor renders honest; the SoC jump rides informational; the
  fleet-spread note recorded, nothing vetoed; the morning target math
  verified against `even_rate_w` itself.
- **T-CAL-TAPER** — the signature (99% + CCL 0 sustained 600 s); the hold
  window observed; `hold_interrupted` honesty (no fabricated completion,
  no protective write); the attribution split (poor-solar notice vs
  pod-refused alert, cut at `poor_surplus_kwh`) and its §6.3(d)
  semantics (solar satisfies, pod-refused fails); deadline expiry paths.
- **T-CAL-MEASUREMENT** — the full record's fields (both SoC words, the
  delta pair, spread before/after, jumps, energy, rates); the C10 quality
  gate: a delta change computed against a STALE or non-GOOD system-word
  endpoint can NEVER satisfy criterion (b) — the staleness-artifact leg is
  the named regression vector; the re-anchor
  signature's four members each holding/failing ((c) via EITHER stop
  member, (d) via the attribution semantics); graduation writes the
  durable fact exactly once; stand-down survives restart; the
  acknowledge-inhibit path.
- **T-CAL-ECONOMICS** — the projection/audit figures match the tariff
  arithmetic (displacement, refill, BOTH branches of the exported case:
  ≈ +99 c house-absorbed, ≈ −26 c fully exported — C7's correction is the
  named vector) — the honesty
  test that the numbers on the surface are the numbers in the record.
- **T-CAL-PROJECTION/EVENTS** — additive keys only; absent-block frames
  byte-identical; advise renders `submits: never` beside the plan; the
  horizon_bounded flag on every due figure; tiers
  per §9; skip/deferral reasons verbatim; no control path subscribes.
- **T-CAL-ARCHITECTURE** — the adviser composes the facade intent twin and
  no transport of its own; the historian via injected port (no application
  import of adapters); no new write method exists anywhere on the path
  (mutation-test the composition); the night-writer detector stays quiet
  through a traverse (the claimed-unit gate).
- **T-CAL-SIMULATOR** — the scripted legs: a pinned-SoC pod (traverses,
  re-anchors — the happy measurement); the frozen-SoC word (T-CAL-FROZEN-
  WORD's plant) and the late-step twin (C4); a CCL-taper script (the top
  anchor lands or refuses);
  spread-before/after scripting; the sensing-band meter (a commanded 200 W
  reads nonzero, a 100 W reads zero — the min-rate justification leg); a
  restart-in-mid-traverse harness for the C2 reconstruction.
- **T-CAL-CONSOLE** — the card's states × modes (uncommissioned, advise,
  idle, due, traversing, morning-after, stand-down); the morning-facts
  entry; the pinned sentence AND the C16 stop-route line wherever
  traverse styling appears; alert tiers incl. the interrupted-morning
  alert; shot matrix.

## 12. For-the-panel flags — with the panel's answers folded in

1. **The 5–10% band is half-closed by the standing policy.** With
   `minimum_soc_pct` at 10.0, only floor 10.0 commissions (§4.3/§7). Is
   the right shape to keep, or should the contract ask the operator
   whether the 5–8% half of the SOURCED band is worth a policy revision?
   This design's answer is KEEP — the science's own caution (undervoltage
   risk at reported low SoC) argues for the conservative edge — but the
   panel should see that the "config-bounded [5,10]" is, at today's
   policy, a constant. **Panel: the KEEP recommendation endorsed — floor
   stays at 10 (the ruling is recorded in §16); §13 item 2 keeps the
   operator's door open.**
2. **The energy bound leans on the capacity assumption.** An OVERSTATED
   `assumed_capacity_wh` pushes the lying-word guard deeper than the
   physical anchor (the kernel's 2.80 V floor is the last line); an
   understated one stops a healthy pod early (a floor-miss alert, the safe
   direction). rhs's 4,200 Wh is cell-count-derived, unmeasured — the same
   open assumption night-V2 §2.6 carries, now with a second consumer.
   **Panel: answered by mechanism — C8's SUM sizing keeps the stop inside
   the band even behind an undercounting meter, and the measured-capacity
   graduation (§14) is the standing exit; §13 item 1 unchanged.**
3. **The rhs-class interlock reads a row the sibling writes.** The durable
   `health_probe_completed` dependency is one-directional and
   fail-deferred (§3.2), but it IS a cross-contract coupling; the panel
   should confirm the row's shape is stable enough to lean on (the watch
   is CONTRACT v1.1, implemented census+probe). **Panel: accepted as
   designed — the row is the watch's own §10 vocabulary, implemented and
   shipped; C11's example fix was the only correction wanted.**
4. **One deep discharge into an evening we cannot see.** The traverse runs
   unattended at 800 W for up to ~7 h with every standing guard judging
   it, but no calibration-specific kill switch beyond the standing ones;
   the TTL lapse is the crash story. Is the guard set complete enough for
   a DISCHARGE-to-10% objective, or does the panel want a
   calibration-specific abort route (e.g. the console traverse card's
   stop, an interactive act)? This design's answer: the standing guards
   plus non-renewal suffice — the mission is one pod, one night, 800 W —
   but the question is asked rather than assumed. **Panel: no new kill
   switch (the §16 ruling) — C2's retire-the-night closes the one
   interleaving the standing set missed, and C16 puts the EXISTING stop
   route (a manual claim preempts instantly) on the card in words.**
5. **`system_soc_pct`'s staleness on a cycled unit.** The kernel's own
   comment says the system word "may be hours stale on a cycled unit" —
   which makes the delta instrument (§6.1) partially a staleness
   detector. The record should (and does, via the quality words on the
   historian rows) carry each word's quality; the panel may want the
   delta measurement explicitly quality-gated before it can satisfy the
   re-anchor signature. **Panel: REQUIRED — C10 folds the gate in: both
   endpoints' system-word quality GOOD and fresh before criterion (b) can
   be satisfied; T-CAL-MEASUREMENT carries the staleness-artifact leg.**

## 13. Operator decisions this package needs

1. **Capacities** (shared with night-V2 §13 item 1, now load-bearing in a
   second place): "Confirm rhs's usable capacity — 4,200 Wh assumed,
   cell-count-derived. An overestimate deepens the frozen-word guard's
   stop; the kernel's 2.80 V floor is beneath it either way, but the
   record's energy figures deserve the truth."
2. **The floor:** "The traverse stops at 10% (the policy floor and the
   science's conservative anchor). Opening 5–8% would require lowering the
   kernel's global discharge floor for every writer on this controller —
   this design recommends against; confirm, or rule."
3. **Same night as a health-watch probe?** The windows cannot overlap
   (validated), so both run mechanically; the cycled pod's own probe at
   23:00 will skip at the floor (honest, named). The real question: do you
   want the watch's census EVIDENCE WINDOW to include the traverse night
   (it will — the 6 h lookback sees the deep pod), or should a calibration
   night suppress that pod's census rendering? This design's default:
   include it — a deeply discharged pod cannot look stuck, and the row
   says what happened.
4. **The hold and solar hours:** the default ends all observation at
   midday and never writes to protect a hold (§5.3). Confirm, or rule the
   hold may extend into the afternoon when the house draws mid-hold.
5. **X/N defaults:** X=30, N=60 gives a quarterly-class cadence; lhs-class
   exclusion at sub-25% on 5-of-14 days. These are the research's numbers;
   the first season's trigger rows are the evidence to retune them.
6. **The mid-first sequencing:** the supervised measurement targets mid
   (the pod the program exists for). Confirm mid, or name another first.

## 14. Future extensions (documented, deliberately unbuilt)

An automated BMU event-log read as the calibration-event cross-check
(evidence first — no registers in the inventory today); a measured
capacity graduation from the traverse records themselves (each
floor-reached cycle MEASURES deliverable energy — after a season, the
`assumed_capacity_wh` map can be trued against its own product); an
energy-scorecard correlation (calibration nights vs the day records'
round-trip efficiency); a second-cycle cadence for pods whose re-anchor
decays faster than N days; a charge-direction taper assist for
pod-refused top anchors (firmly out of scope until a documented case
exists — the autonomy owns 95→100%); asymmetric floor personalization per
unit class. **Boundary restated (the sibling's A14, from the tenant's
side):** this program stays in its own block and its own window; any
future maintenance program does the same; nothing in this contract
extends the health-watch stages, the schedule union, or the night
adviser's target machinery.

## 15. Same-round amendment sweep (at implementation time, not by this file)

1. `docs/API_CONTRACTS.md`: the write-enabled section names the
   calibration adviser's discharge intents under
   `energypod:calibration-adviser`; the snapshot key and events from §8.
2. `docs/DESIGN_NIGHT_CHARGE_V2.md` §2.6's capacity-assumption note gains
   this contract as a second consumer (the equality validation's reason).
3. `docs/DESIGN_BATTERY_HEALTH_WATCH.md`: NO amendment — A14 already
   granted the boundary; the one interaction (the probe-row interlock and
   the disjoint windows) is this contract's own dependency, recorded here.
4. `docs/CONTINUITY.md` non-negotiable invariants amended by the
   coordinator thread at round close (this design does not edit it).
5. The `config.live-write-example.yaml` block comment for
   `battery_calibration:` when the implementation wave lands (the §10
   sequencing, the advise default, the sensing-floor justification for
   `min_discharge_w`).

## 16. Amendment log (v1.1 — the adversarial round)

The panel's verdict: WITH-AMENDMENTS, architecture sound, no redesign —
the adviser shape, the ordered stop set, the measurement-first doctrine,
the A14 boundary, and the night-V2 interaction were verified against the
code. All sixteen amendments are folded in-place above; this log is the
index.

- **C1 (BLOCKER)** — the trigger made the designed first target
  permanently ineligible: `None if never` + `due requires is not None`
  meant a pod never below X since the historian's first rollup could
  never be due, and the historian holds no pre-May data, so mid could
  never produce a `last_deep_date` (the v1 §8 example presumed rollups
  that cannot exist). Fixed: once the horizon is judgeable, a never-deep
  pod is DUE on its HORIZON AGE, flagged `horizon_bounded` on every
  surface; `None` defers only where `evidence_short` defers. §3.1/§3.3/
  §7/§8/§10/§11.
- **C2 (BLOCKER)** — same-night restart mid-traverse was unspecified and
  both readings broken: resume would reset the energy accumulator and
  `start_soc` against a half-emptied pack (a full second ~4,700 Wh
  traverse with only the 2.80 V cell floor beneath); no-resume wrote no
  completed row and had no crash-honest reconstruction. Fixed by the
  health-watch A4 pattern: a durable `calibration_traverse_opened` row at
  traverse start, reconstructed at boot as `inconclusive_interrupted` +
  morning alert (notice tier normally, alert iff the pod was left deeper
  than floor + `reanchor_delta_pct`); restart RETIRES the night; no
  same-night resume exists under any input; mid-leg guard aborts write
  their completed row with the figures reached. §4.4/§7/§8/§9/§11.
- **C3 (MAJOR)** — the skip-if set omitted a live not-own OPTIMIZER claim:
  the excess adviser charges from surplus across the traverse's open
  hours, and two equal-priority opposite-direction OPTIMIZER intents is
  the tie-flap the night adviser eliminates by exclusion. Added, with the
  dawn-corner precedence stated: FREE surplus outranks the anchor, one
  direction — the anchor waits, never contests; the adviser's own `cal-`
  intent is never a foreign claim. §3.3/§11.
- **C4 (MAJOR)** — graduation criterion (c) `floor_reached` was
  unsatisfiable on the frozen-word pod (the floor member reads the frozen
  word), so mid's first measurement necessarily ended
  `floor_miss_energy_bound` → stand-down even when the traverse physically
  delivered the anchor and the word re-stepped. Fixed: (c) is satisfied
  by EITHER stop member — `floor_reached`, OR `floor_miss_energy_bound`
  followed within the close by a discontinuity landing at/below
  `floor_pct + floor_epsilon_pct` (new key, default 2.0); the
  between-ticks step race is named in §4.3. §4.3/§6.3/§7/§11.
- **C5 (MAJOR)** — the forecast_act close-dependence was unstated and
  §6.3(d)'s parenthetical did not say whether a solar miss satisfies.
  Fixed: §5.1 states the inverted arithmetic (a SUNNY forecast targets
  LOW — the pod refills 8→~50 and the top anchor rides the same
  under-priced morning); §6.3(d) pins the semantics —
  `top_anchor_missed_solar` SATISFIES, `taper_never_observed` FAILS.
  §5.1/§6.3/§11.
- **C6 (MAJOR)** — the ~N-day `evidence_short` quiescence after historian
  commissioning was unstated, and no operator mechanism existed to
  request mid's first measurement now. Fixed: §10 states the quiescence;
  a guarded config-borne, audited one-shot `request_measurement` names
  one unit, is consumed at the next `plan_local`, overrides SELECTION
  only (never authority, never mode), and records the `due_waived`
  waiver. §3.3/§7/§10/§11.
- **C7 (MINOR)** — the economics miscounted the exported worst case as
  break-even: 8.7 c received against ~35 c refill is ≈ MINUS 26 c per
  cycle; +99 c is the house-absorbed branch. Both branches now carried,
  trivial-but-correct. §5.1/§11.
- **C8 (MINOR)** — the margin bound left zero metering allowance: a ~2%
  undercount across a ~4.7 kWh leg (~94 Wh) landed the frozen-word stop
  at ~4.1% physical, beneath the band edge. Fixed: `metering_allowance_wh`
  (default 100) joins `energy_margin_wh` (default lowered to 100) under
  the SUM rule `margin + allowance <= (floor_pct − 5)/100 ×
  min(capacity)`; the refusal names the whole sizing. §4.3/§7/§11.
- **C9 (MINOR)** — the energy integration's discipline was unstated.
  Pinned: the LIVE `battery_watts` word at tick cadence, never a historian
  re-read; a gap beyond `integration_max_gap_s` (default 10, the
  scorecard's precedent) is excluded-never-interpolated and ENDS the leg
  `aborted:telemetry_lost` (a gap is by construction a staleness event
  that denies the intent at the kernel — the undercount-fires-late
  direction is structurally unreachable). §4.3/§7/§11.
- **C10 (MINOR)** — the delta instrument was not quality-gated while the
  system word may be hours stale on a cycled unit; staleness alone could
  satisfy (b) as a false re-anchor. Ruling REQUIRED, folded: both
  endpoints' system-word quality GOOD and fresh before (b) can be
  satisfied; the staleness-artifact leg is a named regression vector.
  §6.3/§8/§11/§12-5.
- **C11 (MINOR)** — the §8 example showed rhs `deferred_probe_required`
  beside a passing probe inside the 7-day window (a contradiction); the
  example now reads eligible/`measurement_pending` and rides the C1
  horizon-bounded arithmetic. §8.
- **C12 (MINOR)** — cross-validations added: `trigger_floor_pct >=
  floor_pct` (with the completed-anchor-resets edge at equality);
  `eligibility_window_days >= cycles_daily_min_days`; bounds for
  `taper_sustain_s`, `taper_soc_pct`, `poor_surplus_kwh`,
  `reanchor_delta_pct`, `floor_epsilon_pct`, `hold_float_w`. §7/§11.
- **C13 (NOTE)** — the fit check assumed commanded = delivered; at mid's
  unmeasured 0.8× discharge delivery the traverse needs ~7.0 h against
  the v1 6.5 h window. Resolved by folding the delivery floor in
  (`assumed_delivery_frac` default 0.8, `delivery_bias.py` the graduation
  path) and widening the default window to 15:00 (7.5 h) so the DESIGNED
  first target fits at the planning figure rather than alerting on its
  first night. §4.1/§7/§11.
- **C14 (NOTE)** — the close silently assumed a refill writer:
  `night_charging.enabled` is currently false and Docker does the refill;
  if NEITHER runs, the cycled pod sits at 8–10% until solar. Stated in
  §5.1 with the morning line naming the observed refill source, and made
  a named precondition of the supervised measurement. §5.1/§10/§11.
- **C15 (NOTE)** — unlike the health watch's A6, `mode: act` carries no
  receipt gate. Recorded as CONSCIOUS: the act is ordinary OPTIMIZER
  dispatch (the night-charge precedent — no mode register, no arm, no
  novel write class), while the staging that matters (measurement-first,
  the C6 one-shot, the supervised first night) is durable-row and
  sequencing discipline; the panel saw the first act is a ~7 h deep
  discharge of the pathological pod and endorsed the distinction. §7/§10/
  §12-4.
- **C16 (NOTE)** — with C2 retiring the night on restart, no new kill
  switch is wanted; the traverse card NAMES the existing stop route
  verbatim: a manual claim preempts instantly. §9/§11/§12-4.

**Reviewer rulings, recorded verbatim:**

1. "Stop-set sound against frozen word/blips/gaps (lying word caught by
   the 2.80 V floor + dynamic-limit collapse, exposure bounded ~3.3 Wh at
   800 W between 15 s cell refreshes — inherited standing protection);
   restart (C2) was the one interleaving that defeated it."
2. "Floor KEPT at 10 — the KEEP recommendation endorsed (the panel's
   answer to §12-1)."
3. "No new kill switch (C16)."
4. "Mid-first commissioning blocked only until C6's one-shot exists."
