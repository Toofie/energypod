# Operator console control-surface gap analysis

Prepared: 2026-08-23. Research and documentation only — no code, API, hardware, or
test interaction was performed to produce this document.

Purpose: state precisely (1) what an operator can control in our web console today,
(2) every user-controllable element the decompiled vendor application offered, (3) a
prioritized recommendation of which additional capabilities we should incorporate
mapped onto our safety architecture, and (4) the standing exclusion list that must
never surface in any product interface.

Sources:

- Vendor decompile: `C:\Users\vagrant\Downloads\EnergyPod_RE\src\MiniESapp\`
  (principally `MiniESapp.cs`, `SysControl.cs`, `GlobalFun.cs`, `LoginForm.cs`,
  `CommCfgForm.cs`). Citations are `file:line` in that tree.
- Our console: `web/src` (read-only; another agent is actively working there),
  `src/energypod/api/rest.py`, `src/energypod/api/mcp.py`, domain/application
  modules, and `docs/{API_CONTRACTS,UI_CONTRACTS,PRODUCT_ROADMAP,PROTOCOL_EVIDENCE,
  CONTINUITY}.md`.
- Prior integration: `C:\Users\vagrant\Downloads\modbus` (the operator's own
  Streamlit/byd stack).

Register addresses are zero-based PDU addresses, consistent with
`docs/PROTOCOL_EVIDENCE.md` §3.

---

## Part 1 — What the operator controls today

### 1.1 Console surface (web/src)

| Capability | Where in the UI | API call |
|---|---|---|
| Session unlock (paste-only bearer token, in-memory) | Token gate on every load | any call, 401 ends the session |
| Emergency stop, reachable from every screen, focus-trapping confirm naming affected units | Header `StopControl` (`web/src/app/StopControl.tsx:23-138`) and Now view | `POST /api/v1/emergency-stop` |
| Emergency-stop acknowledgement, exact stop id typed back | Now view (`NowView.tsx:1369-1410`) | `POST /api/v1/emergency-stop/{stop_id}/acknowledge` |
| Arm, with live readiness checklist (qualified / latch state / control-readiness reasons) and explicit "ARM" confirm | Now view arm dialog (`NowView.tsx:1414-1469`) | `POST /api/v1/arm` (`{"unit_ids": [...], "confirmation": "ARM"}`) |
| Disarm (confirmation dialog; no confirmation literal — safety-positive) | Now view (`NowView.tsx:1471-1487`) | `POST /api/v1/disarm` |
| Charge / discharge dispatch: direction as separate actions, positive watts, duration in minutes (UI bound 5 min = API `ttl_s` ≤ 300 s), per-unit checkboxes, plain-language preview, one idempotency key per operator action | Now view dispatch dialog (`NowView.tsx:1489-1584`) | `POST /api/v1/intents` |
| Inhibit acknowledgement for latched units, explicit confirm, explains re-arm is a separate step | Now view + Batteries cards | `POST /api/v1/units/{unit_id}/inhibit/acknowledge` |
| Current request as four separate facts: Requested / Allowed / Actual (+age) / Remaining time (live countdown) | Now view fact strip (`NowView.tsx:1184-1201`) | snapshot + `observation.published` stream frames |
| Fleet picture: lifecycle badge, four-fact connection indicator (page loaded / API reachable / stream live / control readiness), restart notice, announcements | Shell (`AppShell.tsx:113-191`) | `GET /api/v1/health`, WS `/api/v1/events` |
| Home: fleet reserve, per-unit requested/allowed/actual, next planned action with expiry countdown, limiting factors in plain language | Home view | `GET /api/v1/snapshot`, `/api/v1/health` |
| Batteries: fleet cards (SOC, direction+power, quality/age, warnings, latch state) and per-unit detail tabs Summary / Cells / Events / Details with the full observation projection | Batteries view | `GET /api/v1/units/{unit_id}`, `GET /api/v1/audit` |
| Activity: newest-first audit timeline with cursor pagination ("Load more") and client-side filters by kind and by unit | Activity view | `GET /api/v1/audit?limit&after_sequence` |

Planned views render honest "not available yet" placeholders: Energy flow, Insights,
Schedule, Plan history (`web/src/app/views.ts:45`). Nothing dead-link pretends to work.

### 1.2 REST surface (src/energypod/api/rest.py)

- `GET /healthz` — unauthenticated liveness only (rest.py:449-458).
- `GET /api/v1/snapshot` (observe) — fleet view incl. per-unit telemetry summary (rest.py:460-462).
- `GET /api/v1/health` (observe) — liveness / service readiness / control readiness, with `process_instance_id` and `uptime_s` (rest.py:464-466).
- `GET /api/v1/audit` (observe + audit:read) — bounded pages with `after_sequence` cursor (rest.py:468-476).
- `GET /api/v1/units/{unit_id}` (observe) — full latest-observation projection (rest.py:478-486).
- `POST /api/v1/intents` (dispatch) — `unit_ids`, `direction ∈ {charge, discharge}`, `watts > 0` (fleet total), `ttl_s ≤ 300`, optional `reason` (rest.py:71-97, 488-508).
- `POST /api/v1/arm` (arm + interactive principal + `"confirmation": "ARM"`) (rest.py:510-536).
- `POST /api/v1/disarm` (arm scope; interactive not required) (rest.py:538-561).
- `POST /api/v1/emergency-stop` (stop scope) and `.../{stop_id}/acknowledge` (stop:acknowledge) (rest.py:563-657).
- `POST /api/v1/units/{unit_id}/inhibit/acknowledge` (arm + interactive) (rest.py:659-697).
- `POST /api/v1/events/session` + `WS /api/v1/events` (observe) — single-use ticket handshake (rest.py:699-756).

Every mutation requires an `Idempotency-Key`; errors are the structured envelope.

### 1.3 MCP surface (src/energypod/api/mcp.py)

`get_snapshot`, `get_health`, `get_recent_audit` (observe; audit additionally
audit:read), plus optional `dispatch_intent` when explicitly configured with the
`dispatch` scope — TTL capped at 30 s by default (mcp.py:89, 108-183). MCP cannot
arm, acknowledge, or touch policy. `get_unit_detail` is queued (CONTINUITY).

### 1.4 Capability that exists but is NOT yet surfaced (exposure gaps)

1. **Schedules exist in the domain, nowhere else.** `ScheduleEntry`/`SchedulePlan`
   validation with cross-midnight, overlap, and timezone contracts
   (`src/energypod/domain/schedule.py:37-165`), a deterministic evaluator producing
   short-lived `SCHEDULE` intents (`src/energypod/application/scheduling.py:34-85`),
   and the `ScheduleRepository.get()/replace(version, entries)` port
   (API_CONTRACTS "Application ports") — but no REST endpoint, no facade exposure,
   and no UI beyond the "Schedule — not available yet" nav placeholder.
2. **Mode words are read but not decoded or exposed.** The read plan already
   includes `0x8100` (debug-mode readback) and the `0x0100` system block
   (`register_layout.py:85-91`), which carries control mode (Remote/Local) and work
   mode — but `decode.py` emits neither, so the console cannot show them. This is
   fix-queue item (iv) in CONTINUITY.
3. **Cumulative energy counters are read but not surfaced.** The IoT plan includes
   the `0x4101` 12-register energy block (`register_layout.py:143`), but
   `Observation` carries no energy fields; the vendor home chart (PV / buy / sell /
   charged / discharged / load) has no analog.
4. **Advisory grid/load power is in flight.** The wire decoder now emits
   `grid_power_w` / `load_power_w` (`decode.py:297-312, 352-353`;
   `observations.py:76, 100-101`); facade readthrough and UI display are part of
   the in-flight excess-solar work.
5. **Policy (bounds) has no read surface.** The dispatch preview says "subject to
   the site power limit" but the operator cannot see what the limit is.
6. **Health instance identity** (`process_instance_id`/`uptime_s`) was added for
   the console (CONTINUITY 2026-08-23, f20602c) but the console does not render it
   yet (queued follow-up).
7. **MCP `get_unit_detail`** exists as a facade method but no MCP tool.
8. **Audit filters** are client-side only over loaded pages; the server cursor
   pagination is exposed, unit/kind filtering is not.

---

## Part 2 — Full vendor application inventory (cited)

The vendor application is a single-device Windows desktop tool (Metro UI, Modbus
RTU/serial or reverse-connected Ethernet). It has no fleet concept, no schedule
editor, and no tariff logic. Everything below is grouped by category with the wire
effect and a classification:

- **(a)** legitimate operator capability (a fair product analog exists or is worth adopting),
- **(b)** commissioning/service function (device setup, not daily operation),
- **(c)** debug/service mode or maintenance write that `PROTOCOL_EVIDENCE.md` and
  the roadmap exclude from product exposure — **must never surface** (Part 4).

### 2.1 Session and roles

| Control | What it does | Class |
|---|---|---|
| Login dialog, three hardcoded accounts: `BYDadmin`/`101028` (type 3), `BYDuser`/`101028` (type 2), `Installer`/`898888` (type 1) | `LoginForm.cs:39-66`. Passwords are shared literals compiled into the binary; admin and user differ only in UI visibility. | n/a — we do better (scoped, rotatable bearer credentials; interactive-principal requirement for arming) |
| Role-gated menu visibility (debug/installation/settings/clear-energy/serial controls per role) | `MiniESapp.cs:1903-1954`; logout hides them `MiniESapp.cs:2239-2260` | (b) pattern worth keeping conceptually: privilege tiers — ours are scopes |

### 2.2 Power control (the "Debug" page — despite the name, the only power dispatch the app has)

| Control | Wire effect | Class |
|---|---|---|
| P objective + Q objective text boxes, Send button | `MiniESapp.cs:2172-2212` (`metroButton1_Click_1`) → `SysControl.SendPQPower` `SysControl.cs:1442-1461` → FC16 write of `[1, (ushort)(short)P, (ushort)(short)Q]` at `0x0200`. Guard: refuses to send unless device debug mode (`0x8100` readback) is 0 "Normal Mode" (`MiniESapp.cs:2179-2184`). P in W, Q in var, signed raw values with no direction semantics anywhere in the UI (PROTOCOL_EVIDENCE §4b). | (a) — this is the one vendor control we replicate, bounded: our intents → arbiter → allocator → SafetyKernel → `0x0200` `[1, signed-P, signed-Q]` with direction+positive-watts semantics and every limit applied. Q must NOT become an operator control (see Part 4). |
| "Keep Send" checkbox (default checked) — continuous renewal | Repeats the PQ write every 1000 ms until the operator stops it; on stop writes `[1,0,0]` (`SendPowerAways`, `MiniESapp.cs:2221-2237`, stop at 2233-2236). This is the vendor's "heartbeat": no TTL, no safety re-evaluation, runs until a human clicks Stop or the app dies (firmware watchdog then clears it). | (a) concept, (c) as-implemented — we keep the renewal idea (kernel renews only while safety passes; measured watchdog ≈3.5-4.0 s) and add what the vendor lacks: expiry, per-cycle evaluation, fail-closed |
| Plot Enable button | No click handler is wired anywhere in the form (`MiniESapp.cs:4795-4799` declares it; no `Click +=` exists). A dead button shipped in the service tool. | none — cautionary tale |

### 2.3 Debug / maintenance mode selector

| Control | Wire effect | Class |
|---|---|---|
| Debug mode combo: Normal Mode / Standby / Charge / Discharge / Circulation / Fixing SOC / Verify Capacity (values 0-6) | Bind list `bindDebugModeType` `MiniESapp.cs:2101-2157`; any selection writes one register at `0x8000` via `SendSysCtrl` and immediately rereads `0x8100` (`MiniESapp.cs:2159-2170`; readback `DebugModeRead` `SysControl.cs:372-396`). Mode names `GlobalFun.cs:152-165`. Firmware behavior of every non-zero value is Unknown (PROTOCOL_EVIDENCE §8). | **(c) — excluded from product exposure.** Read-only visibility of the word is recommended (Part 3, R1); the write never surfaces. |

### 2.4 Settings page — device configuration writes

| Control | Wire effect | Class |
|---|---|---|
| Parameter Set button: RS485 #1/#2 baud + station address | `metroBtnParamSet_Click` `MiniESapp.cs:2277-2402` → `SetRs485Param` writes the pair at `0x8002` (`SysControl.cs:1478-1493`). Station addresses validated 1-247 (`GlobalFun.cs:280-297`). | (b) commissioning |
| Same button: MAC address (6 hex fields) | → `SetNetParam` writes MAC + both server records at `0x8008` (`SysControl.cs:1495-1510`; collection `MiniESapp.cs:2310-2365`). | (b) commissioning |
| Same button: server 1/2 IP + port | Same `0x8008` write (`MiniESapp.cs:2324-2365`). | (b) commissioning |
| Same button: serial number (21-char `BEP000[3-5]K…` pattern) | → `SetSerialNum` writes ASCII at `0x8018` (`SysControl.cs:1512-1527`; pattern `GlobalFun.cs:356-373`). Rewrites device identity. | (b)/(c) — service-only; never a console capability |
| Parameter-set enable toggle | Writes 1/0 at `0x8036` to unlock/lock the grid-standard group (`metroToggleSetting_CheckedChanged` `MiniESapp.cs:2524-2574`). | (b)/(c) service interlock |
| Region combo (Default / United Kingdom / Australia / New Zealand; list `MiniESapp.cs:96`) | Chooses the grid-standard list; UK writes code 1 directly (`metroCmbRegion_SelectionChangeCommitted` `MiniESapp.cs:2467-2485`). | (b) commissioning |
| Grid standard combo (G98/G99, AS4777 AU state variants, AS4777 NZ; lists `bindGridStandard` `MiniESapp.cs:2015-2082`) | Writes the selected code at `0x8034` (`SetGridStandardCode` `MiniESapp.cs:2487-2500`). | (b) commissioning — grid-code compliance region is a regulatory property of the installation, not a daily operator choice |
| DRED enable toggle (visible only for AU/NZ regions, `MiniESapp.cs:1605-1612`) | Writes 1/0 at `0x8035` (`metroToggleDredEnable_Click` `MiniESapp.cs:2508-2522`). DRED = demand-response equipment interface. | (b)/(c) — utility-program function; excluded until separately evidenced |

### 2.5 Maintenance clears — standing exclusions

| Control | Wire effect | Class |
|---|---|---|
| Clear historical energy (menu item and Settings button, OK/Cancel confirm "The historical energy record will be reset to 0!") | Writes `0xFF00` (65280) at `0x8001` (`MiniESapp.cs:2262-2275` menu, `2444-2459` button), then rereads the energy block. | **(c) — evidenced and excluded** (PROTOCOL_EVIDENCE §5; CONTINUITY 2026-08-21) |
| Clear battery low-voltage protection (confirm warns the pack is under 2.0 V/cell and the device refuses startup; clearing permits startup) | Writes `0xFF00` at `0x8037` (`metroBtnClearBatteryLowVoltageProtection_Click` `MiniESapp.cs:2576-2590`). | **(c) — hardest exclusion**: this defeats a cell-undervoltage lockout. Never in any product surface. |

### 2.6 Connection (application side, not device writes)

| Control | Effect | Class |
|---|---|---|
| Connect dialog: device id (default 4), connection type (RS485 / Ethernet), serial port/baud/parity/word/stop/timeout, or Ethernet TCP port 507 reverse listener | `CommCfgForm.cs:137-140` (open), `510-699` (labels/defaults); poll loops start per selected view (`MiniESapp.cs:1257-1264`). | n/a — our analog (gateway endpoints, unit identity pinning) correctly lives in controller configuration, not the console. |
| Open / Close device menu | `MiniESapp.cs:1086-1089, 1732-1736`. | n/a |

### 2.7 Read-only operator visibility worth noting

| Display | Source | Class |
|---|---|---|
| Home energy-flow numbers (PV / battery / grid / load power) and a six-bar historical energy chart (PV total, buy, sell, charged, discharged, load) | `MiniESapp.cs:1359-1376`; energy from `0x4101` (`EnergyInfoReadIot` `SysControl.cs:768-797`). | (a) — good operator surface; see R8 |
| System status / work mode / control mode / network status words | `0x0100` block (`SysInfoRead` `SysControl.cs:398-458`); mode strings: work mode 2 Economy / 6 Remote dispatch / 8 Timing (`GlobalFun.cs:167-176`), control mode 1 Remote / 2 Local (`GlobalFun.cs:204-212`), system status (`GlobalFun.cs:28-40`); rendered `MiniESapp.cs:1375-1378`. | (a) for display — see R1. The app never writes these words. |
| PCS detail: inverter run mode (Matching Load / Remote PQ Power / Remote PF Power / Remote PF Current / Remote DC voltage, `GlobalFun.cs:178-188`), grid V/I/f, P/Q/S, charge/discharge/apparent/reactive limits, objectives readback at `0x1060+17/+18`, temperatures, relay/fan/LED status | `PcsInfoReadIot` `SysControl.cs:472-567`; rendered `MiniESapp.cs:1379-1410, 1384-1408`. | (a) — telemetry parity item; our unit detail covers most; dynamic limits and objectives are the safety-relevant parts |
| DCDC detail (branch powers, limits, temperatures) | `DcdcInfoReadIot` `SysControl.cs:604-677`. | (a) telemetry parity (nice-to-have) |
| BMS/BECU detail: SOC, SOH, limits, energies, cell extrema, full per-cell voltage/temperature tables, BECU selector buttons 1/2/3 | `BmsInfoReadIot` `SysControl.cs:714-766`; `CellInfoReadIot` `833-928`; selector `MiniESapp.cs:1814-1827`; labels `1432-1483`. | (a) — we have this (Cells tab) |
| Fault/warning recording: current vs history lists, happen/disappear lifecycle, clear-history (local list only, no device write) | `UpdateFaultWarninigList` `MiniESapp.cs:1615-1647`; toggle `1878-1894`; local clear `1896-1901`. | (a) — Activity view + audit covers this with real provenance |
| Force-charge mode byte | Read at `0x0100+7` (`SysControl.cs:414`) but never displayed or written by the app. | Unknown purpose; no action |

### 2.8 What the vendor app does NOT have (findings worth knowing)

1. **No scheduling, tariffs, time-of-use, or work-mode selection anywhere.** The
   work-mode word (Economy / Remote dispatch / Timing) is display-only; there is no
   editor for it and no schedule UI. The operator's scheduling experience came
   entirely from their own prior integrations (`modbus/byd/schedule.json` windows
   like "Night Charge 00:01-05:59 @2500 W", edited in the Streamlit dashboard
   `byd/dashboard.py:873-902`; manual per-battery charge/discharge with power
   sliders and durations in `byd/ui/manual_control.py`).
2. **No fleet concept** — one device per connection; our three-unit fleet had to be
   driven one app instance at a time.
3. **No audit trail** — only a local fault/warning log file; no record of who
   commanded what power when.
4. **No TTL, no bounds, no safety layer** on the power path: a signed P/Q value is
   transmitted verbatim, renewed forever until manually stopped.
5. **Shared hardcoded passwords** (`LoginForm.cs:41-57`): admin and user accounts
   share `101028`; the installer password `898888` sits in the same binary.
6. **A dead control shipped** (Plot Enable, §2.2) and mixed Chinese/English
   validation messages (`GlobalFun.cs:289, 308, 327, 346, 365`) — signs of
   tool-grade software, not product-grade.

---

## Part 3 — Gap analysis and recommendations

Priorities: **P1** = closes a live operator pain or unlocks daily-use value on the
existing architecture; **P2** = clear value, moderate surface; **P3** = polish or
dependency-gated. All recommendations obey the architecture rule: **intents with
TTLs through the arbiter/kernel, never raw register writes; policy keys for bounds;
advisory layer for smart features.**

### R1 (P1) — Mode-word visibility: "is this pod actually listening?"

- **What:** decode and display, per unit, the device debug-mode word (`0x8100`,
  value names from `GlobalFun.cs:152-165`) and system control mode (`0x0100+1`:
  Remote/Local, `GlobalFun.cs:204-212`), read-only; show a warning chip on Home/Now
  when any unit is not `Normal`/`Remote`, and (per fix-queue item iv) refuse intents
  with an explicit reason code when the mode is wrong.
- **Why the operator benefits:** the 2026-08-23 "silent actuation loss" incident
  had exactly this signature — authorized watts that never physically happened
  because a pod can silently ignore `0x0200` objectives when latched in a
  non-Normal/non-Remote state (CONTINUITY, "SILENT ACTUATION LOSS"). The vendor app
  checks this before every send (`MiniESapp.cs:2179-2184`); our console currently
  cannot even show it.
- **Architecture mapping:** pure read path — the blocks are already in the read plan
  (`register_layout.py:85-91`); add decode fields (advisory/status class, outside
  the safety-critical completeness set), expose in snapshot/unit-detail, consume in
  the existing kernel refusal vocabulary as a new reason code. No new writes.
- **Surface:** decode fields + 2 facade projections + unit card/badge + 1 reason
  code. **Dependencies:** CONTINUITY fix-queue item (iv), sequenced after the
  in-flight excess-charging implementation.

### R2 (P1) — "Why is my battery not at the commanded rate?" — explanation layer

- **What:** per-unit decision attribution on the audit/event stream (fix-queue item
  ii) and a plain-language explanation strip in Now/Home: ramp limiter climbing
  ("power is ramping up to your request, ~2-3 cycles"), SOC ceiling/floor refusal,
  BMS dynamic limit, export bound, non-participation (zero-watt setpoint),
  starvation (see R4).
- **Why:** the operator repeatedly sees Allowed < Requested or Actual ≠ Allowed and
  today must read raw reason codes in Activity. Every refusal already carries a
  machine-readable reason code; the gap is per-unit attribution and translation.
- **Architecture mapping:** no new authority; the kernel already produces reason
  codes and requested/authorized/measured per decision (API_CONTRACTS "Safety
  kernel"). UI-side mapping table code → plain sentence, with the raw code on
  demand (UI_CONTRACTS standard).
- **Surface:** kernel audit/event payload extension + console explainer. **Dep:**
  fix-queue items (i)/(ii) (suppressed-write audit, per-unit attribution).

### R3 (P1) — One-tap dispatch presets ("charge mid at 500 W for 1 h")

- **What:** per-unit quick actions with remembered presets (unit, direction,
  watts, duration), one tap to review-and-confirm. Long durations are served by a
  console-driven **holding renewal**: the console re-submits short intents (≤300 s
  each, same idempotency discipline) with a visible countdown and one-tap cancel —
  the vendor's "Keep Send" made safe.
- **Why:** today every action is a multi-field dialog; the live sessions show the
  operator wants "charge this pod a bit" as one thought. A "1 h" hold also answers
  the real question "will this finish charging before the sun goes".
- **Architecture mapping:** identical to any manual intent — `POST /api/v1/intents`
  with short TTLs, renewal driven by the console (or later an `AGENT`-source
  helper). No TTL cap change, no persistent override (roadmap principle 4). Expiry
  or closed tab ends the hold; the firmware watchdog (~3.5-4.0 s) plus pod autonomy
  is the fail-safe.
- **Surface:** UI-only for v1 (preset store is local); optional tiny API for saved
  presets later. **Dep:** none hard; starvation preview (R4) should land with it.

### R4 (P1) — Fleet vs per-unit dispatch UX: no more silent starvation

- **What:** before confirming a multi-unit intent, show the projected per-unit
  split; while the greedy allocator remains, warn when a selected unit would
  receive 0 W ("rhs will get nothing; lhs takes the full request — dispatch per
  unit or pick one"). After the allocator fix, the same preview becomes accurate.
- **Why:** the greedy first-unit-takes-all split (`allocation.py:93-124`, sorted
  ids, `min(remaining, capacity)`) silently halved an operator's two-unit discharge
  on 2026-08-23. The console currently presents watts as a fleet total with no
  split anywhere.
- **Architecture mapping:** UI-side projection for now (mirror of the allocator's
  deterministic arithmetic over snapshot headroom); the durable fix is fix-queue
  item (v) — capacity-aware split or per-unit targets plus an `allocated_zero`
  reason code surfaced through R2.
- **Surface:** console preview + (later) allocator change with its reason code.
  **Dep:** fix-queue item (v); R2 shares the attribution plumbing.

### R5 (P1) — Charge/discharge schedules (Phase 3 made real)

- **What:** expose the existing schedule domain — REST `GET/PUT` for the versioned
   plan (compare-and-swap on version, as `ScheduleVersionConflict` already models)
   plus a Schedule view: day/week timeline editor, next-action display on Home,
   pause/skip, plan history in Activity.
- **Why:** the operator's prior stack ran exactly this (`byd/schedule.json` "Night
  Charge 00:01-05:59 @ 2500 W"); it is the highest-value automation a
  non-technical operator understands: "charge cheap overnight, use it in the
  evening".
- **Architecture mapping:** the whole chain is contracted and half-built:
  validated immutable entries (`schedule.py:37-165`), evaluator emitting
  short-lived intents through the existing arbiter priority
  (`scheduling.py:34-85`; priority schedule < manual < agent, API_CONTRACTS),
  `ScheduleRepository` port. Evaluated windows are intents with TTLs — never
  hardware commands. Editing is separate from activation; versions are auditable.
- **Surface:** 2 REST endpoints + facade methods + one view. **Dep:** arbiter and
  domain ready; sequencing after in-flight work per CONTINUITY.

### R6 (P1) — Grid/CT per-phase import-export display

- **What:** per-unit grid power (import/export, signed per the live-proven
  convention: negative = import, PROTOCOL_EVIDENCE §4c) and load power, plus a
  fleet net-export figure; quality and age shown, never zero-filled. Natural home:
  Home ("what is powering the home") and Batteries detail.
- **Why:** the operator's mental model of the system is per-phase solar surplus;
  this is also the evidence the excess-solar feature acts on, so showing it makes
  the feature legible ("mid is exporting 1.4 kW — that is what charged rhs").
- **Architecture mapping:** advisory readthrough only — `grid_power_w`/`load_power_w`
  are already outside the safety-critical completeness set by contract; the decoder
  landed with the in-flight work (`decode.py:297-312`); facade snapshot/unit-detail
  readthrough is specced (API_CONTRACTS "Observation fields and readthrough").
- **Surface:** facade projections (in flight) + Home/Batteries tiles. **Dep:**
  excess-solar implementation stream (read-tier promotion lands with it).

### R7 (P2) — Adjustable safety bounds as guarded policy view, then guarded changes

- **What:** staged. (i) Read-only Settings > Safety: show the active policy
  version and per-unit bounds (SOC floor/ceiling, per-unit charge/discharge caps,
  fleet caps, ramp limit, authorization lifetime) in plain language. (ii) Later, a
  guarded change flow for per-unit SOC floor/ceiling and power caps: `arm` scope +
  interactive principal + typed confirmation + audit + policy version bump, with
  changes clamped inside commissioned envelopes (never above hardware/commissioned
  maxima; validated cross-field as config is today).
- **Why:** "keep mid above 20% because it backs up the fridge" is a household
  decision, not an engineering one. Today the only way to change it is editing the
  controller config file and restarting.
- **Architecture mapping:** bounds are enforced by OUR kernel (policy keys), not
  device registers — a policy change alters what the kernel will authorize, never
  what the wire can carry. Versioned immutable policy + audited replacement
  preserves the audit-reconstruction guarantee. The 2026-08-23 live sessions
  showed the operator hitting `soc_above_charge_ceiling` refusals without any way
  to see why the ceiling is where it is.
- **Surface:** (i) is a read endpoint + a Settings panel; (ii) adds one guarded
  mutation endpoint. **Dep:** none for (i); (ii) after R2 (explanations) so
  refusals and settings agree.

### R8 (P2) — Energy totals view ("today's scorecard")

- **What:** decode the cumulative energy block (`0x4101`: grid buy, grid sell,
  load consumption, PV production, BMS charge, BMS discharge — `SysControl.cs:768-797`,
  low-word-first ×0.1 per §6) and show a daily/total card in the vendor home
  chart's spirit: bought, sold, charged, discharged, solar, load.
- **Why:** money legibility. The operator's prior dashboard led with exactly these
  numbers; "sell to grid" is why the excess-solar feature exists.
- **Architecture mapping:** advisory readthrough; counters are already in the read
  plan but not decoded into `Observation`. The clear-energy maintenance write
  (0x8001) stays excluded (Part 4) — totals reset only by device service, never by
  the console. Note the decode contract must pin the ambiguous energy role labels
  first (decode.py header; PROTOCOL_EVIDENCE field-mapping S5).
- **Surface:** decode fields + snapshot projection + one Home/Insights tile with
  daily deltas computed server-side. **Dep:** evidence pinning of role labels;
  rides the cold-ring tier (no cadence impact).

### R9 (P2) — Excess-solar auto-charging: state surface, then a guarded toggle

- **What:** show the adviser's live state — enabled/disabled, eligible/active/
  yielded, target unit, current bound and rate, attribution
  `energypod:excess-adviser` + `optimizer` in Activity. Then (only after the
  operator confirms net-across-phases billing, currently PENDING in the design) a
  guarded runtime toggle with a clear on/off state and audit, so the operator does
  not edit YAML to use their own feature.
- **Why:** "the battery grabbed the spare solar by itself" must be visible and
  switchable, or trust erodes; the design's authority posture (advisory-only,
  operator precedence, hand-back by non-renewal) is exactly what the toggle
  controls.
- **Architecture mapping:** the adviser is already an advisory component with no
  special authority (API_CONTRACTS "Excess-solar accelerated charging";
  `excess_charge.py` module contract). A runtime toggle changes configuration
  state, not policy semantics: OFF ⇒ adviser composes out (existing default); ON
  requires the existing config gates. Toggle = audited facade mutation with `arm` +
  interactive, mirroring inhibit-acknowledgement's shape.
- **Surface:** state projection (read) + one guarded endpoint + a Settings switch
  with a plain on/off state. **Dep:** in-flight implementation (bound/hysteresis/
  renewal commits already landing); operator net-billing confirmation.

### R10 (P3) — Console polish from the deferred queues

- Surface `process_instance_id`/`uptime_s` from `/api/v1/health` (instance swap vs
  stall — the f20602c follow-up); reconnect banner (queued); MCP `get_unit_detail`
  tool (CONTINUITY queue); server-side audit filters (unit/kind) once the store
  cursor contract extends; fault/warning current-vs-history presentation parity
  (Activity covers the content; the vendor's two-tab current/history split is a
  nice readability pattern).

### What we deliberately do BETTER — keep, and do not trade away

Compared with the vendor app (and the prior Streamlit stack), the following are
product advantages that no gap-closing work should erode:

1. **Fail-closed safety kernel** between every intent and the wire: stale/unknown/
   contradictory safety data denies non-zero power; zero is always permitted.
   The vendor transmits any signed value an operator types.
2. **Everything expires.** Intents have TTLs; authorizations are short-lived,
   single-use, generation-fenced; the firmware watchdog is the final backstop.
   The vendor's keep-send runs until a human stops it.
3. **One writer, arbitrated:** emergency stop > manual > agent > optimizer >
   schedule; live-verified that a console manual intent supersedes an in-flight
   optimizer intent. The vendor app has no arbitration and no stop precedence.
4. **Arm is a deliberate, interactive, audited step** with an external-writer
   preflight (a foreign objective latches INHIBITED instead of fighting). The
   vendor's only gate is a debug-mode check.
5. **Authorization transparency:** requested vs allowed vs actual are always three
   separate facts; every refusal carries a machine-readable reason rendered
   verbatim; no clamped action is ever shown as delivered.
6. **Complete, durable audit trail** with principal attribution and correlation
   ids — the vendor logs only fault transitions to a local file.
7. **Scoped, rotatable credentials**; no shared hardcoded passwords; browser
   sessions use single-use tickets, never query-string tokens.
8. **No maintenance surface at all**: the product is structurally incapable of the
   writes in Part 4 (the transport write gate permits only `0x0200` PQ;
   `register_layout.py:93-94`, API_CONTRACTS "Write-enabled run mode").
9. **Fleet-native** identity-pinned units, per-unit topology commissioning, and
   honest not-yet placeholders instead of dead controls.

---

## Part 4 — What must never be exposed (standing exclusion list)

These are excluded by standing safety policy (PRODUCT_ROADMAP "Delivery model":
debug, calibration, fixing-SOC, circulation, capacity-verification, passive-voltage
and other maintenance functions are excluded from the normal product control path;
PROTOCOL_EVIDENCE §5/§8/§12; CONTINUITY non-negotiable invariants). They must not
appear in the console, REST, or MCP — not gated, not role-hidden, not
"admin-only".

| Excluded capability | Wire write | Evidence |
|---|---|---|
| Debug/service mode selection — Standby, Charge, Discharge, Circulation, Fixing SOC, Verify Capacity (any non-zero value) | `0x8000` (values 0-6) | PROTOCOL_EVIDENCE §8; write site `MiniESapp.cs:2159-2170`; firmware semantics Unknown for all non-zero values |
| Clear historical energy counters | `0x8001` + `0xFF00` | PROTOCOL_EVIDENCE §5 writes table; write sites `MiniESapp.cs:2262-2275, 2444-2459`; CONTINUITY 2026-08-21 (evidenced and kept absent from production interfaces) |
| Clear battery low-voltage protection (defeats a <2.0 V/cell startup lockout) | `0x8037` + `0xFF00` | PROTOCOL_EVIDENCE §5; write site `MiniESapp.cs:2576-2590` |
| Device identity/network/grid-standard configuration: RS485 params, MAC, server IPs/ports, serial number, grid standard, region, DRED enable, parameter-set enable | `0x8002`, `0x8008`, `0x8018`, `0x8034`, `0x8035`, `0x8036` | PROTOCOL_EVIDENCE §5; write sites `SysControl.cs:1478-1527`, `MiniESapp.cs:2277-2402, 2487-2574`. Commissioning/service procedures at most, separately evidenced — never console surface |
| Raw register editors / generic write primitives of any kind (the vendor's `SendSysCtrl(startAddr, data)` shape, `SysControl.cs:1463-1476`) | any address | API_CONTRACTS "Write-enabled run mode": the only writable registers are `[1, signed P, signed Q]` at `0x0200`, enforced by the transport write gate (`register_layout.py:93-94`) |
| Prior-attempt pseudo-contracts: `0x0201` active-only writes, `EssForceState` values at 512, `0x1001` "function selected" writes, 40120-40124 "passive-voltage profile" | see table | PROTOCOL_EVIDENCE §12 — rejected claims that must not become contracts |
| Reactive-power (Q) control as an operator capability | `0x0200` word 3 | PROTOCOL_EVIDENCE §13 item 6 (reactive limits/feasibility unresolved); policy reactive limit is zero by default |

Two boundary notes. First, mode-word **read** (`0x8100`, `0x0100+1/+2`) is not only
allowed but recommended (R1) — the exclusion is on the write and on treating
non-Normal modes as product features. Second, if a maintenance function is ever
genuinely needed (for example a vendor-sanctioned capacity verification), the
roadmap's bar applies: an isolated, locally enabled service procedure after vendor
behaviour, termination conditions, and hazards are independently established — a
separate tool and a separate authorization, never a console menu item.

---

## Summary table (recommendations at a glance)

| # | Capability | Priority | New authority? | Surfaces |
|---|---|---|---|---|
| R1 | Mode-word visibility + intent refusal when not Normal/Remote | P1 | no (read + reason code) | decode, snapshot/unit-detail, badge, kernel reason |
| R2 | Per-unit decision attribution + plain-language explanations | P1 | no | audit/event payload, Now/Home explainer |
| R3 | One-tap dispatch presets + visible holds (renewed short intents) | P1 | no (ordinary intents) | console UI |
| R4 | Starvation-aware fleet dispatch preview (then allocator fix) | P1 | no (allocator change is deterministic math + reason code) | dispatch preview, allocator |
| R5 | Charge/discharge schedules (domain exists; REST + Schedule view) | P1 | no (SCHEDULE intents through arbiter) | 2 endpoints, facade, one view |
| R6 | Per-phase grid import/export + load display | P1 | no (advisory readthrough) | facade projections (in flight), Home/Batteries |
| R7 | Safety-bounds view, then guarded policy changes | P2 | policy keys only (kernel-enforced) | read endpoint + Settings; later one guarded mutation |
| R8 | Energy totals view (buy/sell/PV/charge/discharge/load) | P2 | no (advisory decode) | decode, projection, Home tile |
| R9 | Excess-solar state surface + guarded toggle | P2 | no (adviser config state) | read projection + one guarded endpoint |
| R10 | Instance identity, MCP unit detail, audit filters, history parity | P3 | no | various |
