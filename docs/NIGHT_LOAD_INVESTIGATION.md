# Night-load investigation — why the ~150-200 W nighttime import is not served by the pods

Written 2026-08-23 (site evening 21:11-21:26, Australia/Brisbane), read-only:
every live fact below came from GET requests to the running controller
(`http://127.0.0.1:8080/api/v1`) plus read-only inspection of the vendor
decompile, the prior integration, and the durable audit/ledger stores. No
dispatch, no restart, no configuration change. The remedy is an operator
decision; this document only measures and frames.

---

## 0. The answer, in one paragraph

**The pods ARE serving the night load — essentially all of it.** During the
entire 14-minute measurement window (21:11-21:26 site), all three pods ran
autonomously in load-matching (`run_mode_w` 0 "Matching Load", `work_mode_w`
2 "Economy", `ctrl_mode_w` 1 Remote, `debug_mode_w` 0 Normal — every sample,
every unit) and discharged 0-536 W tracking their phases' loads minute by
minute, with no intent from us (all units `disarmed`, empty schedule plan).
What the home meter registers as the "consistent ~150-200 W" is the sum of
three small **per-phase residuals of roughly 20-68 W each** (26-minute means:
lhs 40.7 W, mid 40.5 W, rhs 27.9 W; fleet mean **109 W**) that the pods'
own matching loop never closes — the same ~40 W/phase residual appears in
the 2026-08-22 evidence capture at thirty times the load, so it is a fixed
floor, not an unserved fraction of house load. The best-supported reading of
that floor is the pods' own continuous standby draw on their AC input side
(the existing evidence row already attributes "~39 W standby draw" to mid on
that capture; PROTOCOL_EVIDENCE §4c), possibly compounded by a small firmware
matching dead-band. Nothing is holding the pods back: no PV gating, no
night-idle mode, no external writer, no SOC floor — the mode words are the
optimal autonomous state all night, and the audit shows zero
`external_writer` latches across the last 33 hours including two nights.

---

## 1. Measured present state (live, GET-only)

### 1.1 The sampled window

13 snapshots at ~70 s spacing, site 21:11:35-21:25:35 (2026-08-23), via
`GET /api/v1/snapshot`. Sign convention per PROTOCOL_EVIDENCE §4b/§4c
(live-proven): battery watts positive = discharge; grid word negative =
import.

| Unit | battery W (discharge) | grid W (import) | load CT W | run/work/ctrl/dbg | SOC |
|---|---|---|---|---|---|
| lhs | 397..536, mean 472 | -45..-32, mean -39 | 320..359 | 0 / 2 / 1 / 0 | 70-71% |
| mid | 139..398, mean 274 | -68..-33, mean -43 | 183..339 | 0 / 2 / 1 / 0 | 88% |
| rhs | 0..166, mean 96 | -44..-20, mean -28 | 0..26 | 0 / 2 / 1 / 0 | 98% |
| **fleet** | **mean 842 W delivered** | **-133..-95, mean -110** | **mean 554** | uniform | — |

Supporting facts from the same window:

- **Import, independently integrated by the energy scorecard** over 26.1
  minutes (durable `energy_baseline`, wall-time anchored): lhs 40.7 W,
  mid 40.5 W, rhs 27.9 W — **fleet mean 109 W** — with
  `export_watt_seconds` exactly 0 on all three (the pods never exported).
  `GET /api/v1/energy/days` returns no completed records yet (the scorecard
  was commissioned mid-day; the first day roll lands at site midnight), so
  the baseline is the live per-phase integration of record.
- All units `lifecycle: disarmed`, `inhibit_latched: false`, no active
  stops, `intent: null`, schedule plan v5 **empty** (posture `yield`,
  allowed windows `06:00-20:00`), excess adviser disabled. Every watt the
  batteries moved tonight was the pods' own autonomy.
- **Matching genuinely throttles to zero**: at the one sample where rhs's
  load CT read 0 W, rhs's battery read 0 W — the PCS runs happily at small
  outputs (83-166 W following a 0-26 W seen load) but never goes far enough
  to close the last ~20-45 W of grid import.
- Dynamic limits: discharge headroom 6.6-8.0 kW per pod (rhs at 98% SOC has
  a 0 W *charge* limit — it is full — but discharges freely).
- No correlation between battery output and residual import: lhs swung its
  output by 139 W while its import moved 5 W; mid swung 259 W for 35 W. The
  residual is a **floor**, not a proportional loss.

### 1.2 The same floor at 30x the load (prior capture, 2026-08-22)

PROTOCOL_EVIDENCE §4c's capture: import `-1736 / -37 / -48 W` against load
CT `1701 / 1063 / 1781 W`. rhs and lhs were matching ~1.1-1.8 kW loads and
still imported only 37-48 W; mid, idle, imported 1736 W against a 1701 W
load CT — a ~39 W gap the evidence row itself attributes to standby draw.
Tonight's residuals (20-68 W) sit in the same band. Two independent
captures, loads from 26 W to 1.8 kW: **the residual is a constant of the
installation, ~20-50 W per phase, ~95-135 W total**, which is exactly the
"consistent ~150-200 W" the operator's home meter reports (the operator's
band plausibly includes hours when the meter also catches loads above the
floor).

### 1.3 What the pods did overnight (audit evidence)

- Tonight's awareness layer recorded `unexpected_autonomy` (measured watts
  outside the `expected_autonomy_band_w [-2600, 300]` while no intent claims
  the unit) at 39-85 events/hour from site 16:00-20:00 — lhs and mid
  load-matching above 300 W all afternoon/evening, disarmed. This is the
  "evening Matching Load" behaviour the operator already knows (lhs held
  ~780 W, CONTINUITY 2026-08-24).
- The audit is **silent from site ~00:00 to ~06:00** (the census's 7.3 h
  overnight blind spot, repeating tonight): no control decisions, no
  autonomy-band excursions, no inhibits. Zero events is consistent with the
  pods matching loads below 300 W all night (the recorder's band tops out at
  +300 W) — it does not prove it; see §4 and the open questions.
- **A schedule window ran after sunset tonight**: the operator's "Testing"
  plan published at 19:34, armed 19:32 (all three units,
  `arm_sole_writer` — the arm-time preflight found no foreign PQ objective
  even while lhs was matching >300 W), and the window 19:35-19:36 executed
  **38 authorized cycles at {lhs: 999, mid: 999, rhs: 999} W discharge**,
  `safety_checks_passed` on every one, ending by non-renewal at window close
  with watchdog hand-back to autonomy. Sunset in Brisbane in late August is
  ~17:30 — this is direct evidence that **per-battery dispatch works in
  darkness with zero PV**.

---

## 2. The engagement rules (vendor evidence)

Searched `C:\Users\vagrant\Downloads\EnergyPod_RE\src` (decompiled service
tool) and the vendor protocol summary reverse-engineered from it.

**Mode vocabulary** (`GlobalFun.cs:167-212`):

- `run_mode_w` — PCS run mode: **0 "Matching Load"**, 1 Remote PQ Power,
  2 Remote PF Power, 3 Remote PF Current, 4 Remote DC voltage
  (`GlobalFun.cs:182-188`).
- `work_mode_w` — system work mode: **2 "Economy"**, 6 "Remote dispatch",
  8 "Timing" (`GlobalFun.cs:169-176`).
- `ctrl_mode_w`: 1 Remote, 2 Local. `debug_mode_w`: 0 Normal Mode,
  1 Standby, 2 Charge, 3 Discharge, 4 Circulation, 5 Fixing SOC,
  6 Verify Capacity (`GlobalFun.cs:152-165`).

**The service tool exposes NO engagement configuration.** The entire
write surface is: PQ dispatch `[1, P, Q]` at `0x0200` renewed every 1000 ms
(`SysControl.cs:1442-1461`, `MiniESapp.cs:2221-2230`), debug/factory mode
at `0x8000` (`MiniESapp.cs:2159-2170`), and comms/identity/grid-standard
maintenance at `0x8002/0x8008/0x8018/0x8034-0x8037`. Work mode is
**display-only** (`MiniESapp.cs:1376`). There is no load-matching
threshold, no SOC-reserve floor, no minimum-output setting, no PV gating,
and no day/night logic anywhere in the tool — those behaviours live in the
pod firmware, which the tool cannot configure. The PQ path is refused
unless debug mode is 0 (`MiniESapp.cs:2180-2183`); all pods read 0 tonight.

**Conclusions against the candidate explanations:**

- **Threshold above which matching engages?** Not in evidence, and
  refuted operationally: rhs matched loads of 0-26 W (battery 0-166 W).
  Matching engages at essentially any load; what it does not do is close
  the last ~20-50 W per phase.
- **PV-gated (matching only after a PV day)?** No vendor evidence of any
  such gate, and tonight's discharge (0-536 W per pod after sunset) refutes
  it for this fleet.
- **SOC floor for autonomous night discharge?** None visible in the tool;
  the pods matched tonight at 70-98% SOC with kW of discharge headroom.
  (Our own kernel's 10% floor governs only OUR dispatches.)
- **Minimum PCS output below which it won't run?** It runs at 83-166 W
  (rhs). Whether the firmware would hold a commanded ~40-50 W objective is
  **untested** — the smallest live-proven objectives are -200 W (the
  2026-08-22 direction trial) and 999 W (tonight's 19:35 run).
- **Mode misconfiguration?** No: run 0 / work 2 / ctrl 1 / debug 0 is the
  fleet-uniform, self-managing state — exactly the state in which the pods
  serve the most night load they can.
- **Externally held?** No (§4).

**The prior integration (C:\Users\vagrant\Downloads\modbus) never served
night load either.** Its default schedule was `"Night Charge" 00:00-06:00
at 3000 W` (`byd/config.py:76-79`) and its scheduler is charge-only
(`byd/scheduler.py:106-142`; outside windows it resets the pod to "normal
mode"). Its status inference treats any discharge >30 W with grid <10 W as
"Load Matching" (`byd/battery.py:320`) — i.e., it *observed* the same
autonomous matching we measured, and its own answer to night was to charge
into it, not to serve it. Its `stop()` wrote force-state 5
(STANDBY_TRICKLE_CHARGE) at `0x0200` (`byd/battery.py:304-306`) — a
reminder that the objective register doubles as the legacy force-state
word (PROTOCOL_EVIDENCE §4b), which is why two writers at night is a real
collision, not a hypothetical.

---

## 3. Why the ~150-200 W goes unserved — the mechanism

Per-phase power balance tonight: the pods inject battery power onto their
phases until their grid CTs sit at a small residual import (~20-50 W); the
load CTs read loads the pods largely cover. The residual:

1. is **constant** across loads from 26 W to 1.8 kW (two captures, §1.1-1.2),
2. is **real energy** — the operator's home meter registers it as
   consumption (a pure CT offset would not appear on the revenue meter),
3. is **never closed autonomously** — the pods stop matching at it (rhs
   idles its battery at 0 W while still importing 25-44 W),
4. has **no vendor-configurable remedy** in the service tool (§2).

The most-supported physical reading: each pod's own electronics draw
roughly 30-45 W continuously from the grid side of their phase — the
08-22 capture's "~39 W standby draw" — which sits outside what the
matching loop drives to zero, optionally compounded by a small firmware
dead-band. Distinguishing standby draw from a dead-band needs one further
measurement (a ~50 W commanded discharge while watching the grid CT), not a
document. Either way, the actionable conclusion is identical: **the floor
is only closable by an explicit PQ objective of ~40-50 W per pod** — the
firmware's autonomy will not do it on its own.

---

## 4. The night-writer interplay — are the pods being held?

No. Across the 5,179 audit events pulled (site 08-22 21:33 → 08-23 20:24):

- **Zero `external_writer` latches** — none overnight, none in 33 hours.
  The only inhibit cycle was mid, site 14:14-14:17, acknowledged and
  unrelated to writers; the four emergency stops (09:14-10:18) were
  operator presses, all acknowledged.
- All four mode words are the self-managing set on every unit in every
  sample tonight (`0/2/1/0`). No pod is in Standby/debug, Local, or a
  Timing profile that idles it.
- The arm-time sole-writer preflight — the mechanism that would catch a
  night writer — **armed all three units cleanly at 19:32 tonight**
  (`arm_sole_writer`) *while lhs was actively matching above 300 W*: in
  Match-load the served objective readback reads zero, so autonomy does not
  look like a foreign writer. (The `pod_autonomy` classification covers
  only negative P — a *discharging* foreign objective would still latch,
  `src/energypod/application/actor.py:824-839` — but the pods' matching
  does not present as one.)
- The honest blind spot stands: the audit is dark site ~00:00-06:00 and
  this host cannot see other LAN clients of the gateways (census). If the
  external apps write at night, tonight's evidence says their writes are
  either absent or benign to the pods' matching — the pods were in the
  optimal state at 21:26 and the arm preflight found no objective at
  19:32. The conclusion "nothing is holding the pods back" is strong but
  rests on the evening shoulder, not the 02:00-05:00 dead zone.

**Therefore the fix class is not "coordinate with the external apps to
stop holding the pods" — they are not holding them. The only role the
external writers play is on OUR side of the option space: if WE start
holding PQ objectives at night, we become a second writer in their window
(§5, option ii).**

---

## 5. Options, ranked

### (iii) — Trial first: a small manual evening discharge (recommended first step)

**What it is.** One evening, arm and dispatch a manual per-battery intent
of ~40-50 W per pod (e.g. `POST /api/v1/intents` with
`watts_by_unit {lhs:50, mid:45, rhs:30}`) while watching the snapshot's
per-phase `grid_power_w`.

- **What it takes.** Nothing new: manual intents, per-unit watts, and the
  cancel path are all live and operator-tested. Units arm cleanly in the
  evening shoulder (proven 19:32 tonight).
- **Conflicts.** None with posture (manual intents are not window-gated),
  none with the external writers for a bounded 5-minute trial. The
  actuation-coherence watchdog's 150 W floor means a 40-50 W setpoint sits
  in its "refuses to conclude" dead zone — no false alarms by design
  (CONTINUITY 2026-08-24 recovery entry).
- **Expected coverage.** The trial answers the two open physics questions
  in one shot: does the PCS hold a ~45 W objective, and does the grid CT
  go to ~0 while it does? If yes, full coverage of the floor is proven at
  the cost of one evening's curiosity.
- **Limit.** Manual intents are capped at 300 s TTL (`src/energypod/api/
  rest.py:99`) — this can never be the standing overnight cover; it is the
  cheap experiment that de-risks the real options.

### (ii) — Our night schedule serving the deficit (the real cover, if wanted)

**What it is.** A published schedule entry (or entries) commanding
~40-50 W per battery across the night, e.g. 20:30-06:00.

- **What it takes.** The PARTITION grant, by design: a commissioned config
  revision widening `schedule.allowed_windows_local` beyond the day-only
  default, a controller restart (posture is derived at composition), the
  first night publish, and the one-time durable night acknowledgement
  (`docs/DESIGN_SCHEDULES.md` §3; the REST layer refuses with
  `schedule_window_not_allowed` → `night_posture_acknowledgement_required`
  in exactly that order). Then a daily arm after each boot (restarts boot
  disarmed — CONTINUITY 2026-08-23 schedules entry) and the existing
  schedule runner renews the ≤300 s intents indefinitely.
- **What it conflicts with — stated honestly.** The night belongs, by the
  operator's own environment fact and the shipped posture, to the external
  writer applications. Holding PQ objectives all night makes the controller
  a second 0x0200 writer in their window; the objective register is also
  the legacy force-state word, so interleaved writes are not additive but
  last-writer-wins. The grant therefore requires a stand-down agreement
  with whoever runs those apps for the granted window — exactly the
  "coordinate, don't fight" posture the design encodes. Without it, the
  structural enforcement still protects everyone (a real foreign objective
  latches `external_writer` and the console says so), but the night
  devolves into a write fight. Note the schedule_state vocabulary still
  cannot say "window open but units disarmed" (the recorded 2026-08-23
  FINDING) — expect to re-learn the arm requirement after each restart
  until that projection word exists.
- **Expected coverage.** ~100% of the 95-135 W floor, subject to the
  (iii) trial confirming a ~45 W objective holds. The 19:35 run proves
  the mechanism end-to-end in darkness (38 authorized cycles, per-battery
  watts, clean hand-back).

### (i) — A vendor/consumer-app mode change

**What it is.** Setting the pods' work mode to 8 "Timing" with a night
discharge profile, or any vendor-app setting that deepens autonomous
matching — configured in the vendor's consumer/cloud app (the same family
as the night writers), not the service tool.

- **What it takes.** Access to and coordination with that app's owner;
  no evidence in the decompile of its configuration surface (the service
  tool can neither set nor read Timing profiles — work mode is
  display-only, `MiniESapp.cs:1376`).
- **Conflicts.** The night writers themselves; also opaque to our audit
  trail (their writes would appear, if at all, as the existing blind spot).
- **Expected coverage.** Unknown — no evidence exists that Timing would
  close the floor either; matching already runs and leaves it. Ranked
  last on current evidence: the mechanism that leaves the floor in place
  is the pods' own loop, not a missing vendor mode.

### (iv) — Leave it

**What it costs.** 1.5-2.0 kWh per 10-hour night (the posture's
20:00-06:00 definition) at the operator's import rate — roughly 45-60
kWh/month. Against that: serving it consumes ~1.7-2.4 kWh of battery per
night (low-load one-way efficiency at 40-50 W output is the poor end of
the curve), the energy must come from somewhere (PV surplus is free but
finite; grid charging pays the round trip), and every alternative adds
either a night-partition agreement or a recurring manual ritual. If the
night tariff is flat and low, this option is not irrational; it is simply
the do-nothing baseline the others must beat.

---

## 6. Economics frame (kWh only — money awaits the operator's tariff keys)

- **The prize.** 150-200 W × 10 h = **1.5-2.0 kWh/night**; measured floor
  tonight 95-135 W ≈ 1.0-1.35 kWh/night. Scorecard tariff keys
  (`energy_scorecard.tariff`) are unset; when the operator supplies rates,
  the scorecard will price this line directly.
- **Battery availability.** SOC at measurement: mid 88%, rhs 98%, lhs
  70-71%. All three can cover a night at ~45 W each (≈0.45 kWh/pod ≈
  single-digit SOC points on a 5 kWh-class pack); lhs is the only one with
  a multi-night story to watch.
- **Losses.** Serving ~1.35 kWh AC at 40-50 W output costs perhaps
  1.5-1.7 kWh of stored energy (low-load conversion). If the stored energy
  is otherwise-curtailed PV surplus (rhs is full — its dynamic charge
  limit is 0 W, so tomorrow's surplus has nowhere to go), the loss is
  nearly free and the night discharge usefully **re-opens charge headroom
  for the next PV day**. If it must be grid-charged, the round trip
  (~75-85%) eats most of the arbitrage at flat tariffs.
- **Verification is already built.** The scorecard's per-phase import
  integration is exactly the instrument that will show whether any remedy
  works: compare `grid_import_kwh` nights before/after (the daily records
  begin at the first site midnight after commissioning).

---

## 7. Open questions for the operator

1. **The one-evening trial (option iii):** dispatch ~{lhs:50, mid:45,
   rhs:30} W for 5 minutes some evening and watch the per-phase
   `grid_power_w` — do the CTs go to ~0? This settles standby-vs-dead-band
   and the PCS's willingness to hold ~45 W. (Requires only the normal
   arm; nothing else changes.)
2. **Is the night cover worth having at all (option iv vs ii):** what is
   the night import rate, and is it flat or time-of-use? At a flat low
   rate, 1.5-2 kWh/night may not justify a standing night controller.
3. **The partition grant (option ii), if wanted:** who operates the
   external night-writing apps, and will they stand down for a granted
   window (or hand the night to us entirely)? This is the prerequisite the
   posture system will enforce regardless.
4. **The 02:00-05:00 blind spot:** one deliberate overnight observe run
   (already queued in the census as the between-cycles foreign-objective
   detector's companion) would confirm the pods hold `run_mode 0` and the
   same floor through the dead zone — tonight's inference rests on the
   evening shoulder.
5. **Per-phase asymmetry:** rhs's floor (~28 W) is materially lower than
   lhs/mid (~40 W each). If the trial in (1) runs, note whether the
   residuals are the pods' standby (three different draws) or their CTs'
   offsets — it changes nothing operationally but closes the evidence.

---

## Appendix — evidence index

| Fact | Source |
|---|---|
| 13-sample window 21:11:35-21:25:35, per-unit batt/grid/load, mode words | `GET /api/v1/snapshot` ×13 (transcripts in working notes) |
| 26.1-min integrated import lhs 40.7 / mid 40.5 / rhs 27.9 W, zero export | durable `energy_baseline` (read-only, `var/live-write.sqlite3`); `GET /api/v1/snapshot` `energy_today` |
| Residual floor at 1.1-1.8 kW loads, ~39 W standby attribution | PROTOCOL_EVIDENCE §4c capture table (2026-08-22) |
| Mode vocabulary | `EnergyPod_RE/src/MiniESapp/GlobalFun.cs:152-212` |
| Service tool has no engagement/mode configuration; PQ 1 s renewal; debug-mode gate | `MiniESapp.cs:2101-2230, 1376`; `SysControl.cs:1442-1527` |
| Prior integration: night CHARGE schedule, charge-only engine, "Load Matching" status inference, standby stop | `modbus/byd/config.py:76-79`, `byd/scheduler.py:106-142`, `byd/battery.py:304-325` |
| Zero external_writer in 33 h; arms clean at 19:32; 4 e-stops operator-pressed; 19:35-19:36 schedule run 999 W/battery ×38 cycles after sunset | `GET /api/v1/audit` (5,179 events, site 08-22 21:33 → 08-23 20:24) |
| Overnight audit silence site 00:00-06:00 (blind spot) | same audit pull, hourly histogram |
| Day-only posture, partition mechanism, refusal order | `docs/DESIGN_SCHEDULES.md` §3; `GET /api/v1/schedule` (v5, empty, posture yield) |
| Manual intent TTL ≤300 s; schedule TTL ≤300 s | `src/energypod/api/rest.py:99`; `src/energypod/runtime/config.py:688-691` |
| Discharging foreign objective would latch at arm; matching does not | `src/energypod/application/actor.py:756-839` (live: `arm_sole_writer` 19:32 tonight) |
