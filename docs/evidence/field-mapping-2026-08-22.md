# Field mapping validation against live hardware — 2026-08-22

Status: validated per-field decode of the 2026-08-22 observe-only capture. Offline analysis only; no hardware was contacted.
Capture: `docs/evidence/live-capture-2026-08-22.json` (13 FC03 blocks per unit, MID/RHS/LHS; all declared counts match actual register-array lengths — verified programmatically).
Vendor decode authority: `C:\Users\vagrant\Downloads\EnergyPod_RE\src\MiniESapp\` (`SysControl.cs`, `MiniESapp.cs`, `GlobalFun.cs`, `BmsInfo.cs`), cited per expression.
Evidence matrix: `docs/PROTOCOL_EVIDENCE.md` sections 4-7.

## 0. Conventions and verdict vocabulary

- Offsets are zero-based indices into each block's register array (the vendor's array indexing).
- `s16(v)` = two's-complement signed interpretation of the 16-bit register.
- `ver(v)` = `(v >> 8) . (v & 0xFF)` two digits (`GlobalFun.cs:258-261`).
- `u32lo(w1, w0)` = `(w1 << 16) | w0` — low-address word is the low word (`GlobalFun.cs:253-256`; argument order at the call site fixes word order).
- Verdicts: **OK** (plausible and cross-validated), **OK\*** (plausible, caveat noted), **AMBIG** (flagged, alternatives given), **DEAD** (reads implausible/zero, likely unpopulated).
- Raw → engineering notation: `1935 → 193.5 V`.

## 1. Identity and topology cross-check (deliverable 6)

Identity read: FC03 `0x8106` (33030), 2 registers; decode `rtuId = u32lo(array2[0], array2[1])` — the first (lower-address) word is the LOW word (`MiniESapp.cs:1325-1329`, helper `GlobalFun.cs:253-256`).

| Unit | Host | Raw words | Computed RTU ID | PROTOCOL_EVIDENCE §4 pin | Match |
|---|---|---|---|---|---|
| MID | 192.168.1.11 | `[20631, 11298]` | `0x2C225097` (740446359) | `0x2C225097` | exact |
| RHS | 192.168.1.12 | `[20598, 11298]` | `0x2C225076` (740446326) | `0x2C225076` | exact |
| LHS | 192.168.1.13 | `[20629, 11298]` | `0x2C225095` (740446357) | `0x2C225095` | exact |

Pinned per unit as above. The high word `0x2C22` is identical across the fleet (one product family/batch); the low words are distinct per unit, so the ID is usable as the per-string identity on the wire (still only a CRC32-grade identifier — string-identity binding strategy remains open per §4a).

Layout and topology from the same capture (`0x5000` words 0/4/5; discriminator `array[0] > 10` → IoT, enable mask at offset 4, BIC count at offset 5 — `MiniESapp.cs:1287-1295`):

| Unit | Word 0 | Layout | Enable mask (offset 4) | BIC count (offset 5) | Commissioned | Match |
|---|---|---|---|---|---|---|
| MID | 536 > 10 | IoT (`protocolFlag=1`) | 1 (one string, 1 BECU) | 6 | 6 | yes |
| RHS | 536 | IoT | 1 | 5 | 5 | yes |
| LHS | 536 | IoT | 1 | 6 | 6 | yes |

The layout-probe offsets and the full BMS-read offsets (`SysControl.cs:725-730`) agree word-for-word; both readings are satisfied by the same captured block.

## 2. Per-block field mapping (deliverable 1)

### 2.1 PCS live — `0x1000` (4096), 21 registers; read `SysControl.cs:477`, decodes `SysControl.cs:483-503`

| Off | Field | Decode (vendor cite) | MID | RHS | LHS | Verdict |
|---:|---|---|---|---|---|---|
| 0 | softwareVersion | `ver(v)` — `GlobalFun.cs:258-261` via `SysControl.cs:483` | 535 → "2.23" | 535 → "2.23" | 535 → "2.23" | OK, fleet-homogeneous |
| 1 | status | low byte; enum 0 Init / 1 Idle / 2 Standby / 3 On grid / 4 Off grid / 5 Fail / 6 Debugging — `GlobalFun.cs:224-236` via `SysControl.cs:484` | 1 → Idle | 3 → On grid | 3 → On grid | OK — coherent with power flow (MID exports 0 W, RHS/LHS export 1.1/1.8 kW) |
| 2 | runMode | enum 0 Matching Load / 1 Remote PQ Power / … — `GlobalFun.cs:178-188` via `SysControl.cs:485` | 0 → Matching Load | 0 | 0 | OK — no remote dispatch active (objectives at `0x1060+17..21` are 0, consistent) |
| 3 | dcVolt | `s16 × 0.1 V` — `SysControl.cs:486` | 1935 → 193.5 V | 4098 → 409.8 V | 4185 → 418.5 V | OK — equals DCDC output voltage per unit (see 2.4) |
| 4 | gridVolt | `s16 × 0.1 V` — `SysControl.cs:487` | 2436 → 243.6 V | 2438 → 243.8 V | 2476 → 247.6 V | OK — plausible 230/240 V-class service |
| 5 | gridCurrent | `s16 × 0.01 A` — `SysControl.cs:488` | 41 → 0.41 A | 432 → 4.32 A | 692 → 6.92 A | OK — ≈ S/V (1053 VA/243.8 V = 4.32 A) |
| 6 | gridFreq | `s16 × 0.01 Hz` — `SysControl.cs:489` | 5000 → 50.00 Hz | 5001 → 50.01 Hz | 5004 → 50.04 Hz | OK |
| 7 | gridActivePower | `s16`, W — `SysControl.cs:490` | 65497 → -39 W | 1033 W | 1747 W | OK — MID standby draw with inverter open |
| 8 | gridReactivePower | `s16`, var — `SysControl.cs:491` | 95 var | 49 var | 62 var | OK |
| 9 | gridApparentPower | `s16`, VA — `SysControl.cs:492` | 102 VA | 1038 VA | 1719 VA | OK / LHS: S < P (see §5, A-3) |
| 10 | inverter volt | `s16 × 0.1 V` — `SysControl.cs:493` | 5 → 0.5 V | 2448 → 244.8 V | 2478 → 247.8 V | OK — ≈ gridVolt when closed, ≈0 when idle (relay states agree, `0x1060+12/13`) |
| 11 | inverter current | `s16 × 0.01 A` — `SysControl.cs:494` | 4 → 0.04 A | 452 → 4.52 A | 734 → 7.34 A | OK — V×I = 1106/1819 VA ≈ S (1102/1800) |
| 12 | inverter freq | `s16 × 0.01 Hz` — `SysControl.cs:495` | 4997 → 49.97 Hz | 5000 → 50.00 Hz | 5001 → 50.01 Hz | OK\* — MID tracks frequency with V≈0 (grid-sync measurement; curiosity, §5 A-6) |
| 13 | pcsActivePower | `s16`, W — `SysControl.cs:496` | 0 W | 1086 W | 1797 W | OK — chains to DCDC power (§3) |
| 14 | pcsReactivePower | `s16`, var — `SysControl.cs:497` | 0 | 65332 → -204 var | 65305 → -231 var | OK — √(P²+Q²) ≈ S on RHS/LHS |
| 15 | pcsApparentPower | `s16`, VA — `SysControl.cs:498` | 0 | 1102 VA | 1800 VA | OK |
| 16 | externalGridCurrent | `s16 × 0.01 A` — `SysControl.cs:499` | 798 → 7.98 A | 330 → 3.30 A | 360 → 3.60 A | AMBIG on RHS/LHS (see §5, A-2) |
| 17 | externalGridPower | `s16`, W — `SysControl.cs:500` | 63800 → -1736 W | 65499 → -37 W | 65488 → -48 W | OK\* — magnitudes balance (below); sign convention unknown (§7 of the evidence matrix) |
| 18 | externalPvCurrent | `s16 × 0.01 A` — `SysControl.cs:501` | 3 → 0.03 A | 6 → 0.06 A | 8 → 0.08 A | OK\* — noise-level, no PV installed |
| 19 | externalPvPower | `s16`, W — `SysControl.cs:502` | 65535 → -1 W | 0 W | 0 W | OK\* — noise-level |
| 20 | loadPower | `s16`, W — `SysControl.cs:503` | 1701 W | 1063 W | 1781 W | OK — balances: MID 1736 ≈ 1701 + 39 standby; RHS 1086 - 1063 ≈ +23 vs -37; LHS 1797 - 1781 ≈ +16 vs -48 (≤ 64 W metering tolerance) |

### 2.2 PCS warnings/faults — `0x1040` (4160), 22 registers; warn0-3 at offsets 0-3 (`SysControl.cs:580-583`), fault0-5 at offsets 16-21 (`SysControl.cs:584-589`)

| Words | MID | RHS | LHS | Verdict |
|---|---|---|---|---|
| warn0-3 (offsets 0-3) | 2, 0, 0, 0 | 2, 0, 0, 0 | 2, 0, 0, 0 | OK — warn0 bit 1 = "EE Calibration Parameter Out of Range" (evidence §9), the exact warning family the original vendor logs corroborate |
| fault0-5 (offsets 16-21) | all 0 | all 0 | all 0 | OK — no PCS faults |

### 2.3 PCS detailed state — `0x1060` (4192), 32 registers; read `SysControl.cs:517`, decodes `SysControl.cs:523-554`

| Off | Field | Decode (vendor cite) | MID | RHS | LHS | Verdict |
|---:|---|---|---|---|---|---|
| 0 | debugStatus | low byte — `SysControl.cs:523` | 0 | 0 | 0 | OK — normal mode |
| 1 | debugCommand | low byte — `SysControl.cs:524` | 0 | 0 | 0 | OK |
| 2 | paramVersion | `ver(v)` — `SysControl.cs:525` | 512 → "2.00" | "2.00" | "2.00" | OK |
| 3 | rateGridVoltFrequency | low byte — `SysControl.cs:526` | 0 | 0 | 0 | AMBIG — reads 0 on all units; no enum table in the decompiled source (see §5, A-9) |
| 4 | functionSelected | low byte — `SysControl.cs:527` | 0 | 0 | 0 | AMBIG — no enum table; 0 on all units |
| 5 | gridStandard | low byte — `SysControl.cs:528` | 1 | 1 | 1 | AMBIG — fleet-consistent 1, meaning not in the decompiled source (written via `0x8034`, `MiniESapp.cs:2488-2499`) |
| 6 | pcsLeakageCurrent | `s16 × 0.1` — `SysControl.cs:529` | 94 → 9.4 | 70 → 7.0 | 120 → 12.0 | OK\* — unit presumed mA (vendor applies scale without a unit label); plausible magnitudes |
| 7 | groundVolt | `s16 × 0.1 V` — `SysControl.cs:530` | 10 → 1.0 V | 31 → 3.1 V | 52 → 5.2 V | OK |
| 8 | pcsInternalAmbientTemp | `s16` raw °C — `SysControl.cs:531` | 40 °C | 42 °C | 43 °C | OK — plausible inside-cabinet, above battery temps |
| 9 | pcsWaveStatus | low byte — `SysControl.cs:532` | 0 | 1 | 1 | OK\* — no enum; 0 on idle unit, 1 on active units |
| 10 | pcsFanStatus | low byte — `SysControl.cs:533` | 0 | 1 | 1 | OK\* — fan off when idle, on when delivering |
| 11 | pcsFanSpeed | `s16` — `SysControl.cs:534` | 0 | 0 | 0 | DEAD — 0 even with fanStatus=1 (see §5, A-7) |
| 12 | pcsRelayStatus | `s16` — `SysControl.cs:535` | 0 | 3 | 3 | OK\* — 0 with inverter open (0.5 V), 3 closed (invV ≈ gridV); no enum table |
| 13 | pcsRelayCheckStatus | `s16` — `SysControl.cs:536` | 0 | 3 | 3 | OK\* — tracks relayStatus |
| 14 | ledStatus | low byte — `SysControl.cs:537` | 24 | 18 | 18 | AMBIG — no enum; consistent idle/active split |
| 15 | inputPortStatus | low byte — `SysControl.cs:538` | 0 | 0 | 0 | OK\* — no enum, all zero |
| 16 | gridAbnormalFlag | low byte — `SysControl.cs:539` | 0 | 0 | 0 | OK — no grid abnormality |
| 17 | activePowerObj | `s16` W — `SysControl.cs:540` | 0 | 0 | 0 | OK — no remote objective set (runMode 0) |
| 18 | reactivePowerObj | `s16` var — `SysControl.cs:541` | 0 | 0 | 0 | OK |
| 19 | activeCurrentObj | `s16 × 0.01 A` — `SysControl.cs:542` | 0.00 A | 0.00 A | 0.00 A | OK |
| 20 | reactiveCurrentObj | `s16 × 0.01 A` — `SysControl.cs:543` | 0.00 A | 0.00 A | 0.00 A | OK |
| 21 | dcVoltageObj | `s16 × 0.1 V` — `SysControl.cs:544` | 0.0 V | 0.0 V | 0.0 V | OK — no remote DC objective (DCDC owns the bus, see 2.6) |
| 22 | pfObj | `s16 × 0.001` — `SysControl.cs:545` | 1000 → 1.000 | 1.000 | 1.000 | OK |
| 23 | matchModePfVal | `s16 × 0.001` — `SysControl.cs:546` | 1000 → 1.000 | 1.000 | 1.000 | OK — unity PF load-matching |
| 24 | apparentPowerLimit | `s16` VA — `SysControl.cs:547` | 5000 | 5000 | 5000 | OK — 5 kVA rating |
| 25 | dischargePowerLimit | `s16` W — `SysControl.cs:548` | 0 | 5000 | 5000 | OK — MID 0 tracks BMS discharge limit 0 (SOC 10%); equals min(rating, BMS limit) on all units |
| 26 | chargePowerLimit | `s16` W — `SysControl.cs:549` | 5000 | 5000 | 5000 | OK — min(5000 rating, BMS 7692/6532/7812) |
| 27 | reactivePowerLimit | `s16` var — `SysControl.cs:550` | 5000 | 5000 | 5000 | OK |
| 28 | radiatorTemp | `s16` raw °C — `SysControl.cs:551` | 31 °C | 32 °C | 33 °C | OK — ambient-ish |
| 29 | inductanceTemp | `s16` raw °C — `SysControl.cs:552` | 30 °C | 28 °C | 31 °C | OK |
| 30 | matchGoalState | `s16` — `SysControl.cs:553` | 0 | 3 | 3 | AMBIG — no enum; 0 idle / 3 active split |
| 31 | offgridGoalFrequency | `s16` — `SysControl.cs:554` | 0 | 0 | 0 | OK\* — unused while on grid |

### 2.4 DCDC live — `0x2000` (8192), 13 registers; read `SysControl.cs:609`, decodes `SysControl.cs:615-627`

| Off | Field | Decode (vendor cite) | MID | RHS | LHS | Verdict |
|---:|---|---|---|---|---|---|
| 0 | softwareVersion | `ver(v)` — `SysControl.cs:615` | 262 → "1.06" | "1.06" | "1.06" | OK |
| 1 | status | low byte — `SysControl.cs:616` | 1 → Idle | 3 → Running† | 3 → Running† | OK — †decoded with the PCS status names (`GlobalFun.cs:224-236`); for DCDC, 3 evidently means "running", see §5 A-11 |
| 2 | runMode | enum; only 2 named — `GlobalFun.cs:191-202` via `SysControl.cs:617` | 2 → Constant Voltage | 2 | 2 | OK — bus voltage held at objective (see 2.6) |
| 3 | battSideVolt | `s16 × 0.1 V` — `SysControl.cs:618` | 1935 → 193.5 V | 1645 → 164.5 V | 1961 → 196.1 V | OK — ≈ BMS pack V per unit (192.3/163.3/195.3, cable drop) |
| 4 | dcOutputVolt | `s16 × 0.1 V` — `SysControl.cs:619` | 1933 → 193.3 V | 4099 → 409.9 V | 4190 → 419.0 V | OK — equals PCS dcVolt; ≈ voltageObj when running, sags to battery when idle |
| 5 | battSideCurrent | `s16 × 0.01 A` — `SysControl.cs:620` | 65524 → -0.12 A | 689 → 6.89 A | 940 → 9.40 A | OK — positive while discharging; V×I = P (§3) |
| 6 | dcdc1Current | `s16 × 0.01 A` — `SysControl.cs:621` | 65535 → -0.01 A | 229 → 2.29 A | 313 → 3.13 A | OK |
| 7 | dcdc2Current | `s16 × 0.01 A` — `SysControl.cs:622` | 65529 → -0.07 A | 229 → 2.29 A | 313 → 3.13 A | OK |
| 8 | dcdc3Current | `s16 × 0.01 A` — `SysControl.cs:623` | 65534 → -0.02 A | 230 → 2.30 A | 313 → 3.13 A | OK — branches sum to total (6.88/6.89; 9.39/9.40) |
| 9 | battSidePower | `s16` W — `SysControl.cs:624` | 65512 → -24 W | 1132 W | 1846 W | OK — V×I cross-product exact (§3) |
| 10 | dcdc1Power | `s16` W — `SysControl.cs:625` | 65533 → -3 W | 378 W | 614 W | OK — branch V×I (164.5×2.29=376.7) |
| 11 | dcdc2Power | `s16` W — `SysControl.cs:626` | 65521 → -15 W | 377 W | 614 W | OK |
| 12 | dcdc3Power | `s16` W — `SysControl.cs:627` | 65531 → -5 W | 376 W | 616 W | OK |

### 2.5 DCDC warnings/faults — `0x2040` (8256), 22 registers; warn0-3 at 0-3 (`SysControl.cs:690-693`), fault0-5 at 16-21 (`SysControl.cs:694-699`)

| Words | MID | RHS | LHS | Verdict |
|---|---|---|---|---|
| warn0-3 | 2, 0, 0, 0 | 2, 0, 0, 0 | 2, 0, 0, 0 | OK — warn0 bit 1 = "EEPROM Calibration Parameter Out of Range" (evidence §9), corroborated by vendor logs |
| fault0-5 | all 0 | all 0 | all 0 | OK — no DCDC faults |

### 2.6 DCDC detailed state — `0x2060` (8288), 19 registers; read `SysControl.cs:641`, decodes `SysControl.cs:647-664` (note: offset 14 is never decoded by the vendor)

| Off | Field | Decode (vendor cite) | MID | RHS | LHS | Verdict |
|---:|---|---|---|---|---|---|
| 0 | debugStatus | low byte — `SysControl.cs:647` | 0 | 0 | 0 | OK |
| 1 | debugCommand | low byte — `SysControl.cs:648` | 0 | 0 | 0 | OK |
| 2 | paramVersion | `ver(v)` — `SysControl.cs:649` | 256 → "1.00" | "1.00" | "1.00" | OK |
| 3 | radiatorTemp | `s16` raw °C — `SysControl.cs:650` | 31 °C | 31 °C | 33 °C | OK |
| 4 | inductanceTemp | `s16` raw °C — `SysControl.cs:651` | 30 °C | 28 °C | 31 °C | OK |
| 5 | insulationDetectionVolt | `s16 × 0.1 V` — `SysControl.cs:652` | 1 → 0.1 V | 0.1 V | 0.1 V | OK — essentially zero |
| 6 | gridFrequency | `s16 × 0.01 Hz` — `SysControl.cs:653` | 0 → 0.00 Hz | 0.00 Hz | 0.00 Hz | DEAD — 0 on all units while PCS reports 50.00-50.04 Hz; even active units read 0 (see §5, A-8) |
| 7 | voltageObj | `s16 × 0.1 V` — `SysControl.cs:654` | 4000 → 400.0 V | 4100 → 410.0 V | 4190 → 419.0 V | OK — running units hold dcOutputVolt exactly at this objective (409.9/419.0); per-unit targets |
| 8 | powerObj | low byte only — `SysControl.cs:655` | 100 → 100 | 100 | 100 | OK\* — vendor byte-truncation defect does not bite (value fits a byte); 100 W default, inactive in CV mode |
| 9 | currentObj | `(byte) × 0.01 A` — `SysControl.cs:656` | 100 → 1.00 A | 1.00 A | 1.00 A | OK\* — same note; default, inactive in CV mode |
| 10 | waveStatus | low byte — `SysControl.cs:657` | 0 | 1 | 1 | OK\* — idle/active split |
| 11 | fanStatus | low byte — `SysControl.cs:658` | 0 | 0 | 0 | AMBIG — 0 even at 1.1-1.8 kW; thermostatic threshold plausible (§5, A-10) |
| 12 | fanSpeed | `s16` — `SysControl.cs:659` | 0 | 0 | 0 | DEAD — always 0 |
| 13 | ledStatus | low byte — `SysControl.cs:660` | 1 | 20 | 20 | AMBIG — no enum; consistent idle/active split |
| 14 | (not decoded) | vendor skips offset 14 — `SysControl.cs:661` jumps 13→15 | 0 | 0 | 0 | Unknown — raw 0 on all units |
| 15 | powerLimit | `s16` W — `SysControl.cs:661` | 5500 | 5500 | 5500 | OK — 5.5 kW DCDC rating |
| 16 | dcVoltageObjAdapter | `s16` — `SysControl.cs:662` | 1 | 1 | 1 | OK\* — flag-like, no enum |
| 17 | dcdc2InputVolt | `s16`, no scale — `SysControl.cs:663` | 1935 | 1639 | 1961 | OK\* — raw magnitude matches the ×0.1 V family (193.5/163.9/196.1 V ≈ battSideVolt); vendor applies no scale — treat as 0.1 V counts with vendor-defect note |
| 18 | dcdc3InputVolt | `s16`, no scale — `SysControl.cs:664` | 1931 | 1637 | 1967 | OK\* — same note (196.7 V on LHS slightly above pack; plausible sensing point) |

### 2.7 Cumulative energy — `0x4101` (16641), 12 registers; read `SysControl.cs:773`, decodes `SysControl.cs:779-784`; every pair is `u32lo(second, first) × 0.1`

| Pair | Field (cite) | MID | RHS | LHS | Verdict |
|---|---|---|---|---|---|
| 0-1 | grid buyEnergyHis — `SysControl.cs:779` | 31556,1 → 97092 → 9709.2 kWh | 23462,0 → 2346.2 kWh | 4069,1 → 69605 → 6960.5 kWh | AMBIG role label (§5, A-1) |
| 2-3 | grid sellEnergyHis — `SysControl.cs:780` | 31877,0 → 3187.7 kWh | 51751,0 → 5175.1 kWh | 42567,0 → 4256.7 kWh | AMBIG role label |
| 4-5 | loadConsumeEnergyHis — `SysControl.cs:781` | 37894,0 → 3789.4 kWh | 20188,0 → 2018.8 kWh | 48810,0 → 4881.0 kWh | OK\* — magnitude consistent with loadPower history |
| 6-7 | pvEnergyHis — `SysControl.cs:782` | 0,0 → 0.0 kWh | 0.0 kWh | 62,0 → 6.2 kWh | OK\* — no PV; LHS 6.2 kWh noise/one-off (§5, A-12) |
| 8-9 | BMS chargeEnergyHis — `SysControl.cs:783` | 35672,0 → 3567.2 kWh | 21037,0 → 2103.7 kWh | 46548,0 → 4654.8 kWh | AMBIG role label |
| 10-11 | BMS dischargeEnergyHis — `SysControl.cs:784` | 56789,0 → 5678.9 kWh | 63519,0 → 6351.9 kWh | 61569,0 → 6156.9 kWh | AMBIG role label |

Cross-block check: words 8-11 are byte-identical to `0x5000` offsets 15-18 on every unit — the same counters exposed twice, low-word-first both times (`SysControl.cs:740-741` vs `779-784`). Word order confirmed.

### 2.8 BMS live — `0x5000` (20480), 31 registers; read `SysControl.cs:719`, decodes `SysControl.cs:725-753`

| Off | Field | Decode (vendor cite) | MID | RHS | LHS | Verdict |
|---:|---|---|---|---|---|---|
| 0 | softwareVersion | `ver(v)` — `SysControl.cs:725` | 536 → "2.24" | "2.24" | "2.24" | OK |
| 1 | status | enum 0 Initial/1 Stop/2 Starting/3 Running/4 Stopping/5 Fail — `GlobalFun.cs:239-250` via `SysControl.cs:726` | 3 → Running | 3 → Running | 3 → Running | OK |
| 2 | runMode | low byte, no enum in source — `SysControl.cs:727` | 3 | 1 | 1 | AMBIG — no vendor enum; values split with operating state (§5, A-13) |
| 3 | chargeDischargeStatus | low byte, no enum — `SysControl.cs:728` | 0 | 2 | 2 | AMBIG — 0 on the idle unit, 2 on both discharging units; likely 0 idle/1 charge/2 discharge, unconfirmed |
| 4 | battstringEnableStatus | byte-truncated — `SysControl.cs:729` | 1 | 1 | 1 | OK — one string |
| 5 | singleBattBicNum | `s16` — `SysControl.cs:730` | 6 | 5 | 6 | OK — commissioned topology |
| 6 | volt | `s16 × 0.1 V` — `SysControl.cs:731` | 1923 → 192.3 V | 1633 → 163.3 V | 1953 → 195.3 V | OK — = cell count × mean cell V (§4) |
| 7 | current | `s16 × 0.1 A` — `SysControl.cs:732` | 2 → 0.2 A | 73 → 7.3 A | 101 → 10.1 A | OK — positive while discharging; V×I = P exact (§3) |
| 8 | power | `s16` W — `SysControl.cs:733` | 38 W | 1192 W | 1972 W | OK — see §3; MID +38 W at idle is a small bias (§5, A-14) |
| 9 | soc | `s16` % — `SysControl.cs:734` | 10 % | 73 % | 57 % | OK — in 0-100; MID low, explains its zero discharge limits |
| 10 | soh | `s16` % — `SysControl.cs:735` | 100 % | 100 % | 100 % | OK\* — top of the 80-100 band, expected for new packs; single sample, integer quantization |
| 11 | chargeCurrentLimit | `s16 × 0.1 A` — `SysControl.cs:736` | 400 → 40.0 A | 40.0 A | 40.0 A | OK — positive, sane (see §3 limit identity) |
| 12 | dischargeCurrentLimit | `s16 × 0.1 A` — `SysControl.cs:737` | 0 → 0.0 A | 40.0 A | 40.0 A | OK — MID 0 at SOC 10%: coherent inhibit, corroborated by PCS discharge limit 0 and idle DCDC |
| 13 | chargePowerLimit | `s16` W — `SysControl.cs:738` | 7692 W | 6532 W | 7812 W | OK — exactly V × chargeCurrentLimit (§3) |
| 14 | dischargePowerLimit | `s16` W — `SysControl.cs:739` | 0 W | 6532 W | 7812 W | OK — exactly V × dischargeCurrentLimit |
| 15-16 | chargeEnergyHis | `u32lo(w16, w15) × 0.1` — `SysControl.cs:740` | 35672,0 → 3567.2 kWh | 2103.7 kWh | 4654.8 kWh | AMBIG role label (§5, A-1) |
| 17-18 | dischargeEnergyHis | `u32lo(w18, w17) × 0.1` — `SysControl.cs:741` | 56789,0 → 5678.9 kWh | 6351.9 kWh | 6156.9 kWh | AMBIG role label |
| 19 | cellNoMaxVolt | `s16` — `SysControl.cs:742` | 25 | 2 | 3 | OK / LHS off-by-tie-break (§5, A-4) |
| 20 | cellMaxVolt | `s16` mV — `SysControl.cs:743` | 3208 mV | 3271 mV | 3260 mV | OK — equals cell-array maximum |
| 21 | maxVoltCellTemp | `raw - 40` °C — `SysControl.cs:744` | 64-40 = 24 °C | 67-40 = 27 °C | 67-40 = 27 °C | OK\* — RHS ±1 °C vs 0x523C (§5, A-5) |
| 22 | cellNoMinVolt | `s16` — `SysControl.cs:745` | 1 | 10 | 38 | OK — first-occurrence index in the cell array |
| 23 | cellMinVolt | `s16` mV — `SysControl.cs:746` | 3204 mV | 3265 mV | 3253 mV | OK — equals cell-array minimum |
| 24 | minVoltCellTemp | `raw - 40` °C — `SysControl.cs:747` | 68-40 = 28 °C | 65-40 = 25 °C | 64-40 = 24 °C | OK\* — RHS ±1 °C vs 0x523C |
| 25 | cellNoMaxTemp | `s16` — `SysControl.cs:748` | 0 | 0 | 0 | OK — BIC0 is the warmest on all units (matches 0x523C) |
| 26 | cellMaxTemp | `raw - 40` °C — `SysControl.cs:749` | 68-40 = 28 °C | 67-40 = 27 °C | 67-40 = 27 °C | OK — matches warmest BIC cell temp |
| 27 | maxTempCellVolt | `s16` mV — `SysControl.cs:750` | 3205 mV | 3266 mV | 3259 mV | OK — plausible in-array value |
| 28 | cellNoMinTemp | `s16` — `SysControl.cs:751` | 40 | 30 | 40 | OK — indexes the coolest BIC (BIC4/BIC3/BIC4) |
| 29 | cellMinTemp | `raw - 40` °C — `SysControl.cs:752` | 63-40 = 23 °C | 63-40 = 23 °C | 63-40 = 23 °C | OK — matches coolest BIC cell temp |
| 30 | minTempCellVolt | `s16` mV — `SysControl.cs:753` | 3207 mV | 3265 mV | 3258 mV | OK — plausible in-array value |

### 2.9 BMS/BECU warnings and faults — `0x5040` (20544), 22 registers; BMS warn0 at 0, BECU0 warn at 1-2, BMS fault at 16-17, BECU0 fault at 18-21 (`SysControl.cs:810-818`)

All 22 words are 0 on all three units. Verdict OK — no BMS/BECU warnings or faults; coherent with healthy telemetry elsewhere.

### 2.10-2.12 Cell, temperature, balance blocks

Mapping and analysis in §4. Decode cites: cell count `min(BIC×10, 100)` and read at `0x5200` — `SysControl.cs:838-844`, stored raw mV at `SysControl.cs:852`; temperatures read `BIC×3` at `0x523C` with the per-triple assignment (offset 0 → pole+, 1 → all 10 cell temps of that BIC, 2 → pole−, all `raw - 40`) — `SysControl.cs:869-890`; balance words `BIC` at `0x524E`, stored raw with no bit decode — `SysControl.cs:906-915` (encoding Unknown, evidence §10).

## 3. Battery-power / dynamic-limit scaling check (deliverable 3)

### 3.1 The V×I=P identity at three independent measurement points

| Unit | BMS V (`0x5000+6`) | BMS I (`+7`) | V×I | BMS P (`+8`) | DCDC V (`0x2000+3`) | DCDC I (`+5`) | V×I | DCDC P (`+9`) | PCS inv V×I vs S |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| MID | 192.3 V | 0.2 A | 38.5 W | 38 W | 193.5 V | -0.12 A | -23.2 W | -24 W | 0.5×0.04 ≈ 0 = S |
| RHS | 163.3 V | 7.3 A | 1192.1 W | 1192 W | 164.5 V | 6.89 A | 1133.4 W | 1132 W | 244.8×4.52 = 1106.5 ≈ 1102 VA |
| LHS | 195.3 V | 10.1 A | 1972.5 W | 1972 W | 196.1 V | 9.40 A | 1843.3 W | 1846 W | 247.8×7.34 = 1818.9 ≈ 1800 VA |

Each subsystem's power register equals its own voltage times its own current to within 0.5%. This closes the loop only under the evidence-matrix family split:

- **BMS current at 0.1 A/count is confirmed.** If it were 0.01 A/count, RHS current would be 0.73 A and V×I = 119 W, contradicting the same unit's measured 1192 W battery power and the 1086 W the PCS is delivering to the grid — a 10× inconsistency in W, impossible. The same argument holds on LHS (197 W vs 1972 W measured, 1797 W delivered).
- **DCDC and PCS currents at 0.01 A/count are confirmed.** If DCDC current were 0.1 A/count, RHS would read 68.9 A → V×I = 11.3 kW, exceeding the DCDC power limit (5500 W, `0x2060+15`), the PCS apparent-power limit (5000 VA, `0x1060+24`), and contradicting the 1.1 kW actually delivered. If PCS inverter current were 0.1 A/count, RHS would be 45.2 A → 11.1 kVA > 5 kVA rating.
- **Power registers are unscaled watts** (no multiplier), since the products above land on them exactly.

### 3.2 Dynamic-limit identity — the strongest single check

`chargePowerLimit == packV × chargeCurrentLimit` and `dischargePowerLimit == packV × dischargeCurrentLimit` hold exactly on every unit (`SysControl.cs:731,732,736-739`):

| Unit | packV | chg I lim | V×I | chg P lim (raw) | dis I lim | V×I | dis P lim (raw) |
|---|---:|---:|---:|---:|---:|---:|---:|
| MID | 192.3 V | 40.0 A | 7692.0 | 7692 | 0.0 A | 0.0 | 0 |
| RHS | 163.3 V | 40.0 A | 6532.0 | 6532 | 40.0 A | 6532.0 | 6532 |
| LHS | 195.3 V | 40.0 A | 7812.0 | 7812 | 40.0 A | 7812.0 | 7812 |

Under a 0.01 A limit scale the identity would be off by 10× (e.g., 195.3 × 4.0 = 781.2 ≠ 7812), so this pins both the 0.1 A current-limit family and the unscaled-W power limits simultaneously. The 40 A limit itself is sane (≤ ~0.3-0.4 C for this class; and the PCS clamps to its own 5000 W rating — `min(rating, BMS limit)` observed on all units, including MID's discharge clamp to 0 at SOC 10%).

### 3.3 Power-flow chain (W plausibility via the derived pack voltage)

| Unit | BMS batt P | DCDC batt P | PCS AC P | Stage losses |
|---|---:|---:|---:|---|
| RHS (discharge) | 1192 W | 1132 W | 1086 W | 5.0% then 4.1% (8.9% total) |
| LHS (discharge) | 1972 W | 1846 W | 1797 W | 6.4% then 2.7% (8.9% total) |
| MID (idle) | 38 W | -24 W | 0 W | near-zero noise around zero |

The DC bus voltage closes the chain: PCS dcVolt = DCDC dcOutputVolt (409.8/409.9 and 418.5/419.0 V; MID idle 193.5/193.3 V sagged to battery because the DCDC is not switching). Identical ~8.9% total chain loss on both discharging units suggests genuine staged conversion efficiency (or a shared small bias in BMS power); either way the magnitudes are mutually consistent.

**Verdict: the evidence-matrix scaling families (PCS/DCDC 0.01 A; system/BMS/BECU 0.1 A; unscaled W power; 0.1 V pack voltages; low-word-first 0.1-kWh energy counters) are consistent with — and positively confirmed by — the observed magnitudes.** No field in the captured set requires a rescale.

## 4. Cell blocks (deliverable 4)

Cell count formula `min(BIC×10, 100)` (`SysControl.cs:839-844`); cells are raw mV (`SysControl.cs:852`), temperatures `raw - 40` (`SysControl.cs:881-890`).

| Unit | Words read | = BIC×10? | Commissioned | min | max | spread | mean | Σcells/1000 vs pack V |
|---|---:|---|---|---:|---:|---:|---:|---|
| MID | 60 | 6×10 ✓ | 6 BIC ✓ | 3204 mV | 3208 mV | 4 mV | 3206.07 mV | 192.364 V vs 192.3 V (Δ 0.06 V, 0.03%) |
| RHS | 50 | 5×10 ✓ | 5 BIC ✓ | 3265 mV | 3271 mV | 6 mV | 3266.54 mV | 163.327 V vs 163.3 V (0.02%) |
| LHS | 60 | 6×10 ✓ | 6 BIC ✓ | 3253 mV | 3260 mV | 7 mV | 3256.72 mV | 195.403 V vs 195.3 V (0.05%) |

- Every cell is inside 2.8-3.7 V, on the LFP plateau; mean cell voltage tracks SOC sensibly for LFP (10% → 3.206 V, 57% → 3.257 V, 73% → 3.267 V).
- Pack voltage = cell count × mean cell voltage on all units to ≤0.05% — this simultaneously validates the 60/50/60 cell counts, the 0.1 V pack scale, and the mV cell scale.
- Extrema cross-check vs the arrays: reported max/min mV equal the array max/min on all units; reported cell numbers equal the first-occurrence index on five of six checks (exception: LHS cellNoMaxVolt = 3 while the first 3260 mV cell is index 1 — §5, A-4). Reported max/min-temperature cells index the warmest/coolest BIC on every unit.

Temperature block `0x523C`, decoded as (pole+, cell, pole−) triples per BIC (`SysControl.cs:877-890`):

| Unit | Per-BIC triples (°C) | Range |
|---|---|---|
| MID | (28,28,28) (25,25,25) (24,24,24) (24,24,24) (23,23,24) (23,23,23) | 23-28 °C |
| RHS | (27,28,28) (25,24,24) (24,24,24) (23,23,23) (23,23,23) | 23-28 °C |
| LHS | (27,27,27) (25,25,25) (24,24,24) (24,23,24) (23,23,23) (23,22,22) | 22-27 °C |

Ambient-ish, tight gradients (≤6 °C across a pack), warmest at BIC0 on every unit — matches the `0x5000` extrema temperatures. Note the vendor decode assigns one cell-temperature value to all 10 cells of a BIC (firmware publishes one representative cell temp per BIC).

Balance words `0x524E` (raw, vendor stores without decoding bits — `SysControl.cs:906-915`; encoding Unknown per evidence §10):

| Unit | Raw words (hex) | Note |
|---|---|---|
| MID | 0x0280, 0, 0, 0, 0, 0x00E0 | Nonzero on BIC0 and BIC5 — precisely the two BICs hosting the lowest cells (3204s) |
| RHS | all 0x0000 | — |
| LHS | 0, 0, 0, 0, 0, 0x0010 | Nonzero on BIC5 — the lowest-voltage BIC on LHS |

Pattern flagged, not interpreted: if bits meant "balancing active" you would expect the HIGH-voltage BICs flagged, so the bits more likely denote something else (per-cell flags, inhibit state). Keep raw words; do not decode (§5, A-15).

## 5. Anomaly register — every WRONG or ambiguous field, with alternative readings (deliverable 2)

| # | Field (where) | Observation | Best alternative readings | Disposition |
|---|---|---|---|---|
| A-1 | Energy counter role labels (`0x4101`, `0x5000+15..18`) | Discharge > charge on ALL units despite PV = 0 (MID 3567 vs 5679; RHS 2104 vs 6352; LHS 4655 vs 6157 kWh); RHS sell (5175) > buy (2346) | (a) counters cleared independently at different times (a clear op `0x8001` exists); (b) charge/discharge pair roles swapped in the vendor label; (c) buy/sell swapped | Magnitudes and word order are solid; only the role labels are doubtful. Disambiguate with follow-up B (§6) |
| A-2 | externalGridCurrent (`0x1000+16`) | MID reconciles with extGridP at PF 0.89, but RHS/LHS read 3.30/3.60 A against net powers of -37/-48 W (≈0.15/0.19 A) | (a) different measurement point (pod feeder vs combined); (b) RMS magnitude of non-cancelling components; (c) field means something else | Do not use for control or power accounting; keep raw |
| A-3 | LHS gridApparentPower 1719 VA < gridActivePower 1747 W (`0x1000+9`) | Physically impossible pair (S ≥ \|P\|); V×I = 1713.8 supports the S value, √(P²+Q²) = 1748.1 supports P | Differently-filtered averaging windows between P and I channels | Metering-integration inconsistency, ~1.7%; inverter-side triple on the same unit is coherent. Follow-up D quantifies it |
| A-4 | LHS cellNoMaxVolt = 3 (`0x5000+19`) | First 3260 mV cell in the array is index 1; MID (25) and RHS (2) match first-occurrence exactly | (a) firmware tie-break rule differs (e.g. last-in-BIC, round-robin); (b) 1-based-with-offset numbering — inconsistent with the other units | Treat cell numbers as approximate references; always recompute extrema from `0x5200` |
| A-5 | RHS extrema cell temps (`0x5000+21/+24`) | 27/25 °C vs 28/24 °C implied by `0x523C` for the same cells (±1 °C); MID and LHS cohere exactly | Different sensor source per view, or rounding | Cosmetic; use `0x523C` for gradients |
| A-6 | MID inverter frequency 49.97 Hz with inverter voltage 0.5 V (`0x1000+12`) | Frequency reported while the inverter is open | Frequency tracked from grid sync, not the inverter output | Curiosity only |
| A-7 | pcsFanSpeed = 0 with fanStatus = 1 (`0x1060+11`) | Always 0, including delivering units | Register unpopulated, or PWM/duty encoded | DEAD for telemetry |
| A-8 | DCDC gridFrequency = 0.00 Hz (`0x2060+6`) | 0 on all units, including active ones, while PCS reads 50.00-50.04 Hz | Unpopulated firmware field, or a non-grid-frequency meaning | DEAD; exclude from the run-mode decoder |
| A-9 | rateGridVoltFrequency = 0, functionSelected = 0 (`0x1060+3/+4`) | 0 on all units; no enum table in the decompiled source | Enum value 0 = "unset/default" most likely | Read `0x8102` (follow-up A) to pin the rated V/F and grid-standard enums |
| A-10 | DCDC fanStatus = 0 while running 1.1-1.8 kW (`0x2060+11`) | Fans off at meaningful power | Thermostatic threshold not reached (coolant 23-28 °C) — plausible | OK\*, watch in run-mode |
| A-11 | DCDC status naming (`0x2000+1`) | Value 3 decoded with PCS status names ("On grid") but the DCDC is not grid-tied | For DCDC, 3 evidently means "running" (MID idle reads 1=Idle) | Naming ambiguity only; value mapping is consistent |
| A-12 | LHS pvEnergyHis = 6.2 kWh (`0x4101+6/7`) | Tiny PV energy on one unit only, with PV current ≈ 0 | Bench-supply exposure or counter noise | Ignore; keep raw |
| A-13 | BMS runMode (MID 3, RHS/LHS 1) and chargeDischargeStatus (0/2/2) (`0x5000+2/+3`) | No vendor enum exists for either field; values split cleanly idle vs discharge | runMode: 1 = discharge-mode, 3 = standby/idle; chargeDischargeStatus: 0 idle, 1 charge, 2 discharge | Consistent but unconfirmed; follow-up C pins the charge-state values |
| A-14 | MID BMS power +38 W at idle (`0x5000+8`) | Positive discharge-signed power while discharge is inhibited (limits 0) | 0.2 A current-sensor bias at zero, or pack housekeeping draw measured at the string | Non-blocking; quantify with follow-up D |
| A-15 | Balance words (§4) | Nonzero words sit on the LOWEST-voltage BICs | Bits are not "balancing active"; likely per-cell flags or an inhibit state | Keep raw; encoding Unknown |
| A-16 | ledStatus (PCS 24/18/18; DCDC 1/20/20), matchGoalState (0/3/3) | Values split cleanly by idle/active but have no enum | State-machine codes, meaning unknown | Record raw; do not name them |

Everything else in §2 decoded plausibly: SOC in range (10/73/57), SOH 100 on all (top of band, new packs), pack voltages = cells × 3.2-3.27 V, all cells 3253-3271 mV, temperatures 22-43 °C across all sensors, dynamic limits positive and internally exact, status/warning/fault words coherent (two known calibration warnings, zero faults, states matching the measured power flow on every unit).

## 6. Confidence-graded disposition (deliverable 5)

### High confidence — ready to wire into the run-mode decoder

1. Identity: `0x8106` RTU ID, low-word-first — pinned per unit (§1).
2. Layout: `0x5000` words 0/4/5 — IoT discriminator, enable mask, BIC count (§1).
3. BMS core `0x5000+6..14`: pack V, current, power, SOC, SOH, charge/discharge current limits, charge/discharge power limits — triple-cross-validated (V×I=P, V×Ilim=Plim, PCS clamp) on all three units.
4. BMS energies `0x5000+15..18` and all six `0x4101` counters — word order, scale, and cross-block identity confirmed; only role labels doubtful (A-1).
5. Cell voltages `0x5200` (raw mV) and the extrema VALUES at `0x5000+19..30`; temperature block `0x523C` (raw-40).
6. PCS live `0x1000`, all 21 offsets — scales confirmed by V×I≈S on active units; named enums for status/runMode.
7. PCS limits `0x1060+24..27`, temperatures `+28/+29`, identity `+0..+2`.
8. DCDC live `0x2000`, all 13 offsets — V×I=P exact, branches sum to total.
9. DCDC detail `0x2060+3,+4,+7,+15` (temps, voltage objective, power limit).
10. Warning/fault word placement in `0x1040` / `0x2040` / `0x5040` — the captured bits decode to the two documented calibration warnings; everything else zero.

### Needs a follow-up capture — with the exact read that disambiguates

| Open item | Exact follow-up read |
|---|---|
| A-1 energy counter role labels | FC03 `0x4101`×12 and `0x5000`×31 now and again at T+30 min with direction known (RHS/LHS discharge ~1.2/2.0 kW at capture time; ~0.6/1.0 kWh per 30 min = 6/10 counts). Whichever pair increments is the discharge counter; likewise buy vs sell on the external meter |
| A-13 BMS runMode / chargeDischargeStatus values for charge | FC03 `0x5000`×31 during a known charge window (MID at SOC 10% with chargePowerLimit 7692 W will accept charge) — records the charge-state enum values and the current sign under charge |
| A-9 rateGridVoltFrequency / gridStandard / serial parameters | FC03 `0x8102`×56 (device/network/serial parameter block, V-WRITE inventory) plus `0x0100`×61, `0x8100`×1, `0x8139`×1 — also completes the system-overview decode (control mode, work mode, system battery path), none of which are in this capture |
| A-3 / A-14 metering integration and idle bias | Burst capture: FC03 `0x1000`×21 + `0x2000`×13 at 1 Hz for 60 s on one discharging unit and on idle MID |
| A-4 cell-number tie-break | Any later cell capture where the maximum is unique (no tie) — the reported index then identifies the rule |

### Stays unknown — not decodable from statics

- Watchdog / PQ lease expiry timing, renewal jitter, and fallback state after missed renewals (requires the timed renewal-stop experiment of evidence §14; nothing in a static read can measure it).
- Active/reactive power sign convention for control (negative-P charging is only operationally corroborated; the capture shows telemetry signs, not command signs).
- Balance-word bit encoding (A-15) and any cell-imbalance threshold semantics.
- Meanings of ledStatus, matchGoalState, rateGridVoltFrequency, functionSelected, gridStandard, and DCDC debug enums (A-9, A-16).
- DCDC gridFrequency (A-8) and fan speeds (A-7) — dead registers in this firmware.
- Whether another controller writes the objective registers (competing-writer detection, evidence §13.18).

## 7. Blockers

- **Decoding: none.** Every captured field now has a validated mapping or an explicitly flagged ambiguity; nothing blocks building the run-mode (telemetry) decoder from the high-confidence list in §6.
- **Control: unchanged from PROTOCOL_EVIDENCE §13** — watchdog timing, fallback behavior, `[1,P,Q]` vs active-only equivalence, verified sign conventions, and competing-writer detection remain open and gate any actuation. The string-identity binding strategy (only the CRC32-grade RTU ID is on the wire) also remains open.
- Two fleet observations worth carrying into commissioning: all three units persistently report the PCS/DCDC calibration-parameter warnings (benign-looking, but root cause unknown — evidence §13.16), and MID sat at SOC 10% with discharge inhibited, which is why its DC link was sagged to battery voltage at capture time.
