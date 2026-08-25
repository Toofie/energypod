# Evening load sharing — the netted-meter fleet discharge program

Design date 2026-08-25, panel-folded the same day. Status: CONTRACT v1.1 —
the adversarial review's verdict was WITH-AMENDMENTS, implementable after
the two blockers folded: the architecture, the claim discipline, the
windows, and the validations were all verified sound; the CONTROL LAW was
not (E1/E3 — the v1.0 identity clipped the loop's only export-direction
correction) and the traverse-night arithmetic was not (E2/E5 — excluded
units' flow was counted and never subtracted, and the per-unit exclusion
races at the sibling's renewal seam). All fourteen amendments (E1–E14)
are folded into the sections where they landed, and the reviewer rulings
are recorded verbatim in the amendment log (§18).
Parents: `docs/CONTINUITY.md` update log 2026-08-25 (the rebalancing
research round — the HTW Berlin efficiency figures, the meter-netting
finding, the shelved-transfer ruling, and the operator's stated goal this
program serves), `docs/DESIGN_CALIBRATION_CYCLING.md` (the sibling evening
adviser whose window this program shares, whose §7 cross-validation
arithmetic this contract mirrors, and whose C15 ruling — act-mode ordinary
dispatch needs no receipt gate — this program inherits), and
`docs/DESIGN_BATTERY_HEALTH_WATCH.md` (the A14 sibling boundary: own block,
own window, own vocabulary), `docs/DESIGN_NIGHT_CHARGE_V2.md` (the adviser
doctrine, the tariff arithmetic, the load-baseline provider), the shipped
machinery (`src/energypod/application/night_charge.py` — the tick shape,
the claim-exclusion discipline, the facade twin; `excess_charge.py` — the
fleet grid rollup this program's control basis is built from; `safety.py`
— the kernel's per-unit deny set and per-direction fleet limit), and the
old stack's own precedent (`C:\Users\vagrant\Downloads\modbus\manager.py`
610-705, reconstructed as §1.2's first row).

This document is DESIGN ONLY. It touches no `src/`, `tests/`, `config/`, or
`web/` file, authorizes no live-hardware interaction, and grants no new
authority of any kind: the program is a strategy ADVISER in the exact
night-charge class — it submits ordinary short-TTL `OPTIMIZER` DISCHARGE
intents under a composed automation principal through the internal facade
twin, judged by the arbiter, allocator, SafetyKernel, and actor exactly as
the night and calibration advisers' intents are. No new write primitive, no
mode register (values 2–6 of `0x8000` stay PERMANENTLY PROHIBITED, as
everywhere), no arming (the program never arms a unit — dispatch requires
arm, and arm is the operator's interactive act, the health-watch doctrine),
no schedule publication. Every actuation is gated behind its own config
block that is ABSENT by default.

## 0. What this is — and is not

The house load is concentrated on roughly one and a half of the three
phases: the kitchen/downstairs phase carries most of the evening draw, the
garage phase almost nothing, upstairs partially. Each pod CT-follows only
its OWN phase, so the kitchen pod does the evening work alone while the
garage pod sits full with nothing to serve — uneven by late afternoon, the
working pod drained, the full pods' energy stranded. The meter NETS across
phases (NET-billed, an established site fact), so a discharge commanded on
an idle phase is not lost: it offsets the kitchen phase's import watt for
watt at the meter, ONE conversion (BAT2AC ~95.7% mean at rated power) with
no second conversion anywhere. The program is the fleet-discharge adviser
that makes all three phases contribute to the one load the meter sees: it
measures the netted site exchange and the fleet's own discharge (both
directly measured), computes the TOTAL work being done or demanded, splits
that total across the eligible pods by a SoC-weighted share so the fleet
also CONVERGES while it serves, and submits the split as one renewed
short-TTL intent — the same discipline the night charge runs its charge
legs on, with the direction flipped.

**What this is not.** It is NOT a balancer. This is not a rhetorical
disclaimer: the 2026-08-25 research round examined a battery-to-battery
transfer balancer (the old stack's `balance_battery_soc`, never scheduled,
25% export bias) and ruled it DEFERRED — the honest round-trip figure is
~80–90% (two conversions against one), and the ruling stands verbatim:
*build the transfer balancer only if one pod remains regularly
low-by-late-afternoon while others sit full.* This program is the
PARTICIPATION fix that ruling preferred — convergence as a side-benefit of
useful work, zero double conversion — and its own records (the per-evening
convergence deltas, §8) become the trigger's evidence base: if the
SoC-weighted sharing of §5 leaves one pod regularly stranded anyway, THAT
is the observation that re-opens the balancer, and this contract says so
where the numbers live. It is not an import-blocker: when the evening load
exceeds what the fleet can serve, the program serves what is servable and
the grid covers the rest — the grid is the backstop, never an obstacle to
be fought. It is not a daytime program: the window is the evening (§4),
late-surplus hours belong to the excess adviser, and the two advisers
partition the netted-meter axis between them without ever meeting (§7).
And it is not a wedge-fighter: every standing guard (foreign writer,
latched stop, park, manual/agent/schedule claim, a live not-own OPTIMIZER
claim) judges these intents, and a refusal is a SKIP with the guard's
reason, never a retry.

One pinned sentence rides every surface this feature adds, verbatim:
*"The meter nets — every commanded watt exists to cancel a metered watt;
convergence is the weighting's side-effect, and no watt is ever exported
for it."* (v1.0 pinned "spent", and the panel was right to strike it,
E7: the re-split rides the partial-load curve and pays a priced ~3–5%
pack-energy premium on the re-shared watts — §5.1 carries the price and
§5.3 the mitigation — but no watt is ever EXPORTED for convergence, and
that is the honest edge of the claim.)

## 1. The evidence base

One operator-commissioned research round (2026-08-25, recorded in
CONTINUITY) plus this site's own archaeology; each finding carries its
evidence class, because thresholds built on the wrong class are how Wave 0
bugs happen (the standing doctrine, inherited).

### 1.1 Sourced findings (the 2026-08-25 rebalancing round)

| Finding | Source class | Consequence in this design |
|---|---|---|
| One-way BAT2AC efficiency: 95.7% mean at rated power, collapsing to 63–86% at partial load (HTW Berlin Stromspeicher-Inspektion 2025) | Sourced measurement | The per-pod power floor ~500 W (§5): shares below the collapse region buy the same watts with measurably more pack energy; the efficiency case for FEWER pods at LARGER shares, not more pods at tiny ones |
| Battery→AC→battery round trip ≈ 80–90% — the operator's assumed 5% transfer bias was WRONG; honest starting factor charge = 0.85 × discharge | Sourced measurement | The §0 not-a-balancer ruling: any transfer design spends 10–20% of every moved kWh; this program's netted routing spends nothing (one conversion, already paid for by the serving) |
| Import and export accumulate INDEPENDENTLY on the meter: 30.77 c paid per imported kWh against 2.0 c earned per exported kWh, simultaneously | Site tariff (commissioned block) + the round's finding | The spill arithmetic (§3.4): overshoot is not "free offset in reverse", it is a 2 c sale against a 30.77 c displacement — and undershoot is a 30.77 c purchase; both priced, neither waved at |
| Meter cross-phase netting is the norm but CONFIGURABLE VARIANCE EXISTS — verify from interval data before any commissioning | Sourced caveat | §10's commissioning step: the netting assumption is VERIFIED from the site's own interval data and recorded as evidence BEFORE the first act-mode evening; the program's entire mechanism is this one fact |
| No vendor guidance exists for independent-pod balancing; the German BYD mega-thread's mid-range divergence is partly BMS artifact | Sourced absence | The program claims NOTHING about SoC truth: convergence is measured in the same BMS word every other program reads, and the weighting is a share rule, not a gauge fix |

### 1.2 Local archaeology (the old stack, this fleet's telemetry, our code)

| Finding | Class | Consequence in this design |
|---|---|---|
| The old stack's `discharge_all_batteries` (manager.py:610-705): total = Σ per-pod LOAD CT words; fleet command = total × 1.1 + 35 W; per-pod share weighted by SoC³ (weight = soc ** 3); pods ≤ 20% SoC skipped; scheduled 17:00–23:59 by `_schedule_task_within_forced_discharge` (manager.py:185-189) at 1 s cadence — and the scheduler registration line is COMMENTED OUT (manager.py:157): the feature was scheduled, then disabled | Confirmed by prior-integration code | The §1 precedent, reconstructed honestly: the SoC-weighted split, the 20% participation floor, and the evening slot are site precedent; the CT-word load basis (rejected, §3.2) and the deliberate 10% spill (replaced by the bounded tolerance, §3.4) are the two parts this contract does NOT inherit, each with its reason |
| The +10% + 35 W fudge is itself evidence the pods deliver slightly less than commanded — the mirror image of OUR filed +15–16% discharge OVERSHOOT | Prior-integration comment + our filed bias | The command derate (§3.4): commanded = desired ÷ `assumed_discharge_over_frac` (default 1.16, the filed range's safe edge — E1) so the fleet does not structurally spill; `delivery_bias.py` is the graduation path (a doctrine change to consume, E14/§16) |
| The participation pathology this program fixes is live: rhs the spectator (load CT ~16 W against siblings' ~100/~180 W), mid pinned at 100% while flowing 3.3 kWh/day, lhs doing the work — the fleet's evening capability is ONE pod's depth, not three | Live-observed in this fleet's telemetry | The operator's goal ("more power available in the evening") quantified in §0: kitchen-pod-alone ≈ 3.75 kWh usable evening energy (95→20% of 5,000 Wh) against fleet-shared ≈ 10.65 kWh (0.75 × 14,200 Wh) — 2.8× the evening capability, no new hardware |
| The excess adviser's fleet grid rollup (`excess_charge.py`): `grid_power_w` summed across pods (positive = export), worst-word-wins evidence (`missing > bad > stale > good`), one unreadable phase is NEVER treated as zero | Confirmed by our code | The control basis's evidence rules are a REUSE, not a reinvention (§3.3): the same words, the same fail-closed classification, the opposite sign convention's consumer |
| The kernel's per-unit deny set and per-direction fleet limit: `soc_below_discharge_floor` at `policy.minimum_soc_pct` (10.0), telemetry staleness at 3.0 s, the ramp term `ramp_limit_w_per_s (1000) × heartbeat_interval_s` inside `_unit_limit`, `fleet_discharge_limit_w` (6000) applied to the discharge subtotal, per-unit static/dynamic discharge limits | Confirmed by our code | The §5 caps and the §6 submission shape: the program's arithmetic bounds itself by the same limits the kernel enforces; the kernel remains the backstop, never the plan |
| The night adviser's claim discipline: `_CLAIMING_SOURCES` (MANUAL/AGENT/SCHEDULE/OPTIMIZER), exclusion at SUBMISSION time, the adviser's own held intent identified by its id prefix (`night-`/`cal-`) and never a foreign claim | Confirmed by our code | The twin vocabulary (§7): principal `energypod:evening-adviser`, intent prefix `els-`, the same exclusion walk — the C3 tie-flap discipline, inherited whole |
| The load-baseline provider: same-slot-last-week (30-min slots, 12 h horizon, 1 week back), historian-backed, gap-excluded | Confirmed by our code | §8's honest money line: the displaced-import estimate compares the window's actual import against the baseline's — a PROVENANCE-CARRYING ESTIMATE, never a measured saving |
| Tariff block commissioned: general import 30.77 c, off-peak 7.27 c (00:00–06:00), feed-in 2.0 c | Commissioned config | §3.4's arithmetic and §8's money lines, stated not waved at |

## 2. Doctrine decisions (each with its reason)

- **The METER is the target, and the meter NETS.** The only quantity this
  program owes the operator is the netted site exchange: import toward
  zero, never export beyond a bounded spill (§3.4). Per-phase flows are the
  mechanism's physics, not its objective — the program never reads a phase
  role as a control input (the phase map is commissioning EVIDENCE, §10,
  never a config fact the loop depends on), because the netted meter makes
  any pod's discharge equivalent to any other's at the bill.
- **The control basis is DERIVED, both words directly measured.** Total
  work = netted import + fleet discharge (§3.3) — the conservation identity
  on the AC bus, evaluated per tick from the grid and battery words. This
  is the honest total-load measurement the old stack's CT-word sum could
  not be (§3.2), it needs no forecast, and it is SELF-CORRECTING: an
  under-command leaves import for the next tick to see; an over-command
  leaves export; the recomputed split converges on the measurement. The
  `even_rate_w` discipline's spirit, applied to a matching target instead
  of a deadline.
- **Convergence is the WEIGHTING, never a watt.** The share rule
  (SoC^exponent × capacity, §5) makes the fuller pods work harder, so the
  fleet converges as a side-effect of serving — but the program adds ZERO
  watts beyond the measured work (it does pay the weighting's honest
  price: the re-split rides the partial-load curve, a ~3–5% pack-energy
  premium on the re-shared watts, §5.1 — priced, never hidden), and it
  will not start (or stay running) to finish a convergence: if the total
  work is below one pod's floor,
  the evening idles honestly. The night charge re-converges the fleet
  EVERY morning anyway (the capacity-proportional share collapse targets
  one fleet percentage), so convergence here has a daily reset — it is a
  side-benefit to be harvested, never a mission that could justify a
  spilled watt.
- **An ADVISER, not a schedule entry.** The night-charge-class shape, for
  the night-charge-class reasons (the calibration contract's §2 second
  doctrine, all five counts): the target is a MEASUREMENT that moves tick
  by tick (a schedule's watts-x-duration arithmetic cannot follow a
  load); crash-safety by construction (a short-TTL intent dies in ≤10 s
  and the firmware watchdog returns every pod to its own CT-following
  autonomy — which is exactly the status quo ante, a fail-safe this
  program gets for free); renewal is remove-then-submit, one held intent,
  per-unit exclusion at submission time, no stop triples, no idle
  intents, no zero-watt submissions, ever.
- **Fail-closed is WITHDRAW, not hold.** The night charge's evidence
  failure holds at a positive 100 W charge — safe, because a small charge
  cannot over-import. This program's evidence failure must NOT hold at a
  positive discharge: a fleet free-running discharge on stale grid words
  is unbounded export risk. The fail-closed direction is NON-RENEWAL: the
  intents lapse, the watchdog hands every pod back to its own autonomy,
  and the vendor's CT-following serves each phase as it did before this
  program existed — the operator's tolerated incumbent, the honest
  fallback (§3.3).
- **The fleet SERVES, it never blocks.** When the evening load exceeds
  what the eligible fleet can serve (caps, floors, participation), the
  program commands the servable share and the grid covers the remainder —
  reason `capability_limited` on the projection (§6), the money line
  honestly reduced (§8). There is no mode, no posture, and no arithmetic
  anywhere in this contract whose effect is to reduce or delay grid
  import's availability.
- **The window is the EVENING, and it shares the traverse's.** 16:00
  opening (after solar has had the day — the operator's own observation
  is that the unevenness exists BY late afternoon), 22:30 no-new-renewal
  (the same end wall the calibration traverse already validated against
  the 23:00 health watch and the 00:00 night window). Unlike every prior
  cross-program check on this controller, fleet-time OVERLAP with the
  calibration traverse is PERMITTED (§4): both are evening discharge
  programs on disjoint unit sets, and the contention rule is per-unit
  exclusion, not window separation — a conscious, stated distinction, not
  an oversight (§4, §7).
- **Economics stated, both branches carried.** A served evening kWh
  displaces 30.77 c of import against ~8.07 c of off-peak refill
  (7.27 ÷ 0.9 η) — net ≈ +22.7 c per kWh; a spilled kWh earns 2.0 c
  against the same refill — net ≈ −6.1 c. Both numbers ride the morning
  line (§8) so neither meter surprises anyone; the spill tolerance (§3.4)
  is sized so the second branch stays a rounding error.

## 3. The load source — the honest total-load measurement

### 3.1 The requirement

The split's denominator and the loop's target are the same number: the
TOTAL work the fleet must do to hold the netted meter at zero — the
PV-netted house draw. Three candidate sources were weighed; the decision
is (b), with (a) retained as a commissioning instrument (§10) and (c) as
the bookkeeping baseline (§8).

### 3.2 (a) The per-pod LOAD CT sum — REJECTED as the control basis

The old stack's basis: `total_load = Σ load_power_w` across pods. It is
fragile in three named ways, two of them live on this fleet: the garage
pod's CT legitimately reads ~0 (its phase carries almost nothing — a TRUE
zero, not a fault); a dead CT word (the rhs class, ~16 W) biases the sum
LOW silently; and the CT words see per-phase load only — PV is invisible,
so a sunny 17:00 with the array still producing would read as
commandable load the meter says is already covered. The direction of the
CT-sum failure is also the worst kind: it UNDER-reads, the program
under-commands, and the defect presents as the program merely not helping
— discoverable only by cross-check. The words remain exactly what the
PHASE MAP (§10) is derived from; they are just not the control basis.

### 3.3 (b) The DERIVED basis — PROPOSED, and the loop it closes

Both words are already in the standing read plan, both are direct
measurements, and together they are the AC-bus conservation identity.
The v1.0 form of this subsection clipped the netted exchange at zero on
the export side; the panel caught what that clipping does (E1: the clip
removed the loop's only self-correcting term in the export direction, so
the identity re-commanded exactly what was delivered, EVERY export level
became an equilibrium, and a delivery bias one notch above the derate —
1.16 real against 1.15 assumed, the default sitting on the filed range's
dangerous edge — grew the error geometrically to the 6,000 W fleet cap).
The form below is the rewrite: SIGNED exchange, no clipping anywhere in
the loop, and the excluded units' flow subtracted (E2):

```
net_exchange_w   = -Σ_u grid_power_w(u)    # SIGNED, import-positive —
                                           #   the words are export-
                                           #   positive; the sum is the
                                           #   netted meter view, sign
                                           #   intact
served_w         = Σ_u max(0, battery_watts(u))   # EVERY pod, whatever
                                                  #   writer commands it
elsewhere_w      = Σ_{u ∉ participants} max(0, battery_watts(u))
work_w           = served_w + net_exchange_w      # the PV-NETTED draw:
                                                  #   grid-supplied plus
                                                  #   fleet-supplied
desired_output_w = max(0, work_w − elsewhere_w)   # what THIS program's
                                                  #   pods must deliver
commanded_total_w = round(desired_output_w / assumed_discharge_over_frac)
```

**The closed-loop property (E1), pinned as the invariant it is:** the
correction term is SIGNED BOTH WAYS — a measured import shortfall ADDS
to the next command, a measured export overshoot SUBTRACTS from it — so
ZERO NETTED EXCHANGE is the loop's sole equilibrium: any standing error,
either sign, is corrected on the next tick. The clipped v1.0 form made
every EXPORT level an equilibrium instead (with the import term floored
at zero, `work_w` re-derived from the fleet's own delivered watts and
the loop re-commanded its own output forever), and the geometric growth
the panel traced lived exactly there. The rewrite removes the clip, the
derate default moves to the filed range's safe edge (1.16, §3.4), and
the growth mode is structurally unreachable — not merely damped.

**The `elsewhere_w` term (E2), the traverse-night fix:** every
NON-participant's measured discharge — the traversing pod under its
`cal-` intent, a manually-claimed pod under the operator's own command,
a guard-skipped pod's residual autonomy, a claim-settling pod inside the
§7.1 debounce — is SUBTRACTED from what the participants deliver. The
v1.0 identity counted those watts in `work_w` and never subtracted them,
so on a traverse night the participants were commanded to serve the
whole evening ON TOP of the traverse's 800 W: a linear ramp to cap in
~6 ticks and a standing multi-kW export all evening — and the same path
was operator-reachable through the C16 stop route (claim a pod, and
watch the program fight you with the fleet). With the term, the
participants serve the RESIDUAL after every other writer; the term is
IDENTICALLY ZERO when nothing is excluded (the ordinary three-pod
evening), and it is the arithmetic's one answer to every flavor of
"someone else is flowing".

Read the identity the way the loop uses it. If the kitchen pod's autonomy
is already serving everything (net exchange ≈ 0, its discharge 1,500 W),
`work_w` = 1,500 W and `elsewhere_w` = 0 (the kitchen pod is a
participant): the program takes over the SAME total and re-splits
it by weight — the kitchen pod commanded DOWN to its share, the idle pods
commanded UP, the meter held at zero while the work converges. This is
the operator's central case, and it is why the program engages on WORK,
not on import (§6): sharing an already-served load is exactly the
stranded-energy fix. If the kitchen pod is empty (import 1,500 W, fleet
discharge 0), `work_w` = 1,500 W and the loop drives import to zero —
the bill's case. Both cases are the same arithmetic, which is the point.

**Evidence rules (the excess adviser's rollup family, reused):** every
unit's grid word and battery word is classified `good / stale / bad /
missing` under the KERNEL's own commissioned freshness bound
(`policy.max_telemetry_age_s`, 3.0 s — E10: there is deliberately NO
separate `grid_telemetry_max_age_s` key; a third freshness figure could
drift from the kernel's and the drift would be a policy divergence
masquerading as a tuning knob, so the program reads the one bound the
kernel already enforces); the rollup is judgeable only while EVERY word
is GOOD — one unreadable phase is NEVER treated as zero (a pod with a
dead grid word is a hole in the netting, not a zero). Any non-good word
makes the basis UNJUDGEABLE, and the unjudgeable state is a WITHDRAW
(non-renewal, §2's fail-closed direction) with reason
`grid_evidence_<word>` — never a zero-filled basis, never a held
discharge. The kernel's own staleness denial sits beneath as the
backstop, the same ordering-by-construction the calibration contract's
§4.3 pins.

**The plausibility guard (E3) — because age cannot see a fresh-stamped
frozen word.** The freshness bound catches words that stop ARRIVING; it
cannot catch a word that keeps arriving with a frozen VALUE — and a
frozen word is not merely a wrong display here, the IDENTITY CONVERTS it
into a commanded ramp (a battery word frozen at its last delivered value
holds `served_w` inflated while the meter shows the truth; a frozen grid
word hides the loop's own error signal). Two predicates, both
fail-closed to `grid_evidence_implausible` → withdraw, both config
(§11), both with named simulator plants (§13):

- **(P1) the frozen word:** a unit's grid word numerically unchanged
  across `frozen_word_ticks` (default 8, ~12 s at cadence) while that
  unit's battery word moved more than `frozen_flow_delta_w` (default
  200 W) cumulative in the same span — a phase whose battery moved while
  its grid word never twitched is not a quiet phase, it is a stuck word.
  The mirror (frozen battery, moving grid) triggers the same way.
- **(P2) the reconciliation, one-sided by design:** when the fleet's
  battery words moved by ≥ `delivery_move_floor_w` (default 400 W)
  across the settle window and the SIGNED netted exchange moved LESS
  than `exchange_move_floor_w` (default 150 W) in response, the two
  words cannot both be true. The check is deliberately one-sided
  (ABSENCE of response, not excess): a kettle moves the exchange MORE
  than the fleet did, which is the loop's own signal and never
  corruption — only a meter that fails to answer the fleet's own
  delivery is condemned by it.

**The honesty note:** `work_w` is the PV-NETTED draw, not the gross house
load — at 16:30 on a bright evening the array may be supplying half the
house, and the identity correctly counts only what remains to be done at
the meter. The gross figure is unknowable without a site PV word (none
exists; §16 keeps the question). The netted figure is also precisely the
billed quantity, which makes it the honest target for a program whose
value statement is a bill.

### 3.4 The spill bias — the arithmetic, and the decision

The old stack commanded `load × 1.1 + 35 W` — a deliberate 10% + 35 W
spill. Decide honestly with the commissioned tariff, per kWh of ERROR in
either direction (refill at 7.27 c ÷ 0.9 η ≈ 8.07 c per delivered kWh):

| Error | What happens | Net |
|---|---|---|
| Overshoot 1 kWh (spilled) | exported at 2.0 c; tomorrow's refill buys ~1.11 kWh extra off-peak (~8.07 c) | ≈ −6.1 c |
| Undershoot 1 kWh (unserved) | imported at 30.77 c; the unspent kWh avoids ~8.07 c of tomorrow's refill | ≈ −22.7 c |

Overshoot is the CHEAPER error by ~3.7:1 — the old stack's spill bias was
not irrational, and a naive "never export" bias would be the expensive
reading. But the decision is MATCHING, for three reasons. (1) Both figures
above are costs of ERROR, and the loop of §3.3 is an integral controller:
its STEADY error is zero by construction, so there is no steady bias to
optimize — the arithmetic prices transients only, and a transient is
bounded by the correction lag (tick 1.5 s + the observed ~10 s
authorization-to-power settle): a full load-drop mid-evening spills
~2 kW × ~3 s ≈ 1.7 Wh ≈ 0.1 c — noise, stated so nobody hunts it. (2) The
+15–16% discharge delivery OVERSHOOT (the filed bias) means a
command-equals-desire loop would spill STRUCTURALLY ~15% of everything;
the derate `assumed_discharge_over_frac` (default 1.16, §3.3/the safe
edge — E1: the v1.0 default 1.15 sat on the filed range's DANGEROUS
edge, one notch under the observed 1.16, which is precisely the gap the
clipped loop grew geometrically; over-derating by ~1% costs ~1% of
command headroom, under-derating left a per-tick residual spill for the
loop to re-correct forever. Per-pod graduation is E14's documented
doctrine change, §16) removes the structural term, and no deliberate
bias should be layered on a derated loop. (3) The operator has twice
said they dislike energy going in and out; a standing spill is cycling
for 2 c, and the tolerance below is its bounded, honest descendant.

**The bounded spill, decided — with the deadband's behavior defined
OPERATIONALLY (E1/E9):** the loop corrects only when the measured SIGNED
netted exchange sits OUTSIDE `[−spill_tolerance_w, +import_tolerance_w]`
(defaults 150 W / 100 W). OUTSIDE the band, in EITHER direction: the
tick recomputes `commanded_total_w` per §3.3 — import adds, export
subtracts, no asymmetry, no clipping. INSIDE the band: the loop HOLDS
the standing commanded total unchanged (churn control — the split may
still track membership changes, but the total does not move; a band-edge
oscillating exchange therefore cannot oscillate the command).
`within_tolerance` on the projection (§8.3) is THIS state and nothing
else — a display word, never a distinct stop condition; the stop set is
§6.3's, and the band's only act is hold-vs-recompute. `spill_tolerance_w`
150 W is the +35 W precedent's absolute-floor
spirit at this fleet's scale (it also matches the probe `return_band_w`
family), and the REJECTED 10% proportional spill is recorded here with
its reason: a proportional spill scales with load (300 W at a 3 kW
evening), sits outside any honest tolerance, and buys nothing a converged
loop does not already have. The fail-safe direction, pinned: **never
overshoot into export beyond the bounded spill** — the tolerance is a
ceiling on accepted spill, not an allowance the plan aims for.

### 3.5 (c) The load-baseline provider — the baseline, not the basis

The same-slot-last-week baseline (the commissioned `load_baseline:`
provider) is a FORECAST-shaped prior and stays one: it never gates, never
splits, never commands. It renders the projection's context line
("tonight runs ~18% above last week's slot") and it is the comparison for
§8's displaced-import ESTIMATE. A baseline that commanded would inherit
every weather-and-guests error the night-V2 §12-5 flag already documents,
while the live identity of §3.3 sits available one word away.

## 4. The window — and the cross-program arithmetic

`window_local` default `"16:00"` to `window_end_local` default `"22:30"`.
The end is the NO-NEW-RENEWAL boundary (the calibration contract's own
word): no intent is submitted at or after it, the last intent dies by TTL
(≤10 s) long before any sibling window opens, and non-renewal hands every
pod to its autonomy. The 16:00 open is deliberately AFTER the calibration
traverse's 15:00 open (§7.3): the traverse's selection and first claim
are established first, so precedence between the two evening advisers is
settled by clock order at the claim read, never by an intent contest.

**Why 16:00–22:30:** after solar has had the day (the operator's
observed problem exists BY late afternoon — this window starts where the
problem starts); clear of the calibration traverse's 22:30 end wall and
the health watch's 23:00 open; a full hour of clear air before the night
charge's 00:00 window; and 6.5 h against a typical Brisbane evening
draw. The window is the NOT-BEFORE bound — engagement is work-driven
(§6.1), so on a bright evening the program may sit idle until the sun
drops, and the projection says so rather than implying a defect.

**The cross-program validation arithmetic (§7 of this file names each
refusal's rule), mirroring the calibration contract's §7:**

- `window_end_local + intent_ttl_s <= battery_health_watch.window_local`
  (23:00) when that block is present — at the defaults, 22:30 + 10 s =
  22:30:10, half an hour of margin. Refusal names both walls.
- `window_end_local + intent_ttl_s <= night_charging` window open (00:00)
  always — same arithmetic, same refusal shape.
- `window_local < window_end_local`, both inside one civil day (no
  midnight crossing — the evening is a same-day span), timezone equal to
  `site.timezone` (A9's one-civil-time rule).
- **Deliberately NOT checked: fleet-time disjointness from the
  calibration traverse window.** The calibration contract validates
  against the WATCH and the NIGHT windows because a traverse must never
  bleed into either; this contract shares its evening HOURS with the
  traverse by design (both are evening discharge programs, and on a
  traverse night the fleet runs BOTH — the traversing pod under the
  calibration adviser, the rest under this one). The contention rule is
  PER-UNIT (§7.3), and the validation that enforces it is the shared
  claim-exclusion walk both advisers already run — a rule about intents,
  not about walls. The panel should see this stated as a conscious
  distinction (§14 flag 2): it is the first cross-program window overlap
  this controller commissions, and it is safe precisely because both
  programs are same-direction discharge advisers whose unit sets are
  made disjoint at submission time by the C3 discipline.
- No overlap with any `schedule.allowed_windows_local` pair is REQUIRED
  for the adviser (A15: advisers are window-gated by their OWN blocks,
  never by the schedule union), but a live SCHEDULE claim on a unit is a
  skip-if (§7.1) — the operator's published plan beats the opportunist,
  the night-charge doctrine, no opt-out flag.

## 5. Shares, caps, floors — the split

### 5.1 The weight

```
w(u)      = (bms_soc_pct(u) / 100) ^ soc_exponent × assumed_capacity_wh(u)
share(u)  = commanded_total_w × w(u) / Σ w(participants)
```

**`soc_exponent` default 2.0, bounded [1.0, 4.0].** The exponent is the
one dial between the old stack's precedent (3) and flat sharing (1), and
the trade-off is stated rather than defaulted silently. SoC³ on a
100%-vs-60% pair weights them 4.6:1 — fast convergence, but it drives the
leaner pod's share below the 500 W partial-load floor on modest evenings,
where the floor clamp overrides the weighting anyway (the weighting
fighting its own clamp). SoC¹ weights the same pair 1.67:1 and has the
weakest convergence property of all: with shares ∝ SoC, every pod's SoC
decays with the SAME rate constant — ratios are preserved, absolute gaps
shrink only slowly. SoC² is 2.8:1 on the same pair: the fuller pod still
leads decisively, both pods stay inside the efficient band on typical
evenings, and the equalization term (dSoC/dt ∝ SoC²) is real without
being the mission. The decider for 2 over 3: convergence on THIS site has
a nightly reset (the night charge's capacity-proportional share collapse
targets ONE fleet percentage every morning), so evening convergence is a
side-benefit with a fresh start daily — not a scarce resource to seize
each evening at the cost of partial-load efficiency. The precedent is one
config revision away (`soc_exponent: 3.0` restores it exactly), and §15
hands the operator the choice with these figures.

**The capacity term** makes the weighting operate in the PERCENTAGE space
the night charge targets: power ∝ SoC^exp × capacity keeps dSoC/dt ∝
SoC^exp across UNEQUAL packs, so rhs (4,200 Wh) is not over-drawn
percentage-wise against its 5,000 Wh siblings. The map is the SAME
`assumed_capacity_wh` map night-V2 §2.1 rules one-physical-fact over
(this contract joins the calibration contract as its third consumer;
validation refuses drift, §7 of this file).

**The weighting's honest price (E7), stated where it is paid:** the
re-split moves delivery to lower per-pod setpoints, deeper into the
63–86% partial-load collapse of §1.1 — a ~3–5% pack-energy premium on
the re-shared watts. That is the cost of convergence, it is NOT zero,
and v1.0's "no watt is ever spent for it" pin was false as written; the
re-worded pin (§0) claims only what is true — no watt is ever EXPORTED
for convergence. The mitigation is §5.3's participant rule, which is
efficiency-aware BY CONSTRUCTION (the smallest set above the floor
prefers LARGER shares on FEWER pods — §1.1's own case), and the premium
that survives the set rule is the price this contract chooses to pay for
the operator's stated goal. The split itself is over the PARTICIPANT set
only; every non-participant's measured flow is §3.3's `elsewhere_w`
term, never a share (E2).

### 5.2 The per-pod floor and ceiling

- **Power floor `min_share_w` default 500 W** (bounded [200, cap_w]): the
  partial-load efficiency collapse (63–86% below the rated band, §1.1)
  makes small shares buy the same watts with materially more pack
  energy; 500 W sits above the worst of the collapse region, and the
  200 W lower bound clears the documented ~1 A / ~50 W per-module sensing
  threshold three times over (the calibration contract's own floor
  arithmetic — a commanded rate inside that band can METER as zero).
- **Ceiling `cap_w` default 2,500 W** (= `policy.max_unit_discharge_w`,
  the kernel's per-unit static discharge limit; validation refuses
  anything above it). Three pods at cap would be 7,500 W against the
  kernel's 6,000 W `fleet_discharge_limit_w` — the kernel clamps
  proportionally per direction as its standing behavior, and the
  program's own arithmetic additionally bounds `commanded_total_w` at
  `min(work_derived, policy.fleet_discharge_limit_w)` so the clamp is
  never the thing that saves the plan (an evening load above 6 kW is
  capability_limited anyway, §6.4). One honest sentence (E14): the cap
  bounds the COMMAND; the pods' ~15% delivery overshoot means wire watts
  run above it (2,500 W commanded delivers ~2,900 W), inside the
  vendor's own dynamic headroom and judged every tick by the kernel's
  dynamic-limit term — named so nobody is surprised by the CT.
- **Participation floor `participation_floor_pct` default 20%** (bounded
  [policy.minimum_soc_pct, 50]): a pod at or below the floor DROPS OUT
  (the old stack's threshold, kept), its share redistributes at the next
  tick's recompute, and the kernel's 10% `soc_below_discharge_floor`
  remains the hard backstop beneath — the §4.3-ordering mirror: this
  program's floor is validated ≥ the policy floor, so the adviser's stop
  and the kernel's backstop are ordered by construction, and the kernel
  never receives an at-or-below-floor intent from this adviser's own
  arithmetic. A pod that crosses the floor MID-EVENING is dropped the
  same way (its drop renders on the projection and in the §8 close row).
  There is deliberately NO per-evening Wh budget: depth is governed by
  the floor STATE, never by watts-x-duration arithmetic — the calibration
  contract's "depth is not a dial" doctrine, inherited.

### 5.3 The participant-set rule (the floor's consequence, made deterministic)

`min_share_w` creates a combinatorial fact the weighting cannot resolve
alone: a 900 W evening cannot be split three ways above a 500 W floor.
The rule, recomputed deterministically each tick: **participants = the
smallest set of the HIGHEST-WEIGHT pods that can carry `commanded_total_w`
with every share ≥ `min_share_w`**; the total then splits by weight
inside the set. A 900 W evening is one pod (the fullest) at 900 W — which
is also the convergence-correct choice, the fullest pod draining first; a
3 kW evening is all three at ~1 kW each. The rule is EFFICIENCY-AWARE BY
CONSTRUCTION (E7 makes the preference explicit): the smallest set above
the floor is §1.1's own case — LARGER shares on FEWER pods, each further
from the 63–86% partial-load collapse — so the participant selection is
the weighting's efficiency mitigation, not just its floor consequence.
Join/leave hysteresis is a
stated constant, not a key: a participating pod stays in the set while
its share ≥ 0.8 × `min_share_w`, so a load oscillating around a set
boundary does not churn the fleet — and the latch governs MEMBERSHIP
ONLY, never arithmetic (E8): if the clamped floors alone would exceed
`commanded_total_w`, the arithmetic outranks the latch and the set is
re-selected without it. If `commanded_total_w` < `min_share_w`,
the evening (or the moment) has no servable work and the program IDLES
with reason `below_one_pod_floor` — serving 300 W costs a pod-cycle
inside the efficiency collapse for single-digit cents, and the honest
idle is cheaper than the theatre.

**The split's arithmetic order, pinned (E8):** (1) select the participant
set (the rule above, the latch touching membership only); (2) split
`commanded_total_w` by weight over the set; (3) clamp each share to
[`min_share_w`, `cap_w`] — a latched member whose raw share falls below
the floor is clamped UP, and a clamped-up floor can push Σ shares past
the commanded total; (4) RENORMALIZE the unclamped members so
Σ = `commanded_total_w` exactly (the clamped members hold their clamp
values; the residual is re-split by weight over the unclamped, re-capped,
iterated once if a re-cap binds — the night adviser's `_apply_fleet_limit`
precedent for the shape). The invariant, a named test: whatever the
clamping, the submitted shares sum to `commanded_total_w`, never past it
— v1.0 left the renormalization unspecified and a latched floor could
have over-commanded the evening by its own margin.

### 5.4 Ramp discipline

No new ramp machinery exists or is wanted: the kernel's own ramp term
(`ramp_limit_w_per_s` 1,000 W/s × the 1.5 s heartbeat = 1,500 W of
per-tick movement per unit) is the enforced bound, and the evening load's
own dynamics (cooking ramps over seconds-to-minutes) keep commanded steps
well inside it. The observed ~10 s authorization-to-power settle is a
MEASUREMENT LAG the loop absorbs (the next tick reads what actually
landed and corrects) — named here so the settle is not mistaken for
deadband misbehavior. Renewal is the fleet discipline: one intent,
renewed every fleet cycle, TTL 10 s, remove-then-submit.

## 6. The control loop — the adviser shape

### 6.1 Engagement

Inside the window, each tick: read the fleet grid rollup and battery
words (§3.3's evidence rules), read live claims, read per-pod SoC. The
program ENGAGES when `desired_output_w ≥ min_share_w` — i.e., when there
is work worth one pod's floor AFTER every other writer's contribution
(the §3.3 `elsewhere_w` term — on a traverse night the traverse's own
watts are not work for this program), whether that residual work is
currently being imported (the bill's case) or being done alone by one
pod's autonomy (the stranded-energy case, §3.3). It DISENGAGES
(withdraw, non-renewal) when `desired_output_w` < 0.8 × `min_share_w`
(the §5.3 hysteresis), at window end, on any fail-closed evidence word,
or on preemption. Late surplus needs no special case: when PV holds the
netted meter at zero export or beyond, `work_w` collapses toward the
(small) residual and the program idles on its own arithmetic — the
excess adviser owns the export side of the axis (§7.2).

### 6.2 The tick (once per fleet cycle, ~1.5 s)

1. Classify every unit's grid and battery words (§3.3: quality, the
   kernel's freshness bound, the E3 plausibility predicates). Any
   non-good or implausible word → withdraw with `grid_evidence_<word>` /
   `grid_evidence_implausible`.
2. Outside `[window_local, window_end_local)` → withdraw, `outside_window`
   (non-renewal; the standing exit).
3. Any live EMERGENCY_STOP claim → withdraw entirely,
   `yielding_to_higher_priority` (the night adviser's walk, verbatim).
4. Build the candidate set: per unit, the skip-if walk of §7.1 (claims,
   guards, park, lifecycle); then the participation floor (§5.2).
5. The E5 claim-settle debounce: a candidate that carried ANY claim
   within the last `2 × intent_ttl_s` (20 s) is NOT yet a participant —
   it renders `claim_settling` and its flow, if any, rides the
   `elsewhere_w` term (§7.1 names the interleaving this closes).
6. The E4 non-delivery drop: a participant whose measured positive
   battery watts sit below `delivery_pass_fraction` (0.5) × its
   commanded share for `non_delivery_ticks` (default 3) consecutive
   participating ticks is excluded with reason `not_delivering` — the
   kernel-denied class (temperature, cells, faults: refusals outside the
   skip vocabulary) shows up HERE, as measured delivery, and its share
   redistributes instead of standing as phantom import forever.
   Re-entry is the §5.3 join rule, nothing gentler.
7. Compute `net_exchange_w`, `served_w`, `elsewhere_w`, `work_w`,
   `desired_output_w`, `commanded_total_w` per §3.3 (E1's signed form,
   E2's subtraction, the fleet-limit bound of §5.2).
8. `desired_output_w < 0.8 × min_share_w` → idle, `below_one_pod_floor`.
9. The deadband check (§3.4): the signed exchange INSIDE the band and a
   standing command → HOLD the total (membership may still change; the
   total does not move); OUTSIDE → the recomputed total stands.
10. Select participants (§5.3), split by weight (§5.1), clamp and
    renormalize in §5.3's pinned order (E8).
11. Renew: remove the held intent, submit ONE `OPTIMIZER` DISCHARGE intent
    with `watts_by_unit` for the participant set, `intent_ttl_s` 10 s,
    principal `energypod:evening-adviser`, intent id prefix `els-`.
12. The projection's per-tick frame (§8) carries: `work_w`,
    `net_exchange_w`, `elsewhere_w`, the split, `within_tolerance`, and
    the reason codes.

The loop is STATELESS BY CONSTRUCTION for control: no accumulator, no
phase memory, no per-window latch participates in the command arithmetic
— every watt is recomputed from the two measured words each tick. This
is deliberate and load-bearing: a restart mid-evening loses NOTHING
control-side (the next tick re-derives the same plan from the same
words), which is why §8's bookkeeping reconstructs from historian rows
rather than runtime state, and why this contract needs no
opened-row/reconstruction machinery of the calibration C2 class. The one
piece of window-scoped adviser state is the §5.3 join/leave hysteresis
latch, reset at window boundaries, and its loss on restart costs at most
one tick of set churn.

### 6.3 Stop conditions, per pod and per fleet

Per POD (each renders, never silently): participation floor crossed
(`soc_floor`); NON-DELIVERY (`not_delivering`, the E4 drop — the
kernel-denied class surfacing as measured delivery); cap-and-floor
exhausted — a pod pinned at `cap_w` with the
need still above it is `capability_limited`, not stopped; guard refusal
(the §7.1 set, reason verbatim); the E5 settle hold (`claim_settling` —
a hold, not a stop: the unit re-enters when the debounce clears);
preemption by a MANUAL/AGENT claim or
the emergency stop (`yielding_to_higher_priority` — the operator's own
act, honored instantly, the C16 line rendered beside the live share).
Per FLEET: window end (`outside_window`, non-renewal); work below the
one-pod floor (`below_one_pod_floor`); evidence unjudgeable or
implausible (`grid_evidence_*`, `grid_evidence_implausible` — the E3
guard's word); no eligible units (`no_eligible_units`, with the
per-unit reasons); and the deadband's HOLD state (`within_tolerance` —
a projection word for §3.4's hold, NOT a stop: the command stands and
the loop watches; the panel's E9 correction, folded). Every stop is
NON-RENEWAL — no stop triples, no idle intents, ever.

### 6.4 When the load exceeds the fleet

Serve what is servable: every participant at `cap_w`, the residual import
stands, reason `capability_limited` on the projection and the §8 close
row, the money line computed on what was actually displaced. The fleet
never blocks, delays, or prices the grid's remainder (§2). The honest
capacity arithmetic, stated once so nobody is surprised: a heavy evening
(6.5 h × 2.5 kW ≈ 16 kWh) exceeds the fleet's usable ~10.65 kWh
(95→20% across 14.2 kWh nominal), so on the heaviest evenings the pods
progressively hit the participation floor late — and the tail below the
sharing floor is the pods' OWN autonomy plus import, never import alone
(E12): each pod's CT-following keeps serving its own phase down to the
kernel's 10% floor, so the floor ends the SHARING, not the serving. The
morning line says so with the figures.

## 7. Skip-if, interlocks, and the sibling programs

### 7.1 The skip-if vocabulary (the standing guards, all reused)

Parked / latched stop / inhibited (`external_writer` or otherwise) /
vendor mode word ≠ 0 / unreachable / not_responding / telemetry stale /
foreign objective evidence / a live MANUAL, AGENT, or SCHEDULE claim on
the unit / a live NOT-OWN `OPTIMIZER` claim on the unit (this adviser's
own `els-` held intent is never a foreign claim — the night adviser's
`_OWN_INTENT_PREFIX` pattern verbatim, the third twin after `night-` and
`cal-`). Disarmed units: dispatch requires ARM, the program never arms —
a disarmed unit renders `unit_disarmed` and is honestly counted (the
health-watch doctrine). Every skip is a recorded reason on the
projection, never silence, never a failure.

**The claim-settle debounce (E5), the renewal-seam guard:** the claim
exclusion above reads the intent store at ONE instant, and every adviser
renews remove-then-submit — so a sibling's single failed tick leaves its
unit legitimately UNCLAIMED at this program's read for one control
period. Without a guard, this program claims the gap, the sibling's next
submission finds a live not-own OPTIMIZER claim where its own intent
just stood, and (for the calibration traverse) its skip-if ABORTS the
traverse for the night — no retry, the anchor lost to a seam. The guard:
a unit may enter the participant set only after it has been claim-free
for ≥ `2 × intent_ttl_s` (20 s) CONTINUOUSLY; a unit inside the debounce
renders `claim_settling`, stays out of the set, and its flow — if its
autonomy has resumed flowing — rides the §3.3 `elsewhere_w` term, so the
seam costs at most 20 s of deferred sharing and never a sibling's night.
The same guard covers the excess adviser's renewal seam and the release
seam of the operator's own manual claim (the C16 stop route: release a
claim, and this program waits 20 s before re-entering — honest, and
rendered).

### 7.2 The excess adviser: one axis, two halves, no meeting

The excess adviser charges from measured export; this program
discharges into measured import. The two advisers partition the netted
meter's axis and are mutually exclusive BY MEASUREMENT: this program's
arithmetic produces no command while export exists (`work_w` collapses,
§6.1), and the excess adviser's export bound produces no charge while
import exists (nothing to bound from). The structural guard still
stands for the transient overlap and the unit-level corner: a live
not-own OPTIMIZER claim (an `excess-` intent on a needy unit at the
sunset corner) excludes that unit from this program's set for as long as
it stands — FREE surplus outranks paid support, one direction only, this
program waits and never contests (the dawn-corner rule, mirrored to the
dusk corner). A unit the excess adviser is charging is a unit doing
better-than-import work; the split recomputes without it and the evening
loses nothing the surplus did not replace.

### 7.3 The calibration traverse night: EXEMPT the traversing pod, RUN REDUCED

On a traverse evening the calibration adviser holds a `cal-` intent on
the target pod from 15:00 — the §7.1 claim exclusion (with the E5
debounce covering the renewal seam) excludes that pod
from this program's set ORGANICALLY (no cross-contract row, no config
coupling; the two advisers discover each other exactly the way the night
and excess advisers do, at the claim read), and by mid-evening the
traversing pod's SoC path takes it below the participation floor besides.
The E2 arithmetic makes the exemption REAL rather than nominal: the
traverse's delivered watts (its commanded 800 W at the 0.8 delivery
figure) ride the `elsewhere_w` term, so the participants serve the
evening MINUS the traverse — v1.0 would have commanded them to serve the
evening ON TOP of it (§3.3's E2 paragraph carries the ramp-to-cap
failure in full). The program then runs REDUCED on the remaining pods
rather than deferring the evening, and the decision is justified by
arithmetic, not taste: a
traverse night is ~one night in twenty at the commissioned cadence (N=60
per pod across three pods), deferral would idle the operator's evening
support exactly as often, and the reduced fleet still covers a typical
evening's entire draw — with the bound stated honestly (E12): the POWER
ceiling is 2 × 2,500 W = 5,000 W, but if the traversing pod is the
KITCHEN pod it is ENERGY that bounds the evening — the remaining pair's
usable ~6.9 kWh (lhs+rhs, 0.75 × 9,200 Wh; ~7.5 kWh if rhs is the
traverser) against the ~10 kWh of a full three-pod evening — so a heavy
traverse evening lands `capability_limited` in its tail, the tail being
the pods' own autonomy plus import (§6.4), and the morning line says so.
The traversing pod's absence also barely dents convergence — the pod
finishes the night at the 10% floor and the SAME night's refill closes
the gap the traverse opened. The projection renders the exemption with
its cause (`yielding_to_higher_priority`, the cal- intent's presence
visible in the unit's reason) so a traverse evening's two-pod split is
legible as such, not as a defect.

### 7.4 The health watch and the night charge: clear air by arithmetic

This program's last intent dies by 22:30:10; the health watch opens at
23:00 into the quiet hour it requires (no live intents, no adviser
participation — its window-quiet check passes by construction, and its
probe's fleet-import gate reads a fleet this program has long since
handed back); the night window opens at 00:00 onto pods wherever the
evening and their own floors left them, and refills them as ordinary
below-target units — the two programs compose without reading each
other, exactly as the calibration contract's §5.1 traverse/refill pair
does. Neither sibling needs anything from this contract and no exclusion
is wanted: the refill IS the close of every evening this program opens.

### 7.5 Forecast deferral: NEVER for support; v1 reads no forecast at all

A sunny-tomorrow forecast makes convergence cheaper (the night target
runs low, the morning sun finishes) and a rainy one makes it moot
(everyone refills to 95 regardless) — but tonight's SUPPORT is
tariff-certain either way (30.77 c against 8.07 c), and deferring it on
a forecast would trade a certain saving for a speculative one. Decision:
v1 consumes NO forecast, defers NOTHING on weather, and the
convergence-tilt question (weight the exponent or the floor by
tomorrow's sky) is documented as a future extension (§16) with its
evidence bar — a season of §8 rows first.

## 8. Bookkeeping, audit, events, projection

### 8.1 The measurement record (historian-derived, restart-proof)

Because the loop is stateless (§6.2), the bookkeeping is computed at
window close from HISTORIAN rows, not accumulated in runtime state — a
restart mid-evening loses nothing material, and there is deliberately no
C2-class opened-row machinery to reconstruct (nothing control-shaped is
at stake: an interrupted evening is simply a shorter evening, and the
next tick's plan was always going to be re-derived from the words).

- **Per-pod contribution Wh**: the integral of `battery_watts` over the
  window's ticks where a live `els-` intent named the unit (ATTRIBUTED:
  the kitchen pod's autonomy service before/between claims is NOT
  counted as this program's — the gap-excluded integral discipline, the
  scorecard's own).
- **Import Wh during the window** (measured) and **spill Wh** (the
  integral of export while engaged — the §3.4 tolerance's observable).
- **Displaced-import ESTIMATE**: the baseline provider's same-evening
  import integral minus the measured import, × 30.77 c — carried with
  its provenance ("same-slot-last-week") and never rendered as a
  measured saving; the measured MONEY line is `import_wh × 30.77 c paid`
  beside `spill_wh × 2.0 c earned`.
- **SoC convergence delta**: (max − min BMS SoC) at window open vs close,
  and vs the night window's open — the shelved balancer's trigger
  evidence (§0), recorded nightly.
- **Morning facts**: one entry on the shipped morning-facts surface
  (the History console's morning states) — window totals, the money
  line, the convergence delta, and the honest one-sentence close
  (served X kWh across N pods; import Y kWh at $Z; convergence Δ; the
  `capability_limited` tail if any).

### 8.2 Audit rows (advisory class, the accountant pattern)

- `evening_window_opened` — at first engagement each window: the measured
  `work_w`/`net_import_w`, the participant set with weights, the derate,
  the window figures. The reconstruction row.
- `evening_share_revised` — on each participant-set change (join, leave,
  floor drop, guard skip): the unit, the cause, the before/after split.
  Durable, so the evening's set history survives restart.
- `evening_window_closed` — at window end (or the withdraw that ends
  engagement): the full §8.1 record. The morning line replays from it.
- `evening_phase_map_recorded` — the §10 commissioning evidence row.

**Bus events:** `evening.state_changed` (the night adviser's publication
discipline: semantic-tuple-triggered, 30 s heartbeat while engaged,
nothing published while uncommissioned or idle-by-window). Typed payloads
mirroring the rows; no control path subscribes.

### 8.3 The projection — the `evening_load_share_state` key

Absent when uncommissioned. The example carries a mid-evening frame:

```json
"evening_load_share_state": {
  "mode": "act",
  "window": {"opens_local": "16:00", "ends_local": "22:30"},
  "phase": "sharing",
  "as_of": "2026-08-26T18:41:05+10:00",
  "work_w": 1620,
  "net_exchange_w": 90,
  "elsewhere_w": 0,
  "within_tolerance": true,
  "commanded_total_w": 1408,
  "derate": 1.16,
  "units": [
    {"unit_id": "lhs", "soc_pct": 88.2, "weight": 3878, "share_w": 817,
     "phase": "sharing", "reason": "on_plan"},
    {"unit_id": "mid", "soc_pct": 74.9, "weight": 2807, "share_w": 591,
     "phase": "sharing", "reason": "on_plan"},
    {"unit_id": "rhs", "soc_pct": 61.3, "weight": 1577, "share_w": 0,
     "phase": "sitting_out", "reason": "share_below_floor",
     "note": "3-way split would breach min_share_w; the set rule keeps two"}
  ],
  "baseline_context": "tonight ~12% above last week's slot",
  "last_close": {"served_wh": {"lhs": 2140, "mid": 1680, "rhs": 1210},
                 "import_wh": 610, "spill_wh": 40,
                 "convergence_delta_pct": {"open": 27.1, "close": 12.4}}
}
```

`within_tolerance` is §3.4's deadband state and NOTHING else (E9): true
when the signed exchange sits inside `[−spill_tolerance_w,
+import_tolerance_w]` — the 90 W import of the example is inside
[−150, +100], and v1.0's example rendered it false, the self-contradiction
the panel struck. It is a display word; the band's only act is
hold-vs-recompute (§3.4), and no stop condition keys on it.

Skips render their reason verbatim; `mode: advise` renders the whole
projection with `"submits": "never"` beside it (the invisible-state
rule); `capability_limited` renders as its own fleet phase with the
residual import named.

## 9. Alerting and the console

| Tier | Trigger | Surface |
|---|---|---|
| notice | first engagement of the evening (the card's live line appears) | the card's state chip |
| notice | `capability_limited` (the fleet is at caps and import stands) | the card's honest line with the residual figure |
| notice | a pod dropped at the participation floor mid-evening | the unit row's floor chip |
| alert | `grid_evidence_*` persisting ≥ 10 min inside the window (the program is withdrawn and the evening is unserved) | alert-tier event; names the unreadable phase and the standing autonomy fallback |
| resolved (info) | the window close row | the morning-facts entry (§8.1) |

**Console — recommendation: its own compact card, BESIDE the night,
health-watch, and calibration cards.** The A14 sibling logic applied to
surfaces: this program's vocabulary (work, split, weights, import) matches
no existing card's (verdicts, targets, classes), and grafting a line onto
the Night tile would extend that contract in spirit while its code stays
separate — the same reason the calibration card stands alone. The card is
small and mostly quiet: a live line while engaged (work, import, the
per-pod shares with SoC), the idle reason while not, and the
morning-after close line the day following — which also lands on the
shipped morning-facts surface as one entry. Wherever the live share
renders, the card NAMES the standing stop route (the C16 pattern): "to
stop tonight's sharing, claim any pod — a manual command preempts
instantly." The pinned §0 sentence rides the card's engaged state. One
rhythm / one vocabulary / live-refresh per the standing bar; shot matrix
(states × viewports) per the polish bar.

## 10. Phase-map verification (the commissioning step — the netting half
is COMMISSIONING-BLOCKING, E6)

The program's mechanism rests on two physical facts this contract refuses
to assume: WHICH pod serves which phase, and THAT the meter truly nets.
Both are verified once, before the first act-mode evening, from evidence
this site already holds, and both are RECORDED — but they carry
DIFFERENT authority (the panel's E6 ruling): the MAP is evidence about
the mechanism and stays NON-BLOCKING (no control path reads it, §2); the
NETTING cross-check is the mechanism's load-bearing physical fact, and
on a NON-NETTING meter the entire economics of this program INVERTS (a
shared watt on a per-phase-billed meter is energy SOLD at 2.0 c against
a kitchen-phase import still bought at 30.77 c — the program would
automate a ~28.77 c-per-shared-kWh loss), so the recorded netting
evidence is a HARD GATE on `mode: act` (§11): the config names the
evidence path, validation checks the shape, and boot degrades a missing
file's block to advise LOUDLY (the health-watch A6 receipts shape;
validation stays offline-pure, the night-V2 ruling).

- **The map**, from the historian's evening slots: per pod, the mean and
  the evening peak of `load_power_w` over a trailing window of evenings,
  ranked — the kitchen pod is the highest-CT pod; an idle-phase pod reads
  ~0 LEGITIMATELY (its phase carries nothing), and the map's row says so
  per pod with the figures. The dead-CT class (the rhs ~16 W spectator)
  is distinguished the health watch's own way — sibling comparison plus
  the pod's own battery-watts behavior — and the map records the
  distinction rather than collapsing both to "low CT".
- **The netting**, from the site's own interval data (the 2026-08-25
  finding of record: netting is the norm but configurable variance
  exists — verify before any commissioning): a same-evening comparison of
  the summed per-pod grid words against the meter's recorded import/
  export interval data, over enough evenings to see both signs. The
  evidence file lands in `docs/evidence/` (the standby-cycle precedent)
  and the `evening_phase_map_recorded` audit row references it; the
  operator's bill/interval-data export is the checkable evidence the
  §11 gate names (§15 item 5).
- **A dead garage-phase GRID word, the consequence stated aloud (E13):**
  the control basis needs EVERY unit's grid word GOOD (§3.3 — one
  unreadable phase is a hole in the netting, never a zero), so a pod
  whose grid word dies disables the program EVERY EVENING, fail-closed,
  until the word is fixed. That is the accepted consequence, not an
  oversight: the §9 alert fires nightly naming the phase, the pods' own
  autonomy serves on (the incumbent state), and the commissioning run
  verifies all three grid words' health BEFORE the first act evening so
  the failure class is discovered at commissioning, not on a Tuesday.
- **The census amendment this feeds** (running in parallel): a pod on an
  IDLE phase is NOT a spectator. The health watch's S4 no-CT-view
  predicate currently infers "no CT view" from a low load word against
  loaded siblings; the phase map gives it the honest third case — a pod
  whose CT is low because its PHASE is idle is healthy, and its census
  context should say `idle_phase` rather than implying a sensing fault.
  This contract records the map; the watch's own amendment consumes it
  (the sweep, §17, names the doc). No control path in THIS program reads
  the map — it is evidence about the mechanism, not an input to it (§2).

## 11. Config — the `evening_load_sharing:` block

Absent block = nothing composes (no adviser, no snapshot key, no events,
no REST surface — byte-identical behavior). Present block composes the
`EveningLoadShareAdviser` (one tick per fleet cycle inside the existing
supervision pass, no new task class) under principal
`energypod:evening-adviser`.

```yaml
evening_load_sharing:
  timezone: "Australia/Brisbane"    # REQUIRED; must equal site.timezone
  mode: "advise"                    # advise | act — advise computes and
                                    #   displays the split and submits
                                    #   NOTHING, ever; act is the
                                    #   operator's later revision (the
                                    #   calibration C15 distinction: no
                                    #   receipt gate — ordinary OPTIMIZER
                                    #   dispatch, the night-charge class)
  window_local: "16:00"             # the not-before bound (after the
                                    #   traverse's 15:00 open, by design)
  window_end_local: "22:30"         # no-new-renewal; + ttl must land
                                    #   before the health-watch window and
                                    #   the night window (validated)
  min_share_w: 500                  # [200, cap_w]; the partial-load
                                    #   efficiency floor (63-86% collapse
                                    #   below the rated band)
  cap_w: 2500                       # <= policy.max_unit_discharge_w
  participation_floor_pct: 20.0     # [policy.minimum_soc_pct, 50]; drop-
                                    #   out + redistribute (the precedent)
  soc_exponent: 2.0                 # [1.0, 4.0]; 3.0 restores the old
                                    #   stack's precedent exactly
  spill_tolerance_w: 150            # > 0; the bounded accepted spill (the
                                    #   +35 W precedent's descendant)
  import_tolerance_w: 100           # > 0; the correction deadband's
                                    #   import-side edge
  assumed_discharge_over_frac: 1.15 # [1.0, 1.5]; the command derate vs
                                    #   the filed +15-16% overshoot;
                                    #   delivery_bias.py graduates it
  grid_telemetry_max_age_s: 3.0     # > 0; the rollup's freshness bound
  intent_ttl_s: 10.0                # (0, 300]; > timing.control_period_s
  assumed_capacity_wh: {lhs: 5000, mid: 5000, rhs: 4200}
                                    # MUST equal night_charging's map
```

**Cross-validations (each refusal names its rule):**

- `mode ∈ {advise, act}`; `advise` submits no intent on any tick under
  any input (a named test). No runtime toggle for `mode` exists — the
  authority ladder is climbed by config revision plus restart.
- `window_local < window_end_local`, both same civil day; timezone equal
  to `site.timezone` (refusal names both values).
- `window_end_local + intent_ttl_s <= battery_health_watch.window_local`
  (when present) AND `<= night_charging`'s window open (always) — each
  refusal names its arithmetic. Deliberately NOT validated: disjointness
  from `battery_calibration`'s window (§4's conscious distinction).
- `min_share_w ∈ [200, cap_w]` (the 200 floor names the sensing
  threshold); `cap_w <= policy.max_unit_discharge_w`;
  `participation_floor_pct ∈ [policy.minimum_soc_pct, 50]` (the
  §5.2-ordering mirror — the adviser's stop above the kernel's);
  `soc_exponent ∈ [1.0, 4.0]`; `spill_tolerance_w > 0`;
  `import_tolerance_w > 0`; `assumed_discharge_over_frac ∈ [1.0, 1.5]`
  (a derate below 1.0 would assume UNDER-delivery — the charge-direction
  bias class, refused here); `0 < intent_ttl_s <= 300` and `>
  timing.control_period_s`; `grid_telemetry_max_age_s > 0`.
- `assumed_capacity_wh` present, key set exactly the fleet, every value
  > 0, and EQUAL to `night_charging.assumed_capacity_wh` (and therefore
  the calibration map's — one physical fact, three keys, all enforced).
- The `plant_history:` block must be present (the §8 bookkeeping and the
  §10 map are historian-backed; no degrade path).
- Runtime state does not persist (the standing doctrine): boot recomposes
  from the file; the control loop is stateless by construction (§6.2) and
  the bookkeeping is historian-derived — there is deliberately nothing
  window-shaped to reconstruct.

## 12. Commissioning sequence (the night-charge-class ladder)

1. **THE PHASE MAP AND THE NETTING** (§10, before any act-mode evening):
   the historian derivation, the interval-data cross-check, the evidence
   file, the audit row. The program's mechanism is this evidence.
2. **ADVISE** (config revision one): the block present at `mode: advise`
   with the §11 defaults. Runs the full loop nightly — measures the work,
   computes the split, renders the card, writes the opened/revised/close
   rows with a `submits: never` marker, submits nothing. Minimum run:
   enough evenings to see the honest edges — a traverse evening's reduced
   split, a `capability_limited` peak, a `grid_evidence_stale` withdraw,
   and at least one evening where the split materially re-weights an
   already-served load (the operator's central case, visible in the
   advise rows as commanded shares against the kitchen pod's autonomous
   service).
3. **ACT** (config revision two): `mode: act`. The units must be ARMED
   for dispatch (the program never arms — the standing doctrine); the
   first evenings are supervised reads of the card, not attended events —
   the act is ordinary dispatch under every standing guard, the
   night-charge and calibration precedent, and the card names the
   manual-claim stop route throughout. No receipt gate (C15's conscious
   distinction, inherited: nothing about a discharge share is a novel
   write class).
4. **STEADY STATE**: the morning line is the operator's daily read; the
   convergence deltas accumulate as the shelved balancer's trigger
   evidence (§0); the §15 thresholds are re-examined after the first
   season with the close rows as the evidence base.

## 13. Test matrix (author FIRST, per repo doctrine)

- **T-ELS-CONFIG** — absent block composes nothing (byte-identical
  snapshot); every §11 bound and gate with its named-message refusal; the
  window-end arithmetic against BOTH the watch and night walls (refusals
  naming their numbers); the floor-ordering rule (a participation floor
  below the policy floor refused naming both); the capacity-map equality
  across all three consumers; the historian prerequisite; advise submits
  nothing on any tick; no runtime toggle exists.
- **T-ELS-LOAD-SOURCE** — the identity: `work = import + fleet
  discharge` across the three canonical cases (kitchen-served,
  kitchen-empty, mixed); the rollup's fail-closed words (a single
  non-good grid or battery word → unjudgeable → withdraw with
  `grid_evidence_<word>`, never a zero-filled basis, never a held
  discharge); the PV-netting case (surplus collapses `work_w` and the
  program idles on its own arithmetic); the CT-sum basis is consulted
  NOWHERE on the control path (the named regression vector — a dead CT
  word must not move the split).
- **T-ELS-SHARE-MATH** — the weight (exponent 2 default; the 100-vs-60
  pair at 2.8:1; exponent 3 restoring the precedent at 4.6:1; capacity
  weighting keeping rhs's percentage draw proportional); the participant
  rule (smallest highest-weight set above `min_share_w`; the 900 W
  one-pod case; the 3 kW three-pod case; the join/leave hysteresis at
  0.8 × floor); the floor clamp with its rendered reason; the
  redistribution on a mid-evening floor drop; the fleet-limit bound on
  `commanded_total_w`.
- **T-ELS-LOOP** — the derate (command = desired ÷ 1.15; the structural
  spill without it, the converged spill with it, both asserted); the
  tolerance band (no correction inside `[−spill, +import]`, correction
  outside); the transient bound (a scripted full load-drop spills ≤ the
  tolerance × the settle window); engagement on WORK not import (the
  already-served load re-split with the meter held); withdrawal paths
  (window end, evidence, preemption, below-floor); the stateless
  restart (a mid-evening restart changes nothing control-side — the next
  tick's plan is identical, a named property test); `capability_limited`
  (caps pinned, residual import standing, never a block).
- **T-ELS-SKIP-IF** — the §7.1 set each rendering its skip verbatim; the
  excess-adviser corner (a live `excess-` claim excludes the unit, the
  split recomputes, this program never contests); the traverse evening
  (a live `cal-` claim excludes the traversing pod organically, the
  two-pod split renders with its cause, no deferral exists under any
  input); the own-intent prefix (`els-` never a foreign claim to itself);
  MANUAL/AGENT/SCHEDULE claims and the e-stop preempting instantly.
- **T-ELS-BOOKKEEPING** — the close row's figures (per-pod attributed Wh
  with the autonomy/claim attribution rule; import and spill integrals
  with gap exclusion; the convergence delta triple; the money line's
  provenance-carrying estimate vs the measured paid/earned lines); the
  restart leg (historian-derived reconstruction, nothing runtime-shaped);
  the morning-facts entry's shape.
- **T-ELS-PROJECTION/EVENTS** — additive keys only; absent-block frames
  byte-identical; advise renders `submits: never`; skips verbatim;
  semantic-tuple publication with the 30 s heartbeat while engaged and
  nothing while idle-by-window; no control path subscribes.
- **T-ELS-ARCHITECTURE** — the adviser composes the facade intent twin
  and no transport of its own; the historian via the injected port (no
  application import of adapters); the grid rollup reuses the excess
  family's classification (no second implementation); no new write
  method anywhere on the path (mutation-test the composition); the
  night-writer detector stays quiet through an engaged evening (the
  claimed-unit gate).
- **T-ELS-SIMULATOR** — the scripted legs: a three-phase site with the
  kitchen/garage/upstairs load split (the phase map's plant); a
  dead-CT pod (the rhs class — must not move the split); a
  delivery-overshoot plant (1.0x, 1.15x, 1.3x — the derate's
  regression); a traverse-evening twin (a standing `cal-` claim); a
  surplus-evening script (the excess corner); a restart-in-mid-evening
  harness (the stateless property); a `capability_limited` peak script.
- **T-ELS-CONSOLE** — the card's states × modes (uncommissioned,
  advise, idle-with-reason, sharing, capability_limited, floor drops,
  the morning-after line); the stop-route line wherever the live share
  renders; the pinned sentence; alert tiers; shot matrix.

## 14. For-the-panel flags

1. **The first commissioned window OVERLAP.** This program shares evening
   hours with the calibration traverse by design (§4), with contention
   settled per-unit at the claim read rather than by wall separation. The
   C3 discipline makes the overlap safe (two same-direction discharge
   advisers, disjoint unit sets, exclusion at submission), but every
   prior cross-program check on this controller is a WALL — the panel
   should affirm the per-unit rule is sufficient, or name the interleaving
   it does not cover.
2. **The kitchen pod's autonomy is preempted while shared.** Commanding
   the working pod DOWN from its CT-following service to its weighted
   share is the mechanism that makes "all phases contribute" real (§3.3),
   but it substitutes our open-loop share for the vendor's closed-loop
   CT-following for the intent's TTL at a time — renewed per tick, and
   meter-equivalent by netting, yet it IS a preemption of a working
   autonomous behavior this controller has never displaced before. The
   loop measures the outcome; the panel should confirm 1.5 s renewal is
   the right bound on that substitution.
3. **Engagement on WORK, not import.** The program will command a
   redistribution of an already-zero-import load (the stranded-energy
   case). No watt is ADDED (the §0 pin), but the fleet's commanded output
   replaces autonomous output pod-by-pod — the observable on the wire is
   the same total with a different split. The panel should confirm this
   reads as load support (the operator's ask) and not as the balancer's
   wedge (§0's refusal) — the distinction is that no energy is created,
   moved to storage, or exported beyond tolerance, only the SOURCE of the
   serving watts is re-weighted.
4. **The stalesess fail-closed is withdraw-to-autonomy, not hold.** Every
   other evidence-failure on this controller holds at a bounded positive
   rate (the night charge's `hold_rate_w`). A discharge program cannot:
   the held watts would free-run into export. The panel should confirm
   withdraw (the status quo ante) is the intended fail-safe rather than a
   bounded minimum-share hold.
5. **The exponent default deviates from the precedent.** SoC² vs the old
   stack's SoC³ (§5.1) — the panel should confirm the reasoning (the
   nightly convergence reset, the floor clamp's override) or restore 3.

## 15. Operator decisions this package needs

1. **The exponent:** "Shares are weighted by SoC² today. The old system
   used SoC³ — faster convergence, but it pushes the leaner pod below the
   500 W efficiency floor on modest evenings. Confirm 2, or restore 3."
2. **The spill tolerance:** "The program matches the load rather than
   deliberately over-serving it, accepting up to 150 W of transient
   export (the old system's +35 W margin, scaled). Per kWh, overshoot
   costs ~6 c and undershoot ~23 c — matching with a derate makes both
   rare. Confirm 150 W, or name another."
3. **The window:** "16:00–22:30. The start is after solar has had the
   day; the end clears the 23:00 health watch and the 00:00 night charge.
   Confirm, or name another pair."
4. **The participation floor:** "Pods drop out at 20% SoC (the old
   system's threshold) and the share redistributes. The night charge
   refills regardless. Confirm 20, or name another."
5. **The netting evidence:** "Before the first live evening, the meter's
   interval data is cross-checked against the summed per-pod grid words
   (§10) — the mechanism is this one fact. The bill data export is the
   operator's act."

## 16. Future extensions and open questions (documented, deliberately unbuilt)

- **Daytime support on heavy cloudy days** (the operator's own "later"):
  a low-PV afternoon with import running from midday is mechanically the
  same program in a wider window — but it enters the excess adviser's
  hours properly, interacts with the midday taper observation, and spends
  pack depth the evening then wants. Documented, deliberately unbuilt;
  the §8 rows are its evidence base.
- **The forecast convergence tilt** (§7.5): weight the exponent or the
  floor by tomorrow's sky after a season of rows.
- **A site PV word** would split the bookkeeping's PV-served from
  battery-served energy and make the displaced-import estimate measured
  rather than baseline-derived; none exists today.
- **Excluding autonomously-discharging pods from the participant set**
  (the §14-2 question's config form): the loop makes it unnecessary, but
  a season of close rows would show whether preempting the working pod's
  CT-following ever costs more than the re-weighting gains.
- **The shelved transfer balancer** stays shelved; THIS program's
  convergence deltas are its rebuild trigger's evidence (§0), and the
   trigger's own words are unchanged.

## 17. Same-round amendment sweep (at implementation time, not by this file)

1. `docs/API_CONTRACTS.md`: the write-enabled section names the evening
   adviser's discharge intents under `energypod:evening-adviser`; the
   snapshot key and events from §8.
2. `docs/DESIGN_BATTERY_HEALTH_WATCH.md`: the S4 predicate's idle-phase
   amendment note (§10 here) — the census's no-CT-view class gains the
   phase-map context; the map row is the watch's to consume.
3. `docs/DESIGN_CALIBRATION_CYCLING.md`: a one-line note in its §7
   validation that the evening load-sharing window deliberately shares
   the traverse's hours under per-unit exclusion (so a future reader of
   THAT contract's disjointness checks knows the overlap is commissioned
   elsewhere, not missed).
4. `docs/DESIGN_NIGHT_CHARGE_V2.md` §2.6's capacity-assumption note gains
   this contract as a third consumer.
5. `docs/CONTINUITY.md` non-negotiable invariants amended by the
   coordinator thread at round close (this design does not edit it).
6. The `config.live-write-example.yaml` block comment for
   `evening_load_sharing:` when the implementation wave lands (the §12
   sequencing, the advise default, the §3.4 spill arithmetic).
