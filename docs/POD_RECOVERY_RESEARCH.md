# Pod remote-recovery research and design

Status: research + design only. Nothing here is implemented; no register
writes beyond the standing PQ contract are authorized by this document.
Prepared: 2026-08-23
Trigger: the operator's standing problem, verbatim:

> "Sometimes the batteries enter a state where they are not active or
> synchronized, or fail to respond to ANY commands. Previously I had to go
> outside and toggle the batteries on and off [physical power cycle], after
> which they responded to remote commands and reacted to the CT sensors
> again. Is there any way to restart or recover these batteries remotely?"

Sources mined: the decompiled vendor app (`C:\Users\vagrant\Downloads\EnergyPod_RE\src`),
the prior working integration (`C:\Users\vagrant\Downloads\modbus`), our incident
record (`docs/CONTINUITY.md`, `docs/VENDOR_CONTROL_COMPARISON.md`,
`docs/SYNC_RESILIENCE_AUDIT.md`), and the protocol evidence base
(`docs/PROTOCOL_EVIDENCE.md`). Confidence labels reuse PROTOCOL_EVIDENCE §1
(Confirmed by vendor code / Corroborated operationally / Assumed / Unknown).

---

## 1. EVIDENCE — every candidate recovery mechanism found

### 1.1 The complete vendor-app write inventory (exhaustive)

The vendor application issues exactly one write primitive —
`WriteMultipleRegisters` (FC16) — at ten distinct targets. There are no coil
writes, no FC06, no other write path anywhere in the decompile
(`SysControl.cs:1442-1527` is the full write surface; `MiniESapp.cs` holds the
call sites; verified by sweeping every `.Write*` reference in the tree).

| Register (hex / dec) | Payload | Purpose | Vendor UI path / cite | Status for recovery |
|---|---|---|---|---|
| `0x0200` / 512 | 3: `[1, P, Q]` | PQ objective | `SysControl.cs:1442-1461` (write :1453); UI `MiniESapp.cs:2172-2237` | Our standing write contract. Not a recovery mechanism. |
| `0x8000` / 32768 | 1: mode 0-6 | Debug/maintenance mode select | Combo-box handler `MiniESapp.cs:2159-2170` (write :2166, immediate read-back `DebugModeRead()` :2167) | **The only candidate soft-recovery register.** See §1.3. |
| `0x8001` / 32769 | 1: `0xFF00` | Clear historical energy counters | `MiniESapp.cs:2262-2275` (menu, write :2272) and `MiniESapp.cs:2444-2459` (button, write :2454 with `Sleep(1000)` + re-read :2455-2456) | Evidenced but PROHIBITED (destroys operator data). Never a recovery path. |
| `0x8002` / 32770 | list | RS485 parameters (baud/address) | `SysControl.cs:1478-1493`; UI `MiniESapp.cs:2277-2401` | Commissioning config. Could de-brick a serial misconfiguration — and could brick the bus if mistyped. Out of scope. |
| `0x8008` / 32776 | list | Network parameters (MAC, servers) | `SysControl.cs:1495-1510` | Same as above. Out of scope. |
| `0x8018` / 32792 | list | Serial number | `SysControl.cs:1512-1527` | Identity commissioning. Out of scope. |
| `0x8034` / 32820 | 1: code | Grid standard | `MiniESapp.cs:2487-2500` (write :2496, sleep :2497, re-read :2498) | Out of scope. |
| `0x8035` / 32821 | 1: 0/1 | DRED enable | `MiniESapp.cs:2508-2522` (write :2518) | Out of scope. |
| `0x8036` / 32822 | 1: 0/1 | Parameter-setting enable | `MiniESapp.cs:2524-2574` (writes :2538, :2558) | Out of scope. |
| `0x8037` / 32823 | 1: `0xFF00` | Clear battery low-voltage protection latch | `MiniESapp.cs:2576-2590` (write :2586) | Evidenced but PROHIBITED (defeats a battery-protection latch). Not a wedge remedy. |

### 1.2 Headline: is there a reboot/reset/restart command? — **NO**

Sweeping the entire decompile for reboot / restart / reset / watchdog / kick /
soft-start / recover / power-cycle / re-init produces no register write and no
command path with any such semantics. The vendor application has **no remote
restart mechanism, period**. The proof is stronger than an absence: when the
vendor app itself cannot reach the device, its own remediation text tells the
human to physically cycle it —

> `"The device connected fail, please try again later\n or try again after
> restart the device ..."` — `CommCfgForm.cs:249`

That string is the vendor's answer to this exact question, from the vendor's
own tool. Our R5 (§3) lands in the same place the vendor does.

### 1.3 The 0x8000 debug-mode register: what is and is not established

Values and names (Confirmed by vendor code — `MiniESapp.cs:2101-2156`
`bindDebugModeType()`, `GlobalFun.cs:152-165`): 0 Normal Mode, 1 Standby,
2 Charge, 3 Discharge, 4 Circulation, 5 Fixing SOC, 6 Verify Capacity.
Read-back at `0x8100` / 33024 (`SysControl.cs:372-396`), polled at 1 Hz in
every read chain (`SysControl.cs:151, :190, :246, :333`) and re-read
immediately after every mode write under the same mutex
(`MiniESapp.cs:2166-2167`) — write-then-verify is the vendor's own precedent
for this register.

Established vs unproven, precisely:

| Claim | Status | Evidence |
|---|---|---|
| 0x8000 is the command register; 0x8100 is its read-back | Confirmed by vendor code | `MiniESapp.cs:2159-2170`; `SysControl.cs:372-396` |
| 0 (Normal) is the required state for PQ dispatch | Confirmed by vendor code | The send handler refuses while `debugMode != 0`: "switch the device to 'Nomal Mode' and try again!" (`MiniESapp.cs:2179-2184`; noted in VENDOR_CONTROL_COMPARISON §2.2 — vendor bug: the message is composed but never displayed, a SILENT refusal) |
| Firmware actually ignores `0x0200` when mode ≠ 0 | **Unknown** | PROTOCOL_EVIDENCE §7: the decompile proves the APP checks, nothing about firmware. Comparison §3c U1. |
| Value 1 "Standby" stops the PCS/DC-DC, and value 0 revives it | **Unknown** | PROTOCOL_EVIDENCE §8: "Label only; voltage, contactor, and trickle behavior are Unknown." No vendor code path writes 1 and then 0. |
| A 1→0 cycle re-initializes the control path (soft power cycle) | **Unknown / hypothesis** | No evidence in any source. This is R3's live-trial question. |
| Writing 0x8100 aliases writing 0x8000 | **Unknown** | The HA-derived tester wrote 0/2/3 to `0x8100` directly (`modbus/byd_energypod_manager/verification/battery_tester.py:50-55`, `mode_tester.py:83-119`) — conflicting addressing, rejected as a contract (PROTOCOL_EVIDENCE §12), but it is the most likely path by which a pod was ever left in a non-Normal mode. Comparison §3c U3. |
| The vendor auto-normalizes modes | **FALSE** | The vendor writes 0x8000 only on an explicit human combo selection; there is no automated normalization anywhere (comparison §4.2). Value 0 is the vendor's normal/daily STATE, never an automated WRITE. |

Two adjacent facts support the existence of a managed stop/start state machine
without proving its remote trigger: the battery-status enum has explicit
transitions (1 Stop, 2 Starting, 3 Running, 4 Stopping — `GlobalFun.cs:239-251`),
and the clear-LVP dialog names a startup permission ("the EnergyPod is not
permitted to startup … will be permitted to startup!", `MiniESapp.cs:2582`).
Separately, the prior `byd` iteration drove a standby-like state for years on
these very pods via a DIFFERENT, contract-rejected register — writing
force-state 5 ("STANDBY_TRICKLE_CHARGE") to decimal 512 as its stop, and
force-state 1 ("NORMAL_MODE") before every charge/discharge
(`modbus/byd/battery.py:279-306`, reading 0x8100 as "EB1_FORCE_STATE_ID"
`:372`; disposition in PROTOCOL_EVIDENCE §12). Corroborated operationally that
a stop-like state is enterable and exitable on this hardware; not evidence
about `0x8000` semantics.

### 1.4 The vendor app's own recovery behavior (what it does when a pod goes silent)

- **Failure detection**: every read is try/catch; exceptions increment
  `communicationAbnomalCnt`; 3 consecutive → `devConnectioinStatus = false`
  and a modal box (`SysControl.cs:460-470`). Caveat (PROTOCOL_EVIDENCE §11,
  class Assumed): only *exceptions* count — short/malformed responses return
  false without incrementing, and later successful sub-reads can mask earlier
  failed ones.
- **No automatic reconnect**: the UI tick sees the dead flag, closes the
  transport, and shows a failure icon (`MiniESapp.cs:1346-1354`;
  `CloseCommunication` disposes the master, `MiniESapp.cs:1707-1730`).
  Recovery is a fresh manual connect (`MiniESapp.cs:1086-1120`) — and if that
  fails, the §1.2 "restart the device" message. Transport retries are zero
  (`CommCfgForm.cs:214`).
- **Objective retry pattern**: the "Keep Send" loop (checkbox default ON)
  re-sends the PQ objective every 1000 ms and sends `[1,0,0]` on stop
  (`MiniESapp.cs:2221-2237`, default-on `:4744-4752`). This is the vendor's
  entire "writes not sticking" remedy: keep re-sending, human watching.
- **Nothing is written on connect or teardown**: connect does reads only
  (`MiniESapp.cs:1113-1118`); form close is `CloseAllTask` +
  `CloseCommunication`, no writes (`MiniESapp.cs:2461-2465`).
- **Ethernet mode is a reverse connection** — the PC listens on 192.168.1.10:507
  and the DEVICE connects in (CommCfgForm.cs:254-291). Our Waveshare path is
  the opposite direction (client to gateway :4196). This matters for
  interpreting the operator's history: the vendor app's connection health is
  the pod's outbound TCP health, not our gateway path.

### 1.5 Device-side signals relevant to unresponsiveness (all Confirmed by vendor code / workbook per PROTOCOL_EVIDENCE §9)

- **The pod's own comm-loss vocabulary** — BMS Warning0: bit 11 "No Remote
  Dispatch", bit 12 "No TCP Connection", bit 13 "Electricity Meter
  Communication Disconnected" (the CT meter link), bit 14 "DRED Communication
  Disconnected". The existence of bit 13 is direct evidence the pod models
  its CT-meter link as a monitored, losable communication channel.
- **Sampling-failure faults**: PCS Fault1 bits 4-13 are per-channel sampling
  failures (inverter/grid/busbar/external-grid/leakage/temperature sample) —
  the pod's own "my measurements are dead" alarms.
- **Start/stop faults**: BMS Fault0 bit 1 "Start Fail", bit 2 "Stop Fail" —
  a failed standby-cycle attempt has a named device-side fault.
- **Command lease**: unrenewed PQ objectives expire ≈3.5-4.0 s after the last
  write and autonomy resumes (MEASURED live, PROTOCOL_EVIDENCE §4b). The
  watchdog is a fallback, not a recovery mechanism — but it bounds what any
  recovery action must outrun.
- No watchdog/kick register exists: the lease is renewed by simply rewriting
  `0x0200` (vendor 1 Hz; prior 2.0 s; ours ≤1.5 s).

### 1.6 The prior integration's recovery behavior

- **Production tree** (`manager.py` + `battery.py`): no reconnect logic, no
  mode handling, no reset. Every scheduler tick reconnects from scratch per
  pod — `manage_battery` context manager connect/operate/close
  (`modbus/manager.py:102-111`; explicit connect/close at `:341-362`) — which
  is crude implicit transport recovery (a wedged TCP session died and was
  rebuilt every 2 s, `manager.py:154`). Writes are fire-and-forget:
  `set_active_power` never checks `response.isError()` (`modbus/battery.py:245`).
  **If writes stopped taking effect, nothing detected it.** The only detector
  was the human reading the PVOutput dashboard at 5-minute cadence
  (`manager.py:565-608`). There is no recorded remedy short of waiting for the
  next tick — the operator's physical power cycle predates this rewrite and
  was the human fallback around this whole stack.
- **`reset_batteries_to_power_mode`** (`manager.py:416-445`) sounds like
  recovery but is the passive-mode block's self-defeating reset (it disables
  passive mode then re-enables float — comparison §2.6). Not a wedge remedy.
- **`byd` iteration** (later, not the years-long production path): genuine
  reconnect-with-backoff — `_ensure_connection` closes and reconnects on
  `ConnectionException`/`ModbusException` (`modbus/byd/battery.py:124-136`,
  `:168-171`), plus the force-state write pattern of §1.3.
- **No reboot/reset/watchdog register write exists anywhere in any tree.**

### 1.7 Our controller today

- **R1 exists and is live-proven**: transport resync rebuilds the client from
  the factory (a pymodbus client closed with `reconnect_delay=0` never
  reconnects — observed live as the multi-hour LHS stale incident, fixed
  f788701): `src/energypod/adapters/modbus/waveshare.py:142-158`.
- **Write scope is structurally pinned to the PQ frame**: the transport
  rejects any write that is not exactly `[1,P,Q]` at `0x0200`
  (`waveshare.py:188-189`: "only the evidenced three-register PQ objective is
  writable"). Any `0x8000` write is a code change plus a deliberate
  write-scope policy change, by construction.
- **Mode words are decoded and gating** (landed 2026-08-23 wave + B5):
  `0x8100` debug, `0x0100+1` ctrlMode, `+2` workMode, PCS runMode
  `0x1002` (`decode.py:316-319`); dispatch refuses `device_debug_mode_active`
  / `device_mode_not_remote` on fresh evidence — cached word alone never
  refuses, one bounded re-read before deny (`service.py:1356-1399`).
- **Write failures are visible**: every suppressed heartbeat failure is
  audited `heartbeat_failed` (`composition.py:~1794-1807`).
- **Still queued, not built**: per-heartbeat objective echo read-back
  (queue item iii) and the actuation-coherence watchdog — N cycles
  authorized > 0 with no measured movement ⇒ alarm (queue item vi,
  CONTINUITY 2026-08-23). These are R4's missing half.

### 1.8 Gateway reboot path — unknown

Each pod hangs off its own Waveshare Ethernet-to-RS485 gateway
(.11/.12/.13:4196). The exact gateway model and its configuration are an open
uncertainty (PROTOCOL_EVIDENCE §13.8). Waveshare gateways of this class
typically carry a web UI with a reboot function, but **nothing in our
evidence establishes that the deployed units expose one, on which address,
or that a gateway reboot would help a wedged pod** — and if the pod is the
wedged party (§2), it would not. Treated as an open question for the operator
(§5), not a rung.

### 1.9 Candidate mechanisms, ranked

| # | Mechanism | What the evidence actually says | Class of help |
|---|---|---|---|
| C1 | Transport resync + client rebuild | Implemented, live-proven fix for our transport wedges | Class 1 |
| C2 | Fresh-read mode verification + named refusal | Implemented; vendor's own posture, extended | Class 3 |
| C3 | Operator-authorized `0x8000 ← 0` (Normal) with read-back | Register and normal value evidenced; firmware effect of the write itself never captured live; U3 (aliasing/no-op) unresolved | Class 3 |
| C4 | Standby cycle `0x8000 ← 1`, wait, `← 0` | Label-only hypothesis; prior integration's standby exit via the rejected 512 register is weak corroboration | Class 3-subset / maybe soft Class 2 |
| C5 | Detection: coherence watchdog + classifier + console state | Design ready; inputs already decoded | All classes |
| C6 | Gateway reboot | Unknown model/UI; not evidenced | Class 1 only |
| C7 | Remote restart command | **Does not exist** in any source | — |
| C8 | Physical power cycle | The vendor's own documented remedy | Class 2 (definitive) |

---

## 2. THE UNRESPONSIVENESS TAXONOMY

Four classes. The operational question that separates them: **does the pod
still answer on the bus, and does it do what it answers with?**

| Class | Definition | How it presents through our telemetry / console | Historical incidents | Remedy status |
|---|---|---|---|---|
| **1 — Gateway/network unreachable** | The TCP path to the gateway (or the gateway itself) is down; the pod may be perfectly fine behind it | Connect errors / `TransportConnectionError`, `telemetry_stale` climbing, connection-epoch churn, ages climbing, connection indicator red; heartbeat_failed audit rows | LHS multi-hour telemetry-stale (2026-08-23, pymodbus client never reconnected — fixed f788701); vite dev-proxy 502s (console-side); the deploy dark window misread as a stall | **FIXED** — R1 resync + rebuild, live-proven |
| **2 — Pod not answering on the bus** | Gateway accepts TCP (or at least the network path is up) but slave 4 is silent/garbage on Modbus — firmware wedged | The discriminator we can build but have never yet seen: TCP connect OK while FC03 requests time out with the gateway itself responsive for other units (each pod has its own gateway, so per-pod isolation is clean); all reads stale, writes ACK-less; mode words unservable | **No confirmed instance** in our 2026-08-22→08-24 record. The operator's pre-rewrite power-cycle anecdote is the candidate member (§2.1) | **OPEN** — the honest answer is possibly "none remote" (R5); C4 is the only candidate soft remedy, unproven |
| **3 — Pod answering but ignoring objectives** | Fresh telemetry, ACKed writes, no physical effect — mode/autonomy/other gate between our objective and actuation | Telemetry GOOD and fresh; `0x8100`/`0x0100+1` abnormal (if mode-latched) with our named refusals; authorized > 0 while measured watts never move (the coherence watchdog's signature) | 2026-08-23 22:11Z silent actuation loss — LOOKED like this class, root-caused to OUR publish-fence desync + allocator starvation (both fixed); mode-latch remains theoretical with vendor precedent; B6 autonomy misclassification (2026-08-24, fixed 6c4254b) was our provenance bug wearing this class's clothes | **LARGELY FIXED** for detection (C2 landed; watchdog queued); for an ACTUAL latched mode, the only remedy is C3/C4 — policy-gated, not built |
| **4 — Pod answering with nonsense** | Registers decode, quality GOOD, but behavior contradicts model: uncommanded motion, oscillation | Fresh GOOD telemetry, no intents, no writers, power oscillating; foreign-objective detector (queued) would show nothing we recognize | mid's ±1.2 kW uncommanded mid oscillation (2026-08-23, STILL UNEXPLAINED, ongoing at last record; day-scope) | **OPEN** — diagnosis (U1/U2 mode-word read while it oscillates) before any remedy is even discussable |

### 2.1 Which class is the power-cycle anecdote? — **Class 2, pod firmware wedge**

Reasoning from the operator's own detail — after the power cycle the pods
"reacted to the CT sensors again":

1. **CT-following is pod-local firmware.** The CT/PV/load measurement chain and
   the self-consumption controller live entirely inside the pod (vendor decodes
   the CT surface `SysControl.cs:499-503`; our live captures prove autonomy
   runs with no writer at all; the prior integration's own docs say pods act
   "instead of relying on CT sensors" only in passive mode). If only the
   GATEWAY or network had wedged (Class 1), CT-following would have continued
   invisibly and a gateway reboot would have restored visibility — no pod
   power cycle needed. The CT loop itself dying can only be a pod-side hang.
2. **"Fail to respond to ANY commands" + "not active or synchronized" + CT
   death together** describe a whole-pod firmware hang (PCS/BMS control task
   and likely the comm task with it) — the state a full power-on-reset
   re-initializes (CT sampling, comm stacks, control state machine all
   restart). The device's own warning vocabulary (§1.5: "Electricity Meter
   Communication Disconnected", "No Remote Dispatch", sampling-fault bits)
   names exactly the sub-links that can die.
3. **The alternative reading** — control task hung while Modbus still answers
   (a deep Class 3/Class 4 hybrid) — is possible, and would be a candidate for
   the C4 standby cycle. The two readings are distinguishable next time it
   happens: did telemetry keep flowing while commands stopped working (comm
   alive ⇒ Class 3/4 hybrid ⇒ C3/C4 plausibly help), or did reads die too
   (Class 2 ⇒ nothing remote helps, R5 honest terminal)? That discriminator is
   precisely what R4's classifier records (§3).
4. Notably, Class 2 has never occurred in our three observed operating days —
   this is a rare, possibly environmental (thermal? long-uptime drift?)
   failure mode from the pre-rewrite era. Its rarity is why the design prefers
   detection + a small authorized toolbox over speculative automation.

---

## 3. THE RECOVERY LADDER — staged design, safest first

Design rules inherited from the standing doctrine: positive evidence only;
fail-closed; every action audited; nothing that smells of restored authority
is automatic; boot stays observe-only. The ladder is **classifier-driven** —
each rung's availability is gated on the class R4 assigns, and rungs 2b/3 are
never automation.

### R1 — Transport resync / reconnect (HAVE)

- **Trigger**: any transport error/timeout, `telemetry_stale`, epoch churn.
- **Action**: drop and rebuild the client from the factory, reconnect, resume
  polling (`waveshare.py:142-158`).
- **Evidence basis**: live-proven fix of the LHS multi-hour stale incident
  (f788701); vendor equivalent is manual reconnect; `byd` equivalent is
  `_ensure_connection`.
- **Risk**: none beyond a transient gap; already bounded by staleness gates.
- **Authorization**: none — automatic, as today.

### R2 — Mode normalization

**R2a — fresh-read verify + named refusal (HAVE).** When dispatch is
refused, the mode words are re-read fresh through the owning actor before any
denial (`service.py:1371-1399`), the refusal names the cause
(`device_debug_mode_active` / `device_mode_not_remote`), and the console shows
the operator the concrete next step ("open the vendor MiniES app, check Debug
Mode = Normal Mode and SysControlMode = Remote" — comparison §4.3). This is
exactly the vendor's own posture (read, refuse, never auto-write;
`MiniESapp.cs:2179-2184`), made visible.

**R2b — the operator-authorized `0x8000 ← 0` (Normal) write (THE QUESTION —
designed, deliberately not built).** Reconciliation against the standing
rules, explicitly:

- *For*: `0x8000` is the evidenced command register and 0 is the evidenced
  normal value — the state every observed readback has held on this fleet and
  the precondition the vendor UI itself enforces (`MiniESapp.cs:2159-2170`,
  `:2179-2184`). A write that restores the documented normal state of a
  documented register is the minimal possible member of the mode-write family.
- *Against / unresolved*: PROTOCOL_EVIDENCE §14 gates "any debug/maintenance
  mode" behind official service documentation plus a controlled isolated
  procedure — §8 marks firmware semantics Unknown even for Standby, and no
  `0x8000` write of ANY value has ever been captured on live firmware; U3
  (whether `0x8100` writes alias `0x8000` — the HA tester used the other
  address) is unresolved; and VENDOR_CONTROL_COMPARISON §4.2 kills the
  automation justification outright: the vendor NEVER auto-writes modes —
  value 0 is its daily STATE, and its writes are deliberate human commissioning
  acts with immediate read-back.
- **Resolution**: adopt comparison §4.4's five gates verbatim as the design —
  (a) a live read showing an abnormal mode word on a pod whose stable state is
  known; (b) explicit per-pod, per-word, per-value operator authorization
  (interactive principal, type-back confirmation, like ARM); (c) a single
  write of the NORMAL value ONLY, to the vendor's command address `0x8000`,
  never the HA-derived `0x8100`; (d) immediate read-back verification at
  `0x8100` under the same serialized ownership, with a bounded retry+verify;
  (e) a durable audit event naming the authorizer, prior word, write, and
  verified word. **Precondition**: U3's no-op re-write test
  (`0x8000 ← current value`, no state change) must run first under its own
  authorization to prove the write path and addressing on live firmware.
  Modes 2-6 stay permanently outside any surface we expose — 2 (Charge) and 3
  (Discharge) would command power around the entire safety kernel; 5/6 are
  calibration. This is a commissioning-grade capability, never routine, never
  automatic, and it requires relaxing the structural write gate
  (`waveshare.py:188-189`) for exactly this one address/value pair under an
  explicit policy flag.

### R3 — Standby-cycle recovery (IF the evidence supports it — it currently does not, by itself)

- **Hypothesis**: `0x8000 ← 1` (Standby), wait for the stop transition, then
  `0x8000 ← 0` (Normal) — a soft stop/start of the PCS control path, i.e. the
  nearest remote approximation of the operator's power cycle.
- **Evidence basis (honest)**: Standby firmware behavior is Unknown
  (PROTOCOL_EVIDENCE §8 — voltage/contactor/trickle semantics unproven); the
  prior `byd` iteration's standby-enter/exit via the rejected 512 force-state
  register is operational corroboration that THIS hardware tolerates a
  standby-like state and exits it (comparison §1.3, PROTOCOL_EVIDENCE §12);
  the device's Start Fail/Stop Fail faults (§1.5) say the transition is a
  real state machine with named failure modes. Net: plausible, unproven, and
  the failure mode is asymmetric — **if the return-to-0 write fails or is
  ignored after entering Standby, we have manufactured the very state we were
  trying to cure** (pod parked, possibly uncommandeerable).
- **Design (if ever authorized)**: one separately authorized live trial, one
  pod, operator PHYSICALLY PRESENT as the backstop (the power cycle in
  hand); write 1 → poll BattStatus/PCS status words for the stop transition
  (vendor transition vocabulary §1.3) with a bounded window (the vendor's own
  post-write cadence precedent is `Sleep(1000)` + re-read;
  `MiniESapp.cs:2497-2498`) → write 0 → read-back verify at `0x8100` →
  re-arm and re-dispatch a small objective to prove commandability → full
  audit. Abort-to-human at any unverified step. Until that trial succeeds on
  hardware, R3 does not exist as a capability.
- **Authorization**: its own explicit operator authorization, distinct from
  and after R2b, with the physical-presence condition.

### R4 — The detection layer: an unresponsiveness classifier with a console state

The rung that makes every other rung honest. Built from parts we already have
plus the two queued items:

- **Inputs**: (i) bus-answer pattern per pod — TCP connect outcome vs FC03
  response outcome vs timeout (connect-fail ⇒ Class 1 gateway; connect-ok +
  read-timeout ⇒ Class 2 pod silent), epoch churn, `telemetry_stale`,
  `heartbeat_failed` rows; (ii) mode words already decoded at the core tier
  (`0x8100`) / fresh-at-refusal (`0x0100+1`); (iii) the actuation-coherence
  watchdog (queue item vi: N consecutive cycles authorized > 0 with measured
  `battery_watts` pinned at the autonomous baseline ⇒ "answering but not
  actuating"); (iv) the objective echo read-back (queue item iii) as the
  per-write companion.
- **Output**: an explicit per-pod console state machine —
  `OK` / `gateway unreachable (auto-recovering)` / `pod not responding —
  recovery available` / `pod ignoring objectives — mode: <word>` /
  `behavior unexplained — see diagnostics` — with the applicable ladder rung
  named and the not-applicable rungs greyed with reasons.
- **Evidence basis**: the 2026-08-23 incident proved the cost of its absence
  (6+ minutes authorized-but-not-actuated, detected only by a human);
  comparison §2.5/G4; every input is already polled or designed.
- **Risk**: false positives nag the operator; bounded by requiring N
  consecutive cycles and by classifying on multiple signals.
- **Authorization**: none — read-only, buildable now (§4).

### R5 — The honest terminal: physical intervention

Some wedges — the Class 2 whole-pod hang of §2.1 among them — may genuinely
require the physical power cycle, because no remote restart primitive exists
(§1.2, the vendor's own remediation string). The design must say so rather
than overpromise: when R1 has exhausted itself and R4 classifies Class 2 (and
R3 either is not authorized or did not revive the pod), the console's answer
is an explicit, actionable state — "physical intervention required" — with
the operator checklist: which pod, toggle-off duration guidance, what to
verify after restart (telemetry resumes, `0x8100` = 0, ctrlMode = Remote, CT
words moving), and a pre/post diagnostic snapshot captured for the record.
This is also the moment R4's classifier finally characterizes the anecdote of
§2.1 with data instead of memory.

---

## 4. BUILD NOW vs LIVE TRIAL vs EXPLICIT AUTHORIZATION

| Buildable now — no new authorization, no live hardware interaction needed | Needs a live trial (observe/read-only, separately scheduled) | Needs the operator's EXPLICIT authorization (write-scope policy change) |
|---|---|---|
| R4 detection: actuation-coherence watchdog (queue vi), objective echo read-back (queue iii), per-pod unresponsiveness classifier + bus-answer discrimination (connect-ok vs read-timeout), console recovery states | U1/U2: one authorized mode-word snapshot read (`0x8100`, `0x0100+1/+2`, `0x1002`) on all three pods — ideally while mid oscillates and/or during the next abnormal state (read-only; answers whether a mode word ever explains a Class 3/4 event) | **Any `0x8000` write at all** — starting with the U3 no-op re-write (`0x8000 ← current value`), then R2b normalization (`0x8000 ← 0`), each its own commissioning-grade authorization with read-back + audit |
| R2a completion: refusal guidance text with the concrete vendor-app remedy per comparison §4.3 | Capturing what the console sees during a genuine wedge — requires the next occurrence; the classifier is built so the capture is automatic | R3 standby cycle (`0x8000 ← 1` … `← 0`): separate authorization AFTER R2b, operator physically present, one pod, abort-to-human |
| R5 terminal UX: "physical intervention required" state, per-pod checklist, pre/post diagnostic snapshots | Per-unit characterization of the restart itself (which words change across a physical cycle — ask the operator to tell us before the next toggle so we can snapshot around it) | Relaxing the structural write gate (`waveshare.py:188-189`) for the single `(0x8000, value 0)` pair under an explicit policy flag — a deliberate commissioning decision, never a default |
| The authorization workflow rail for R2b/R3 (interactive principal, type-back confirmation, audit event shape) — build the rail now, keep the train locked | — | Standing prohibitions unchanged: `0x8001`/`0x8037` (0xFF00 clears) remain prohibited; debug values 2-6 remain permanently unexposed; `0x0101` ctrlMode has NO evidenced write anywhere and Local mode stays refusal+escalation, never a write |

Any `0x8000` write is a write-scope policy change: treat it as a deliberate
commissioning-grade decision, never routine automation.

## 5. Open questions for the operator

1. **When did the power-cycle state last occur, and how often?** Which pods?
   Before or during the prior Docker integration's years of operation?
2. **The classifier question — what still worked?** When it happened, did the
   monitoring/telemetry keep flowing from that pod (comm alive, control dead
   — the C4 candidate), or did reads die too (full wedge — R5)? This single
   answer decides which rung could ever have helped.
3. **Symptom order**: did it stop reacting to CTs first and then stop
   answering commands, or the reverse? Any fault/warning showing on the
   vendor app at the time (in particular "Electricity Meter Communication
   Disconnected", "No TCP Connection", "No Remote Dispatch", or any PCS
   sampling fault — §1.5's vocabulary)?
4. **Was the vendor app connected to the pods at the time** (or any other
   master — Home Assistant, the night writers)? Two masters on one RS-485
   path can inter-frame collide; the vendor app's own Ethernet mode is a
   reverse connection the pods dial out for, which is a different failure
   surface from our gateways.
5. **Did the gateway ever need its own restart**, or did toggling the battery
   alone always fix it? (Separates gateway wedge from pod wedge.)
6. **How long was the toggle-off** — seconds or minutes? (Blink-and-back vs
   capacitor-drain full reset; distinguishes a controller reboot from a deep
   hardware re-init.)
7. **Gateway make/model** — is there a web UI reachable on the LAN for
   .11/.12/.13, and does it expose a reboot? (Unverified §1.8; if yes, a
   Class 1 remedy we could eventually automate as R1.5.)
8. **Authorization posture**: knowing the evidence above, would you authorize
   (a) the U3 no-op write and (b) the R2b normalization write as
   commissioning-grade manual actions — and, later and separately, (c) the R3
   standby-cycle trial with you physically present?

---

### Source anchors

Vendor app: `EnergyPod_RE/src/MiniESapp/` (`SysControl.cs`, `MiniESapp.cs`,
`GlobalFun.cs`, `CommCfgForm.cs`); prior integration: `modbus/`
(`manager.py`, `battery.py`, `byd/battery.py`,
`byd_energypod_manager/verification/`); ours: `docs/PROTOCOL_EVIDENCE.md`
(§4b, §5, §7, §8, §9, §11, §12, §13, §14), `docs/VENDOR_CONTROL_COMPARISON.md`
(§2.2, §2.5, §3c, §4), `docs/SYNC_RESILIENCE_AUDIT.md` (§0.1, B5, B6),
`docs/CONTINUITY.md` (2026-08-22/23/24 entries), `src/energypod/adapters/
modbus/waveshare.py`, `src/energypod/adapters/modbus/decode.py`,
`src/energypod/application/service.py`, `src/energypod/runtime/composition.py`.
