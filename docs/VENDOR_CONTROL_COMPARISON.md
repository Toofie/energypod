# Vendor vs prior-integration vs our controller: charging/discharging control

Status: source-analysis study, documentation only
Prepared: 2026-08-23
Scope: read-only comparison of (a) the decompiled vendor application
`C:\Users\vagrant\Downloads\EnergyPod_RE\src`, (b) the operator's prior WORKING
Docker integration `C:\Users\vagrant\Downloads\modbus` (production entrypoint
`manager.py` per `Dockerfile:18`; the `byd/`, `nextgen_battery_manager/`, and
`byd_energypod_manager/verification/` trees are later iterations and are cited
as such), and (c) our controller under `src/energypod`. No hardware was
contacted and no code was changed for this study.

Evidence rule: where the prior production integration and the vendor code
disagree, the prior integration outranks the vendor code — it charged and
discharged these very pods for years. Where the decompile is ambiguous, this
document says so instead of guessing. Register addresses are zero-based PDU
addresses throughout (PROTOCOL_EVIDENCE §3).

Motivating incidents this study must bound (CONTINUITY.md:522-567, :496-520):

1. 2026-08-23 22:11Z+: our kernel AUTHORIZED discharges for 6+ minutes, the
   pods never actuated them, zero errors.
2. Mid pod oscillating ±1.2 kW completely uncommanded (not its usual
   −520..−700 W daytime PV self-charge).
3. Discharge overshoot ~+170 W above setpoint; our ramp limiter starts from
   the pod's own self-charge baseline.

---

## 1. Control-aspect matrix

Legend: **V** = vendor app, **P** = prior production integration (root
`modbus/` tree unless marked `byd`/`tester`), **O** = our controller.

| # | Aspect | V (vendor app) | P (prior integration) | O (ours) |
|---|---|---|---|---|
| 1 | Command path | FC16 `[1, P, Q]` @ `0x0200` (SysControl.cs:1442-1461); P/Q signed `short` cast bit-preserving to `ushort`; UI parses operator text (MiniESapp.cs:2186-2187); "Keep Send" checkbox DEFAULT ON resends every 1000 ms (MiniESapp.cs:4744-4746, 2221-2233; JudgyCircleTime GlobalFun.cs:12-26); stop = `[1,0,0]` via `SendPower(0,0)` on cancel (MiniESapp.cs:2234-2236) | Single-register write of P ONLY @ `0x0201` (513) — no enable word, no Q (battery.py:207-245, write at :245); charge = −P, discharge = +P (battery.py:529-530, :583-584); renewed every 2.0 s by the scheduler (manager.py:154, :169-173). `byd` iteration: writes force-state 1 @ 512 THEN P @ 513 (byd/battery.py:294-302); stop = force-state 5 @ 512 (byd/battery.py:304-306); 1.5 s cadence default (byd/config.py:58) | FC16 `[1, P, Q]` @ `0x0200`, charge negated at the encoder (composition.py:647-663; protocol_codec.py:86-91); stop triple `(1,0,0)` (protocol_codec.py:90-91); transport STRUCTURALLY rejects every other write (waveshare.py:188-189); heartbeat per fleet cycle at `control_period_s` ≤ 1.5 s (config.py:61; composition.py:1515-1527), write bounded by `wait_for` (composition.py:1527) |
| 2 | Modes & preconditions | Reads `debugMode` from `0x8100` every 1000 ms poll (SysControl.cs:372-396, called at :151/:190/:246); REFUSES PQ send unless `debugMode == 0` "Normal Mode" (MiniESapp.cs:2180-2184); writes a debug mode only on explicit user combo selection: write `0x8000` → immediate `DebugModeRead()` read-back (MiniESapp.cs:2159-2170); `ctrlMode` (`0x0101`) only READ and displayed (SysControl.cs:410; MiniESapp.cs:1377) — NEVER written anywhere in the decompile; enums: debugMode 0 Normal/1 Standby/2 Charge/3 Discharge/4 Circulation/5 Fixing SOC/6 Verify Capacity (GlobalFun.cs:152-165), ctrlMode 1 Remote/2 Local (GlobalFun.cs:204-212), workMode 2 Economy/6 Remote dispatch/8 Timing (GlobalFun.cs:167-176), PCS runMode 0 Matching Load/1 Remote PQ Power/2-4 remote PF/current/DC-voltage (GlobalFun.cs:178-189) | Production path NEVER touches `0x8000`/`0x8100`/`0x0101` (no such write exists in manager.py/battery.py). `byd` iteration reads `0x8100` as "EB1_FORCE_STATE_ID" (byd/battery.py:372) and writes force-state to 512 (byd/battery.py:279-292); the verification suite — sourced "from Home Assistant config" — WRITES 0/2/3 directly to `0x8100` and reads back (battery_tester.py:50-55; mode_tester.py:83-119) | NOTHING. `0x8100` is polled in the cold ring (register_layout.py:88) but decode.py has NO channel for it — the word is read and discarded (decode.py:44-97, :300-313); `0x0101`/`0x0102` sit in the system block read once per process (composition.py:1118-1119) and are likewise never decoded; catalog marks `debug_mode_functions: UNKNOWN` and admits only the PQ block as writable (register_layout.py:93-95, :110). No mode precondition exists anywhere on our write path |
| 3 | Autonomy model | PCS runMode 0 = "Matching Load" (pod load-following) vs 1 = "Remote PQ Power" (GlobalFun.cs:178-189) — displayed, never set; workMode "Economy"/"Timing" are device-side EMS strategies (GlobalFun.cs:167-176), read-only; dedicated CT surface decoded: external grid/PV current+power and derived load power (SysControl.cs:499-503), PV power at system +59 (SysControl.cs:432), all displayed on the home screen (MiniESapp.cs:1359-1362, :1400-1403) — the substrate for firmware self-consumption; the app contains NO EMS/solar/self-consumption logic of its own and no switch between "pod decides" and "app decides" other than the debug/run-mode words above | The prior system KNEW autonomy is the default: passive mode "allows the battery to act as a voltage source instead of relying on CT sensors" (battery.py:259-263) and the free-window reset exists for "compatibility with existing CT-based control outside free windows" (manager.py:416-420). No production write ever disabled CT logic (and the 40120-40123 voltage/current writes could not have worked as coded — see §4.4) | Live-proven by our own captures: daytime −520..−700 W self-charge with no writer; rhs flipped +1172 W discharging to −672 W charging the instant our objective cleared; +172 W overshoot = firmware serving local load on top of the objective (CONTINUITY.md:503-507). We read the CT words (grid `0x1000+17`, load `0x1000+20`, decode.py:62-63, :296-298) but have no model of, or switch for, the autonomy they feed |
| 4 | Limits & ramps | NO app-side clamps and NO ramp/soft-start anywhere: the UI parses raw shorts; BMS/PCS dynamic limits and system limits are displayed as read-only labels the operator is trusted to respect (SysControl.cs:430-431, :548-550; MiniESapp.cs:1386-1389, :1444-1445) | int16 range check only in production (battery.py:227-231); `byd` clamps ±5000 W hard-coded (byd/battery.py:57, :255), config default 2500 W (byd/config.py:163); load-compensated charge `max(200, 2000 − load)` (manager.py:348); discharge target `load × 1.1 + 35` (manager.py:669); the only ramp concept is the passive-mode voltage ramp (battery.py:350-377) — an unproven 16-bit-overflow surface, not a P ramp | Static per-unit + fleet caps min'd with the BMS's own dynamic limits (composition.py:726-742), apparent-power envelope, SOC floor/ceiling/jump, cell voltage/imbalance/temperature windows, telemetry/cell staleness, export-evidence bound for optimizer charges (safety.py:23-152); RAMP limiter `|battery_watts| + ramp_rate × heartbeat` (safety.py:390-399) — live config 1000 W/s × 1.5 s (config.live-write-example.yaml:126, :95). Deliberately the strongest of the three |
| 5 | State feedback & verification | Write → `Thread.Sleep(1000)` → read-back pattern for EVERY configuration write (debug mode immediate read-back MiniESapp.cs:2166-2167; grid standard :2494-2499; param toggle :2531-2541; clear-energy :2453-2457). For PQ specifically: NO programmatic verification — `SendPQPower` returns a flag that is initialized false and never set true, and callers ignore it (SysControl.cs:1442-1460; PROTOCOL_EVIDENCE §7). The objective echo registers are decoded (active/reactive objectives, SysControl.cs:540-541) but never displayed or compared; verification is the human watching inverter power and the runMode label at 1 Hz | `set_active_power` does NOT check `response.isError()` (battery.py:245) — no write verification at all; the only closed loop is PVOutput telemetry posting of active power every 5 min (manager.py:565-608) — a human/dashboard sees a flat battery minutes later. `byd` checks `isError` on mode writes (byd/battery.py:274) but not on P writes (:263-267) | Strict FC16 ACK echo validation (address+count+device id, waveshare.py:257-266) — strongest transaction-level check of the three; arm-time sole-writer preflight reads the served objective `0x1060+17/+18` (actor.py:573-625; composition.py:143, :2128-2134). But PER-HEARTBEAT: no objective readback, no authorized-vs-measured coherence check — the actor marks ACTIVE on ACK alone (actor.py:707-718). Nothing would detect a pod that ACKs and ignores |
| 6 | Fault/error handling | `SciExceptionDeal`: 3 consecutive exceptions → device marked disconnected + modal UI box (SysControl.cs:460-470); send button refuses while disconnected (MiniESapp.cs:2174-2178); no retry; failures are VISIBLE | try/except → log → continue, per-battery isolation (manager.py:341-362); temperature/SOC guards skip the action (battery.py:479-522); failures invisible unless someone reads logs — but they ARE logged | Transport raises typed errors and resyncs (waveshare.py:130-158); actor write failure → generation fence, INHIBITED (transient), bounded-zero attempt, revoke (actor.py:696-706); supervision bounds every operation with `wait_for` + `suppress(Exception)` (composition.py:1517-1527, :1565-1567) — fail-closed but SILENT: a suppressed renewal leaves no log or audit event (CONTINUITY.md:532-535, queue item i). Poll failures fail closed through telemetry staleness |
| 7 | Multi-device / fleet | Single device only: static `DeviceInfo.devAddress` (default 4, DeviceInfo.cs:5-7), one session, one process-wide mutex — a commissioning/debug tool, not a fleet manager | Three pods MID/RHS/LHS at .11/.12/.13:4196 (manager.py:832-837), sequential connect/read/write/close per pod per tick; SOC^3-weighted discharge split (manager.py:610-705), one-battery-per-night evening rotation (manager.py:113-149), SOC balancing transfer (manager.py:448-563); no per-phase knowledge in production (a `byd` `phase_balancer.py` experiment exists) | Fleet-first: arbiter → deterministic allocator (export-bounded) → fleet safety limits → per-unit sole-owner actors; per-unit RTU-ID identity binding and commissioning; durable per-cycle audit with fingerprints. Stronger, but the greedy allocator starves alphabetically-later units (CONTINUITY.md:529-532, queue item v) — the prior SOC-weighted split never zeroed a willing unit |
| 8 | Watchdog / expiry | No explicit expiry constant or comment; the 1000 ms keep-send IS the lease (nominal 1000 ms with JudgyCircleTime tolerating up to ~1.5× slip, GlobalFun.cs:12-26 → effective ~1.0-1.5 s renewal); stop = explicit `[1,0,0]` (MiniESapp.cs:2234-2236) | 2.0 s production cadence (manager.py:154) / 1.5 s `byd` default (byd/config.py:58); stop = objective expiry (unrenewed) or `byd` force-state 5; no expiry knowledge — cadence chosen empirically | The only stack with the expiry MEASURED (~3.5-4.0 s, live trial, PROTOCOL_EVIDENCE §4b) and ENFORCED as config invariants: renewal budget strictly inside expiry, write-enabled control period capped at 1.5 s (config.py:352-366, :61); stop triple live-verified returning the pod to baseline within 1.5 s (PROTOCOL_EVIDENCE §4b) |

---

## 2. Narrative per aspect

### 2.1 Command path

All three stacks converge on the same physical interface — an active-power
objective for the PCS — but with three different transaction shapes. The
vendor's `SendPQPower(short objP, short objQ)` builds a three-register FC16
frame `[1, objP, objQ]` at `0x0200` (SysControl.cs:1442-1461); the leading `1`
is an enable/normal-mode word (the vendor always writes 1; the `byd` iteration
independently discovered the same register as a "force state" word with values
1/4/5/6, byd/battery.py:34-38). The prior production integration wrote ONLY
register 513 (`0x0201`, the P word) with a single-register write
(battery.py:245) and relied on the enable word having been latched at 1 — and
it worked for years, which is corroborating evidence that `0x0201`-only
writes are accepted by this firmware (PROTOCOL_EVIDENCE §7 nonetheless keeps
the vendor's full frame as the normative contract, and so do we). Our encoder
reproduces the vendor frame byte-for-byte with charge negated
(composition.py:655-660), and our transport refuses any other write shape
(waveshare.py:188-189) — the strictest of the three.

Sign convention: negative P = charge, positive P = discharge. The vendor UI is
sign-transparent (the operator types a signed number; labels say only "Active
Power Obj."/"Reactive Power Obj.", MiniESapp.cs:4753-4758). Both prior
implementations negate inside `charge()` (battery.py:529-530;
byd/battery.py:297) and we proved the convention live on this firmware
(2026-08-22 direction trial, PROTOCOL_EVIDENCE §4b). One inconsistency to
flag: the verification tester's `test_power_mode` calls +1000 "1kW charge"
(mode_tester.py:58-65), contradicting the production sign — the tester
comment is wrong or the experiment was never load-verified; production code
outranks it.

Renewal cadence: vendor 1000 ms (default-ON keep-send loop,
MiniESapp.cs:4744-4746, :2221-2233), prior 2.0 s (manager.py:154), ours
≤1.5 s config-capped (config.py:61). All sit inside the measured ~3.5-4.0 s
firmware watchdog.

### 2.2 Modes and preconditions — who sets them, when, and how they are verified

This is the aspect with the sharpest three-way difference.

Vendor: the mode words are PERIPHERAL STATE the app reads continuously and a
PRECONDITION it enforces, never something it manages automatically.

- `debugMode` is polled every 1000 ms as part of the main read chain
  (SysControl.cs:372-396; chained at :151, :190, :246, :333).
- The PQ send handler refuses when `debugMode != 0`, with the string "switch
  the device to 'Nomal Mode' and try again!" (MiniESapp.cs:2180-2184). Note a
  vendor bug: the message is composed into a local string and then the handler
  returns without ever displaying it — a SILENT refusal, coincidentally the
  same UX failure mode as our incident.
- The ONLY mode write is a human act: selecting a value in the debug-mode
  combo writes one register to `0x8000` and immediately reads back
  `0x8100` under the same mutex (MiniESapp.cs:2159-2170). Write-then-verify
  is the vendor's own precedent for control-register writes; every other
  configuration write follows write → `Sleep(1000)` → re-read
  (MiniESapp.cs:2444-2458, :2487-2500, :2516-2521, :2531-2541).
- `ctrlMode` (`0x0101`) is decoded (SysControl.cs:410) and displayed
  (MiniESapp.cs:1377) but NEVER written — there is no write to any address in
  the 0x0100 block anywhere in the decompile (the complete write inventory is
  `0x0200`, `0x8000`, `0x8001`, `0x8002`, `0x8008`, `0x8018`, `0x8034-0x8037`;
  SysControl.cs:1442-1527, PROTOCOL_EVIDENCE §5). How a pod ENTERS Remote
  ctrl mode is therefore not answered by the vendor app: it is device-side
  configuration (panel, cloud/EMS, or factory). The send handler does not
  check ctrlMode at all — only debugMode.

Prior integration: the production Docker system never read or wrote any mode
register, and it worked for years. That is strong operational evidence that
mode-latching is an ABNORMAL state on these pods, not a daily requirement —
nobody was renewing or checking modes at night while the pods charged and
discharged on schedule. Caveat, stated honestly: we cannot prove the pods were
never parked in a good mode by the vendor app or the Home Assistant setup
before the prior integration took over (the vendor app last ran 2024,
CONTINUITY.md:502); "worked without mode management" means the GOOD mode is
sticky, not that modes are irrelevant. The later `byd` iteration and the
verification suite show the operator actively experimented with force states —
writing 0/2/3 to `0x8100` directly, sourced from a Home Assistant config
(battery_tester.py:50-55; mode_tester.py:83-119) — which conflicts with the
vendor's addressing (command `0x8000`, readback `0x8100`) and is the most
likely path by which a pod could have been left latched in a non-Normal state.
PROTOCOL_EVIDENCE §12 already rejects the `EssForceState` interpretation as a
contract; treat it as evidence that the mode surface is tinkerable, not as a
specification.

Ours: nothing — and the gap is precise. We POLL `0x8100` (cold ring,
register_layout.py:88) and the system block containing `0x0101`/`0x0102`
(once per process, composition.py:1118-1119), then throw the words away:
`decode_observation` has no channel for any mode word (decode.py:44-97). No
layer — actor, safety kernel, control kernel — can see a mode, so no
precondition can exist. This is compatible with the 2026-08-23 incident: a pod
that ACKs our FC16 frames but ignores the objective because of its mode state
produces exactly "authorized but not actuated, zero errors."

### 2.3 Autonomy model — what the decompile reveals

The single most valuable question. Four independent evidence lines:

1. Firmware has a self-consumption mode and a remote mode. PCS runMode 0 is
   literally named "Matching Load" — the inverter following its own view of
   the load — vs 1 "Remote PQ Power" (GlobalFun.cs:178-189). System workMode
   offers "Economy" and "Timing" — device-side scheduling strategies — vs 6
   "Remote dispatch" (GlobalFun.cs:167-176). The vendor app only DISPLAYS
   these words (MiniESapp.cs:1376-1377, :1385); it never switches them.
2. The firmware's CT inputs are first-class telemetry: external grid and PV
   current/power and a derived load power (SysControl.cs:499-503), a system
   PV power (SysControl.cs:432), all surfaced on the vendor home screen
   (MiniESapp.cs:1359-1362, :1400-1403). A pod that measures grid/PV/load has
   everything it needs to self-consume.
3. The operator's prior system documents the same conclusion from the other
   direction: passive mode exists so the pod can act "as a voltage source
   instead of relying on CT sensors" (battery.py:259-263), and normal
   operation is described as "existing CT-based control" (manager.py:416-420).
4. Our own live captures: uncommanded daytime self-charge, an instant
   +1172 W → −672 W flip the moment our objective cleared (the pod resumed
   its OWN objective), and discharge overshoot ≈ local load served on top of
   our battery-power objective (CONTINUITY.md:503-507).

Is there an app- or firmware-side switch between "pod decides" and "app
decides"? The decompile says: not in the app. The switch surface is the mode
words themselves (runMode/workMode/debugMode/ctrlMode) plus the prior
integration's passive-mode block at 40120-40123 — but no trustworthy write to
any of them is evidenced: the vendor never writes runMode/workMode/ctrlMode,
the HA-derived 40120 block is rejected by PROTOCOL_EVIDENCE §12 (see §2.6),
and `0x8000` semantics beyond the name are Unknown (§8). What is NOT
ambiguous: autonomy is real, default-ON, CT-driven, additive (the overshoot),
and takes the pod back the instant our lease lapses (by watchdog design).

### 2.4 Limits and ramps — who bounds the power

Vendor: nobody, in software. The app is a trusted operator's screwdriver: raw
signed shorts go to the wire; the dynamic limits (BMS charge/discharge power
limits, PCS apparent/discharge/charge/reactive limits, system limits) are
displayed as labels (SysControl.cs:430-431, :548-550; MiniESapp.cs:1386-1389,
:1444-1445) and the firmware is expected to clamp. There is no ramp or
soft-start anywhere in the decompile — a step from 0 to 5000 W would be sent
as such.

Prior: minimal — an int16 range check in production (battery.py:227-231), a
hard ±5000 W clamp in the `byd` iteration (byd/battery.py:57, :255), and
behavioral bounding by construction (charge `max(200, 2000 − load)`,
manager.py:348; discharge `load × 1.1 + 35`, manager.py:669 — the +10%+35 W
fudge is itself evidence the pods deliver slightly less than commanded, the
mirror image of our +170 W overshoot question). The only ramp concept on
these pods is the passive-mode DC-voltage ramp (battery.py:350-377) — a
voltage-source ramp, not a P ramp, on the unproven 40120 block.

Ours: the richest bounding stack of the three — static + dynamic + apparent +
ramp min() per unit (safety.py:376-399), fleet caps (safety.py:131-137,
:401-414), SOC/cell/temperature/staleness envelopes (safety.py:204-367), and
the export-evidence bound. Two consequences worth naming because they
interact with the incidents:

- Our ramp limiter starts from the pod's OWN telemetry (`battery_watts`,
  safety.py:393) — i.e. from the autonomous baseline. When the pod is
  self-charging at −600 W, a 1500 W/cycle ramp allowance yields a first
  discharge command of only ~900 W. That is our clamp working as designed,
  but it means our commanded power can never cleanly separate "our"
  contribution from the pod's autonomous contribution.
- When we command 0 or let the lease lapse, the pod does not go to zero — it
  returns to its OWN baseline (the −672 W flip, the post-stop +19 W resting
  state in PROTOCOL_EVIDENCE §4b). "Stop" in this system means "stop us,"
  not "stop the pod."

### 2.5 State feedback — how each stack would have caught our incident

Vendor: the transaction is unverified (always-false return, ignored), but the
1 Hz human loop is the verification: inverter power, work mode, control mode
and debug mode are all on screen (MiniESapp.cs:1375-1385, :1528). An operator
pushing Send would see power not move and/or runMode stuck at "Matching Load"
within seconds. Additionally, the app's write-then-read-back pattern for
every configuration write (§2.2) is the precedent for verifying that a
control write landed.

Prior: no write verification (battery.py:245), and detection was deferred to
the PVOutput dashboard (5-minute cadence, manager.py:565-608) — a flat
battery on the graph, noticed by a human, minutes to hours later. This stack
would NOT have caught the incident quickly; it survived because its failure
mode (transport errors) is loud in logs and its objective was renewed so
often that transient misses were self-healing.

Ours: strongest at the transaction layer (FC16 ACK echo validated,
waveshare.py:257-266), arm-time sole-writer preflight on the objective
readback (actor.py:573-625) — a check NEITHER other stack has — but nothing at
the effect layer. The heartbeat marks the unit ACTIVE on ACK alone
(actor.py:707-718); no per-heartbeat objective echo (queue item iii), no
authorized-vs-measured coherence watchdog (queue item vi). The console
DISPLAYS requested/authorized/measured separately (CONTINUITY.md:610-612),
which is how a human noticed — but the controller itself raised nothing for
6+ minutes. Detection today is human-only, same as 1990s SCADA; the fix is
already queued and should be treated as the highest-value item because it is
failure-mode-agnostic: it catches mode-latching, wrong-mode, passive-mode,
and any future silent-ignore cause at once.

### 2.6 The prior integration's passive-mode block, assessed honestly

The 40120-40123 "passive voltage mode" surface (VBat_Set_Mode/Value/Ramp,
IBat_Limit; battery.py:259-377, battery_tester.py:58-63) is the prior
system's most interesting and least trustworthy discovery. Three defects in
it must be recorded because they bound how much weight it can carry:

1. The voltage/current targets (360000-440000 mV, 50000-80000 mA) do not fit
   a 16-bit register and cannot have been transmitted as coded — pymodbus
   3.7.0 (requirements.txt:1) rejects out-of-range register values rather
   than masking, so every `set_voltage_target`/`set_current_limit` call
   raised and was swallowed by its try/except. PROTOCOL_EVIDENCE §12 reached
   the same exclusion.
2. `reset_batteries_to_power_mode` disables passive mode and then calls
   `float_passive(...)`, which RE-ENABLES it (manager.py:429-432 calling
   battery.py:421) — the "reset" is self-defeating.
3. The verification README's PASS output (verification/README.md:169-208) is
   a sample transcript, not a captured artifact; which sub-writes actually
   landed on real hardware is unproven.

What remains valuable: `set_passive_mode(1/0)` itself fits in 16 bits and the
docstrings encode real operator knowledge — that the pods default to CT-based
control. The block is evidence that an alternate control plane EXISTS at
40120+; it is not evidence of a safe interface to it.

### 2.7 Fleet coordination

Vendor: one pod at a time (static `DeviceInfo`, DeviceInfo.cs:5-7) — the tool
has no fleet concept, which also means it never had to solve multi-writer
contention, allocation, or per-pod identity. Prior: a three-pod pool with
SOC-weighted discharge, night rotation, and load-following charge — simple,
effective, and never starving a unit (every willing battery got a share,
manager.py:680-693). Ours: the only stack with per-unit identity binding
(RTU ID at `0x8106`), deterministic allocation, fleet limits, and a durable
audit trail — but the current greedy fill starves later units
(CONTINUITY.md:529-532). The prior integration's proportional split is the
correct minimal precedent for the allocator fix (queue item v).

---

## 3. GAP ANALYSIS

### 3a. Where the vendor/prior stack is safer or more correct than ours

Each mapped to the existing fix queue (CONTINUITY.md:557-567) or proposing a
new item.

| # | Gap | Evidence | Queue mapping |
|---|---|---|---|
| G1 | Mode precondition: the vendor refuses to send when `debugMode != 0`; we write blindly. Even the production-prior evidence only proves good modes are STICKY, not that they cannot latch (the HA-derived tester wrote 0/2/3 to `0x8100`, battery_tester.py:51) | MiniESapp.cs:2180-2184 vs our absence (§2.2) | Existing (iv) — poll `0x8100`/`0x0101`, refuse intents with explicit reason. Extend to `0x0102`/`0x1002` (see G2) |
| G2 | Mode words are POLLED BUT DISCARDED: `0x8100` is in the cold ring and the system block is read once, yet decode.py has no channel for either — the cheapest possible fix is blocked on a decode/observation channel that does not exist | register_layout.py:88; composition.py:1118-1119; decode.py:44-97 | NEW item (viii): decode debugMode/ctrlMode/workMode/PCS-runMode into the observation + expose in `/units` and audit rows; prerequisite for (iv) and for the G6 experiment |
| G3 | No post-write objective echo: vendor verifies every config write by read-back (MiniESapp.cs:2166-2167 etc.); we verify the arm-time preflight only, never the heartbeat's own write | actor.py:707-718 | Existing (iii) — fold `0x1060+17/+18` into the poll tiers (the census entry already queues the same detector for foreign objectives, CONTINUITY.md:515-518) |
| G4 | No actuation-coherence watchdog: vendor's human 1 Hz loop closed the loop; ours has no controller-side equivalent, so 6+ minutes of authorized-but-not-actuated passed silently | CONTINUITY.md:522-535 | Existing (vi) |
| G5 | Suppressed write failures are invisible: `suppress(Exception)` + bounded `wait_for` (f20602c hardening) made even LOUD failure modes silent; the vendor at least shows a modal box and marks the device disconnected | composition.py:1517-1527; SysControl.cs:460-470 | Existing (i) (+ (vii) for distinct refusal reasons) |
| G6 | Allocator starvation: the prior SOC-weighted split gave every willing unit a share; ours zeroes alphabetically-later units on fleet intents | manager.py:680-693; CONTINUITY.md:529-532 | Existing (v) |
| G7 | No characterization of the pod's autonomous baseline in our control model: our ramp, our stop semantics, and our measured telemetry all sit ON TOP of an unmodeled −520..−700 W self-consumption controller that flips instantly when our lease clears | CONTINUITY.md:503-507; safety.py:393 | NEW item (ix): an autonomy-characterization experiment (one authorized live mode-word read while mid oscillates; sustained-discharge observation with the already-tier-promoted grid/load words) whose outputs feed the ramp/stop design and the coherence watchdog's expected-motion band |

### 3b. Where ours is deliberately stronger (stated plainly, not self-flagellating)

1. Authority and safety architecture. Single-use, sequence-bound
   authorizations minted only after arbitration, deterministic allocation,
   fleet safety evaluation, and DURABLE audit; generation fencing; emergency
   stop with acknowledgement; observe-only structural boot; per-unit identity
   binding; fail-closed telemetry staleness (control_kernel.py:124-241;
   actor.py:646-718; safety.py:36-152). Neither precedent has any of this:
   the vendor is a single-device Windows Forms debug tool whose PQ success
   flag is always false (SysControl.cs:1442-1460); the prior integration is
   try/except-log-continue over three sockets.
2. Write shape discipline. Only the evidenced `[1, P, Q]` frame at `0x0200`
   is structurally writable (waveshare.py:188-189) with strict ACK echo
   validation (waveshare.py:257-266) — versus a prior stack that single-wrote
   `0x0201` (worked, but unevidenced as equivalent) and experimented with
   unproven register blocks, and a vendor tool that will happily send any
   int16 the operator types.
3. Watchdog discipline. We MEASURED the expiry (~3.5-4.0 s) and enforce the
   renewal budget as a configuration invariant (config.py:352-366, :61). The
   other two stacks' cadences were folklore that happened to be fast enough.
4. Sign conventions and register semantics. Live-proven on this exact
   firmware, per-register documented (PROTOCOL_EVIDENCE §4b/§4c), with the
   prior stacks' internal contradictions (battery.py:210 vs :541;
   nextgen dashboard's inverted help text) catalogued rather than inherited.
5. Sole-writer detection at arm time — reading the served objective readback
   and latching on a foreign nonzero value (actor.py:573-625) — a control
   that NEITHER precedent has in any form.

### 3c. Unresolved questions and the exact next evidence needed

| # | Question | Exact next evidence |
|---|---|---|
| U1 | Does `debugMode != 0` (or `ctrlMode == Local`, or PCS runMode != 1) actually cause the FIRMWARE to ignore `0x0200` objectives — or is the vendor's check only a UI guard? (The decompile proves the APP checks; it proves nothing about firmware. PROTOCOL_EVIDENCE §7 marks run-mode-1 "Unknown") | One authorized live read of `0x8100`, `0x0101`, `0x0102`, `0x1002` on all three pods — mid WHILE it oscillates, rhs/lhs while a discharge is authorized-and-ignored — plus, if any word is abnormal, ONE operator-authorized bounded normalization write with immediate read-back (§4 procedure) and a re-dispatch. A single mode-read snapshot explains or bounds incidents 1 and 2 at once |
| U2 | Which word gates pod autonomy (runMode "Matching Load" vs "Remote PQ Power" vs workMode "Economy"/"Timing" vs the 40120 passive block)? | The same mode-word read of U1, correlated with each pod's oscillation/self-charge state. If mid reads runMode 0 / workMode 2 while rhs/lhs read runMode 1 / workMode 6, the gate is identified without a single write |
| U3 | Is `0x8100` itself writable on these pods, and do `0x8000` (vendor command) and `0x8100` (HA-derived write target) alias? | One controlled write on a maintenance window: write `0x8000←current value` (no-op re-write) and observe `0x8100`; never write a NEW value during this test |
| U4 | Is the +170 W discharge overshoot a steady firmware additive (load-serving) or a transient? Does it scale with local load? | Sustained (60 s+) authorized discharge while logging the already-tier-promoted grid/load CT words (`0x1000+17/+20`) per cycle; compare overshoot to measured load. No new registers needed |
| U5 | What happens overnight (the 7.3 h audit gap)? Is there a night-time competing writer? | The already-queued deliberate overnight observe run + between-cycles foreign-objective detector (CONTINUITY.md:515-520) |
| U6 | What set mid oscillating (±1.2 kW, uncommanded)? | U1's mode-word read on mid during oscillation; plus the cold-ring `0x8100` history if (viii) lands first — the word is already being polled and discarded |

---

## 4. Mode-register policy recommendation

**Default posture: read-verify-and-refuse. Do not add automated mode writes.**

1. Decode and expose first (new queue item viii): `debugMode` (`0x8100`),
   `ctrlMode` (`0x0101`), `workMode` (`0x0102`), PCS runMode (`0x1002`) into
   the observation, `/units`, and audit rows. All four are already inside
   polled blocks; this costs zero extra frames (the census entry's
   foreign-objective detector demonstrates the same zero-frame fold for
   `0x1060+17`).
2. Refuse with an explicit reason (queue item iv): no authority is minted for
   a unit whose `debugMode != 0` or `ctrlMode != 1 (Remote)`. This is
   precisely the vendor's own posture — the vendor app NEVER writes modes in
   the course of normal control; its PQ path only READS the mode and refuses
   (MiniESapp.cs:2180-2184). The write at `0x8000` exists in the vendor UI as
   a deliberate, human, debug-panel commissioning act with immediate
   read-back (MiniESapp.cs:2159-2170) — there is no vendor precedent for
   automated normalization, so "the vendor does it daily" is FALSE as a
   justification for automation. This fully reconciles the standing
   PROTOCOL_EVIDENCE warning (§8: firmware semantics of modes 1-6 are
   Unknown; §14: any debug/maintenance mode needs official service
   documentation plus a controlled isolated procedure) with vendor practice:
   the vendor ALSO treats the mode register as a manual commissioning
   control, not a per-command or on-connect one.
3. ctrlMode == Local deserves special handling: NO write path to `0x0101`
   exists anywhere in the evidence (vendor never writes it; prior never
   writes it). There is no known normalization for Local mode. The correct
   response is refusal + operator escalation with a concrete remedy ("open
   the vendor MiniES app, check SysControlMode is Remote"), never a write.
4. The ONE exception — an explicitly authorized normalization write: a
   maintenance action, modeled on the 2026-08-22 direction trial, gated on
   all of: (a) a live read showing an abnormal mode word on a pod whose
   stable state is known; (b) explicit operator authorization for that pod,
   that word, and that value; (c) a single write of the NORMAL value only
   (`0x8000 ← 0` per the vendor command register; U3 must first establish
   whether `0x8100` writes alias it — until then use the vendor's evidenced
   command address, never the HA-derived one); (d) immediate read-back
   verification (vendor precedent, MiniESapp.cs:2166-2167); (e) a durable
   audit event. Modes 2-6 (Charge/Discharge/Circulation/Fixing SOC/Verify
   Capacity) remain permanently excluded from any write surface we expose —
   their firmware behavior is Unknown and two of them (Fixing SOC, Verify
   Capacity) are explicitly flagged by PROTOCOL_EVIDENCE §8 as unsafe to
   expose.
5. Rationale in one line: the prior integration proves good modes are sticky
   across years, so automation buys nothing on the happy path; the vendor
   proves a bad mode silently voids actuation, so visibility plus refusal
   buys the entire incident-1 detection win; and an unproven-semantics write
   from an automated loop is the one thing the evidence base forbids.

---

## 5. Bottom line for the live incidents

- Incident 1 (authorized, never actuated, zero errors): consistent with a
  mode-gated pod silently ignoring `0x0200` objectives (vendor precedent §2.2)
  and/or the allocator starvation (CONTINUITY.md:529-532) — both already
  queued; the coherence watchdog (vi) and echo readback (iii) detect it
  regardless of cause, the mode decode (viii/iv) names it.
- Incident 2 (mid ±1.2 kW uncommanded): the pod's own CT/controller logic,
  default-ON per §2.3; the exact gate is U2, answerable by one authorized
  mode-word read while mid oscillates. Until answered, mid stays excluded
  from fleet intents (already the operator's rule).
- Incident 3 (+170 W overshoot from a self-charge baseline): firmware serving
  local load additively on top of the remote objective (CONTINUITY.md:506-507,
  mirrored by the prior integration's `load × 1.1 + 35` fudge, §2.4); U4
  characterizes it with registers we already poll. Our ramp limiter is
  working as designed and is OURS alone — neither precedent has one.
