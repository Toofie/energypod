# Battery health watch — the nightly readiness, probe, and recovery program

Design date 2026-08-25. Status: CONTRACT v1.2 — the adversarial panel's
verdict was IMPLEMENTABLE WITH AMENDMENTS (nothing unsafe found); the
fifteen panel amendments (A1–A15) and the operator-evidence amendment
(A16, v1.2: the phase-concentrated-load fact, the phase-relative census,
and the load-sharer interlock) are folded into the sections where they
landed, with the two panel rulings recorded verbatim in the amendment
log (§20). Authored before any implementation per the contract-first
doctrine.
Parents: `docs/DESIGN_POD_PARKING.md` (the standby primitive and every guard
around it — this design COMPOSES that machinery, it never forks it),
`docs/DESIGN_NIGHT_CHARGE_V2.md` (the staged-commissioning and config-gating
pattern), `docs/PROTOCOL_EVIDENCE.md` (the register truth),
`docs/evidence/standby-cycle-2026-08-24.md` (the live standby-cycle proof),
the shipped machinery (`src/energypod/application/recovery.py`, `parking.py`,
`actor.py`, the excess/night adviser pattern), and the two operator-commissioned
research rounds whose findings §1 carries.

This document is DESIGN ONLY. It touches no `src/`, `tests/`, `config/`, or
`web/` file, authorizes no live-hardware interaction, and grants no new
authority by itself: every actuation in it is gated behind its own config
block that is ABSENT by default, and the one standing doctrine it revises
(alarm-only `0x8000`) is revised by a named, separately-commissioned stage
whose default posture writes nothing, ever.

## 0. What this is — and is not

Since April, batteries on this fleet have intermittently gotten STUCK: the BMS
reports high SoC while the pod neither charges from solar nor serves load nor
obeys remote commands — a spectator at full charge. The operator has
commissioned a nightly maintenance program: at ~23:00 local, in the idle hour
before the 00:00–06:00 night-charge window, per battery — verify readiness,
run a bounded actuation test, and for flagged batteries attempt recovery via
one standby cycle, alerting with evidence on failure. One recovery attempt.
Never thrash.

The program is three separately-commissioned stages, strictly ordered:

- **Stage C — census** (zero writes): read the mode words, CT words, SoC and
  battery watts per unit, evaluate the stuck-signature predicates against the
  trailing evidence window, and record the verdict as an audit fact plus a
  notice-tier projection. Safe to commission on any site, alone.
- **Stage P — probe**: for each healthy-and-idle unit, one bounded actuation
  probe through the sanctioned PQ path — a small, fixed, discharge-direction
  command with a sustained-window pass/fail judgment we define (no public
  standard exists), echo verification, cancel, and return-to-baseline proof.
- **Stage R — recovery** (THE doctrine revision): for units the census flagged
  AND the probe failed — one standby cycle per unit per night via the
  commissioned park/resume primitives, followed by a probe re-run to prove
  command-following; on failure, a defined ladder ending in the terminal
  `write_unverified` posture plus the defined-restart advisory. NEVER a second
  attempt that night.

**What this is not.** It is not a vendor-recommended routine: no public
source endorses nightly standby cycling as prevention, and this contract says
so — the nightly cycle is an operator-directed recovery and maintenance act,
evidence-supported as benign (live-proven exactly once, on one unit, on this
fleet), not a vendor practice. It is not a wedge-fighter: the program never
contests a foreign writer, never takes over a foreign word, never re-parks
over divergence — every such case is a skip plus an alert. It is not a
substitute for the physical-restart advisory: when remote recovery is
exhausted, saying so IS the product (the standing R5 rail). And it is not
electrical isolation, ever: the fixed sentence — *"Parking is not electrical
isolation — the battery stays connected at full voltage. Never perform
physical work on a parked pod."* — rides every surface this feature adds,
verbatim, exactly as DESIGN_POD_PARKING §0 pins it.

**Values 2–6 of `0x8000` (Charge, Discharge, Circulation, Fixing SOC, Verify
Capacity) are PERMANENTLY PROHIBITED and must never appear in any write this
feature issues, on any path, under any configuration.** Stage R introduces no
new write primitive whatsoever: it drives the existing, structurally
`{0, 1}`-bound `request_debug_mode_change` mailbox operation through the
existing `ParkController`, whose transport-layer validation refuses anything
else. The whitelist is `{0, 1}` and nothing else, ever — inherited, not
restated as a hope.

One context note the operator should read once: the site's own
night-charge-to-100%-by-midday strategy is itself protective — SoC estimation
drifts at persistently high SoC, and periodic full cycles keep the calibration
valid — so the nightly watch complements a fleet that already exercises its
packs daily. The stuck class this program hunts is the pod that STOPPED
participating in that exercise.

## 1. The evidence base

Two research rounds, one local and one sourced; each finding carries the class
of evidence it is, because thresholds built on the wrong class are how the
Wave 0 bugs happened.

### 1.1 Local archaeology (vendor RE code, prior stacks, our telemetry)

| Finding | Class | Consequence in this design |
|---|---|---|
| The vendor app has NO reset/reboot command anywhere; `0x8000` standby/normal with write→readback→verify at `0x8100` is the ONLY soft-recovery register | Confirmed by vendor code | Stage R's recovery act is a standby cycle or nothing; every deeper failure goes to the physical advisory |
| The vendor app performs the mode change only as a human act; PQ commands refuse unless mode == Normal (`MiniESapp.cs:2180-2184`) | Confirmed by vendor code | The revision of alarm-only is therefore genuinely novel for this controller — staged and gated (§12) |
| Standby cycle live-proven on rhs 2026-08-24: standby write → PCS to 0.0 W with comms/telemetry/SoC alive → 76 s hold → normal write → recovery in ~1 s; 1000 W discharge tests clean BEFORE and AFTER (command-following survives the cycle) | Live-observed on this fleet | R's hold default (90 s) brackets the proven 76 s; the post-cycle probe re-run is proven meaningful, not theoretical |
| A parked pod still ACKs writes (ACK-then-ignore) | Live-observed | The probe never runs on a parked unit (skip-if), so ACK-then-ignore cannot false-pass a probe |
| Our write primitives: `actor.request_debug_mode_change` (read-prior → write → readback → verify inside ONE mailbox dispatch; value domain structurally `{0,1}`; refuses any vendor-directed prior mode); the ParkController's lease/epoch/CAS/durable-first machinery; generation fencing; emergency stop | Confirmed by our code | Stage R composes these; it adds no transport path (§17 architecture pin) |
| Detection exists: the health ladder (`unreachable > not_responding > foreign_writer > inhibited > actuation_incoherent > parked > self_healing > healthy`), the actuation-coherence watchdog, the objective-echo discriminator (`echo_matches_write / objective_not_served / external_writer`), responsiveness classes | Confirmed by our code | Stage C's census rides these words; Stage P's judgment reuses the echo discriminator |
| TWO MONITOR BUGS: (1) the `autonomous_self_charge` judgment keys on EXACTLY zero watts — at 96–99% SoC pods float across zero and the state flaps (158 transitions on rhs in one night); (2) the coherence baseline re-anchors at whatever the pod is ALREADY delivering across an authorization gap, so steady delivery at 87–96% of command read as "no movement" — BOTH live detections were false positives | Live-observed in our telemetry | Wave 0 (§3) fixes both BEFORE any stage composes; auto-recovery must never trigger on `actuation_incoherent` alone (invariant I10) |
| The stuck signature in OUR data (rhs the exhibiting pod): SoC 97–100% with battery watts in a narrow ±100 W band while siblings actively flow; rhs's load-CT word averages ~16 W against siblings' ~100/~180 W | Live-observed | The Stage C predicate vector (§5) formalizes it — PHASE-RELATIVE since A16 (the next row); the cross-pod reading alone is no longer sufficient to flag |
| **The rhs fingerprint has TWO live hypotheses (A16, operator evidence):** (a) a dead meter link — the vendor "Electricity meter communication disconnected" class (BMS Warning0 bit 13, PROTOCOL_EVIDENCE §9); (b) an IDLE PHASE — the operator states the house load is concentrated on ~1.5 of the 3 phases (kitchen/downstairs heavy, garage near-nothing, upstairs partial) and each pod CT-follows only its own phase, so a pod on the near-empty phase legitimately reads ~0 CT and ~0 flow while siblings serve | Operator evidence + vendor decode | The discriminator is EXTERNAL to this watch: the load-sharing round's phase-map verification (which pod owns which phase) plus whether the CT word EVER moves when that phase's circuits are used — a dead link never moves, an idle phase does. Until it settles, the census treats the difference as UNOBSERVABLE (§5's soft note) and rhs is PARTIALLY REHABILITATED as a stuck-mode suspect; its multi-year SoC pinning (never below 92–97%) stands under either hypothesis and is the parallel calibration program's concern, not this watch's |
| The nightly quiet window: `active_schedule` empty at ~23:00; night charge opens 00:00; skip conditions available today: armed/intent/inhibited/latched-stop/foreign-writer/parked, plus park's conflict guard | Confirmed by our composition | The program window and its skip-if set (§4, §6, §7) |

### 1.2 Sourced findings (EFT Systems BYD service guideline V1.5, German/AU
community forums, Fronius register documentation)

| Finding | Source class | Consequence |
|---|---|---|
| Vendor pass/fail criterion (EFT step 8): the system runs properly when SoC displays correctly AND the battery charges/discharges — TWO conditions | Vendor service guideline | Stage C = condition 1's telemetry half (SoC/CT words fresh and plausible, in context); Stage P = condition 2 tested directly. Both must fail before Stage R acts (§7 eligibility) |
| Verify-before-write is the documented rule for BYD power-circuit control words; never undocumented values; maintenance-window discipline | Vendor service guideline + register docs | R composes the existing write→readback→verify primitive; the program window IS the maintenance window |
| Small-power probes are established practice (community: 20–300 W charge steps with observed response; Fronius maintenance charging ~500 W) | Community practice | P's magnitude default 300 W sits inside both practices (§6) |
| NO public "commanded vs measured over N seconds" threshold exists | Absence of standard, established by search | WE define it; 30–60 s sustained windows are consistent with practice (§6.3 pins the arithmetic) |
| Escalation ladder: alert with evidence → re-command → standby cycle → DEFINED-RESTART ADVISORY (battery button-off 5 s, DC off, AC off, WAIT 10 MINUTES — fuse re-engagement lockout, battery ON FIRST, then AC, then DC) → installer with logs | Vendor service guideline + documented practice | The advisory text is pinned verbatim into the alert and health hints (§11) |
| Standby keeps comms alive (steady white LED class); the battery self-powers-off after 30 min WITHOUT inverter comms; force-state transitions are logged in the BMU event log | Vendor documentation | R's hold is 60–120 s — two orders below the 30-min self-off; the BMU log is named as the on-device cross-check for every cycle R performs |
| SoC drifts at persistently high SoC; periodic full cycles keep calibration valid | Vendor/community | The §0 context note; no action in this design |
| Fronius Solar.web "Battery Control" is a competing writer — documented stuck cases where an app action wedged BOTH app and Modbus control | Vendor documentation + community | Single-writer discipline and the foreign-writer guard are load-bearing; R skips any unit with foreign evidence (§7); the Fronius question is an operator unknown (§18) |
| No public endorsement of nightly cycling as prevention; wear negligible at 1/night | Absence of endorsement + community wear figures | The §0 honesty sentence; the consecutive-fail cap (§8) bounds even the benign case |

## 2. Doctrine decisions (each with its reason)

- **The program is a WATCH first.** Stage C alone is commissionable on any
  site and writes nothing; the stages compose only as a prefix
  (`census` → `census,probe` → `census,probe,recovery`), so no site ever runs
  a probe without a census or a recovery without both. Progressive earning,
  the night-V2 posture pattern, applied to physical acts.
- **Every stage is nightly, bounded, and once-per-civil-night.** One program
  run per night, derived from durable audit rows (a restart never re-runs a
  night's program, and never resets an attempt budget — the night-V2 retarget
  rule, applied here). The deadline (default 23:45) is the NO-NEW-ACT
  boundary, not a whole-program-finish claim (A1): no act starts after it,
  a started act completes inside its own bound, and validation checks BOTH
  that the whole program fits between window and deadline AND that the
  deadline plus the worst-case single in-flight act still lands before the
  night window opens — the program can never bleed into the night charge.
- **The probe is ordinary dispatch traffic, not a new write path.** P submits
  short-TTL `optimizer` intents under the composed automation principal
  `energypod:health-adviser` through the internal facade, judged by the
  arbiter, allocator, SafetyKernel and actor exactly like the excess and
  night advisers' intents. Every standing refusal (SoC floor, cell bounds,
  imbalance veto, telemetry staleness, foreign writer, park) judges the probe
  — a probe a guard refuses is a SKIP with that guard's reason, never a
  failure of the battery.
- **The probe's judgment belongs to the program, not the watchdog.** The
  coherence watchdog stays what it is (a passive detector); the probe's
  pass/fail is a first-class computed verdict with its own figures and audit
  row. A watchdog episode during a probe is corroborating evidence, not the
  verdict.
- **Discharge is the probe direction, unconditionally.** Positive P at full
  charge is the safe side on every axis: the stuck population sits at ≥95%
  SoC where a charge probe flirts with the cell-voltage ceiling and the
  top-of-charge imbalance veto (the LHS 54 mV at 99% incident vetoed a fleet
  intent — a policy veto misread as a stuck verdict is exactly the Wave 0
  failure class repeated); discharge serves house load instead of importing;
  and it proves the same PQ path, precondition class, and register family a
  charge probe would. One direction also means one statistic — the nightly
  evidence stays comparable. The kernel's `minimum_soc_pct` is the backstop
  at the bottom; at 300 W × 50 s the deepest possible excursion is ~4 Wh
  against a ≥4.2 kWh pack.
- **Stage R composes the parking machinery; it does not relax it.** Park
  refuses while armed/under-intent/latched — R therefore DISARMS its target
  unit first (a stop-direction act, the emergency-stop family), and the
  conflict guard fires never. R never takes over a foreign word
  (`takeover: "FOREIGN"` stays operator-only, forever); a stuck unit whose
  standby word is not ours is a SKIP plus an alert. R refuses vendor modes
  2–6 exactly as park does. The lease expiry stays ALARM-ONLY — if the
  controller dies mid-cycle, the standing rule owns the outcome (no write,
  ever, including boot).
- **R never leaves a unit MORE enabled than it found it (A7 unifies the
  phrasing; §12 owns it).** Its composite sequence — disarm → park → hold →
  resume → (one bounded re-arm → verification probe → disarm) — normally
  ends every unit DISARMED and NORMAL, the exact state the operator's own
  recovery walkthrough would leave; where it cannot reach that state it
  alerts that it could not (I9). The single upward step is the bounded
  verification re-arm, which exists only in the `auto` posture and is the
  ONLY automation arm authority this controller ever composes (§12, panel
  ruling 1); `advise` is the default and contains no re-arm at all.
- **One attempt, one night, one lifetime cap.** At most one standby cycle per
  unit per civil night (durable-row-derived); never a second write after an
  ACKed-but-unverified one; after `consecutive_fail_limit` (default 3) failed
  nightly recoveries the unit's R stage stands down to advisory-only until an
  operator acknowledgement resets it.
- **MCP observes; it does not recover.** No MCP mutation tool; read surfaces
  gain the watch projection and the advisory. The agent-loop contract text
  gains: "the nightly health watch is a commissioned program, not an agent
  act; recommend the operator surfaces."
- **Honesty when stages are absent.** An uncommissioned stage renders as
  uncommissioned on every surface that would otherwise suggest it (the
  parking advisory's `commissioned: false` pattern): a site without R never
  sees "recovery" offered as if it could run.

## 3. Wave 0 — the two monitor fixes (prerequisites, own wave, own tests)

No stage of this program composes until Wave 0 ships, because both bugs
corrupt the very evidence the program would act on: a flapping health state
poisons the census context, and a false `actuation_incoherent` is precisely
the misfire an automated recovery must never stand on. Wave 0 is detection
work only — no new write of any kind — and lands in the recovery monitor
(`src/energypod/application/recovery.py`).

> **S4 idle-phase amendment (DESIGN_EVENING_LOAD_SHARING §10, recorded for the
> watch's own later sweep):** the census's no-CT-view predicate infers "no CT
> view" from a low load word against loaded siblings; the evening program's
> phase map gives that class its honest third case — a pod whose CT is low
> because its PHASE is idle is healthy, and its census context should say
> `idle_phase` rather than implying a sensing fault. The map row (the
> `evening_phase_map_recorded` audit row, ranked per-pod evening load figures)
> is the watch's to consume; no control path in the evening program reads it.

### 3.1 W0-1 — the self-charge float deadband (health flapping)

The `autonomous_self_charge` healing reason currently requires
`measured != 0.0` — exact float equality against a signed watt word. At
96–99% SoC the pods float across zero (observed rhs straddles of −16/0/+33 W)
and the derived state flapped 158 times in one night. The fix is a deadband
on the classification input: a cycle with `|measured| < self_charge_deadband_w`
(default 25 W) renders NEITHER self-charging NOR flap — the unit is floating
at the top, which is the healthy steady state of a full pack.

Justification of 25: the float-at-full straddle observed on this fleet sits
within ±33 W of zero, while genuine CT-following self-charge is the
−520…−560 W class and deep self-charge −2.27 kW — two populations separated
by an order of magnitude, with the metering noise floor in the tens of watts.
25 splits them with margin in both directions. Samples beyond the deadband
(e.g. a +99 W float) still render `self_healing` — genuine CT-following
float remains visible; the deadband kills the zero-crossing flap, not the
float visibility. Config key joins the recovery policy block
(`self_charge_deadband_w`, validated `(0, 100]` — at 100 it would begin to
eat the legitimate float class, so that is the ceiling).

### 3.2 W0-2 — coherence judged on DELIVERY, not movement alone (false positives)

The live false positives: an authorization gap (intent renewal lapse,
stand-down/resume) reset the episode and re-anchored `baseline_watts` at the
watts the pod was ALREADY delivering, so steady delivery at 87–96% of command
read as "no movement" and accumulated an incoherence streak. Two changes,
both invariants:

- **W0-2a — the baseline survives short authorization gaps.** The baseline
  re-anchors only when the measured power has returned to the idle band OR
  the authorization gap exceeded `coherence_gap_grace_s` (default 12 s — the
  night-writer detector's handback-grace precedent, which covers the observed
  ~4–8 s watchdog hand-back with margin). A gap shorter than that, with
  delivery continuing at the commanded level, is the SAME episode: the
  baseline does not move.
- **W0-2b — incoherence requires failed DELIVERY, not failed movement.** A
  cycle counts toward a streak only when BOTH fail: movement from the episode
  baseline below `min(0.5 × authorized, floor)` AND delivered power in the
  commanded direction below the same bound. Steady delivery at 87–96% of
  command passes the delivery test and is coherent (it IS delivery — the
  watchdog docstring's own words); a dead pod fails both. The episode may
  OPEN — the health state may become `actuation_incoherent` — only once the
  objective-echo discriminator has classified the episode
  `echo_matches_write` or `objective_not_served`; `echo_unreadable`,
  unclassified, or `external_writer` episodes record evidence and downgrade
  (the external-writer class belongs to the standing latch path).

- **W0-3 — the standing pin, restated as this feature's invariant I10:** AUTO
  RECOVERY MUST NEVER TRIGGER ON `actuation_incoherent` ALONE. Stage R's
  eligibility is census-flag AND probe-failure (§7.1) — the health ladder is
  context, never a trigger. A named test pins it.

**Release-cut pin (A13).** Wave 0's keys carry defaults, so config alone
cannot order it before the stages — the ordering is therefore a RELEASE
fact, stated here: the Wave 0 fixes land in the same release cut as ANY
stage composition of this block, never after. The structural pin behind it
is I10's test (no automated response anywhere keys on
`actuation_incoherent`), and the panel verified as FACT, not discipline,
that no rung of this program writes on a signal Wave 0 fixes: the census
predicates read raw words, the probe verdict is computed from
commanded-vs-measured, and R's triggers are rows — Wave 0 is a prerequisite
because the EVIDENCE would be noisy without it, never because a write path
depends on it.

Wave 0's tests: T-BHW-WAVE0 (§17).

## 4. The program — window, phases, composition

**Composition.** A `HealthWatchController` in `application/`, composed only
when the `battery_health_watch:` block is present (the parking/night pattern:
an ABSENT block composes NOTHING — no snapshot key, no events, no REST
surface, byte-identical behavior). It ticks once per fleet cycle inside the
existing bounded supervision pass (no new task class), carrying a small
per-night phase machine; its durable facts are audit rows, so a restart
reconstructs conservatively (a probe interrupted by a restart is
inconclusive; a night whose rows show a completed stage does not re-run it;
an in-flight R cycle is owned by the standing lease rules). "An attempt",
for every once-per-night budget in this contract, is ANY health-watch audit
row for that unit that civil night (A4): one crash anywhere retires the
night's program unambiguously — and a restart finding health-adviser rows
for a unit-night with NO `health_recovery_outcome` row (the interrupted
composite: a crash after the re-arm can leave a unit ARMED, NORMAL,
lease-less, and un-alerted, which would otherwise silently night-charge)
raises an alert naming the state the unit was actually left in. Crash-safe
at every arrow; crash-HONEST at every arrow too (A4/A5, panel ruling 2).

**Window.** `window_local` (default `"23:00"`) starts the program;
`deadline_local` (default `"23:45"`) is the NO-NEW-ACT boundary (A1): no
probe, cycle, or verification act STARTS after it; an act started before it
completes inside its own bound. The arithmetic is validated, not assumed
(§9): window start + the whole-program bound (worst case ≈ 21 min at the
default stage set) must land at or before the deadline, AND deadline + the
worst-case single in-flight act (a recovery cycle, ≈ 5 min) must land
before the night window opens — both refusals name their arithmetic.
Validation also refuses any overlap with `night_charging.window_local` or
any `schedule.allowed_windows_local` pair — the program lives in the quiet
hour by construction, not by hope. At runtime the program additionally verifies
the window is genuinely quiet: `active_schedule` empty for the slot, no
live intent claims, no adviser participation — else it defers to the next
night with reason `window_not_quiet` (never fights, never wedges itself in).

**Phases, per night:** `await_window → census → probe(lhs → mid → rhs,
strictly sequential, one unit at a time) → recovery(eligible units, strictly
sequential) → record → done`. The whole program is bounded well inside the
window at defaults (census: one pass over historian rows; probe: ≤ ~2 min
per unit; recovery: ≤ ~5 min per unit — three units worst case ≈ 21 min
against a 45-minute budget). A stage not in `stages:` is skipped and renders
as uncommissioned.

**Skip-if vocabulary (the standing guards, all reused):** armed / under
intent / latched stop / inhibited (`external_writer` or otherwise) /
parked / vendor mode word / unreachable / not_responding / telemetry stale /
foreign objective evidence / schedule conflict / quiet-load gate unmet or
unjudgeable on stale grid words (probe only, at start and cancel — A8).
Every skip is a recorded verdict with its reason —
never silence, never a failure.

**Interplay with the night-writer detector:** the detector samples only
unclaimed, non-ACTIVE units and honors the handback grace, so the probe's own
objectives are never misread as a foreign writer (a named test pins it —
T-BHW-PROBE). The probe at 23:00 sits outside every schedule window, and
advisers (excess, night, health) are window-gated by their OWN blocks, not by
the schedule union — the union governs published schedules only; the design
relies on that distinction, says so here, and amends DESIGN_SCHEDULES to own
it (§15, A15).

**Precedence, corrected (A12):** the probe rides `optimizer`, which
OUTRANKS `schedule` in the standing order (manual > agent > optimizer >
schedule). The probe is therefore preempted by MANUAL and AGENT claims and
by the emergency stop — NOT by a schedule claim (none should exist in the
quiet window, and the quiet-window check would already have deferred). The
preemption test uses exactly those claimant classes.

**Interplay with night charge:** the program finishes (or stands down) before
00:00. A unit the program left DISARMED after a recovery cycle is excluded
from that night's charge with the honest `units_disarmed` reason — deliberate
(§7.4), visible on the night tile, and the operator's morning re-arm is the
designed human check on a recovered pod.

**Interplay with the evening load-sharing program (A16):** the load-sharer
under design ends its dispatches at 22:30 — thirty minutes before this
window opens — and the watch's window remains EXCLUSIVELY its own: no
shared minutes, no hand-off overlap, no stage of this program borrowing
the sharer's machinery (the A14 boundary runs both directions). The
boundary is enforced from the watch's side by the standing runtime quiet
checks (no live claims, no adviser participation, else `window_not_quiet`
defers the night), and the sharer's own end is non-renewal hand-back
(seconds, not minutes) — 22:30 plus hand-back leaves the fleet quiet well
before 23:00. When that program's block lands, its window end must
validate at or before this watch's `window_local` minus a hand-back
margin; the cross-block check joins §9's list under that contract's own
naming.

## 5. Stage C — the census (zero writes)

One evaluation per unit per night at window open, over the trailing evidence
window (`stuck.evidence_window_h`, default 6 h) read from the telemetry
historian through an injected port (the load-baseline pattern — no
application import of adapters; the census REQUIRES the `plant_history:`
block at validation, because an N-hours stuck predicate cannot be honestly
computed from one instant). Every word it reads — mode words, CT words
(grid at PCS `0x1000+17`, load at `0x1000+20`), SoC, battery watts — is
already in the standing read plan or the historian: the census issues NO
reads of its own and NO writes, ever.

**Readiness check (the vendor's condition-1 half).** Per unit: telemetry
fresh (age within the commissioned bound), SoC present and plausible
([0, 100], within the fleet SoC-difference policy of siblings or flagged),
mode words decoded, health ladder state recorded. This is context, recorded
in the census row — the readiness to ACTUATE is Stage P's to test.

**The stuck-signature predicates** — ALL must hold across the evidence
window, and the row records the full vector so a wrong threshold is
discoverable, not hidden:

| # | Predicate | Default | Justification |
|---|---|---|---|
| S1 full | `soc_pct ≥ stuck.soc_floor_pct` on ≥ `stuck.soc_hold_frac` of samples, over ≥ `stuck.min_soc_hours` continuous hours | 95%, 0.9, 6 h | The exhibiting population sat 97–100%; 95 catches it with margin; the 6-h floor excludes a top-of-charge visit from counting |
| S2 still | `|battery_watts| < stuck.still_w` on ≥ `stuck.still_frac` of samples | 50 W, 0.9 | The observed stuck band is ±100 W at its edge, well inside the siblings' active-flow class (≥500 W); 50/0.9 tolerates float jitter without admitting a flowing pod |
| S3 the house needed them | fleet mean grid import > `stuck.grid_import_w` OR any sibling `|battery_watts| > stuck.sibling_flow_w`, on ≥ `stuck.flow_frac` of samples | 500 W, 500 W, 0.5 | A full pod is a spectator only when energy was actually moving — import beyond standby or a sibling actively flowing; the corroboration that separates "stuck" from "everything genuinely idle" |
| S4′ in-phase non-following (A16) | unit `load_power_w > stuck.load_floor_w` — load EXISTS on the unit's OWN phase, per its own CT word — while `|battery_watts| < stuck.still_w`, on ≥ `stuck.load_frac` of the SAME samples | 30 W, 0.8 | The pod watches its own phase's load go unserved and does nothing. Self-contained and phase-relative: NO cross-pod comparison. The v1.1 cross-pod form (unit CT ~0 while a sibling reads > `sibling_load_w`) is RETIRED as a flag — with the house load concentrated on ~1.5 of 3 phases, a pod on the near-empty phase legitimately reads ~0 CT and ~0 flow while siblings serve, and that is not the stuck signature |
| S5 mode corroboration | `debug_mode_w == 0` on all samples | — | Distinguishes the spectator signature from every mode-latch class (parked, vendor 2–6, remote-PQ latch), which the parking/foreign surfaces already own — the census never re-classifies another surface's state |

**Verdicts:** `nominal`; `stuck_suspected` (S1 + S2 + S3 + S4′ + S5 — the
predicate vector rides the row); **`phase_idle_or_ct_silent` (A16 — a SOFT
informational note, NEVER a flag):** S1 + S2 hold and the unit's own-phase
load word read ≤ `stuck.load_floor_w` across the window while ≥1 sibling
carried load above `stuck.sibling_load_w` — the pod is EITHER on an idle
phase OR its CT link is dead, and the census CANNOT tell which (the only
per-phase load measurement is the suspect word itself). The note names the
discriminator — the phase-map verification (§16/§18) and whether the word
has EVER exceeded `stuck.load_floor_w` in the trailing
`stuck.phase_live_window_h` (a word that never moves annotates the note
`ct_link_suspect`; one that moves clears it) — and hands off to Stage P as
the only test that sees through both hypotheses. `degraded_evidence`
(historian gaps beyond the integration discipline — excluded samples,
never interpolated; the night renders no verdict rather than a wrong one);
`excluded:<class>` (parked / vendor-mode / inhibited / unreachable —
named, owned elsewhere). The stuck flag is a NOTICE-tier fact on first
occurrence and promotes to ALERT tier when it persists
`stuck.flag_persistence_nights` (default 2) consecutive nights — one noisy
night is evidence, two is a pattern. The soft note NEVER promotes: it is
informational at every age, because its two hypotheses have different
owners (an idle phase is nobody's fault; a dead link is the vendor's
warning class) and the census is not entitled to pick one.

## 6. Stage P — the actuation probe

### 6.1 Who gets probed, and what stops a probe

Per unit, in fixed sorted order (lhs, mid, rhs), strictly one at a time with
`probe.inter_unit_gap_s` (default 30 s) between: every unit that is
census-`nominal` or `stuck_suspected`, passes the skip-if set (§4), is
ARMED (dispatch requires arm; the program NEVER arms — a disarmed unit is
skipped `unit_disarmed`, honestly counted on the projection), has mode word
0, fresh telemetry, no foreign evidence, and the quiet-load gate (A8):
fleet mean grid IMPORT magnitude — the control-grade PCS word
(`0x1000+17`), NOT the load-CT mean, which a dead CT biases low (rhs's
16 W) and which is per-phase load, not house draw — below
`probe.quiet_load_w` (default 1000 W, the operator's demand-figure class).
The gate is checked at probe start AND re-checked at cancel (§6.2); any
unit's grid word missing or beyond telemetry age makes the gate
unjudgeable, and the night's probes skip with `quiet_evidence_stale` —
never run on bad evidence (the demand rule's fail-closed shape).

### 6.2 The probe leg

1. Record the pre-probe baseline (mean measured battery watts over the last
   `probe.settle_s` of idle observation).
2. Submit ONE `optimizer` discharge intent at `probe.probe_w` (default
   300 W, positive P, direction fixed by doctrine §2) under
   `energypod:health-adviser`, renewed per fleet cycle through the internal
   facade — ordinary dispatch traffic, every standing guard judging it.
3. SETTLE: `probe.settle_s` (default 20 s) excluded from judgment — the
   observed authorization-to-power ramp is ~10 s on this fleet; 20 s covers
   it twice.
4. SUSTAIN (the judgment core): `probe.sustain_s` (default 30 s) at the
   control-rate observation cadence (~1.5 s → ~20 samples).
5. ECHO: one bounded objective-echo read at probe end (the existing
   discriminator; budget one read per probe, the episode precedent).
6. CANCEL (explicit bounded zero through the intent path — not mere
   non-renewal; the probe ends deliberately), re-check the quiet-load gate,
   then verify return-to-baseline: `|measured − baseline| ≤
   probe.return_band_w` (default 150 W, the movement floor family) within
   `probe.baseline_return_s` (default 30 s; the observed watchdog hand-back
   is ≤ ~10 s). If fleet import MOVED by more than `probe.load_move_w`
   (default 300 W, the probe's own magnitude class) between probe start and
   cancel, the baseline judgment is confounded by demand and downgrades to
   `inconclusive_baseline_confounded` (A8) — a pod whose autonomy resumed
   into a demand spike is not a broken pod.
7. A MANUAL or AGENT claim arriving mid-probe preempts it instantly, as
   does the emergency stop (the precedence order, corrected in §4 — a
   schedule claim does NOT outrank the probe, A12): verdict
   `inconclusive_preempted`, never a fail.

### 6.3 The pass/fail threshold — defined here, because no standard exists

**Sustained delivery** — on ≥ `probe.pass_sample_frac` (default 0.8) of
core samples, the signed measured watts in the probe direction reach
≥ `probe.pass_fraction × probe_w` (default 0.5 × 300 = 150 W). A pod
serving at 87–96% of command (the fleet's own known delivery spread — mid
legitimately delivers 75–87% of a charge; discharge overshoots +15–16%,
the filed bias) passes with margin; a spectator at <50 W misses by 3×.
The 0.5 fraction is the coherence watchdog's own `_MOVEMENT_FRACTION` —
one judgment band for delivery across the controller, not two. And it is
80% of samples, not the mean: a single-sample spike (mid's oscillation
class) can never pass a probe; a genuinely serving pod holds essentially
always (the 120 s evidence legs held steady throughout). 80% of ~20
tolerates four outlier samples.

**The verdict matrix (A3) — measured × echo × baseline-return → verdict.**
The echo dimension is load-bearing and was implicit in v1; under
DESIGN_POD_PARKING §6's pinned device model (an ignored write leaves the
served-objective words UNCHANGED), the stuck class could present either
way, and the matrix now says exactly which presentation earns which
verdict:

| Measured in core | Objective echo | Baseline return | Verdict |
|---|---|---|---|
| delivery band met | `echo_matches_write` | yes | **`pass`** |
| delivery band met | `echo_matches_write` | no | `fail_baseline_not_returned` |
| delivery band met | `objective_not_served` / `external_writer` / unreadable | — | `inconclusive_echo_mismatch` |
| movement, below band | any readable | — | `fail_partial` |
| **still (< `stuck.still_w`)** | **`echo_matches_write`** | yes | **`fail_no_response`** — the R-eligible class: the write path is proven fine (the echo follows) and the actuation path is dead — the wedge, confirmed under command |
| still | `objective_not_served` / `external_writer` | — | `inconclusive_echo_mismatch` — advisory only: under the pinned model this is the write-never-served class (mode/precondition), and whether the real stuck population lives HERE is open question 7 (§18) |
| still | `echo_unreadable` | — | `inconclusive_aborted` |

Read the fifth row against the standing hint vocabulary:
`echo_matches_write` + stillness is exactly recovery.py's "transport and
write path proven fine, defect inside the pod" class — the class the
standby cycle exists for. The sixth row is the honest limit of R's KNOWN
reach: if the historical stuck class presents as not-served rather than
echoed-and-dead, Stage R is inapplicable to it and the defined-restart
advisory is the whole remote story — which is why the §7.1 conjunction
keys on `fail_no_response` specifically, and why §18 item 7 asks the
operator the question only the historical events can answer.

Energy and wear: 300 W × ~50 s ≈ 4 Wh ≈ 0.08% of a 5 kWh pack, one per
unit per night — negligible by the community wear figures, and stated as
such rather than waved at. One honest side effect (A8): on a quiet house
the probe's 300 W discharge briefly EXPORTS up to ~300 W for ~50 s (the
pod feeds a bus with nobody drawing) — ~4 Wh at the feed-in rate, trivial,
and named here so nobody is surprised by the export meter.

**The remaining verdict words:** `fail_partial` (movement in direction but
below the delivery band on the qualifying share: degraded command-following,
ALERT tier, NOT R-eligible alone); `fail_baseline_not_returned` (did not
stop cleanly: its own alert, its own advisory line — the bounded-zero
discipline owns the response); `inconclusive_echo_mismatch` (evidence of
interference or the mode-check class; the standing MODE_CHECK_HINT advisory
renders; never stuck evidence); `inconclusive_baseline_confounded` (§6.2
step 6); `inconclusive_aborted` (guard refusal, preemption, telemetry loss,
restart: no verdict, evidence recorded, next night is another chance);
`skipped:<reason>` (the skip-if set, verbatim).

## 7. Stage R — recovery (the doctrine revision)

### 7.1 Eligibility — the conjunction, deliberately conservative

A unit is R-eligible for a night iff EITHER route holds:

- **Route A — the conjunction:** census `stuck_suspected` that night AND
  probe `fail_no_response` that night. The vendor's own two-condition rule
  made structural: condition 1's display half failed (the census context:
  full, still, load unserved on its own phase, house needed power) AND
  condition 2's actuation half failed under direct command.
- **Route B — the unobservable phase (A16):** census
  `phase_idle_or_ct_silent` that night AND probe `fail_no_response` on
  ≥ `recovery.probe_fail_nights` (default 2) CONSECUTIVE nights. For a pod
  whose phase the census structurally cannot see (idle, or CT-silent —
  the only per-phase load measurement is the suspect word itself), the
  commanded probe is the only spectator test that exists, and REPETITION
  substitutes for the missing census half: two independent nightly
  failures of a commanded discharge are a second evidence line, not a
  re-run of the first. Every other guard, bound, and cap applies
  unchanged — one cycle (I1), the fail cap (I4), all skip-ifs.

A probe failure on a `nominal` unit is one weak night — alert, re-probe
tomorrow, no write (route B is deliberately scoped to the soft-note class,
where the census's blindness is structural, not where it simply did not
fire). A census flag WITHOUT a probe failure
(the unit actuates when asked — the CT-dead-but-commandable class) is NOT
the wedge a standby cycle is proven to fix: it renders the advisory and the
restart checklist, no cycle. The probe half keys on `fail_no_response`
SPECIFICALLY — the matrix's echoed-and-dead row (§6.3) — because that is
the presentation a standby cycle is proven to address; the not-served
presentation stays advisory whatever the census says (open question 7).
And per invariant I10, `actuation_incoherent`
or any other ladder state is context, never a trigger. A probe that was
skipped or inconclusive on a census-flagged unit (e.g. it was disarmed)
leaves the unit NOT eligible — the evidence is incomplete, and an
incomplete case never mints a mode write.

### 7.2 The cycle (posture `auto`)

Per eligible unit, strictly sequentially, each step re-checking the skip-if
set inside the standing locks:

1. **Re-verify** — no intent claims the unit, no latched stop names it, no
   foreign objective evidence, mode word 0, telemetry fresh, no open lease.
   A standby word of 1 that is not ours: SKIP + alert (`foreign_standby` —
   the operator's takeover resume is the only exit, forever). A word in 2–6:
   SKIP + alert (`vendor_mode` — the vendor app must clear it).
2. **Disarm** — the standing facade disarm under `energypod:health-adviser`,
   reason "health-watch recovery cycle", audited, generation fenced. A
   stop-direction act: the emergency-stop family, fail-safe by construction.
   This step exists so that PARK'S CONFLICT GUARD NEVER NEEDS WEAKENING —
   R composes with the guards, not around them.
3. **Park** — via the `ParkController` with `lease_s = recovery.hold_s + 60`
   (inside the parking block's commissioned bounds), reason "nightly
   health-watch recovery (census flag + probe no-response)", principal
   `energypod:health-adviser`, ledger origin `automation` (the additive
   vocabulary word, §10). Durable-first pending row, ONE mailbox
   write→readback→verify — the existing primitive, values {0, 1} only.
4. **Hold** — `recovery.hold_s` (default 90 s, bounded [60, 120]). The live
   proof held 76 s and recovered clean; 90 brackets it with margin; the
   30-min no-comms self-power-off is two orders away; comms, telemetry and
   SoC stay alive throughout (live-proven), so the watch keeps observing
   exactly the pod it is cycling. If comms die mid-hold, the STANDING rules
   own it: the lease expires alarm-only, no write, ever — R inherits the
   safest failure mode its parent machinery already has.
5. **Resume** — write 0, readback verify, the standing resume checklist
   captured (comms age, SoC drift, faults while parked, latched stops).
   A crash between the VERIFIED resume write and the lease commit is the
   one window where parking's divergence pass would read OUR completed act
   as a foreign resume (it closes any open lease over word=0); the
   resume-side adoption closes that window (A5, §12): a pending
   `unit_resumed` row with no completing row, against a word already 0
   inside the bounded recency window, commits the lost closing row as OURS
   — a store write only, never a device write. Outside the window, the
   honest `observed_foreign` reading stands with the pending row as the
   operator's correlation.
6. **Prove command-following (the P re-run).** The verification probe re-runs
   the §6 leg unchanged on the same unit, because the live evidence proved
   command-following SURVIVES the cycle (clean 1000 W legs before AND
   after) — recovery without that proof is the original sin repeated:
   declaring health without testing actuation. The re-run requires ARM; see
   §7.4 for the one bounded re-arm and the doctrine it carries. The re-arm
   can be REFUSED (A2): a foreign objective observed during the park makes
   the next arm the operator's takeover acknowledgement (parking §3,
   interactive-only), and a latched stop names the unit outright — both
   end the sequence at `rearm_refused`, posture `recovered_unproven`.
7. **Verdict + record** (§10): `recovered` on a passing re-run;
   `recovered_unproven` when the re-run was inconclusive (preemption, guard
   refusal, telemetry loss, restart) or the re-arm refused — the cycle
   plausibly worked and the proof is missing, an alert names the operator's
   morning verification as the exit, and NO re-run is issued (I1); else the
   failure ladder (§8) — and never a second cycle that unit, that night.

In posture `advise` (the default), steps 2–6 do not run: the program renders
the recovery advisory (the DESIGN_POD_PARKING §7 walkthrough, extended with
the night's census/probe evidence and the §11 advisory text) as an ALERT-tier
card naming the exact operator sequence — disarm → park → resume → re-arm →
verify — and records what the operator did through the standing surfaces as
the night's recovery evidence. Advise is a real posture, not a stub: it is
the standing posture until the operator's config revision, exactly as
`forecast_suggest` was for night V2.

### 7.3 Never a second attempt

One cycle per unit per civil night, derived from the durable audit rows
(a restart mid-night never re-budgets — the night-V2 retarget rule). After an
ACKed-but-unverified write there is NO re-issue and NO second attempt, that
night or by the program again for that unit until the posture resolves: the
word state is unknown, and a blind second write into an unknown word state
is precisely the thrash class this program exists to prevent. After
`recovery.consecutive_fail_limit` (default 3) failed nightly recoveries on
one unit, that unit's R stage stands down to advisory-only until an operator
acknowledgement resets it (the acknowledge-inhibit pattern, privileged) —
three consecutive failed cycles is a hardware story, and the program's
answer to a hardware story is the advisory, not persistence.

### 7.4 The re-arm tension, stated honestly

The verification probe needs ARM; arming is an enabling act; every enabling
act in this controller is interactive — "a timer is not a principal". The
design's resolution: R's composite sequence includes ONE bounded re-arm in
the `auto` posture, whose sole purpose is the verification dispatch, bounded
by the program window, audited under the health principal, and followed by
disarm — the sequence is authority-net-zero and the unit ends DISARMED and
NORMAL, exactly where the operator's own walkthrough would leave it. This is
the single most aggressive step in the contract. It is defensible because
the live evidence did exactly this (the post-cycle leg was armed via the API
and proved `sole_writer`-clean) and because the alternative — declaring
recovery without the proof — is dishonest in the direction that matters.
But it is a genuine amendment of "advisers never arm", it is called out in
§12 as such, and `mode: advise` is the escape hatch the operator keeps: in
advise, no re-arm by the program ever occurs, on any night, for any unit.
The refused re-arm (A2) is the honest terminal of the sequence's own
preconditions: when the takeover acknowledgement or a latched stop stands
between the program and the verification dispatch, the program does not
ask twice — the unit ends disarmed, the posture is `recovered_unproven`,
and the operator's morning (re-arm with the takeover acknowledgement if
required, then observe or probe) is the named exit.

## 8. The failure ladder and the never-thrash invariants

The ladder per write in the cycle (each rung exhausts before the next):

1. **Built-in retry** — the actor's bounded single retry inside
   `request_debug_mode_change` (existing, unchanged).
2. **Transport resync** — on a clean transport refusal (no ACK), the actor's
   automatic reconnect (existing) then ONE re-issue of that write. A second
   refusal ends the night's attempt: verdict `failed_write`, ALERT,
   advisory.
3. **ACKed-but-unverified** — no re-issue, no second attempt: the standing
   `write_unverified` terminal posture (the lease persists, parked stays
   true, ours stays ours), ALERT, the §11 advisory. The word state is
   unknown; the program writes nothing further that night.
4. **Verified cycle, verification probe FAILS** — `failed_no_effect`, ALERT,
   advisory; counts toward the consecutive-fail cap.
5. **Verified cycle, verification probe INCONCLUSIVE** (preemption, guard
   refusal, telemetry loss, restart) or **re-arm REFUSED** (a foreign write
   during the park — the next arm is the operator's takeover
   acknowledgement, parking §3; or a latched stop) — `recovered_unproven`
   (A2): ALERT, advisory, the unit ends disarmed, the operator's morning
   verification is the named exit, never a re-run (I1), and the outcome
   does NOT count toward the cap — the cap bounds cycles that ran and did
   not recover, not proofs that went missing.
6. **Cap reached** — advisory-only for that unit until the operator
   acknowledgement. The ladder ends, always, in a human act. The cap
   counts `failed_write`, `write_unverified`, and `failed_no_effect` —
   every outcome where a cycle was attempted and the unit was not
   recovered (A2's exclusions stated from the positive side).

**The invariants (every one a named test):**

- **I1** — at most ONE standby cycle per unit per civil night, derived from
  durable rows; a restart never re-budgets.
- **I2** — the program runs at most once per civil night, and no ACT
  starts outside `[window_local, deadline_local]` (A1); an act started
  inside the interval completes within its own bound (a recovery cycle
  ≈ 5 min), and the §9 validation proves both the whole-program fit and
  the worst-case completion land before the night window opens — a write
  sequence is never abandoned mid-leg.
- **I3** — after an ACKed-but-unverified mode write: no further mode write
  to that unit that night, by the program, under any input.
- **I4** — `consecutive_fail_limit` attempted-and-not-recovered cycles
  (`failed_write`, `write_unverified`, `failed_no_effect`; NOT the
  `recovered_unproven` outcomes, A2) ⇒ advisory-only until the operator
  acknowledgement.
- **I5** — values {0, 1} only, structurally: R adds no transport path; the
  named-method whitelist and the transport-layer bound are inherited and
  architecture-test-pinned. Values 2–6 can never appear.
- **I6** — no mode write while: any intent claims the unit; any foreign
  objective evidence stands; a latched stop names it; the word is a vendor
  mode; telemetry is stale; the unit is armed or under-intent (R disarms
  first — the park conflict guard itself is UNCHANGED).
- **I7** — the emergency stop is supreme over the program: a stop mid-program
  fences the fleet generation, issues the bounded zero, and the program
  writes nothing further that night; an in-flight lease follows the standing
  expiry rules.
- **I8** — boot never writes the mode register and never starts the program
  for a night whose rows show an attempt — and an ATTEMPT is any
  health-watch audit row for that unit that civil night (A4): one crash
  retires the night's program, and the interrupted-composite reconstruction
  (§4) alerts on health-adviser rows with no `health_recovery_outcome` row.
  R runs only from its own window evaluation.
- **I9** — R never leaves a unit MORE enabled than it found it (A7): the
  normal end state is disarmed-and-Normal, and where it cannot reach that
  state it alerts that it could not; the standing `units_disarmed`
  night-charge exclusion is the visible consequence.
- **I10** — auto-recovery never triggers on `actuation_incoherent` alone
  (Wave 0, §3.2).
- **I11** — every probe and cycle figure is audited with its evidence; every
  skip, fail, and inconclusive is a recorded verdict, never silence.

## 9. Config — the `battery_health_watch:` block

Absent block = nothing composes (no controller, no snapshot key, no events,
no REST surface). Presence composes the stages listed in `stages:` — a
strict prefix of `[census, probe, recovery]`; anything else is a validation
error naming the rule. Every stage's knobs are its own; no stage reads
another feature's block except the named prerequisite gates.

```yaml
battery_health_watch:
  timezone: "Australia/Brisbane"   # REQUIRED — the window is a civil-time fact
  window_local: "23:00"            # program start; must not overlap the night
                                   #   window or any schedule window
  deadline_local: "23:45"          # no new acts after this; started legs finish
  stages: ["census"]               # a strict prefix of [census, probe, recovery]
  stuck:
    evidence_window_h: 6           # historian lookback (>= 1)
    soc_floor_pct: 95.0
    soc_hold_frac: 0.9
    min_soc_hours: 6
    still_w: 50
    still_frac: 0.9
    grid_import_w: 500
    sibling_flow_w: 500
    flow_frac: 0.5
    load_floor_w: 30
    sibling_load_w: 100            # the soft note's sibling-load context (A16)
    load_frac: 0.8
    phase_live_window_h: 168       # has this unit's CT word EVER moved? the
                                   #   dead-link vs idle-phase annotator (A16)
    flag_persistence_nights: 2     # notice -> alert promotion
  probe:
    probe_w: 300                   # [100, 500] and <= policy.max_unit_discharge_w
    settle_s: 20                   # [10, 60]
    sustain_s: 30                  # [20, 60]; settle+sustain <= 90
    pass_fraction: 0.5             # (0, 1]
    pass_sample_frac: 0.8          # (0, 1]
    return_band_w: 150             # > still_w
    baseline_return_s: 30          # [10, 120]
    quiet_load_w: 1000             # fleet-mean grid-IMPORT gate (A8), at
                                   #   probe start AND re-checked at cancel
    load_move_w: 300               # mid-leg demand move that confounds the
                                   #   baseline judgment (A8) -> inconclusive
    inter_unit_gap_s: 30           # >= 0
  recovery:
    mode: "advise"                 # advise | auto (auto is the operator's
                                   #   LATER revision; advise is the default
                                   #   and writes nothing, ever)
    hold_s: 90                     # [60, 120]
    consecutive_fail_limit: 3      # >= 1; counts attempted-and-not-recovered
    probe_fail_nights: 2           # >= 2 (A16 route B): consecutive probe
                                   #   failures that make an unobservable-
                                   #   phase unit R-eligible without a flag
    # A6: mode auto requires a supervised-verification receipt (a
    # docs/evidence/ path, the section 16 step-5 file) for EVERY fleet unit,
    # or the literal "excluded" for a unit that stays advise. Validation
    # checks shape; boot checks file existence and DEGRADES a missing
    # receipt's unit to advise loudly (validation stays offline-pure).
    auto_receipts: {lhs: "excluded", mid: "excluded", rhs: "excluded"}
```

**Cross-validations (each refusal message names its rule):**

- `stages` is a strict prefix of `[census, probe, recovery]`.
- `window_local < deadline_local`; the whole `[window, deadline]` interval
  disjoint from `night_charging.window_local` and from every
  `schedule.allowed_windows_local` pair; and the TWO deadline-arithmetic
  checks (A1): (a) window start + the whole-program bound (the sum of the
  commissioned stages' worst-case durations) ≤ deadline, (b) deadline + the
  worst-case single in-flight act (a recovery cycle, ~5 min) ≤ night window
  open — each refusal names its arithmetic.
- The block's `timezone` must equal `site.timezone` — one civil-time truth
  per site; divergence is refused naming both values (A9).
- `stuck.min_soc_hours ≤ stuck.evidence_window_h` — equality INTENDED and
  allowed (the defaults 6 = 6: the evidence window is exactly the
  full-at-top horizon; strict `<` would force a wider window for no
  semantic gain); `flag_persistence_nights ≥ 1` (A9);
  `stuck.phase_live_window_h ≥ stuck.evidence_window_h` and within the
  historian's full-resolution retention (168 h against the 14-day
  commissioned default — a liveliness window shorter than the evidence
  window is nonsense, and one beyond retention is unknowable);
  `recovery.probe_fail_nights ≥ 2` — a single night NEVER suffices on
  route B (A16).
- `stages` includes `census` ⇒ the `plant_history:` block is present (the
  evidence window is historian-backed; there is deliberately NO
  degrade-to-single-instant path — a one-look stuck verdict is the refused
  direction).
- `stages` includes `probe` ⇒ `mode: write_enabled` with a policy (probes
  are dispatch); `probe_w ∈ [100, 500]` and `≤ policy.max_unit_discharge_w`;
  `settle_s + sustain_s ≤ 90`; `return_band_w > still_w`; the fractions in
  (0, 1].
- `stages` includes `recovery` ⇒ the `parking:` block is present AND the
  mode is `write_enabled` (R composes parking's primitive; without it the
  stage is refused at validation, never silently incapable); `hold_s ∈
  [60, 120]`; `mode ∈ {advise, auto}`; `recovery.mode: auto` additionally
  requires `probe` in `stages` (no auto recovery without the probe that
  both triggers and verifies it) AND `auto_receipts` present with a key
  for EVERY fleet unit — each value an evidence path (`docs/evidence/...`,
  the §16 step-5 receipt) or the literal `excluded` (that unit stays
  advise); a missing key is a validation error (A6): the config file
  itself enforces the supervised-verification sequencing, so no operator
  revision can skip advise and the supervised night in one edit. Boot
  verifies the named files exist and degrades a missing receipt's unit to
  advise with a loud note (validation stays offline-pure — the night-V2
  ruling).
- Runtime state does not persist (the standing doctrine): boot recomposes
  the block; there is deliberately no runtime toggle for any stage or for
  `recovery.mode` — the console can never flip authority into existence.

## 10. Audit, events, projection — additive vocabulary only

**Audit rows** (advisory class except R's, which ride the parking ledger):

- `health_census_recorded` — per unit per night: the full predicate vector,
  the evidence-window provenance, the verdict.
- `health_probe_completed` — per unit per night: commanded/measured figures,
  the qualifying-sample count, the echo classification, baseline figures,
  the verdict class.
- `health_recovery_outcome` — per unit per night: eligibility basis (census
  row + probe row), the cycle result, the verification verdict (including
  `recovered_unproven`, A2), the ladder rung that ended it, if any.
- R's mode writes produce the EXISTING parking rows — `unit_parked`,
  `unit_resumed` — under principal `energypod:health-adviser` with reason
  naming the watch: ONE ledger, one truth, the human-correlated narrative
  and the lease table unchanged. The lease `origin` vocabulary gains ONE
  additive word: `automation`, DERIVED not stored (A10 — `ParkLease` and
  the `park_leases` table carry no origin column, and the controller's
  payloads hardcode `operator` today): a row whose principal carries the
  `energypod:` prefix renders `automation`, anything else `operator`; the
  foreign/unrecorded/none semantics are untouched, and a word=1 under an
  open `automation` lease is OURS — resume by R-completion or by the
  operator, no takeover. The honest edges, stated: a boot-ADOPTED
  automation park derives `automation` from the adopted row's principal;
  where the pending row is evicted beyond the audit window, the standing
  `unrecorded` path renders — never a fabricated origin in either
  direction.

**Bus events:** `health.census` (notice tier; alert on persistence
promotion), `health.probe` (alert tier on fail classes; quiet otherwise),
`health.recovery` (alert tier on every outcome except `recovered`, which is
the resolved tier). Typed payloads mirroring the rows; no control path
subscribes.

**Projection** — the `health_watch_state` snapshot key (absent when
uncommissioned):

```json
"health_watch_state": {
  "stages": ["census", "probe"],
  "window": {"opens_local": "23:00", "deadline_local": "23:45"},
  "phase": "done",
  "as_of": "2026-08-25T23:41:05+10:00",
  "units": [
    {"unit_id": "rhs",
     "census": {"verdict": "phase_idle_or_ct_silent", "nights": 4,
                "note": "ct_link_suspect",
                "predicates": {"full": true, "still": true, "house_needed": true,
                                "load_unserved_in_phase": false,
                                "modes_normal": true}},
     "probe": {"verdict": "fail_no_response", "probe_w": 300,
               "qualifying_samples": 3, "core_samples": 20,
               "consecutive_fail_nights": 2,
               "echo": "echo_matches_write"},
     "recovery": {"mode": "advise", "verdict": "advised", "route": "b",
                  "attempts_total": 0, "consecutive_fails": 0}},
    {"unit_id": "lhs",
     "census": {"verdict": "nominal", "nights": 0, "predicates": null},
     "probe": {"verdict": "pass", "probe_w": 300,
               "qualifying_samples": 20, "core_samples": 20,
               "echo": "echo_matches_write"},
     "recovery": {"mode": "advise", "verdict": null,
                  "attempts_total": 0, "consecutive_fails": 0}}
  ]
}
```

Skipped units render their skip reason verbatim; uncommissioned stages
render `{"mode": "uncommissioned"}` — never absence-that-looks-like-health.

## 11. Alerting and the defined-restart advisory

**Severity tiers (the operator's four distinguishable mornings):**

| Tier | Trigger | Surface |
|---|---|---|
| notice | census `stuck_suspected`, first night | unit card watch chip; the predicate vector one tap away |
| alert | probe `fail_no_response` / `fail_partial` on any unit; census flag persisted ≥ `flag_persistence_nights` | alert-tier event; the watch card promotes the unit row |
| alert (urgent styling) | any recovery failure ladder end: `failed_write`, `write_unverified`, `failed_no_effect`, cap reached | fleet banner + the defined-restart advisory card, with the night's evidence excerpt |
| alert | `recovered_unproven` (A2: inconclusive verification re-run, or re-arm refused) — the cycle plausibly worked, the proof is missing | the advisory card with the morning-verification walkthrough; `attempts_total` beside it |
| resolved (info) | `recovered` — cycle verified AND the verification probe passed | the card's morning line: before/after figures, the cycle's checklist, `attempts_total`, one honest sentence on what was done |

Every alert carries its evidence the way the vendor's own tooling does
(the Be-Connect pattern): the audit row reference plus the telemetry excerpt
— census predicate vector, probe figures, the cycle's write/readback words —
so the operator can act without opening a log directory.

**The recurring-success arithmetic, owned (A11).** The never-thrash caps
bound FAILURES; a pod that fails, cycles, and passes EVERY night would mint
`recovered` indefinitely — up to 365 shallow cycles a year. Each cycle is
negligible by every sourced wear figure (~4 Wh, two mode transitions), but
a unit that NEEDS the cycle most nights is not wear — it is a hardware
story the advisory should have caught. The morning facts therefore carry
`attempts_total` and the trailing-30-night recovery rate per unit, so the
chronic case is VISIBLE in the operator's daily read and belongs, when it
appears, to the installer-with-logs tier of the ladder (§18 item 3 decides
whether a self-healing class makes auto-R needless at all).

**The defined-restart advisory — pinned verbatim, the escalation ladder's
terminal text** (EFT Systems BYD service guideline V1.5 procedure; it
extends, and where R has run supersedes the wording of, the standing
`PHYSICAL_RESTART_HINT`):

> Remote recovery exhausted. Defined-restart procedure, in this order:
> battery button OFF for 5 s; DC off; AC off; WAIT 10 MINUTES (the
> fuse re-engagement lockout — do not shorten it); battery ON FIRST,
> then AC, then DC. Then verify telemetry resumes, Debug Mode reads
> Normal Mode and SysControlMode reads Remote in the vendor MiniES app.
> Take the logs to the installer if the pod does not return.

The 10-minute wait and the battery-first ordering are load-bearing text,
not decoration — they ride the alert, the health hint, and the console card
identically. The advisory also names the on-device cross-check available for
every R cycle: force-state transitions are logged in the BMU event log
(vendor-documented), so a cycled pod's own log corroborates — or
contradicts — our ledger.

## 12. Doctrine amendments — precisely what is revised, and what never is

This feature revises TWO standing rules, each by a named, separately
commissioned path, each default-OFF. Everything else is inherited verbatim.

**REVISED — alarm-only `0x8000`.** DESIGN_POD_PARKING's hardening (and its
§9 flag 1 reversal: "no autonomous mode write, ever, including boot") stood
on the absence of any commissioned automation case for the write. The
operator has now commissioned one: Stage R, `recovery.mode: auto`, gated by
its own config block AND `stages` including `recovery` AND the `parking:`
block AND `mode: write_enabled`. The revision is scoped to exactly that
composition: one cycle per unit per night, one principal, values {0, 1},
every parking guard intact. Boot still never writes (I8); lease expiry
still never writes; every other 0x8000 path on Earth stays
interactive-only. REST park/resume remain `arm`-scope interactive routes —
the amendment adds an INTERNAL composer, it opens no API.

**REVISED — interactive-principal-only park/resume.** The parking doctrine
("every enabling act — arm, resume, enable, publish — is an interactive
human act; a timer is not a principal") is amended for R's bounded composite
in the `auto` posture: disarm (stop-direction, the safe family), park,
resume, one verification re-arm, disarm. The principled line the amendment
draws: a TIMER may still never resume (expiry remains alarm-only — R's
resume is the commissioned recovery act itself, not an expiry response, and
it carries its own audit principal and its own guards); and the sequence is
authority-net-zero (I9) — R never leaves a unit more enabled than it found
it, which is what keeps "enabling acts are interactive" true in substance
for everything outside the sequence. The one bounded re-arm inside the
sequence is this section's most aggressive sentence and is restated in
§7.4; `advise` posture contains none of it. Panel ruling 1, recorded: the
in-sequence re-arm is THE ONLY automation arm authority this controller
ever composes — no other adviser, stage, timer, or surface gains an arm
path from this contract's precedent, and any future automation arm must
survive its own panel.

**EXTENDED (A5) — crash ownership of the verified-but-unrecorded resume.**
Parking's divergence pass closes any open parked lease over word=0 as
`observed_foreign`; a crash between R's verified resume write and the
lease commit would therefore read OUR completed act as a foreign resume —
and a nightly program multiplies that window. The mechanism extends in
this round: resume-side adoption (§7.2 step 5) commits the lost closing
row as ours from the pending `unit_resumed` row plus the already-0 word,
a store write only — so the crash-ownership claim ("our acts stay ours
when they fail") now covers the verified-but-unrecorded case, not just
the pending-forever one. §15 amends DESIGN_POD_PARKING accordingly.

**AMENDED-VOCABULARY (additive).** The park lease `origin` vocabulary gains
`automation` (§10). `target_reached`-style widening: none — this feature
widens no existing word. `actuation_incoherent`'s semantics are NARROWED by
Wave 0 (stricter, not looser), and the recovery advisory of
DESIGN_POD_PARKING §7 gains the `commissioned` honesty for the watch
("the nightly program can execute this when
`battery_health_watch.recovery.mode: auto` is commissioned") — the
advisory never renders a capability the site lacks.

**NEVER REVISED — the enumerated standing rules:**

1. Values 2–6 of `0x8000` are PERMANENTLY PROHIBITED. The transport-layer
   `{0, 1}` bound, the named-method-only reachability, the
   architecture-fitness pin — all inherited; R adds no path around any of
   them.
2. The fixed sentence, verbatim, on every surface: *"Parking is not
   electrical isolation — the battery stays connected at full voltage.
   Never perform physical work on a parked pod."* No countdown R renders
   may imply time-bounded safety; the hold is policy, never safety.
3. No mode write while any competing writer or intent is active; no writes
   under foreign objective evidence; the foreign-writer latch and the
   night-writer detector are untouched, and R skips every unit they name.
4. The emergency stop remains supreme over everything (I7).
5. Boot never parks, never un-parks, never writes (I8).
6. Expiry is the ALARM, never the actor — inherited whole; it is R's
   crash-safety story.
7. Single-writer discipline: the probe is dispatch through the arbiter under
   its own principal; R never writes onto a unit another principal claims.
   The Fronius-app Battery Control wedge class is the documented reason
   this rule exists (§1.2).
8. `debug_modes_enabled` remains its always-false tombstone; the parking
   block remains the one policy flag that can compose the mode write; this
   feature's block composes the USE of that primitive, never a new one.

## 13. Console

- **The Health Watch card** (feature-detected on `health_watch_state`):
  one row per battery — census chip (nominal / flag nights / excluded
  reason), probe verdict with its one-line figures ("fail — 3/20 samples
  moved"), recovery verdict. The morning-after line renders the last
  night's outcome and, after a recovery, the before/after evidence with the
  honest done-what sentence.
- **The advisory card** (alert styling): the §11 defined-restart checklist
  in full, the night's evidence excerpt, and — in `advise` posture — the
  operator walkthrough (disarm → park → resume → re-arm → verify) with
  links to the standing surfaces. In `auto`, the same card reports what the
  program did and names the re-arm the operator owes.
- **The uncommissioned honesty**: stages absent from `stages:` render
  "not commissioned" where their UI would be — a site without R never sees
  "recovery" offered as a suggestion it cannot execute.
- **One rhythm / one vocabulary / live-refresh** per the standing bar; the
  not-isolation sentence rides the card's parked-state styling; shot matrix
  (states × viewports, before/after vision-verified) per the polish bar.
- MCP: `get_unit_detail`/`get_snapshot` gain the watch fields; NO mutation
  tool; the agent-loop text amended per §2.

## 14. Simulator contract

- `script_stuck(unit, at_s, echo_class)` — the spectator signature: SoC
  pinned ≥95, battery watts within ±50, load CT dead (≈0) while sibling
  hooks flow, mode words normal, and the ECHO CLASS PINNED (A3):
  `matches` yields the echoed-and-dead leg (census flags → probe
  `fail_no_response` → R advisory/cycle per posture); `not_served` yields
  the advisory-only leg (probe `inconclusive_echo_mismatch`, R never
  eligible). Both legs are end-to-end tested — R's reach and its limit
  are both first-class behaviors.
- Probe legs: `script_probe_delivery(fraction, samples)` — a delivering
  pod at 0.5–1.2× command (the fleet bias band) must PASS; 0.0× must fail
  `fail_no_response`; 0.3× must fail `fail_partial`; a spike pattern
  (90% at zero, 10% at 2×) must NOT pass (the 80% rule).
- Cycle legs: the existing `script_debug_mode` plus
  `script_cycle_wedge(exit_fails=True)` — the exit readback refusing, to
  exercise the `write_unverified` ladder end-to-end.
- The demand/skip hooks: guard refusals render skips, never fails.
- The night-writer detector stays quiet through a scripted probe (the
  claimed-unit gate — the pinned test's simulator side).

## 15. Same-round amendment sweep (mandatory, before implementation)

1. `docs/PROTOCOL_EVIDENCE.md` §5 note: the sanctioned `{0, 1}` surface now
   names TWO composers — the parking routes and this design's Stage R
   (gated as §9/§12 state); values 2–6 language unchanged.
2. `docs/API_CONTRACTS.md`: the write-enabled section names the health
   watch's probe as dispatch under `energypod:health-adviser`; the
   "operator-only REST surface" sentence for park/resume gains the internal
   composer clause; the snapshot key and events from §10.
3. `docs/DESIGN_POD_PARKING.md`: §7's recovery advisory commissioning
   honesty extended (§12 here); §3's origin vocabulary note gains the
   `automation` DERIVATION rule (principal prefix, §10 here — no schema
   change); §4's boot adoption gains the resume-side twin (A5: a pending
   `unit_resumed` row plus an already-0 word closes as OURS inside the
   bounded recency window — the crash-ownership extension).
4. `docs/CONTROL_SURFACE_GAP_ANALYSIS.md`: the standing-exclusion row notes
   the one sanctioned automation composer; Part 2's "do BETTER" item
   re-pointed.
5. `docs/CONTINUITY.md` non-negotiable invariants + protocol facts amended
   by the coordinator thread at round close (this design does not edit it).
6. The policy config block comments (recovery keys) gain Wave 0's two new
   keys with their justification lines, beside the existing watchdog text.
7. `docs/DESIGN_SCHEDULES.md` (A15): the load-bearing distinction —
   advisers are window-gated by their OWN config blocks, never by the
   schedule's allowed-window union; the union governs published schedules
   only — is this design's reliance and belongs in that contract's own
   text, not only in this one.

## 16. Commissioning sequence

Wave 0 first, always; then the stages in order, each a config revision plus
a restart — the authority ladder is climbed one deliberate step at a time.

1. **WAVE 0** (§3): both monitor fixes, their tests, and a quiet-night
   observation window — the flapping counter and the coherence baseline
   verified clean on live telemetry BEFORE anything composes on top of them.
2. **CENSUS** (config revision one): `stages: ["census"]` with the §9
   defaults; requires `plant_history` (already commissioned on this site).
   Runs nightly, writes nothing, accumulates `health_census_recorded` rows.
   Minimum run: enough nights to see the quiet/flag boundary — including at
   least one night rhs exhibits (or definitively does not).
3. **PROBE** (config revision two): `stages: ["census", "probe"]`. The
   units must be ARMED at 23:00 for probes to run (the program never arms);
   the first probe nights are supervised reads of the morning-after card,
   not attended events — the probe is 300 W × 50 s of ordinary dispatch.
4. **RECOVERY-ADVISE** (config revision three): `stages: ["census",
   "probe", "recovery"]`, `mode: advise` — the standing posture. On a
   flagged night the operator executes the walkthrough through the standing
   surfaces; the program records and correlates. This step is where the
   operator calibrates their own trust in the census/probe conjunction.
5. **THE SUPERVISED LIVE VERIFICATION** (the gate to `auto`, once per
   unit, mirroring `docs/evidence/standby-cycle-2026-08-24.md`): with an
   operator AT the console (and per DESIGN_POD_PARKING §7, at the pod the
   first time per unit), one eligible unit, one night: the program runs its
   full auto cycle under observation; the evidence file records the
   transition table — dry read, disarm row, park write/readback/times,
   hold observations (comms age, pack V, SoC, measured watts), resume
   write/readback/times, the verification probe's figures, the baseline
   return — plus the 0x8100 word throughout and the before/after probe
   comparison. The unit's BMU event log is pulled afterward as the
   on-device cross-check (vendor-documented force-state logging). The
   evidence file lands in `docs/evidence/` and is the receipt
   `auto_receipts` names (A6).
6. **RECOVERY-AUTO** (config revision four): `mode: auto` PLUS the
   `auto_receipts` map naming every fleet unit's evidence path or its
   explicit `excluded` — the validation refuses the revision without it
   (A6), which is what makes this sequence unskippable in one edit. The
   §12 amendments take effect from this revision and not before. (Wave 0
   and the stages ship in the same release cuts as their compositions —
   the A13 release-cut pin, §3.)
7. **STEADY STATE**: the morning-after card is the operator's daily read;
   the consecutive-fail cap and the advisory are the terminal story; the
   thresholds are re-examined after the first season with the census rows
   as the evidence base.

## 17. Test matrix (author FIRST, per repo doctrine)

- **T-BHW-WAVE0** — the deadband: zero-straddling float samples render
  steady health (the 158-transition night replays to ~0); the ±33 W class
  both sides; beyond-deadband float still self_healing; config bounds.
  The delivery fix: steady delivery at 87% and 96% of command NEVER trips
  (the two live false positives as named regression vectors); a short
  authorization gap does not re-anchor the baseline; a gap beyond
  `coherence_gap_grace_s` does; a dead pod still alarms; echo gating —
  `echo_unreadable`/`external_writer` episodes never open the state; I10:
  no automated response exists keyed on `actuation_incoherent` at all.
- **T-BHW-CONFIG** — absent block composes nothing (byte-identical
  snapshot); prefix rule; every §9 gate and bound with its named-message
  refusal; window disjointness incl. the schedule-union check; the TWO
  deadline-arithmetic refusals (A1: program-fit and worst-act-before-night,
  each naming its arithmetic) and a started-at-deadline act completing
  inside its bound; timezone-mismatch refusal (A9); `min_soc_hours`/
  `evidence_window_h` with equality allowed (A9); probe requires
  write_enabled; recovery requires parking + write_enabled + probe in
  stages; `auto_receipts` — every fleet unit keyed, path-or-excluded
  values, missing-key refusal, boot's missing-file degradation to advise
  (A6); no runtime toggle exists for stages or mode.
- **T-BHW-CENSUS** — the predicate vector: each predicate's hold/fail edge;
  S3's either-corroborator; S4′ IN-PHASE non-following (A16) with both
  named shapes: the GARAGE-PHASE shape — SoC pinned ≥95, own-phase load
  word ~0, siblings actively flowing — censuses `phase_idle_or_ct_silent`
  or `nominal`, NEVER `stuck_suspected`; the IN-PHASE shape — own-phase
  load above `load_floor_w` while the battery stays still — still flags
  (with S1/S2/S3/S5); the dead-link-vs-idle-phase annotator over
  `phase_live_window_h` (a word that never moves annotates
  `ct_link_suspect`; one that moves clears it; the note never promotes);
  S5 excludes parked and vendor-mode units (owned elsewhere); historian
  gaps render `degraded_evidence` (never interpolated, never a verdict);
  persistence promotion; zero extra frames (the read-plan pin);
  audit/event/projection shapes; uncommissioned-stage honesty.
- **T-BHW-PROBE** — skip-if set each rendering its skip; the pass math:
  0.5×/0.8× thresholds, the fleet-bias band passes; the FULL verdict matrix
  (A3) — every measured × echo × baseline cell routes to its pinned
  verdict, with the echoed-and-dead row → `fail_no_response` and the
  still-and-not-served row → advisory-only; spike patterns never pass;
  preemption → inconclusive, with MANUAL/AGENT/e-stop as the preempting
  classes and a schedule claim NOT preempting (A12); return-to-baseline and
  its `inconclusive_baseline_confounded` downgrade on a mid-leg demand move
  (A8); the quiet-load gate on grid-import words incl. the
  stale-evidence skip; explicit cancel; sequential ordering and
  inter-unit gap; detector-quiet-through-probe (claimed gate); disarmed
  unit → skipped and counted; no probe outside the window; restart
  mid-probe → inconclusive, once per night.
- **T-BHW-RECOVERY** — eligibility: route A's conjunction (flag-without-
  fail, fail-without-flag, inconclusive-probe all refuse) AND route B
  (A16: soft-note unit + `probe_fail_nights` consecutive
  `fail_no_response` verdicts eligible; ONE failure not; a `nominal`
  unit's repeated failures NOT — the route is scoped to the
  structurally-blind class); the §7.2 sequence with
  audit rows under the health principal and origin `automation` (the A10
  derivation, incl. the adopted-row and evicted-window edges); lease
  bounds from the parking block; hold bounds; foreign word=1 → skip+alert;
  word 2–6 → skip+alert; values {0,1} structural (mutation tests on the
  composed path — no new transport call exists); the ladder rungs in order
  incl. resync-then-one-reissue, ACKed-unverified → NO further write (I3
  across restart); `failed_no_effect` counting to the cap and
  `recovered_unproven` NOT counting (A2); the refused-re-arm paths (a
  foreign objective → the operator takeover acknowledgement; a latched
  stop) ending `recovered_unproven` with the unit disarmed; the
  inconclusive verification re-run never re-issued (I1); resume-side
  adoption of the verified-but-unrecorded crash (A5) and its
  outside-the-window honest degradation; cap → advisory-only until
  acknowledgement; the verification probe re-run and its verdicts; advise
  posture performs NO disarm/park/re-arm ever; once-per-night across
  restart (durable-row derivation).
- **T-BHW-NEVER-THRASH** — the meta pin: across any interleaving of
  restarts, guard changes, and repeated triggers, the invariant set I1–I11
  holds; property-style: no sequence of inputs produces two cycles per unit
  per night or a post-unverified write.
- **T-BHW-EMERGENCY** — stop mid-program (idle, probing, mid-hold, mid-
  exit): bounded zero, no further program write, lease follows standing
  rules, alert names the state it stopped in.
- **T-BHW-RESTART** — program interrupted in every phase reconstructs
  conservatively; a night with rows never re-runs; boot never starts the
  program (I8); the interrupted-composite case (A4): health-adviser rows
  with no `health_recovery_outcome` row — including the crash-after-re-arm
  state (armed, normal, no lease) — raises the alert naming the resulting
  state and retires the night; simulator-mode (memory store) behavior
  pinned.
- **T-BHW-PROJECTION/EVENTS** — additive keys only; absent-block frames
  byte-identical; tiers per §11; skip reasons verbatim; the
  `origin: automation` lease vocabulary end-to-end (park_state, dispatch
  provenance, resume paths).
- **T-BHW-ARCHITECTURE** — the controller composes ParkController and the
  facade intent path; NO new transport write method; the application module
  imports no adapters.providers (the historian port pattern); no control
  path subscribes to the health events.
- **T-BHW-CONSOLE** — the card's states × postures (uncommissioned stages,
  notice, alert, urgent, resolved); the advisory checklist verbatim incl.
  the 10-minute wait and battery-first ordering; the not-isolation sentence
  where parked styling appears; the morning-after line; shot matrix.

## 18. Open questions — the operator unknowns this design preserves

Each one is load-bearing somewhere above; the design's defaults are chosen
so that any answer changes a THRESHOLD or a POSTURE, never a safety rule.

1. **Always rhs — and is rhs even the suspect it read as?** Every
   exhibiting data point is the 50-cell unit, but A16's
   phase-concentration fact PARTIALLY REHABILITATES rhs: if it is the
   garage-phase pod, its ~16 W CT average and still battery may be an
   idle phase, not a wedge. Two discriminating facts are pending — the
   phase-map verification (which pod owns which phase; the load-sharing
   round's commissioning step) and whether rhs's CT word EVER moves when
   its phase's circuits are used. Under EITHER hypothesis its multi-year
   SoC pinning (never below 92–97%) stands, and that is the parallel
   calibration program's concern. The census rows (with the
   `ct_link_suspect` annotator) and route B's probe verdicts will answer
   the rest.
2. **Did reads stay alive during the historical stucks?** This decides
   Stage R's reach: the standby cycle is only WRITABLE into a pod whose
   comms answer. If the historical class was comms-dead, those units were
   `not_responding` — R never applies, and the defined-restart advisory is
   the entire remote story (the standing R5 rail, unchanged). If reads
   stayed alive (the wedge-with-telemetry class), R is the designed answer.
   The operator's memory of the April–August events is the only evidence
   source; the design carries BOTH paths and this question picks the
   emphasis.
3. **Historical fix duration** — how long stucks lasted and what ended them
   (self-heal? a power cycle? the next dawn?). A class that self-heals in
   hours argues for census+advisory and against auto-R (a cycle at 23:00 on
   a pod that would have healed by 02:00 is a needless write); a class that
   persisted for days argues for auto. `min_soc_hours` and the whole staged
   posture are calibrated against this answer.
4. **Do you use the Fronius app's Battery Control?** The documented
   wedge-both-apps class (§1.2). If yes: the foreign-writer guard and the
   single-writer discipline are the fence, and the census should EXPECT
   occasional foreign objectives at night rather than treat them as
   anomalies; if no, foreign evidence on a watch night is a stronger signal.
5. **Firmware vintage** — are all three pods on the same firmware, and does
   it match the rhs unit the standby cycle was proven on? The live proof is
   one unit, one firmware, one trial; the per-unit supervised live
   verification (§16 step 5) is the design's answer, but a mixed-vintage
   fleet should say so before step 6.
6. **The re-arm rule after a recovery** (§7.4): accept the bounded
   in-sequence re-arm in `auto`, or rule the posture down so verification
   is always the operator's act? Both are designed; the choice is the
   operator's and the default (`advise`) assumes nothing.
7. **During a historical stuck event, did the served-objective word
   (0x1060+17) follow remote writes?** (A3.) The verdict matrix makes R's
   reach conditional on the answer: if the stuck class echoes our write
   while never actuating (`echo_matches_write` + stillness →
   `fail_no_response`), Stage R addresses it directly; if the served
   objective never moved (`objective_not_served` + stillness), R is
   inapplicable to the real population and the defined-restart advisory is
   the whole remote story. The operator's memory of the April–August
   events — or one deliberately supervised probe into the next event —
   answers it, and the answer decides whether §7 is load-bearing or a
   well-guarded empty room.

## 19. Future extensions (documented, deliberately unbuilt)

Per-unit threshold graduation from accumulated census rows; a charge-
direction probe variant for the low-SOC stuck class (none observed); the
BMU event log as an automated cross-check read (none of its registers are
in the vendor read inventory — evidence first); census-driven SoC-
calibration correlation with the energy scorecard's day records; a "verify
now" operator-triggered probe route (interactive, the §7.4 escape hatch
formalized); asymmetric hysteresis on the flag promotion tiers.

**Boundary with future programs (A14):** future maintenance programs — the
in-flight calibration-cycling research among them — take their OWN
top-level config block and their OWN window, and do NOT extend this
block's stage enumeration or window machinery; they land beside this
contract as siblings, never inside it as stages.

## 20. Amendment log (v1.1 — the panel round)

The adversarial panel's verdict: IMPLEMENTABLE-WITH-AMENDMENTS; nothing
unsafe found. All fifteen amendments are folded in-place above; this log
is the index.

- **A1 (MAJOR)** — the deadline arithmetic was inconsistent (a ~21-min
  program against a 23:45 deadline could complete at 00:06, inside the
  night window; I2 contradicted itself). The deadline is now the
  no-new-ACT boundary with TWO validated arithmetic checks (program fit;
  worst-case in-flight act before night open), and I2 says "no act STARTS
  outside the interval; a started act completes within its bound".
  §2/§4/§8-I2/§9/§17.
- **A2 (MAJOR)** — the ladder lacked rungs for the INCONCLUSIVE
  verification re-run and the REFUSED re-arm (a foreign write during the
  park makes the next arm the operator's takeover acknowledgement; a
  latched stop also refuses). Both now end at `recovered_unproven`:
  alert, unit disarmed, the operator's morning verification the named
  exit, never a re-run (I1), excluded from the consecutive-fail cap.
  §7.2/§7.4/§8/§11/§17.
- **A3 (MAJOR)** — the echo routing for the stuck class was unstated, and
  under the pinned ACK-then-ignore device model Stage R could have been
  dead code on its exact target population. The verdict matrix now pins
  `fail_no_response` ≡ stillness + `echo_matches_write`, and stillness +
  `objective_not_served` ≡ advisory-only; `script_stuck` pins its scripted
  echo class; open question 7 asks the historical echo behavior that
  decides R's reach. §6.3/§7.1/§14/§17/§18.
- **A4 (MAJOR)** — a crash after the re-arm left a unit armed, normal,
  lease-less, and un-alerted — silently night-charging. Restart
  reconstruction now detects the interrupted composite (health-adviser
  rows with no `health_recovery_outcome` row) and alerts naming the
  resulting state; "an attempt" is defined as any health-watch row for
  that unit-night, so one crash retires the night unambiguously.
  §4/§8-I8/§17.
- **A5 (MAJOR)** — a crash between the VERIFIED resume write and the lease
  commit would read our completed act as a foreign resume (parking's
  divergence closes any open lease over word=0; boot adoption existed for
  parks, not resumes). Resolved by MECHANISM — the panel offered
  mechanism-or-honest-misattribution, and the mechanism is stronger and
  symmetric with park-side adoption: a pending `unit_resumed` row plus an
  already-0 word commits the lost closing row as OURS inside the bounded
  recency window. §7.2/§12/§15.
- **A6 (MAJOR)** — `recovery.mode: auto` was protected only by prose; an
  operator's first revision could skip advise and the supervised
  verification in one edit. `recovery.auto_receipts` now names
  supervised-verification evidence (or an explicit per-unit `excluded`)
  for every fleet unit; auto without it is a validation error, and boot
  degrades a missing receipt's unit to advise loudly. §9/§16/§17.
- **A7 (MINOR)** — the authority invariant was stated three ways with
  different teeth; unified on §12's phrasing ("never MORE enabled than it
  found it; normally ends disarmed"), and I9 reworded to
  "disarmed-and-Normal, or alerting that it could not". §2/§8-I9/§12.
- **A8 (MINOR)** — the quiet-load gate now reads fleet-mean grid IMPORT
  (the control-grade PCS word — not the load-CT mean, which a dead CT
  biases low and which is per-phase load, not house draw), skips on stale
  evidence, re-checks at cancel, and downgrades a demand-confounded
  baseline judgment to `inconclusive_baseline_confounded`; the brief
  ~300 W export case is named in §6.3. §6.1/§6.2/§6.3/§9.
- **A9 (MINOR)** — cross-validations added: `min_soc_hours ≤
  evidence_window_h` with equality intended and allowed;
  `flag_persistence_nights ≥ 1`; block-vs-site timezone divergence
  refused. §9.
- **A10 (MINOR)** — `origin: automation` is DERIVED (the `energypod:`
  principal prefix), not a schema change — `ParkLease` and `park_leases`
  carry no origin column today; the boot-adopted and evicted-window edges
  render honestly. §10/§15.
- **A11 (MINOR)** — never-thrash bounded failures, not successes: the
  recurring-success arithmetic is owned (a nightly fail-cycle-pass loop is
  negligible wear but a hardware story), and `attempts_total` plus the
  trailing-30-night rate ride the morning facts so the chronic case is
  visible. §8/§11/§18.
- **A12 (NOTE)** — precedence corrected: the probe rides `optimizer`,
  which outranks `schedule`; it is preempted by MANUAL/AGENT/e-stop claims
  only, and the preemption test uses those claimant classes. §4/§6.2.
- **A13 (NOTE)** — Wave 0's ordering is pinned as a RELEASE fact (same
  cut as any stage composition), with the panel's verification recorded
  as fact: no rung of this program writes on a signal Wave 0 fixes —
  I10's test is the structural pin. §3/§16.
- **A14 (NOTE)** — future maintenance programs (the in-flight
  calibration-cycling research) take their own top-level block and window,
  beside this contract, never inside its stage enumeration. §19.
- **A15 (NOTE)** — the "advisers are window-gated by their own blocks,
  not the schedule union" distinction is amended into DESIGN_SCHEDULES,
  where a load-bearing rule belongs. §4/§15.
- **A16 (MAJOR, v1.2 — operator evidence)** — the house load is
  concentrated on ~1.5 of 3 phases and each pod CT-follows only its own,
  so the census's cross-pod spectator predicate would FALSE-FLAG the
  garage-phase pod. S4 is now PHASE-RELATIVE (S4′ in-phase non-following:
  the unit's OWN CT word shows load while its battery stays still); the
  cross-pod shape is retired to a soft informational note
  (`phase_idle_or_ct_silent`, never a flag, never promoting) annotated by
  a dead-link-vs-idle-phase discriminator (`phase_live_window_h`); rhs's
  ~16 W CT is recorded under BOTH hypotheses with the phase-map
  verification as the external discriminator, partially rehabilitating
  rhs as a stuck-mode suspect (§1/§18) while its SoC pinning stands for
  the calibration program; route B makes an unobservable-phase unit
  R-eligible after `probe_fail_nights` (≥2) consecutive probe failures —
  repetition substituting for the census half; and the evening
  load-sharer's 22:30 end is pinned as exclusive-before this window, with
  the cross-block validation joining §9 when that contract lands.
  §1/§4/§5/§7.1/§9/§17/§18.

**Panel rulings, recorded verbatim (the v1.1 round):**

1. "RE-ARM AFFIRMED as designed, with A2+A6 as conditions; the in-sequence
   re-arm must be stated in §12 as the ONLY automation arm authority ever
   composed; shipping advise-only-v1 would defer ever testing the
   program's central claim end-to-end, and with A6's config-enforced
   sequencing the panel prefers the contract's own sequencing."
2. "CRASH WALK: crash-SAFE at every arrow; crash-HONEST at only some — A4
   and A5 complete the story; no interleaving leaves an unowned write or
   unowned parked word."




