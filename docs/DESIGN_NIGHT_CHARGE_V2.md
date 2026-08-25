# Night charge V2 — the forecast-aware top-up target

Design date 2026-08-24, panel-folded the same day. Status: CONTRACT v2 —
the adversarial panel's verdict was SHIP WITH AMENDMENTS, and all twelve
amendments (A1–A12) are folded into the sections where they landed, not
appended as a list; what remains open is the §13 operator decisions.
Parent: `docs/DESIGN_NIGHT_CHARGE.md`
(v1 — every v1 safety doctrine is INHERITED UNCHANGED by this document; V2
changes WHAT the overnight charge aims at, never WHEN, never HOW FAST, never
UNDER WHOSE AUTHORITY). Pattern parents: `docs/DESIGN_POD_PARKING.md`
(doctrine style), the provider layer (ARCHITECTURE section 17;
`config/config.live-write-example.yaml` `forecast_providers:`), the
forecast-vs-recorded-surplus cross-check scorer
(`src/energypod/adapters/providers/cross_check.py`), and
`docs/PRODUCT_ROADMAP.md` Phase 4/5 (progressive earning: recommendation →
shadow → supervised → automation).

This document is DESIGN ONLY. It touches no `src/`, `tests/`, `config/`, or
`web/` file, authorizes no live-hardware interaction, and grants no new
authority of any kind: V2 submits the same short-TTL `OPTIMIZER` CHARGE
intents under the same `energypod:night-adviser` principal through the same
internal facade as v1, judged by the same arbiter, allocator, SafetyKernel,
and actor. The one thing that changes is the number the strategy aims at.

## 0. What this is, in one paragraph

The operator's words, and they are the law of this feature: "the whole point
of the night charge is to charge the batteries to the point where solar
generation will bring them to 100% by midday." The overnight charge is a
computed TOP-UP TARGET, not a blind fill: on a sunny forecast the batteries
need less from the grid because the morning sun will finish the job; on a
rainy one they need everything the window can give. V1 charges every
commissioned unit to the 95% ceiling every night regardless of tomorrow's
sky — correct, safe, and deliberately wasteful on the site's good mornings
(a 6.5 kW DC array against a ~14.2 kWh fleet fills most of the pack before
lunch for much of the year). V2 replaces the fixed ceiling with a per-battery
target derived from the Solcast rooftop forecast and the historian's load
baseline, bounded below by a reserve floor and above by the unchanged charge
ceiling, computed at window open, revised mid-window only on a materially
changed forecast, and — until the forecast has EARNED trust on this site's
own scoreboard — computed and DISPLAYED but not obeyed: below trust, and on
every forecast failure, the strategy charges to the v1 target at full voice,
loudly. The feature buys less off-peak grid energy on confident mornings; it
never buys safety, never writes outside the night window, and never touches
the daylight hours — finishing to 100% by midday remains the pods' own
autonomy plus the excess-solar adviser's job, not this adviser's.

## 1. The law, translated

| The operator's words | The mechanism |
|---|---|
| "charge the batteries to the point where solar generation will bring them to 100% by midday" | The per-battery overnight target §2: leave exactly the headroom the forecast morning SURPLUS can fill, no more. |
| "sunny forecast → shallow or skipped overnight charge" | A high `E_credit` drives the target toward `floor_pct`; a unit already at/above its target at window open sits the whole window out (`complete`, reason `target_reached`). |
| "rainy → full charge" | A near-zero `E_credit` drives the target to the charge ceiling — v1 behavior, by computation rather than by default. |
| "by midday" | `midday_local` (default `12:00` site civil time) — the operator's declared finish line, §2.3. |
| "computed, bounded and honest" | Bounded by `floor_pct` below and the v1 charge ceiling above; honest in one sentence on the tile, §8, and one audit row carrying the full arithmetic, §7. |

## 2. The target computation

### 2.1 The formula, pinned

At window open (and per §5.2 on a qualifying revision), for each eligible
unit `b`:

```
E_surplus_kwh = Σ_slots max(0, pv_power_w(q) − load_baseline_w) × slot_h
E_deficit_kwh = Σ_slots max(0, load_baseline_w − pv_power_w(q)) × slot_h
                 over slots ∈ [window_end, midday_local)          (site civil)

E_credit_kwh  = charge_efficiency × E_surplus_kwh − E_deficit_kwh

target_soc_pct(b) = clamp(100 − 100 · E_credit_kwh · share(b) / assumed_capacity_wh(b),
                          floor_pct, min(100, policy.max_soc_pct))
```

Three deliberate readings, each with its reason:

- **E is NET, not gross PV.** The batteries charge only from SURPLUS — house
  load consumes PV first on the shared AC bus, and the pods' own autonomy
  DISCHARGES through every pre-surplus slot after window end (a winter
  06:00–06:40 breakfast is served by grid and battery before sunrise). A
  gross-PV reading would systematically over-credit the morning and
  under-charge the pack — the exact failure direction the operator's
  objective refuses. The deficit term is charged at full value (no
  efficiency derate) and assumed ENTIRELY battery-served: conservative, and
  stated as such, because the grid/battery split of a morning deficit is
  not observable from the data this site holds.
- **The upper bound is the ceiling, not 100.** Our own intents die at
  `policy.max_soc_pct` (95) — the kernel's `soc_above_charge_ceiling` is the
  honest refusal and the backstop. The span 95–100% belongs to the pods'
  own autonomy (rhs sat at 98% on the v1 commissioning night, on its own);
  a computed target above 95 charges to 95 and the morning finishes the
  rest, exactly as v1's full-charge nights already do.
- **One capacity truth.** `assumed_capacity_wh` is the SAME map v1's `even`
  pacing uses (the Wh mapped to the 0–100% SOC span). V2 widens its
  requirement: the map must be present with exactly the fleet units IFF
  `pacing: even` OR `target_policy ≠ full` — two keys for one physical
  fact would drift, and a wrong estimate's failure mode is visible in both
  uses (early/late completion, never a hazard).

### 2.2 `share(b)` — finish-aligned, capacity-proportional

`share(b) = assumed_capacity_wh(b) / Σ_fleet assumed_capacity_wh`.

Reason: the operator's law is about the FLEET finishing — "bring them to
100%". Capacity-proportional share makes every battery's headroom the SAME
PERCENTAGE (the algebra collapses: `target_soc_pct` is one number for the
whole fleet), so all three finish together under an evenly-dividing
surplus. An equal-thirds share would hand the smaller pack (rhs, 50 cells
against lhs/mid's 60) a deeper percentage target on the same kWh —
finish-aligned it is not. The honest caveat: how surplus ACTUALLY divides
among three per-phase pods under vendor autonomy is an empirical question
this site has not answered; the historian can answer it (per-pod morning
charge kWh), and a measured-share graduation is documented as a future
extension (§14). Until then the tile's morning-after honesty line (§8) makes
each battery's landing visible, so a wrong share model is discovered, not
hidden.

### 2.3 `midday_local` — the operator's declared finish line

A civil-time bound (`"12:00"` default), same IANA zone as the window, DST-
honest through the v1 pure helpers by construction. This is NOT solar noon
and does not pretend to be: it is the household's planning fact ("full by
midday" on the wall clock). Solar noon's drift (equation of time, ~±15 min
seasonal at this latitude) is absorbed by the window's slack — production
continues past midday, and the objective is a finish LINE, not a finish
INSTANT. Site fact, pinned: Queensland does not observe DST, so the
civil/solar offset is seasonal-only here; on a DST site civil-midday would
shift an hour twice a year and the operator would re-rule `midday_local`
then (§12 flag 6).

### 2.4 The quantile — p10 drives, p50 explains

**Recommendation: `forecast_quantile: 0.1` — the p10 slice computes the
target; the p50 slice rides the projection for explanation.** The reasoning,
stated as the trade-off it is:

- **The objective is lexicographic.** First: full by midday (the operator's
  stated law). Second: buy as little grid as possible to get there. A
  quantile is a service-probability dial, and the honest reading of THIS
  dial is not the nominal one (panel A7): the target nets p10 PER SLOT,
  and the sum of per-slot p10s sits below the p10 of the sum — slot errors
  partially cancel — so a p10-driven target finishes the fleet full on
  ~95–99% of days, not the flat ~90% a single-series p10 implies and not
  the ~50% of a p50 target. The price of that extra service is savings
  forgone: the per-slot stack buys more grid than a portfolio-level p10
  would. Under lexicographic ordering that is the right side to err on,
  and p10 stays.
- **The money asymmetry agrees, when there is money.** Over-charging buys
  off-peak kWh at ~12c that the sun then displaces, exporting the surplus
  at ~9c: the over-buy costs ≈ 3–4 c/kWh (spread + round-trip losses). Under-
  charging leaves a shortfall the evening serves from grid at ~28c that the
  window could have bought at 12c: ≈ 16 c/kWh. Four-to-one against
  under-charging. But the FEATURE's own sign is a tariff fact, not a
  constant (panel A3): every stored kWh moved off the overnight buy and
  onto morning surplus costs `(import − FIT)/η` — grid storage costs
  `import/η` per stored kWh, filling from surplus forgoes `FIT/η` of
  export — so the feature only pays while FIT < off-peak import. On the
  legacy 44–52 c QLD FiT against ~12 c off-peak that INVERTS to a loss of
  ~35 c per stored kWh, and this design refuses to automate a loss:
  tariff keys present AND export FIT below off-peak import are a HARD
  prerequisite for `forecast_act` (§6 validation, §9 step 4, §13 item 6).
  Until the keys exist the site runs kWh-only, SUGGEST-only.
- **The honest counter-case.** Every conservative choice stacks (§2.5), and
  on volatile days p10 can be a fraction of p50 — the feature's savings
  shrink toward v1 exactly when the sky is hardest to predict, and every
  over-bought kWh is a cycle on packs whose owner has said, twice, that they
  dislike energy going in and out. p50 would maximize expected saving at
  the price of missing the objective half the time. The panel saw it as a
  genuine dial and kept p10 with the corrected service number; moving it
  remains a config revision with the operator's name on it.

### 2.5 The conservatism stack, named

Four independent conservative choices — p10 (less sun), full-deficit-served
(more morning drain), `charge_efficiency` 0.9 (less credit), `floor_pct`
(hard reserve) — all push the same direction, and their stack is BOUNDED:
every conservatism converges on the v1 full charge, the known-safe incumbent,
and can never overshoot it. The feature's savings are spent only when the
forecast is confidently good. That shape is deliberate: a wrong-side error
must land on the incumbent's behavior, not on an empty pack.

### 2.6 Site facts and ASSUMPTIONS, explicit

- Fleet: mid (60 cells), rhs (50 cells), lhs (60 cells); 6.5 kW DC array
  (5 kW AC), north-facing, tilt 30, loss factor 0.9 (the Solcast site
  record holds the plane — this file never re-states it).
- **ASSUMPTION (operator to confirm, §13 item 1):** `assumed_capacity_wh`
  ≈ {lhs: 5000, mid: 5000, rhs: 4200} Wh. The 5000-class figure is the
  docs' standing estimate (NIGHT_LOAD_INVESTIGATION §5 "a 5 kWh-class
  pack"; v1 §2.2 "~5000-class on this fleet"); rhs's 4200 is DERIVED from
  cell-count proportionality (50/60 × 5000 ≈ 4167), not measured. The
  estimate shapes WHAT (this section) and pacing (v1), never safety — a
  wrong figure surfaces as early/late completion, and its error direction
  is named: an UNDERSTATED capacity overstates headroom and under-charges
  (the wrong way), so confirming rhs's true Wh matters more than confirming
  the 60-cell units'. **A second consumer now leans on this assumption**
  (DESIGN_CALIBRATION_CYCLING §7/§12-2): the calibration traverse's deadline
  rate and its lying-word energy bound both pace from the same map, and that
  contract's config validation refuses a `battery_calibration` map that
  drifts from this one — one physical fact, enforced at both keys. A THIRD
  consumer (DESIGN_EVENING_LOAD_SHARING §5.1/§11) leans on it the same way:
  the evening share weight `SoC^exponent x capacity` paces the percentage
  space THIS section targets, and that contract's validation refuses an
  `evening_load_sharing` map that drifts from either sibling's — one
  physical fact, three keys, all enforced.
- **ASSUMPTION:** `charge_efficiency: 0.9` (round-trip AC→stored, unlabeled
  vendor figure — the derate's role is directional conservatism, not
  measurement).
- Solcast serves p10/p50/p90 at 30-min resolution, 48 h horizon, refreshed
  twice daily (the hobbyist 10-requests-per-UTC-day budget) — every
  forecast read carries `fetched_at`/`issued_at` honesty per the provider
  model, and a cached value never freshens by rereading.

### 2.7 The architecture-fitness pin (load-bearing for the implementer)

No application module may import `adapters.providers` — the pinned
architecture-fitness test says so, and the night adviser lives in
`application/`. The adviser therefore consumes forecasts through an
INJECTED FORECAST PORT (a Protocol, like its observation/intent/submit
ports): composition reads the provider registry and hands the adviser a
narrow `morning_credit_kwh(window_end, midday_local, quantile) ->
MorningCredit` shape — named for what it credits (the morning AFTER the
window, in kWh), not the night it is asked during; the draft's
`tonight_net_energy` lied about both the span and the unit, and the panel's
rename (A12) fixes it. Providers stay advisory-only by construction; a
provider failure is the port returning no-credit, and the fail-safe (§3.3)
answers it.

## 3. Trust gating — progressive earning, config-only promotion

### 3.1 The two postures

`target_policy` is ONE key, THREE states — no invalid combinations exist:

- **`full`** — v1 identity. Byte-identical behavior to the shipped adviser.
  The default, and the state an absent key means.
- **`forecast_suggest`** — the adviser COMPUTES and DISPLAYS tonight's
  per-battery target, its forecast provenance, and the trust scoreboard,
  but charges to the v1 target (the ceiling). The submission math is
  byte-identical to `full` (a named test); only the projection gains the
  `suggested_target_soc_pct` fields. This is roadmap Phase 5's
  recommendation-then-shadow posture applied to a single number, and it
  runs from day one at zero actuation risk — the displayed number is
  advisory and says so beside itself (§8's banner), and the scoreboard
  needs the days anyway.
- **`forecast_act`** — the computed target governs. Entered ONLY by a
  config revision + restart after the operator accepts the §3.2 evidence
  (promotion is a human act, exactly like every other authority grant on
  this controller — the excess toggle, the partition grant, the arm). The
  console can never flip this key; there is deliberately no route.

### 3.2 The scoreboard (the trust evidence)

The daily unit is a DAY, not a watt-tick: after `midday_local` each day the
controller evaluates the morning just finished — the forecast series that
was ACTUALLY USED at window open (archived verbatim in the §7
`night_target_set` audit row) against the historian's recorded PRE-BATTERY
surplus integrated over `[window_end, midday)`:

```
E_recorded_kwh = Σ_slots max(0, fleet_grid_export_w + fleet_battery_charge_w) × slot_h
err_d = |E_surplus_forecast − E_recorded_kwh| / max(E_recorded_kwh, 1.0)
```

**The recorded basis is PRE-BATTERY, and the basis is load-bearing (A1).**
Surplus that lands on the AC bus splits two ways — exported, or absorbed by
the batteries. The export channel alone is a POST-battery proxy that reads
LOW on exactly the mornings the forecast was RIGHT: the batteries were
taking the surplus, so the meter had nothing to show, and an export-only
scorer would call the feature's best hits its worst misses — suspension by
measurement artifact. `E_recorded` reconstructs the surplus the batteries
absorbed: the historian's per-unit `grid_power_w` (export-positive) plus
the battery charge word, fleet-summed under the all-units-reported rule,
floored at zero per slot. The same basis flaw sits in
`cross_check.py`'s export-only proxy — advisory there (a display scorer),
GOVERNING here (the promotion gate reads the number), so the night-V2
implementation wave must FIX or SUPERSEDE it: this contract does not ship
a gate that scores its own best mornings as failures.

Rolling trust over the last `trust.required_days` SCORED days. A day is
EXCLUDED, not counted as a failure, when the historian record is
incomplete or when no forecast was used (`full`-posture days and §3.3
fallback nights archive nothing to score — A12) — gaps never poison
evidence they merely fail to inform. Note the exclusions stop there
deliberately: a morning the batteries refused charge still SCORED, because
on the pre-battery basis the refused surplus was exported and measured —
the attribution below adjusts the landing's accounting, never the
surplus error:

- **Earned** when: `days_scored ≥ 14` AND `mean(err_d) ≤ 0.30` AND the
  signed bias inside ASYMMETRIC bounds — `+10% over-forecast / −20%
  under-forecast` (bias = mean signed error, positive = over-forecast; a
  scorer passing the mean with a persistent over-forecast systematically
  under-charges, so over-forecast is the dangerous direction and its
  tolerance is the tighter half — A4) — AND at least 3 low-surplus AND 3
  high-surplus mornings among the scored days, the buckets cut at the
  terciles of THIS SITE'S own recorded `E_recorded` distribution (A4:
  the regime gate kills the one-stable-high fortnight — 14 correlated
  fair-weather passes prove nothing about the cloudy morning the feature
  must survive).
- **Provisioning** below that (the honest "not enough days yet" word).
- **Suspended** when a previously-earned trust breaches (a rolling
  re-evaluation drops the mean, the bias, or the regime mix out of
  bounds): ACT demotes itself to full targets, loudly (§3.3), until the
  rolling window re-earns.

**Proposed gate: 14 days, 30% mean, +10/−20% bias, 3-and-3 regime mix.**
Calibration honesty: 14 is a threshold of convenience (two weeks, one
synoptic cycle, and exactly the historian's full-resolution retention —
hourly rollups extend it forever if the operator wants 28), NOT
statistical rigor; forecast errors are autocorrelated (one bad synoptic
week is ~seven correlated busts), and the first validation season is
late-winter Brisbane — summer convection is a different sky. The regime
buckets are the structural answer to both autocorrelation and season: a
passing window must now contain both kinds of sky. The remaining
mitigations: SUGGEST mode runs regardless (zero actuation risk while
evidence accrues), promotion is the operator's explicit act against
displayed evidence, and the verification half is honest about what it
cannot see (§12 flag 3: the DEFICIT term is unverifiable from surplus-only
history — the scoreboard validates the surplus credit, the morning-drain
term rides unverified). Asymmetric hysteresis — suspension harder to EXIT
than earn was to ENTER — was offered by the panel and accepted as P2
(§14): the demote-itself-loudly behavior is v2's protection, the harder
re-entry is the follow-up.

The landing evidence carries the ATTRIBUTION the panel required (A1): a
below-target morning splits "forecast missed" from "battery would not take
it" by the recorded `dynamic_charge_limit_w` (rhs reads 0 W when full —
observed), so a morning the BMS refused is never charged to the forecast's
account.

This evaluator REUSES the cross-check scorer's machinery (the historian
row walk, the all-units-reported timestamp rule, the no-fabricated-score
refusal) at energy aggregation — it does not reuse the watt-level
`ForecastScore` shape, because the adviser consumes kWh, and gating on
watt MAE would test a number nobody acts on. It diverges from that scorer
on the basis, per A1 above: export-only is the flaw, not the starting
point.

### 3.3 The fail-safes — every one lands on v1-full, loudly

In ANY posture, per window, the forecast-aware computation is abandoned —
targets revert to the v1 ceiling for every unit, and the projection says
why — when:

- the forecast series is MISSING (no Solcast data covering the window) →
  `forecast_missing`;
- the forecast is STALE (`fetched_at` older than the provider layer's
  `stale_after_s` — a cached 12 h-old forecast is legitimate; a 26 h-old
  one is not) → `forecast_stale`;
- the load baseline is unavailable for the morning span — the
  `load_baseline:` provider missing, or ANY slot of last week's
  same-slot span unavailable in the historian (a single gap is a hole in
  the netting, not a spot to interpolate around: the conservative pin,
  A9) → `forecast_no_load_baseline` ⇒ full targets — there is
  deliberately NO degrade-to-PV-only path: gross PV over-credits the
  morning and under-charges, the refused direction;
- trust is `suspended` or the posture is below ACT for any reason →
  `forecast_below_trust`.

The umbrella doctrine: **never silently skip charging.** Every failure of
foresight buys MORE off-peak energy, not less — the conservative direction
is the incumbent's behavior — and every one of these states renders on the
tile and rides the reason codes, so a fallback week is visible from across
the room. This mirrors v1's fail-closed HOLD doctrine with the polarity
appropriate to its layer: there, bad evidence must stop a charge; here,
bad foresight must not stop the CHARGE, only the DISCOUNT.

## 4. What V2 never touches (the v1 inheritance, itemized)

Every v1 safety doctrine is inherited verbatim — this section exists so the
panel can diff it in one place:

1. **Rate cap** — `rate_cap_w` and the kernel's per-unit/site clamps;
   V2 changes kWh aimed at, never W drawn.
2. **The demand stand-down rule** — unchanged and still sovereign over
   WHEN: load CT words never grid words, threshold/hysteresis, zero-watt
   non-participation, autonomy hand-back, fail-closed HOLD at
   `hold_rate_w`. A stand-down mid-charge under a forecast target simply
   resumes at the recomputed rate when demand falls (§5.4).
3. **Fail-closed evidence doctrine** — §3.3's mirror, not its replacement.
4. **Window non-renewal hand-back** — TTL lapse + watchdog at window end;
   no stop triples, no idle intents, exactly one held intent, ever.
5. **Pacing** — `cap_first`/`even` unchanged; under `even` the forecast
   target is just a smaller `target_soc_pct` in the same self-correcting
   deadline rule.
6. **Park/disarm exclusion** — `unit_parked` outranks `units_disarmed`;
   `skipped_full`/`no_charge_headroom` eligibility sugar unchanged.
7. **Partition doctrine** — the night window, its grant, the durable
   acknowledgement, and the night-writer detector story are untouched.
8. **Precedence** — manual > agent > optimizer > schedule, the dawn-corner
   one-sided yield to the excess adviser, per-unit exclusion at submission
   time: all unchanged. The dawn corner gains a note (§5.5): dawn surplus
   is deliberately NOT credited in `E_credit` — the per-tick closed loop
   already monetizes in-window surplus, and crediting it would count the
   same energy twice. Conservative, and stated.

## 5. Completion, re-evaluation, and the scope boundary

### 5.1 Completion — `target_reached`, a widened word

A unit whose authoritative SOC reaches its commissioned target STOPS:
excluded from the submission (zero-watt non-participation, the standing
doctrine), per-unit phase `complete`, reason `target_reached`. The word
already exists in v1's vocabulary, where it meant "reached the 95%
ceiling"; V2 widens it to "reached the commissioned target, whatever policy
set it" — and the ceiling case keeps its OWN word (`at_ceiling` +
`skipped_full`), so the two causes never blur. Additive, but the panel
should see the drift explicitly (§11 item 2): a v1-era consumer reading
`target_reached` as "at 95%" is now wrong on forecast nights, and the
per-unit phase is the disambiguator. Completion needs no epsilon — SOC
crosses the target mid-interval by at most rate × period ≈ 0.03% of the
pack.

**Completion is ONE-DIRECTIONAL for the window (A2).** Against a target
FALL it is sticky: a completed unit never rejoins the submission because
the sky improved — the energy is bought, un-charging is not a thing, and
re-targeting a finished unit downward would be paying twice for the same
morning. Against a target RISE it is not: a cloudier revision that lifts
the fleet target above a completed unit's MEASURED SOC re-opens that unit
— it re-enters the submission at the next tick and charges the gap under
the unchanged rate cap, riding the same §5.2 re-target event (the
2-per-window and 60-minute caps count it like any other). The morning
genuinely needs the energy; the panel struck the draft's blanket
stickiness and its paying-twice rationale for exactly this direction.

### 5.2 Mid-window re-evaluation

Solcast refreshes twice daily, so a new series can land inside the window.
On each arrival (the port surfaces a new `fetched_at`):

- Compute the new `E_credit`. A MATERIAL change — `|ΔE_credit| ≥
  max(retarget_threshold_pct × E_credit_current, 0.5 kWh)` (20% OR half a
  kWh, whichever is larger; the absolute floor keeps tiny-forecast noise
  from re-targeting) — re-targets the STILL-CHARGING units mid-window,
  both directions: a sunnier revision lowers their targets (some may
  complete immediately — sticky against further FALLS from then on, §5.1),
  a cloudier one raises them (the conservative direction, always allowed)
  and re-opens any completed unit the new target stands above (§5.1).
  Units in demand stand-down INHERIT the revised fleet target (A6): the
  demand rule still gates WHEN they act, but on resume they charge toward
  the REVISED target, never the window-open one.
- **Rate limits, pinned:** at most TWO re-targets per window, minimum 60
  minutes apart, §5.1 re-openings included. The caps are derived from the
  durable `night_target_revised` audit rows for tonight's civil window,
  NOT from runtime counters — runtime state does not survive restarts
  (§6), and a restart mid-window must not reset the re-target budget
  (A6). The natural cadence (2 fetches/day) makes the cap almost never
  binding — it exists for the restart-primed fetch and the operator's
  re-commissioning legs.
- Every re-target writes the §7 audit row with the delta and the new
  provenance; the projection's target fields move with it.

### 5.3 The scope boundary — daylight is not ours

The night adviser writes NOTHING outside `window_local`, ever. Finishing
to 100% by midday is the PODS' OWN autonomy (the vendor CT-following that
already carried rhs to 98%) plus the excess-solar adviser's acceleration —
two owners that already exist, need nothing from V2, and are the reason
the night adviser can stop early with a clear conscience. No window
extension, no daytime hold-over intent, no "top up what solar missed at
11:50" — a 95%-ceiling-bound fleet that lands at 97% by noon because the
sky under-delivered is the forecast's miss, priced on the scoreboard, not
a hole for this adviser to fill. Stated once, pinned.

### 5.4 The demand stand-down interplay (the flag, answered in-design)

A stand-down hands the pod to its own autonomy, which may DISCHARGE into
the spike — so a unit can resume at a LOWER SOC than it stood down at.
Nothing needs new machinery: the plan recomputes from MEASURED SOC every
tick (v1's standing property), the target is unchanged unless a §5.2
revision lands while the unit stands down — in which case it inherits the
revised fleet target like anyone else (A6) — and the unit simply charges
longer. What cannot be recovered is
window TIME: if the window closes with a unit below target, the close is
honest — reason `window_closed_below_target` (additive) on the window-end
event and audit, the morning solar takes what it takes, and the scoreboard
prices the miss. The demand rule gates WHEN; the forecast shapes WHAT; the
window bounds BOTH.

### 5.5 The dawn corner, restated for V2

Summer sunrise (~04:45) overlaps the window's tail with real export; the
excess adviser may claim a needy unit under the v1 one-sided rule. V2 adds
only this: dawn surplus is NOT credited in `E_credit` (the sum starts at
window end). The reason, corrected by the panel (A12), is not adviser
coupling — it is that the per-tick closed loop ALREADY monetizes
in-window surplus: a still-charging unit is itself the sink (its charge
rate absorbs the dawn watts), the stand-down machinery hands
surplus-heavy ticks to autonomy, and completion stops the charge only at
target. Surplus that arrives while the window is open is handled by the
loop, tick by tick; crediting it in `E_credit` would count the same energy
twice and hold the fleet target down on exactly the mornings the loop
stood everyone down. The conservative direction, again.

## 6. Config — the `night_charging:` block, extended

```yaml
night_charging:
  # ... every v1 key unchanged (timezone, window_local, enabled, rate_cap_w,
  #     demand_threshold_w, demand_exit_hysteresis_w, hold_rate_w,
  #     demand_scope, pacing, demand_telemetry_max_age_s, intent_ttl_s) ...
  target_policy: "full"          # full | forecast_suggest | forecast_act
  forecast_quantile: 0.1         # the DRIVER slice, [0, 1]; p50 explains
  midday_local: "12:00"          # civil, same zone; strictly after window end
  floor_pct: 50.0                # the reserve floor, (0, max_soc_pct)
  charge_efficiency: 0.9         # (0, 1]; the derate on credited surplus
  retarget_threshold_pct: 20.0   # with the 0.5 kWh absolute floor
  retarget_min_gap_min: 60       # and the 2-per-window cap (pinned §5.2)
  assumed_capacity_wh: {lhs: 5000, mid: 5000, rhs: 4200}   # ASSUMPTION §2.6
  trust:
    required_days: 14
    tolerance_pct: 30.0          # mean |err| over scored days
    max_overforecast_bias_pct: 10.0   # the DANGEROUS direction, tighter (A4)
    max_underforecast_bias_pct: 20.0
    min_regime_days: 3           # low- AND high-surplus scored mornings
```

Cross-validations, additive to v1's (each message names its rule):

- `target_policy ∈ {full, forecast_suggest, forecast_act}`; anything else
  is a validation error naming the three words.
- IFF `target_policy ≠ full`: `assumed_capacity_wh` present, key set
  exactly the fleet, EVERY VALUE > 0 (a zero-or-negative Wh is a nonsense
  capacity that would throw targets across the range — refused at
  validation, A12; the v1 `even`-pacing rule widens to this OR); a PV
  forecast source is declared in `forecast_providers` (solcast) and
  `forecast_providers.enabled`; and — for `forecast_suggest`/`forecast_act`
  — the `load_baseline:` provider is declared (it requires `plant_history`,
  which this site runs). Refusals name the missing block by path.
- `forecast_quantile ∈ [0, 1]`; `0 < floor_pct < policy.max_soc_pct`;
  `0 < charge_efficiency ≤ 1`; `midday_local` strictly after every
  configured window's `end_local` on the same civil morning; `0 <
  retarget_threshold_pct ≤ 100`; `retarget_min_gap_min ≥ 15`; the two
  bias bounds > 0 with the over-forecast bound the tighter;
  `min_regime_days ≥ 1`.
- `target_policy: forecast_act` additionally requires the TARIFF KEYS
  present with the export FIT strictly below the off-peak import rate
  (A3, the §2.4 math — a file fact, so the file refuses it; the controller
  does not automate a per-stored-kWh loss). It still requires NOTHING
  about trust at config time — trust is runtime evidence; an
  unearned-trust ACT site simply runs the §3.3 fallback with
  `forecast_below_trust` until the scoreboard earns. (An alternative —
  refusing `forecast_act` at validation until a durable trust fact
  exists — was considered and DECLINED: config validation stays
  offline-pure, never reading the historian; the durable
  `night_trust_earned` audit fact is the operator's promotion receipt
  instead, §9.)

Runtime state does not persist (the v1 doctrine): boot recomposes
`target_policy` from the file, `enabled_origin` stays honest, and the
toggle route remains participation-only — it can never flip the target
policy, the quantile, or the floor.

## 7. Projection, events, audit — additive vocabulary only

### The `night_charge_state` delta

Existing keys unchanged; these keys ADD (absent when `target_policy:
full`, so v1 consumers see byte-identical frames):

```json
"target_policy": "forecast_act",
"trust": {"state": "earned", "days_scored": 19, "required_days": 14,
          "mean_abs_err_pct": 21.4, "bias_pct": -4.2,
          "low_surplus_days": 5, "high_surplus_days": 4},
"forecast": {"source": "solcast", "quantile": 0.1,
             "issued_at": "2026-08-24T08:03:00Z",
             "fetched_at": "2026-08-24T10:00:12Z",
             "e_surplus_kwh": 6.0, "e_deficit_kwh": 0.5,
             "e_credit_kwh": 4.9, "midday_local": "12:00",
             "ceiling_bound_by": null},
"units": [
  {"unit_id": "lhs", "soc_pct": 61.8, "phase": "pacing",
   "target_soc_pct": 65.5, "target_w": 2500, "reason": "on_plan"},
  {"unit_id": "rhs", "soc_pct": 68.9, "phase": "complete",
   "target_soc_pct": 65.5, "target_w": 0, "reason": "target_reached"}
],
"explanation": "lhs to 66% by 06:00 — 4.9 kWh forecast surplus by 12:00 "
               "finishes it (Solcast p10, issued 18:03)"
```

Read the units row the way the panel made us (A8): lhs sits BELOW its
target and paces toward it; rhs sits ABOVE and renders
`complete`/`target_reached` — SOC above target is complete, NEVER pacing
(the draft's example carried lhs the other way and the panel caught it;
T-NC2-TARGET-MATH pins the comparison). `ceiling_bound_by` is the
decomposition word (A10): `sky` when `E_credit ≤ 0` (v1 by computation),
`netting` when real surplus was eaten by the deficit/η terms, null when
the target sits strictly between floor and ceiling — it is the tile's
"which guard fired" line (§8).

In `forecast_suggest`, the per-unit key is `suggested_target_soc_pct` and
the submission math is v1's (a named test pins the byte-identity of the
intent stream). The semantic tuple for `night_charge.state_changed` gains
exactly two members — `target_policy` and `trust.state` — targets and
figures ride every publication but never trigger (the watts doctrine
verbatim). New reason codes, additive: `forecast_missing`,
`forecast_stale`, `forecast_no_load_baseline`, `forecast_below_trust`,
`window_closed_below_target`. `target_reached` widens (§5.1). No phase
word changes.

### Audit rows (advisory class, the accountant pattern)

- **`night_target_set`** — once per window, at the first in-window tick:
  the full arithmetic (E figures, quantile, share model, capacity map,
  per-unit targets, trust snapshot, forecast provenance). The
  reconstruction row — any later decision replays from it.
- **`night_target_revised`** — per §5.2 re-target: the delta, direction,
  affected units, new provenance.
- **`night_trust_evaluated`** — daily post-midday: the day's err, the
  rolling mean/bias/days, the state transition if any.
- **`night_trust_earned`** — durable-once (the partition-acknowledgement
  pattern): the promotion receipt the operator's `forecast_act` revision
  is checked against in the §9 sequence.

## 8. The console delta

The Night tile (feature-detected on the new keys; absent under `full`
hides them, the standing doctrine):

- **Target line per battery:** "lhs → 66% · rhs → 66% · mid → 66%" (one
  number fleet-wide, the §2.2 property) with the per-battery row showing
  target vs SOC.
- **The reasoning sentence, verbatim from the projection's
  `explanation`** — "lhs to 66% by 06:00 — 4.9 kWh forecast surplus by
  12:00 finishes it (Solcast p10, issued 18:03)". One sentence, honest,
  with the age of the forecast beside it.
- **The trust line:** "forecast trust: 19 days scored · mean err 21% ·
  bias −4% · 5 low / 4 high mornings — EARNED" or the provisioning/
  suspended word with the count (the regime counts ride it, A4 — a
  passing mean over one kind of sky must not look like evidence).
- **The suggest banner:** under `forecast_suggest`, "showing forecast
  targets — charging to 95% (v1) until trust is earned; promotion is a
  config revision" — the invisible-state rule: a displayed number that
  does not govern MUST say so beside itself.
- **Fallback styling:** the §3.3 reason codes render as their own tile
  states ("charging to full — forecast stale (26 h)"), never as silence.
- **The decomposition line (A10):** when the computed target clamps to
  the ceiling the tile says WHICH term bound it — "charging to full —
  the morning deficit bound it, not the sky" (`ceiling_bound_by`, §7) —
  so a v1-shaped night with a live forecast is legible as such. Beside
  every target the 95-vs-100 clause stands: targets stop at the ceiling
  because "the pods top the last few percent themselves" (§2.1).
- **The morning notice (A5):** a window that closed below target leaves a
  persistent notice on the Night tile until `midday_local` — "ended the
  night below target — solar is finishing what it can; landing visible
  after midday" — beside its Activity row; at midday the Insights landing
  line takes over.
- **Morning-after honesty (Insights):** the trust line plus each battery's
  yesterday landing vs its target, with the A1 attribution split —
  "forecast missed" against "battery would not take it" (the recorded
  dynamic charge limit) — the wrong-share-model detector (§2.2) and the
  scoreboard's honesty, both made visible.

## 9. Commissioning sequence

1. **PREREQUISITES (config revision one):** declare `load_baseline:` in
   `forecast_providers` (requires the running `plant_history:` — live
   since the history program; the scorer and the baseline both read it);
   Solcast is already declared and keyed. Tariff keys may stay unset
   through SUGGEST (kWh-only is honest) — but step 4 is gated on them
   (A3): answer §13 item 6 before the SUGGEST run ends, not after.
2. **SUGGEST (config revision two):** `target_policy: forecast_suggest`
   plus the §6 keys; restart. The adviser charges v1-full nightly and
   displays targets; the scoreboard accumulates one
   `night_trust_evaluated` row per day. Minimum run: `required_days` of
   scored days including the 3-and-3 regime mix — 14 calendar days
   minimum, realistically more with excluded days.
3. **REVIEW:** the operator reads the trust line and the Insights
   landings across at least one weather regime change. Accept or extend
   the run (28 days is the documented conservative extension).
4. **ACT (config revision three, GATED — A3):** requires the tariff keys
   set with export FIT strictly below off-peak import (§2.4's math; the
   §6 validation refuses the key otherwise, and a legacy 44–52 c QLD FiT
   site fails it on physics, not process — that site stays v1-full and
   this design is content) plus `target_policy: forecast_act`; restart.
   The durable `night_trust_earned` fact should already exist;
   if it does not, the operator is promoting on thinner evidence than the
   design's gate and the CONTINUITY entry says so.
5. **SUPERVISED NIGHTS:** the v1 §3.4 verify discipline, extended with
   one abort criterion: any unit ending a night >5% below its computed
   target for two consecutive nights → back to `forecast_suggest` and the
   quantile/floor re-examined (§13).
6. **SEASONAL RE-VALIDATION:** the first earned trust is a late-winter
   sky; re-read the scoreboard after the first summer convection weeks —
   trust can suspend itself, and the operator should expect it to.

## 10. Test matrix (contract-first, author before implementation)

- **T-NC2-CONFIG** — the §6 matrix: three-state policy; capacity-map OR
  rule with every value > 0 (A12); provider/baseline declarations; the
  `forecast_act` tariff gate — keys present AND export FIT < off-peak
  import, with the legacy-FiT refusal message (A3); floor/quantile/
  efficiency/midday/retarget bounds; the trust-block bounds; midday-
  after-window-end; every refusal message naming its rule; `full` = v1
  config identity.
- **T-NC2-TARGET-MATH** — the formula: share collapse (one fleet
  percentage); clamps both ends; zero forecast → ceiling; huge forecast →
  floor; rhs proportionality; the capacity-error DIRECTION pin (an
  understated map under-charges — the named hazard); deficit subtractions;
  completion threshold; ONE-DIRECTIONAL completion — holds on falls,
  re-opens on a rise above measured SOC (A2); and the completion-
  comparison case: SOC above target renders `complete`/`target_reached`,
  never pacing (A8, the §7 lhs lesson).
- **T-NC2-SUGGEST** — byte-identical intent stream vs v1 under
  `forecast_suggest` while the projection carries the suggested fields;
  the never-silent banner.
- **T-NC2-TRUST** — the daily err from historian rows on the PRE-BATTERY
  basis, export + battery charge (A1 — an export-only scorer is the named
  failure, and the test pins it); all-units rule; gap exclusion;
  full-posture days excluded, not failed (A12); the compound gate —
  N/mean/ASYMMETRIC bias/regime mix 3-and-3 cut at the site's own
  terciles (A4); earned / provisioning / suspended transitions; rolling
  re-earn; the refusal-vs-miss landing attribution; the daily audit rows;
  `night_trust_earned` durable-once.
- **T-NC2-FALLBACK** — each §3.3 trigger producing v1-full targets with
  its reason code and tile state; recovery when data returns; the
  no-PV-only-degrade pin for the missing baseline; and the A9 pin — a
  single unavailable baseline slot fails the whole morning span to
  `forecast_no_load_baseline`.
- **T-NC2-RETARGET** — threshold with the absolute floor; both
  directions; the §5.1 re-open on a rise; caps DERIVED FROM THE DURABLE
  `night_target_revised` ROWS, proven across a mid-window restart (A6);
  stood-down units inheriting the revised target; the 2-per-window and
  60-min limits; audit rows; revision provenance on the projection.
- **T-NC2-DEMAND-INTERPLAY** — stand-down/resume under forecast targets;
  resume-at-lower-SOC self-correction; `window_closed_below_target` at a
  short window.
- **T-NC2-PROJECTION/EVENTS** — additive keys only; `full` frames
  byte-identical to v1; the two-member semantic-tuple extension; nothing
  new published while disabled.
- **T-NC2-ARCHITECTURE** — the adviser's `morning_credit_kwh` port
  injection (A12's name); no application-module import of
  `adapters.providers` (the existing fitness test extended to the new
  consumer); provider failure = no-credit = fallback, never a crash.
- **T-NC2-CONSOLE** — tile target/trust/sentence lines with the regime
  counts; suggest banner; fallback states; the ceiling-bound
  decomposition line with the 95-vs-100 clause (A10); the morning
  below-target notice persisting to `midday_local` (A5); Insights
  landings with the attribution split (A1); shot matrix per the polish
  bar.

## 11. For-the-panel (coordinator rulings, panel-affirmed, nothing dropped)

1. **Ruling 1's formula, amended twice.** (a) `E_forecast_by_midday` as
   gross PV is refused — only NET surplus (PV minus load baseline, minus
   the full-deficit morning drain) credits the morning; gross PV
   over-credits and under-charges, the forbidden direction. (b) The upper
   clamp is the charge CEILING (95), not 100 — the kernel refuses our own
   writes above it, and pretending otherwise puts the arithmetic's top
   end in a region the adviser cannot actuate. The floor I propose at 50
   with the §2.6/§13 caveat that on this array-vs-fleet ratio it BINDS on
   most sunny days, and 40 is a defensible operator choice after a
   season's evidence.
2. **Ruling 4's "new vocabulary word" is a WIDENED word.** `target_reached`
   already exists in v1's vocabulary meaning reached-the-ceiling; V2
   widens it and keeps `at_ceiling`/`skipped_full` for the ceiling case.
   Additive, but semantic drift on an existing word is exactly what
   panels exist to notice.
3. **Ruling 2's "N days within M%" is under-specified in two ways.** Days
   must be non-consecutive-scored (gaps excluded, not counted as
   failures), and a BIAS guard is required alongside the mean — mean-only
   passes a systematically over-forecasting provider that under-charges
   every night. Hence the compound gate, sharpened by the panel (A4):
   14 days, 30% mean, ASYMMETRIC bias +10%/−20% (over-forecast the
   dangerous, tighter direction), and a 3-low/3-high regime mix cut at
   the site's own recorded terciles.
4. **Ruling 6's ±20% needs an absolute floor** (0.5 kWh) — 20% of a tiny
   forecast is measurement noise — and the completion rule,
   ONE-DIRECTIONAL since the panel (A2): falls never un-complete a unit;
   a rise above a completed unit's measured SOC re-opens it inside the
   same caps (§5.1).
5. **Ruling 2's SUGGEST-start is agreed and strengthened:** SUGGEST is not
   a transient — it is the standing posture until the operator's config
   revision, the scoreboard runs inside it, and its intent stream is
   byte-identical to v1 (a named test, not a hope).
6. **The trust scoreboard's verifiable half.** The historian records
   export and battery charge words per unit, and the scoreboard
   reconstructs PRE-BATTERY surplus from both (A1) — the export-only
   proxy reads low exactly when the feature works. The deficit term of
   §2.1 remains unverifiable from either. The gate scores the surplus
   credit only — honest, and the reason the tolerance is 30%, not 15%:
   the unverified term's error rides inside the verified term's score
   only by correlation.

## 12. Challenge-flags — the panel's answers

1. **Quantile asymmetry.** p10 protects the objective and loses ~3–4 c/kWh
   on over-buys; p50 halves the miss rate and wastes the objective half
   the time; the cycling cost of over-buys is real to an operator who has
   twice said they dislike it. Is lexicographic ordering (full-by-midday
   first, cost second) actually the operator's ordering, or their polite
   approximation of a weighted one? Ask them before promotion, not after.
   **Panel:** the ordering stands; the service number was wrong — slot-wise
   netting lands ~95–99%, not 90% (§2.4, A7) — and §13 item 3 now carries
   the corrected figure.
2. **Trust calibration with tiny, correlated n.** 14 days, one season,
   autocorrelated errors; a "passing" fortnight can be one stable high.
   Should promotion additionally require ≥2 distinct weather regimes
   (a synoptic classification the site does not hold — or the operator's
   eyeball on the Insights chart)? And should trust SUSPEND be harder to
   exit than EARN was to enter (asymmetric hysteresis on the gate)?
   **Panel: answered — the regime-diversity gate is in (§3.2, A4: 3 low
   AND 3 high mornings, terciles from the site's own recorded
   distribution); the asymmetric hysteresis is accepted as P2 (§14).**
3. **Per-battery share when the forecast is site-level.** Capacity-
   proportional share assumes surplus divides capacity-proportionally
   across three per-phase pods under vendor autonomy — unverified. If one
   phase's load persistently eats its pod's share, that pod lands short
   while the fleet number passes. The morning-after landing display is
   the detector; when does a measured-share graduation become mandatory
   rather than optional? **Panel: unchanged — the landing display remains
   the detector and the graduation stays a documented extension (§14).**
4. **Demand stand-down mid-charge under a forecast target.** The stand-
   down can leave a unit BELOW its window-open SOC (autonomy discharge
   into the EV), and lost window time is unrecoverable: the design answers
   with recompute-per-tick plus the honest `window_closed_below_target`.
   Should a persistent stand-down instead trigger a mid-window target
   RAISE (revert that unit toward full) once lost time exceeds some
   fraction of the window — or is that the thin end of re-inventing v1
   under stress? **Panel: the design's answer stands — no stand-down-
   driven raise; a forecast-revision raise now reaches stood-down units
   by inheritance (§5.2, A6), which is the only raise path.**
5. **High forecast, hungry morning.** The netting handles the average
   morning; it does not handle an ATYPICAL one (guests, a daytime EV
   charge, weather-driven HVAC) — same-slot-last-week is a baseline to
   graduate from, not a model. The floor and p10 are the guards; is a
   same-day morning-load revision signal (the historian's live morning vs
   the baseline, post-sunrise) worth a future extension, or noise-chasing?
   **Panel: unchanged — future extension (§14).**
6. **DST and civil-midday edges.** Queensland has no DST — pinned site
   fact — so the civil/solar offset is seasonal drift only. But the
   window helpers are DST-honest for OTHER sites, and a DST site's civil
   midday jumps an hour twice a year against a solar morning that does
   not. The design's answer (midday is the operator's wall-clock finish
   line, re-ruled on migration) is a policy answer to a geometry
   question; flag it if this product ever leaves the no-DST latitudes.
   The panel added the baseline's own caveat (A12): the load baseline
   reads same-slot-last-week (168 h), and on a DST site the week
   straddling a transition aligns civil slots an hour off their solar
   morning — the p10/deficit conservatism absorbs it, and a migration
   re-rules `midday_local` at the same moment the baseline drift is
   largest. Recorded here; Queensland's no-DST pin stands.
7. **The conservatism stack's compounding.** Four conservative terms
   (p10, full-deficit, η, floor) multiply: on a marginal day the target
   hits the ceiling and the feature silently becomes v1 — correct, but
   the SAVINGS evaporate in exactly the volatile weather where the
   operator most wants to know. Should the projection carry a
   decomposition line ("ceiling-bound: floor + deficit, not forecast")
   so the invisible-v1 night is at least legible as such?
   **Panel: promoted — the decomposition line and the 95-vs-100 clause
   are tile scope now (§7 `ceiling_bound_by`, §8, A10), struck from §14.**
8. **The BMS's own morning.** rhs's dynamic charge limit reads 0 W when
   full — the pods' own autonomy may REFUSE morning solar the forecast
   credited, or accept it slower than η assumes. No write of ours is
   involved (correctly), but the landing evidence should distinguish
   "forecast missed" from "battery would not take it" before the
   scoreboard charges either to the forecast's account.
   **Panel: answered — the recorded basis is pre-battery and the landing
   carries the refusal-vs-miss attribution (§3.2, §8, A1).**

## 13. Operator decisions this package needs (verbatim-ready)

1. **Capacities:** "Confirm the usable capacity per battery — lhs/mid
   ~5.0 kWh, rhs ~4.2 kWh (derived from 50 vs 60 cells, unmeasured).
   rhs's figure matters most: understating it under-charges."
2. **The floor:** "The overnight target never goes below `floor_pct`
   (proposed 50% — half-pack dawn reserve). On this array it binds on
   most sunny days; 40% trades reserve for solar utilization. Choose."
3. **The quantile:** "The target is computed from Solcast's p10 netted
   PER SLOT — that lands the fleet full on ~95–99% of days (the nominal
   90% is per-slot; the slot-wise sum compounds the conservatism), at
   some savings cost against a portfolio-level p10. p50 saves more on
   average and misses full every second day. Confirm p10."
4. **Midday:** "The finish line is 12:00 site time on the wall clock.
   Confirm, or name another."
5. **The trust gate:** "Promotion to forecast_act requires 14 scored
   days including at least 3 low-surplus and 3 high-surplus mornings,
   mean energy error ≤30%, bias within +10%/−20% — asymmetric, because
   over-forecast is the direction that under-charges (proposed). Extend
   to 28 days or loosen at your preference — the promotion is yours
   either way."
6. **Tariff keys — GATING since the panel (A3):** "Before forecast_act
   can be granted: supply the tariff keys — off-peak import and export
   FIT — and the export rate must sit BELOW the off-peak import rate.
   The feature moves stored kWh off the overnight buy onto morning
   surplus: it pays `(import − FIT)/η` per stored kWh while FIT < import,
   and on a legacy 44–52 c QLD FiT against ~12 c off-peak it LOSES ~35 c
   per stored kWh — the config validation refuses ACT on that physics.
   Staying kWh-only keeps SUGGEST running and ACT refused."
7. **The promotion act:** "After the SUGGEST run, review the trust line
   and the Insights landings, then name the date for the `forecast_act`
   config revision (tariff keys in hand per item 6; grant → supervised
   nights → steady state)."

## 14. Future extensions (documented, deliberately unbuilt)

Measured per-pod share from the historian's morning charge kWh (§2.2's
graduation); asymmetric suspend/earn hysteresis on the trust gate
(accepted P2 at the panel, §3.2); same-day morning-load revision signal
(§12 flag 5); a per-battery quantile (if per-pod forecasts ever exist —
none do); tariff-aware target shaping once the tariff keys exist (the
window itself staying fixed). Decomposition transparency on ceiling-bound
nights left this list for the tile (§8, panel A10).
