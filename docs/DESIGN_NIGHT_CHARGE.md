# Off-peak night charge — the night strategy adviser

Status: DESIGN ACCEPTED PENDING IMPLEMENTATION (2026-08-26). Owner of the wire
contract: `docs/API_CONTRACTS.md` "Off-peak night charge (the night strategy
adviser)" (the surgical amendment in this package). Pattern parents:
`docs/DESIGN_EXCESS_CHARGING.md` + `docs/DESIGN_EXCESS_ACTIVATION.md` (the
ADVISER pattern this design follows) and `docs/DESIGN_SCHEDULES.md` (the
night-partition grant this design consummates). Evidence:
`docs/NIGHT_LOAD_INVESTIGATION.md` (the measured evening matching, the 109 W
floor), the night-writer detector contract (API_CONTRACTS "Night-writer
detector" — the known −2500 W × 3 nightly Docker writer), and
`docs/DESIGN_ENERGY_SCORECARD.md` (the evidence family the demand rule reads).

This document is DESIGN ONLY. It touches no `src/`, `tests/`, `config/`, or
`web/` file. The backend agent follows §7's ordered plan (a second backend
agent is finishing the night-writer detector in `src/` — B1 does not start
before that lands); the web agent follows §7's console steps. No live-hardware
interaction is authorized by this document — the first supervised night is
step 5 of the §3 cutover and requires the §8 operator decisions, verbatim as
written there.

## 0. What this is, in one paragraph

The operator's existing Docker solution force-charges all three batteries at
2,500 W per unit from 00:00 to 06:00 nightly, dropping to a very low rate when
house demand (typically the EV) is high, so the batteries neither drain nor
cycle while expensive-in-their-terms load runs; they pay off-peak rates in
that window and do not want energy going in and out of the battery while the
EV charges. They will decommission it once ours matches it. This design
replaces it with a `NightChargeAdviser`: a strategy layer that computes a
per-battery charge plan each tick inside a commissioned window (reach the 95%
SOC ceiling by window end at ≤ the per-unit cap), submits ordinary short-TTL
`OPTIMIZER` charge intents through the existing internal advisory path, and
stands every battery down to zero-watt non-participation while MEASURED site
demand exceeds a threshold (the operator's directive, §1 — the pod returns
to its own autonomy until demand subsides or the window ends), resumes
pacing when demand falls (with hysteresis), holds at a small positive
fallback rate on bad evidence (fail-closed — the stand-down answers
measured demand, never missing data), and lets batteries already at the
ceiling sit out.
Nothing else moves: the arbiter, allocator, SafetyKernel, actor, and
authority path are unchanged in role; a night charge is an ordinary intent
judged by everything exactly as a manual request is.

## 1. The operator's spec, translated against tonight's measurements

The operator's words, and the mechanism each becomes:

| Their words | The mechanism |
|---|---|
| "force-charges the batteries at 2.5 kW all the way up to full each night between midnight and 6 a.m." | Per-battery CHARGE intents in a commissioned civil-time window (`window_local`, default `00:00–06:00` site time), each unit's rate ≤ `rate_cap_w` (default 2500, the per-battery static cap — the ask sits exactly at it). "Full" is the SOC charge ceiling `policy.max_soc_pct` (95%), not 100%. |
| "If demand goes high, like over 1,000 W, it drops to a very low charge rate" | The DEMAND RULE (§2.4): while measured site demand exceeds `demand_threshold_w` (default 1000), every participating battery STANDS DOWN — zero-watt non-participation, back on its own autonomy. |
| "I prefer it to move into standby mode, then return to an active or enabled state after the night charging period or when demand subsides" (the operator's ruling, 2026-08-24 — this is THE behavior, no selector) | The MEASURED demand stand-down (§2.4): excluded from the submission; the TTL lapse plus watchdog hand the pod back to its own autonomy; charging resumes when demand falls below threshold − hysteresis OR the window ends. Fail-closed is the safety doctrine, not a posture: bad evidence HOLDS at `hold_rate_w` (the evidence-failure fallback alone) — the stand-down answers measured demand, never missing data. |
| "so the battery doesn't drain during that time — I don't want energy going in and out of the battery while the EV is charging" | The point of a held objective is not the watts — it is the OBJECTIVE. While any intent is renewed, the PQ objective REPLACES the pod's own CT-following autonomy (the beat-autonomy doctrine, inverted for night use). The pods autonomously load-match in the evening and WILL discharge into house/EV load unless held (NIGHT_LOAD_INVESTIGATION §1.1: 0–536 W per pod, uncommanded). Under the stand-down directive this guarantee is DELIBERATELY suspended for MEASURED demand events (the row above) and preserved everywhere else — above all in the fail-closed hold, where a held `hold_rate_w` objective means: not discharging blind, not cycling, still inching up while the evidence is missing. |
| "we pay off-peak rates then" | The window is a civil-time fact in config; the import cost itself is the operator's tariff question (§6 — the scorecard prices it when the tariff keys exist). |

Tonight's measured context this is designed against (2026-08-23 evening
captures): fleet SOC 71–98% — rhs sits ABOVE the 95% ceiling (and its BMS
dynamic charge limit reads 0 W: it is full), so from the first night rhs is a
`skipped_full` non-participant and the real charge is lhs (71→95) plus mid
(88→95), roughly 1.2 + 0.35 kWh ≈ under an hour at 2,500 W each. The ~109 W
fleet standby floor is untouched by this feature (§6).

## 2. The strategy layer

### 2.1 Source: `OPTIMIZER`, pinned

The adviser submits ordinary `PowerIntent`s with `source: OPTIMIZER`, exactly
the excess adviser's class, through a new composition-internal facade twin
`submit_night_intent` (the `submit_advisory_intent` pattern verbatim: same
validation, audit, idempotency/correlation contract, never routed on REST or
MCP), source pinned, intent-id prefix `night-`, under the composed principal
`energypod:night-adviser` (scopes observe + dispatch, non-interactive,
site-bound — audit attribution separates it from every console, agent,
schedule-runner, and excess-adviser row).

Why not `SCHEDULE`, pinned:

- **Semantics.** `SCHEDULE` is the arbiter's class for a PUBLISHED,
  operator-authored fixed-watt window — a static fact renewed verbatim. The
  night charge is a STRATEGY: the rate is recomputed every tick from measured
  SOC, measured demand, live claims, and time remaining. Putting conditionals
  behind a static source inverts what both sources mean, and extending the
  schedule runner with conditionals is exactly the design this package
  refuses (the runner's own doctrine: it never checks claims, never reacts —
  it renews a published fact).
- **Precedence shape.** `OPTIMIZER` sits above `SCHEDULE` and below
  `AGENT`/`MANUAL` — the correct envelope for our automation: the operator's
  manual requests outrank it per battery (their standing doctrine), and it
  outranks published schedules only with an explicit yield carve-out (§4.3),
  the mirror image of `yield_to_schedule`.
- **Economics at the corners.** `SCHEDULE` would rank the night charge BELOW
  the excess adviser by arbiter, so free surplus charging would yield
  (`yield_to_schedule`, default true) to PAID off-peak import at the dawn
  corner — the wrong direction. `OPTIMIZER` lets the design pin the correct
  one-sided rule (§4.2: the night adviser yields to the excess adviser; never
  the reverse) without touching the excess module.

### 2.2 The charge plan (per unit, per tick)

For each configured fleet unit, inside the window and while enabled:

1. **Eligibility.** A latest observation exists; lifecycle controllable
   (`ARMED_IDLE`/`ACTIVE`); authoritative SOC (`authoritative_soc_pct`, the
   BMS figure per the 2026-08-24 ruling) is a finite number; and there is
   positive charge headroom under BOTH the BMS dynamic charge limit and the
   static unit cap. Everything else is a per-unit sit-out with an honest
   reason: at/above the ceiling → `skipped_full` (zero-watt non-participation,
   never in the submitted unit set — "batteries already at the ceiling sit
   out"); dynamic limit 0 W (rhs tonight) → `no_charge_headroom` (the BMS's
   own honest refusal — the adviser does not ask what the battery just
   refused); not controllable → `units_disarmed`; parked → `unit_parked`
   (2026-08-24, `docs/DESIGN_POD_PARKING.md` §3 — outranks `units_disarmed`
   for a parked unit: resume, not arm, is the true next step).
2. **Target.** Reach `policy.max_soc_pct` (95) by window end. A unit already
   at/above it sits out for the whole window (`skipped_full`).
3. **The rate, under the pinned pacing rule** (config `pacing`, two values):

   - **`cap_first` (the DEFAULT — Docker parity).** Charge at
     `min(rate_cap_w, dynamic headroom, static cap)` until the ceiling, then
     sit out. This is exactly the incumbent's behavior; cutover parity (§3)
     is the reason it is the default — the operator decommissions when we
     MATCH it.
   - **`even` (deadline-paced — the graduation).** Each tick:
     `required_w = ceil((target_soc − soc)/100 × assumed_capacity_wh × 3600 /
     remaining_s)`, clamped to `[1, rate_cap_w]`, where `remaining_s` is the
     time to window end in the plan's zone. Recomputed from MEASURED SOC every
     tick, so it self-corrects: an optimistic capacity estimate or a demand
     hold pushes `required_w` up next tick, and the rule converges to `cap`
     exactly when behind — there is no separate escalation mode. Minimizes
     time at high charge (the pacing point) at identical kWh. Requires the
     per-unit `assumed_capacity_wh` map (key set exactly the fleet units;
     ~5000-class on this fleet — the estimate only shapes pacing, never
     safety, and a wrong estimate surfaces as early completion or a visible
     `deadline_at_risk`, not as a hazard).

   Both rules produce one whole positive watt per participating unit, summed
   into the intent's fleet total — the `watts_by_unit` dual form, per-battery
   watts native (the 2026-08-23 operator ruling), per-unit targets as CAPS at
   allocation exactly as everywhere else.

### 2.3 The window

`window_local` is a civil-time pair (`["HH:MM","HH:MM"]`, cross-midnight
allowed) in the block's required `timezone` (an IANA name; the off-peak
window is a tariff/civil-time fact the operator confirms — §8 item 1). The
adviser's per-tick evaluation is civil-time containment in that zone,
DST-honest via `zoneinfo` (a DST-transition night is 23 or 25 h; the window
is defined by wall clocks, and the projection's `ends_at` carries the zone's
offset). Two pure helpers (`in_window(local_now)`, `next_window_start(at)`)
are the single implementation of every countdown — no client reimplements
civil-time arithmetic (the schedule helpers' doctrine).

### 2.4 The demand rule — source, threshold, hold, hysteresis

**The source of truth is the per-pod LOAD CT words (`load_power_w`,
0x1000+20), NOT the grid words — pinned, and this is the design's one
non-obvious correctness catch.** The grid word includes OUR OWN charging draw
(evidence: the 2026-08-22 capture, mid idle — grid −1736 W against load CT
1701 W; when a pod charges at X W, grid import rises by ≈X while the load CT
does not move). A demand rule on the grid word would read a 3 × 2500 W charge
as 7.5 kW of "demand" and hold forever — the feature could never charge. The
load words exclude our own charge and carry exactly what the operator means
by demand: house load, including the EV. They are the same evidence family
the scorecard trusts: our own CT integration source, per-field quality,
control-rate under the PCS-block promotion, gaps/failures never interpolated.

- **Measurement.** `demand_scope: fleet` (the DEFAULT — the operator's rule
  was global, and cutover parity wants the global stand-down): `demand_w =
  max(0, floor(Σ_units load_power_w))`. `demand_scope: per_phase` is the
  config option (the graduation: only the pod(s) whose phase shows the demand
  hold; the others keep charging) — cheap, same evidence walk, per-unit
  thresholds.
- **Evidence quality, fail-closed to HOLD.** Every participating-relevant
  unit's load word must be finite, quality GOOD, and no older than
  `demand_telemetry_max_age_s` (default 3.0 s; validated >
  `control_period_s + essential_read_timeout_s`, the excess bound's rule). The
  rollup is worst-word-wins (`missing > bad > stale`, the excess rollup's
  precedence) and carried as `demand_evidence`. A non-good rollup ⇒ HOLD —
  the opposite polarity from the export bound's fail-closed-to-zero, for the
  opposite reason: there, bad evidence must stop a charge; here, bad evidence
  must PRESERVE the no-cycling guarantee (charging blind into an EV at 7 kW
  is the exact outcome the operator refused). The hold-on-bad-evidence state
  is loudly visible (`demand_evidence_stale` etc. on the tile), so it can
  never be a silent never-charges.
- **Threshold and the ONE response.** Engage when `demand_w >
  demand_threshold_w` (default 1000, the operator's own figure; the ~109 W
  fleet standby floor is far below it — no false engages on an idle night).
  The response to a MEASURED engage is the STAND-DOWN (the operator's
  directive 2026-08-24, the one behavior — there is deliberately NO
  posture selector): zero-watt non-participation — the unit is EXCLUDED
  from the submission (the 6abd869/d2163a5 doctrine; the facade refuses a
  zero-watt per-unit target, so exclusion is the only honest spelling),
  the remove-then-submit renewal drops it, and the TTL lapse plus the
  ~3.5-4.0 s watchdog hand the pod back to its own autonomy until demand
  falls below the exit bound OR the window ends (non-renewal, exactly the
  window-end hand-back). The stand-down state is its own phase word
  (`standing_by_on_demand`, fleet and unit) with the `demand_above_threshold`
  code. HONEST TRADE, stated as such: during a stand-down the pod's own
  load-matching autonomy serves part of the house demand, so a battery MAY
  discharge into the spike — the operator's chosen behavior, replacing the
  original design's positive-charge hold.
- **Hysteresis (no flapping).** Resume pacing only when
  `demand_w < demand_threshold_w − demand_exit_hysteresis_w` (default 200;
  validated `0 < hysteresis < threshold`). A load oscillating around 1000 W
  must not toggle the stand-down every tick — each toggle rides the
  kernel's ramp limiter and re-prices the projection. The hold latch is
  adviser state, reset at window open and window close.
- **What resuming does mechanically.** When demand falls back below the
  exit bound, the tick recomputes the paced rate and the unit rejoins the
  submission (under `even` pacing the pause has already raised
  `required_w` — self-correcting).
- **`hold_rate_w` — the EVIDENCE-FAILURE fallback alone, never a demand
  behavior.** The fail-closed arm above (missing/bad/stale ⇒ HOLD at
  `hold_rate_w`) is the one behavior that still charges at a held rate:
  a small POSITIVE charge (default 100 W; never zero, never a discharge —
  zero-watt/idle submissions are refused by the standing doctrine and
  would hand the pod back to matching autonomy on exactly the evidence it
  cannot see). This is the SAFETY DOCTRINE, not backward compatibility:
  the stand-down answers MEASURED demand, never missing data, so a stale
  word must never silently free-run a fleet into autonomy drain during an
  EV night — it holds, loudly, until a GOOD word returns.

### 2.5 The tick

One bounded, suppressed tick per fleet cycle (the adviser-step pattern;
`CancelledError` never swallowed; a failure is survivable per cycle and never
halts the fleet; the TTL lapse plus the firmware watchdog are the designed
hand-back), composed AFTER the excess adviser and BEFORE the energy
accountant and kernel tick:

```
heartbeats → polls → recovery pass → foreign-objective pass
          → schedule runner → excess adviser → NIGHT ADVISER → energy accountant
          → kernel tick
```

Ordering rationale, pinned: published facts first (schedule), then the
opportunists in economics order — FREE surplus before PAID import — so the
excess adviser's same-cycle claim is already in the active set when the night
adviser looks, making the dawn-corner exclusion (§4.2) deterministic within
one cycle. No change to any existing step's order.

Per tick:

1. Outside the window / disabled / unacknowledged (§3): withdraw-if-held,
   idle (`outside_window` / the participation code). Never submit.
2. Read the plan's units' latest observations; compute eligibility, the
   demand rollup, and each participating unit's rate (§2.2, §2.4).
3. Read `intents.active(now)`; apply the per-unit deferrals (§4): any live
   emergency stop → withdraw entirely; otherwise EXCLUDE from this tick's
   submission every unit claimed by a live MANUAL, AGENT, SCHEDULE, or
   not-own OPTIMIZER intent. Excluded units sit out this tick (zero-watt
   non-participation — the 6abd869/d2163a5 doctrine) and rejoin the next
   tick the claim is gone; exclusion at submission is what keeps the
   equal-priority OPTIMIZER corner (the excess adviser) from becoming a
   newest-revision tie flap.
4. Maintain EXACTLY ONE held intent (the remove-then-submit discipline, the
   held-id invariant a named test): no participating units → remove-if-held,
   idle; else remove the previous, submit fresh — `direction: CHARGE`,
   `unit_ids` = the participating set, `watts_by_unit` = each unit's rate,
   `ttl_s: intent_ttl_s` (default 10.0; > `control_period_s`, ≤ 300 s,
   validated).
5. Window end (or entry disable, or participation loss) is NON-RENEWAL:
   remove, then the TTL lapse and the ~3.5–4.0 s watchdog return each pod to
   its own autonomy (daytime PV self-charge). No stop triple, no idle intent,
   ever.

## 3. Config, the PARTITION grant, and the cutover

### 3.1 The `night_charging:` block (block-presence doctrine)

```yaml
night_charging:
  timezone: "Australia/Brisbane"        # REQUIRED — the civil-time fact
  window_local: [["00:00", "06:00"]]    # the commissioned off-peak window
  enabled: false                        # suspended at boot; the toggle starts it
  rate_cap_w: 2500                      # ≤ policy.max_unit_charge_w (validated)
  demand_threshold_w: 1000
  demand_exit_hysteresis_w: 200         # < demand_threshold_w (validated)
  hold_rate_w: 100                      # the evidence-failure fallback (validated)
  demand_scope: "fleet"                 # fleet | per_phase
  pacing: "cap_first"                   # cap_first | even (Docker parity default)
  assumed_capacity_wh:                  # REQUIRED iff pacing: even
    {lhs: 5000, mid: 5000, rhs: 5000}
  demand_telemetry_max_age_s: 3.0       # > control_period_s + essential_read_timeout_s
  intent_ttl_s: 10.0                    # > control_period_s, ≤ 300 s
```

- **A PRESENT block composes** the machinery: the adviser, the
  `night_charge_state` projection, the `night_charge.state_changed` events,
  the guarded toggle route, and the PCS live-block promotion (§3.3). **An
  ABSENT block composes NOTHING** — byte-identical to today: no adviser, no
  projection key, and the toggle answers 409 `night_charging_not_commissioned`.
  Symmetric with the excess feature; the night can never run on a site that
  did not commission it.
- **`enabled: false` is the DEFAULT and the activation doctrine is the excess
  package's verbatim**: participation gates bind to block-PRESENCE (a disabled
  block that could never be enabled safely is refused at validation);
  `enabled: true` participates at boot subject to the acknowledgement latch;
  the runtime toggle flips PARTICIPATION only, never a cap, window, pacing
  choice, or gate; runtime state does NOT persist — boot recomposes from the
  config file (`enabled_origin: "config" | "runtime"` is the honest
  until-restart marker).
- **Cross-validations (bind to block-PRESENCE, the excess pattern):**
  `mode: write_enabled` AND a `policy` block present; `rate_cap_w ≤
  policy.max_unit_charge_w`; `0 < hold_rate_w < rate_cap_w`;
  `0 < demand_exit_hysteresis_w < demand_threshold_w`;
  `demand_telemetry_max_age_s > timing.control_period_s +
  timing.essential_read_timeout_s`; `0 < intent_ttl_s ≤ 300` and >
  `timing.control_period_s`; `assumed_capacity_wh` present with key set
  exactly the fleet units iff `pacing: even`; and the PARTITION grant (below).
  Every message names its rule.

### 3.2 The PARTITION grant — this feature is the reason to grant it

The night window belongs, by the operator's own environment fact and the
shipped schedule posture, to the site's external writer applications — today,
the Docker solution. A controller that holds PQ objectives at night becomes a
second `0x0200` writer in their window, and the objective register is
last-writer-wins (it doubles as the legacy force-state word). The grant is
therefore REQUIRED, and it uses the two existing mechanisms verbatim —
nothing new is invented:

1. **The config revision.** The union of `schedule.allowed_windows_local`
   (a PRESENT `schedule:` block — the policy's single home) must cover the
   `night_charging.window_local` ENTIRELY. This is a config-time
   cross-validation on the `night_charging` block: commissioning night charge
   and granting the partition are ONE deliberate revision (edit both blocks,
   restart). The refusal names the path: "widen `schedule.allowed_windows_local`
   — the night window belongs to the site's other writer applications until
   the partition is granted". The site's derived posture flips to
   `partition`, which GET /api/v1/schedule and `schedule_state` already
   report; the night projection carries it too. For the record the grant on
   this site reads `allowed_windows_local: [["00:00", "20:00"]]`.
2. **The durable night acknowledgement.** The one-time fact the schedules
   surface already defined — `schedule_night_windows_acknowledged` (durable
   audit, keyed existence check at boot, never re-prompted, durable-append-
   FIRST: an audit failure refuses the act) — with a SECOND capture path:
   the first `enable` of night charging (boot-config or toggle) carries
   `"night_posture": "PARTITION_ACKNOWLEDGED"` unless the site already holds
   the fact (either surface's capture counts — it is one site fact; the
   event id is historical and unchanged, durable facts never rename). The
   assertion text is §8 item 2 verbatim. Refusal:
   409 `night_acknowledgement_required`, `details:
   {"acknowledgement": "PARTITION_ACKNOWLEDGED"}`. An unacknowledged site
   composes SUSPENDED with reason `night_acknowledgement_required` — even
   `enabled: true` cannot participate without the fact.

The arm-time sole-writer preflight is untouched and remains the STRUCTURAL
enforcement under PARTITION: if a writer does not stand down, an arm latches
`external_writer` and the console says so honestly — the partition being
enforced, not the controller fighting. (Timing note: with the commissioned
`autonomous_charge_signature_max_w: 2500`, a mid-charge arm at 03:00 reads
the Docker −2500 W objective as `pod_autonomy` and PROCEEDS by beat-autonomy
— coordinate, don't fight — but the cutover arms in a Docker-free interval
by preference.)

### 3.3 The PCS-block promotion (composition detail, load-bearing)

The demand rule reads `load_power_w` at control rate. The PCS live block
(`0x1000`: grid +17, load +20) is promoted into the control-rate core today
when the `excess_charging` block is PRESENT; on the cold ring it serves only
every ~96–108 s — `demand_telemetry_max_age_s: 3.0` would then classify
every read stale and the feature would HOLD forever (an honest but useless
commissioning). Composition therefore promotes the PCS block when EITHER
block is present — the same budgeted plan (steady ≤ 8 windows + probe,
inside the commissioned control period), a one-line composition predicate,
no new tier.

### 3.4 The cutover sequence, pinned step-by-step

1. **GRANT (one deliberate config revision).** Add the `night_charging:`
   block (`enabled: false`) AND widen `schedule.allowed_windows_local` to
   cover the night window; restart (boot disarmed; the adviser composes
   suspended; posture now `partition`). Commissioning check, stated honestly:
   the shipped example's `max_fleet_charge_w: 6000` caps a simultaneous
   three-pod 2,500 W charge (7,500 W — what Docker ran nightly, so the wiring
   takes it); widen to 7500 for exact parity or keep 6000 as a deliberate
   ceiling — the kernel clamps per direction honestly either way and the
   tile shows the clamped rates.
2. **ARM.** The operator arms the units once — `POST /api/v1/arm
   {"unit_ids": [...], "confirmation": "ARM"}` (arm scope + interactive; the
   runner can NEVER self-arm). Arm in a Docker-free interval (after a 06:00
   clear, or after step 3). The armed epoch PERSISTS across windows and
   nights — until the next controller restart: boot is disarmed, and after
   EVERY restart somebody must re-arm before the next window or it opens
   onto `units_disarmed` and the tile says so (§5; the S3 finding, designed
   in here from day one).
3. **STAND DOWN Docker.** The operator stops/disables the Docker solution's
   night schedule on their side. The stand-down agreement IS the captured
   acknowledgement's assertion (§8 item 2) — the durable fact and the
   operational act must agree.
4. **ENABLE.** `POST /api/v1/night-charging {"action": "enable",
   "confirmation": "NIGHT", "night_posture": "PARTITION_ACKNOWLEDGED"}` —
   the first enable captures the durable fact. Runtime-only until the next
   convenient restart; then flip `enabled: true` in the config to make it
   standing (the excess two-step verbatim).
5. **VERIFY one supervised night.** Watch `night_charge_state` (pacing →
   `standing_by_on_demand` when the EV runs → resume → `complete`), the audit
   trail (`night-` intents accepted and authorized under
   `energypod:night-adviser` + `optimizer`), measured battery watts ≈
   commanded, and the scorecard's `grid_import_kwh`/charge figures against
   the plan. ABORT criteria — any one: any unit latching INHIBITED
   (especially `external_writer` — Docker not stood down; that is the
   partition being enforced, and the answer is step 3, not a fight); measured
   charge exceeding plan + 10% for more than two cycles; `demand_evidence`
   non-good persistently (a stuck hold — investigate, do not override); a
   fleet halt; operator command. Abort = disable the toggle (TTL lapse →
   watchdog → autonomy) or the emergency stop if power persists.
6. **DECOMMISSION Docker.** After the acceptance night(s), the operator
   removes the Docker solution. **The night-writer detector is the
   decommissioning verifier**: on subsequent nights
   `GET /api/v1/objectives/observed?last=24h` must show a quiet window — our
   commanded units are not sampled (claimed), the unclaimed units show only
   in-band pod autonomy, and ZERO `expected_nightly_charge` or
   `foreign_objective_observed` classifications appear in 00:00–06:00. Any
   `expected_nightly_charge` signature after stand-down is Docker still
   writing — a real second-writer fight, escalated visibly.
7. **CLOSE the detector's expectation.** After decommissioning, remove
   `foreign_objective_expected_charge_w` from the policy (config revision):
   the "expected nightly writer" is gone, and from that night any
   synchronized −2500 W charge we did not command is FOREIGN again.

## 4. Precedence and interplay, pinned

1. **Manual requests outrank per battery (existing, unchanged).** The
   arbiter's `emergency_stop > manual > agent > optimizer > schedule` already
   displaces the night adviser per unit; the adviser's own tick additionally
   EXCLUDES claimed units from its submission (no futile renewal spam) and
   they rejoin the cycle after the claim lapses. A night-time manual request
   on one battery never disturbs the other two (concurrent per-unit
   operation).
2. **The excess-solar adviser: dormant at night by physics; the dawn corner
   pinned.** Export exists only in daylight; the night window is darkness —
   the two are complementary by physics, as pinned for `yield_to_schedule`.
   The corner is a summer dawn (Brisbane sunrise ~04:45) with the window's
   tail overlapping real export, both advisers OPTIMIZER, potentially the
   same needy unit. The rule is ONE-SIDED by economics: FREE surplus outranks
   PAID import — the NIGHT adviser excludes units claimed by a live
   not-own OPTIMIZER intent (the excess adviser; identified by claim at tick
   time under the pinned ordering, §2.5), and the excess adviser is
   deliberately UNCHANGED (no new yield set, no edit to `excess_charge.py`).
   Deterministic, no equal-priority newest-revision flapping.
3. **Schedules vs the adviser: the adviser IS the night mechanism; schedules
   stay day. Recommended and pinned.** Rationale: (a) a night schedule at a
   fixed 2,500 W is precisely the Docker behavior MINUS the demand rule —
   the operator is replacing it because they want the rule; (b) two night
   mechanisms on one window is two writers of the same minutes — the
   invisible-starvation class (an OPTIMIZER adviser outranking a SCHEDULE
   window by arbiter, silently); (c) the schedule posture gate stays
   day-only and simple. Operationally: do NOT publish night schedules; if a
   night schedule is ever published anyway, the night adviser ALWAYS yields
   per unit to the live SCHEDULE claim — the published fact beats the
   opportunist, the same sentence the fleet-cycle ordering already encodes.
   There is deliberately NO opt-out flag here (the excess feature's
   `yield_to_schedule: false` exists to preserve prior behavior; this
   feature has no prior behavior to preserve, and refusing the flag refuses
   the starvation class at birth).
4. **The 95% ceiling and the BMS's own limits.** The adviser's skip is
   eligibility sugar; the kernel's `soc_above_charge_ceiling` and the BMS
   dynamic 0 W charge limit remain the honest refusals and the backstop. A
   full battery refuses — the projection shows `skipped_full` /
   `no_charge_headroom`, never a fabricated target.
5. **The emergency stop.** A latched stop claims its units and dominates the
   whole cycle while it holds; the night adviser withdraws entirely and
   re-plans the tick after acknowledgement (the window may still complete).
6. **The awareness layer, honestly.**
   - **Coherence watchdog:** at cap rates the charge is confidently
     actuating (movement ≥ max(50%, 150 W)) and normally protected; at HOLD
     rates (50–150 W) the movement sits inside the watchdog's dead zone — it
     REFUSES TO CONCLUDE: no false incoherence alarms, and equally no
     protection — a silently failed hold is invisible to this watchdog by
     design. The hold's verifier is the scorecard's measured charge and the
     projection's commanded figures, not the watchdog.
   - **The autonomy band:** `unexpected_autonomy` fires only while NO intent
     claims the unit — during the window the adviser's intent always claims
     its units, so no events; the commanded −2500 W sits inside the
     commissioned band's −2600 edge anyway, and the widened +1000 positive
     edge keeps the pods' own evening matching quiet. This feature adds no
     awareness noise. If the adviser's renewal lapses, autonomy resumes —
     and the matching discharge that returns IS the honest evidence the hold
     was lost.
   - **The arm requirement (the operational reality, stated):** the runner
     cannot self-arm; arming is an arm-scope interactive human act; boot is
     disarmed; the armed epoch persists across windows but falls on restart.
     Nightly operation therefore has a standing human ritual: re-arm after
     every controller restart. The projection's `units_disarmed` reason and
     the tile's sentence exist so the ritual is never discovered by trying
     (the S3 finding from the operator's own schedule test).

## 5. Events and projection (the adviser-state pattern, mirrored)

### The `night_charge_state` snapshot projection

Feature-detected on the snapshot top level beside `intent`, `adviser_state`,
and `schedule_state`: ABSENT when the block is absent, present whenever it is
composed (including suspended). ONE writer — the fleet loop's post-tick
update through the `NightChargeController` (the `ExcessAdviserController`
mirror); `active` derives from `held_intent_id`, never a lifecycle guess:

```json
"night_charge_state": {
  "enabled": true,
  "enabled_origin": "runtime",
  "acknowledged_partition": true,
  "posture": "partition",
  "active": true,
  "phase": "pacing",
  "window": {"start_local": "00:00", "end_local": "06:00",
             "timezone": "Australia/Brisbane"},
  "window_ends_at": "2026-08-27T06:00:00+10:00",
  "window_ends_in_s": 5341,
  "next_window_at": null,
  "pacing": "cap_first",
  "rate_cap_w": 2500,
  "hold_rate_w": 100,
  "demand_scope": "fleet",
  "demand_threshold_w": 1000,
  "demand_w": 412,
  "demand_evidence": "good",
  "held_intent_id": "night-881-77123.445101",
  "units": [
    {"unit_id": "lhs", "soc_pct": 71.4, "phase": "pacing",
     "target_w": 2500, "reason": "on_plan"},
    {"unit_id": "mid", "soc_pct": 88.0, "phase": "pacing",
     "target_w": 2500, "reason": "on_plan"},
    {"unit_id": "rhs", "soc_pct": 98.0, "phase": "skipped_full",
     "target_w": 0, "reason": "at_ceiling"}
  ],
  "last_action": "renew",
  "last_tick_at": "2026-08-27T01:31:05+10:00",
  "reason_codes": ["window_open", "on_plan"]
}
```

- **`phase` (fleet, ONE vocabulary)**: `idle` (outside the window, or
  suspended/disabled), `holding_on_demand` (the FAIL-CLOSED evidence hold:
  a missing/bad/stale word holding units at `hold_rate_w`),
  `standing_by_on_demand` (measured demand has stood at least one unit down
  to zero-watt non-participation), `pacing` (at least
  one unit charging at a planned rate), `complete` (window open; every
  participating target reached; nothing charging), `skipped_full` (window
  open; every unit sat out from the start). Precedence in that order except
  `complete`/`skipped_full`, which are mutually exclusive by whether
  anything charged.
- **Per-unit `phase`**: `pacing | holding_on_demand | standing_by_on_demand |
  skipped_full | complete | sitting_out` (claimed, disarmed, or no headroom
  this tick) with its `reason`.
- **`demand_evidence`**: `good | missing | bad | stale`, worst-word-wins; a
  non-good rollup forces `holding_on_demand` (or idle when suspended) and the
  matching reason code.
- **`reason_codes` (ONE pinned vocabulary)**: `outside_window`, `window_open`,
  `on_plan`, `deadline_at_risk`, `demand_above_threshold`,
  `demand_below_exit`, `demand_evidence_missing`, `demand_evidence_bad`,
  `demand_evidence_stale`, `at_ceiling`, `no_charge_headroom`,
  `target_reached`, `no_eligible_units`, `units_disarmed`, `unit_parked`,
  `yielding_to_higher_priority`, `disabled_by_config`, `disabled_by_runtime`,
  `night_acknowledgement_required` (`unit_parked` added 2026-08-24,
  `docs/DESIGN_POD_PARKING.md` §3 — additive, and it outranks
  `units_disarmed` for a parked unit: resume, not arm, is the true next
  step).
- Countdowns (`window_ends_in_s`, `next_window_at`) are snapshot-derived; the
  pure window helpers are the single implementation (§2.3).

### Bus events — `night_charge.state_changed`

The exact `excess_adviser.state_changed` mechanics: published when the
semantic tuple changes — `(enabled, enabled_origin, acknowledged_partition,
active, phase, active_unit_ids, demand_evidence, reason_codes)`; the watt and
SOC figures ride every publication but never trigger; a full payload
republishes every 30 s (`"heartbeat": true`) while enabled; NOTHING while
disabled (the disable-carrying state_changed is the last event; a
boot-composed disabled site never publishes — the first snapshot frame
carries the projection). Payload: the projection subset minus
`last_action`/`last_tick_at`, plus `"heartbeat": bool`.

### Audit

`night_charging_toggled` (result enabled/disabled/noop; the Impl-10
commit-then-audit pattern), the shared durable-once
`schedule_night_windows_acknowledged` (§3.2), and — unchanged class — the
per-tick `intent_accepted` rows the internal submit path already writes (the
same cadence the two existing runners produce; complementary by physics).

## 6. Economics and honesty

- **The no-cycling guarantee, restated under the stand-down directive.**
  Inside the window the strategy is CHARGE-ONLY: it never submits a
  discharge, and while it is PACING or FAIL-CLOSED-HELD the renewed
  objective (a positive charge) replaces the pods' own load-matching
  autonomy, so the batteries neither drain into the EV nor cycle. The
  MEASURED demand stand-down is the one deliberate suspension: the pod is
  handed back to its own autonomy for the demand event and MAY serve part
  of the spike (the operator's directive, 2026-08-24, accepted as such);
  bad evidence never takes that arm — it holds. The guarantee is bounded
  by the renewal cadence: if the adviser dies mid-window, the TTL
  (≤ 300 s, default 10 s) and the ~3.5–4.0 s watchdog hand the pod back to
  autonomy for the gap — the designed fail-safe, stated as the gap it is.
  Window END is exactly that hand-back, on purpose (daytime self-charge
  from PV resumes).
- **The import cost is the operator's tariff question.** The window charges
  real energy from the grid at the off-peak rate (tonight's fleet needs
  ≈ 1.6 kWh; a depleted fleet ≈ 3.75 kWh + losses). The scorecard's
  `grid_import_kwh` per day measures it from day one; the MONEY line appears
  when the operator supplies the tariff keys (`energy_scorecard.tariff`,
  currently unset — §8 item 4). This design takes no position on the rate.
- **The standby floor is untouched.** The ~109 W fleet residual
  (NIGHT_LOAD_INVESTIGATION) persists — a separate, still-open question this
  feature neither fixes nor worsens.
- **Pacing economics.** `even` pacing shifts the same kWh to lower rates for
  longer (less time at high charge) at no energy cost; `cap_first` is parity.
  The choice is the operator's, per §8.

## 7. Implementation plan (ordered)

Backend agent, in order (each slot lands its own red→green cycle; B1 does
not start before the night-writer detector work in `src/` lands; run ONLY
the named files while the live controller runs):

1. **B1 — Config** (1 slot): `NightChargingConfig` in
   `src/energypod/runtime/config.py` — the §3.1 keys, every §3.1
   cross-validation (including the PARTITION-coverage check against the
   schedule policy and the capacity-map rule), `ControllerConfig.
   night_charging: NightChargingConfig | None = None`. Red family:
   `tests/unit/test_config.py` (the night block matrix: block gates,
   partition coverage refusal naming the widening path, hold/hysteresis/threshold
   ordering, ttl/freshness bounds, capacity keys iff even, absent-block
   identity).
2. **B2 — The strategy module** (2 slots): new
   `src/energypod/application/night_charge.py` — the pure helpers
   (`in_window`, `next_window_start`, the demand rollup with the
   worst-word-wins classifier, the per-unit plan under both pacing rules),
   `NightChargeAdviser` (the §2.5 tick against injected ports: clock,
   observations, intents, submit), and `NightChargeController` (the
   projection/event mirror: participation + acknowledgement latch, single
   writer, semantic-tuple throttle, 30 s heartbeat). Red family: NEW
   `tests/unit/test_night_charge.py` (plan math both pacings incl. the
   even-pacing self-correction after a hold; ceiling/headroom/disarmed
   skips; demand engage/exit hysteresis; fail-closed HOLD on each evidence
   word; per-unit exclusion under every claim class; one-held-intent
   invariant; window-end non-renewal; the load-word-not-grid-word pin: a
   full-rate charge must NOT trip the demand rule).
3. **B3 — Facade** (1–2 slots): `submit_night_intent` (the
   `submit_advisory_intent` twin: source pinned OPTIMIZER, `night-` prefix,
   audit/publication contract, never routed) and `set_night_charging` (the
   guarded toggle; the shared durable acknowledgement capture,
   durable-append-FIRST, keyed boot load; the §3.1/§3.2 refusal shapes).
   Red families: `tests/unit/test_service_facade.py`,
   `tests/unit/test_facade_audit_content.py` (the ack captured once via
   EITHER path; audit failure refuses).
4. **B4 — REST** (1 slot): `POST /api/v1/night-charging` (arm scope;
   interactive additionally to enable; Idempotency-Key; the 422/409
   envelopes: `night_charging_not_commissioned`,
   `night_acknowledgement_required`, `night_enable_refused` (details:
   reasons + unit_ids/stop_ids — manual/agent/schedule claims and latched
   stops refuse; latched INHIBITED does not; disable never refused); 200
   carries `persisted: false` spelled and the projection for optimistic
   adoption). Red families: `tests/api/test_rest_contract.py`,
   `tests/api/test_boundary_hardening.py`.
5. **B5 — Composition, projection, events** (1–2 slots): block-presence
   wiring (absent = byte-identical); the PCS promotion predicate widened to
   either-block; the adviser step composed AFTER the excess adviser
   (ordering a named test); `night_charge_state` single-writer projection;
   `night_charge.state_changed` publication; the boot posture/ack gate. Red
   families: `tests/unit/test_composition.py` (composition, ordering,
   projection writer, absent-block byte-identity, the promotion predicate),
   `tests/api/test_event_contract.py` (transitions + heartbeat, nothing
   while disabled).
6. **B6 — Simulator, example config, docs, suite** (1 slot): the simulator's
   demand scenario (the existing `script_load_power_w` hook driven by a
   scripted night: an EV spike engaging/releasing the hold; the golden SOC
   model already moves SOC under charge); the commented `night_charging:`
   block + the grant example in `config/config.live-write-example.yaml`; the
   `docs/CONTINUITY.md` entry; full-suite green. Red family:
   `tests/simulator/test_simulated_pod.py` (the night scenario block).

Console agent, in order (all feature-detected on `night_charge_state`; an
absent key hides everything): **W1** the wire model, fixtures, and snapshot
adoption (`web/src/app/` — the excess tile's data-plane pattern) → **W2**
Home's Night tile (phase sentence, per-battery target/SOC rows, the demand
reading + evidence word, window countdown; the `units_disarmed` sentence
rendered as the arm instruction it is) + the Insights line (the nightly
import kWh, money when tariff keys exist) → **W3** the toggle + the
typed-confirmation dialog (the 409 IS the routing — the EXCESS flow; the
partition assertion §8 item 2 verbatim, required checkbox, resend with
`"night_posture": "PARTITION_ACKNOWLEDGED"`) + Activity rows
(`night_charging_toggled`, the one-time acknowledgement) + the
`night_charge.state_changed` event case. Red families:
`web/src/views/home/HomeView.test.tsx` (tile states incl. holding/disarmed),
`web/src/app/useConsoleData.test.tsx` + `SharedDataPlane.test.ts` (event
case, feature detection), `web/src/views/activity/ActivityView.test.tsx`.

**Risk notes (pinned for both agents):**

- The demand rule reads the LOAD words, never the grid words — a grid-word
  implementation self-holds forever at cap rates; the B2 pin test names it.
- The adviser never submits idle/zero-watt intents and never issues stop
  triples; hand-back is non-renewal, always.
- Exactly one live `night-` intent, ever (the held-id invariant is a named
  test); per-unit exclusion happens at SUBMISSION time, never by idle
  "placeholder" intents.
- `hold_rate_w` is the EVIDENCE-FAILURE fallback rate, a POSITIVE charge;
  a zero fallback hands the pod back to matching autonomy on exactly the
  evidence it cannot see — the opposite of the safety doctrine — and the
  config validation refuses it.
- Absent-block behavior is byte-identical to today — the same discipline as
  every feature-detected surface before it.
- The console can never widen the partition or touch the window/caps; the
  ungranted or unacknowledged states render their config paths, never a
  toggle that pretends.

## 8. Operator decisions this package needs (verbatim-ready)

1. **The off-peak window:** "The off-peak window is 00:00–06:00 site time
   (Australia/Brisbane). Confirm the window and the timezone."
2. **The night partition grant:** "Stand the external writer applications
   down for the night window and grant it to the controller: widen
   `allowed_windows_local` in the controller config (a config revision and
   restart), then acknowledge once — 'the external writer applications stand
   down for the granted window; the controller owns it.' Captured once as a
   durable fact, never asked again."
3. **The cutover date and approach, including the Docker stand-down:**
   "Name the cutover date, the Docker stand-down moment (before the first
   supervised night), and who performs each side. Approve the sequence:
   grant → arm → stand down → enable → one supervised night → decommission
   Docker, verified by the night-writer detector's quiet window."
4. **Tariff keys (optional):** "Supply the off-peak import rate for the
   energy scorecard's tariff keys, or stay kWh-only — the nightly import
   cost is priced only when the keys exist."
