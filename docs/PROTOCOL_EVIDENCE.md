# EnergyPod protocol evidence

Status: evidence baseline, not an implementation specification  
Prepared: 2026-08-21  
Scope: read-only analysis of the decompiled application, its bundled configuration/logs, and prior local integrations. No live battery or gateway was contacted.

## 1. Purpose and confidence model

This document records what the available artifacts actually prove. A future protocol implementation must not promote an `Assumed` or `Unknown` item to a control contract without new evidence.

| Classification | Meaning in this document |
|---|---|
| **Confirmed by vendor code** | A literal call, decoder, enum, or behavior exists in the decompiled `EnergyPod.exe` source or a file that the application loads at runtime. This confirms application behavior, not necessarily correct firmware behavior; known code defects are called out. |
| **Corroborated operationally** | The behavior is repeated in the user's prior working integrations or original application logs, but is not fully established by the decompiled application. No raw packet captures are available. |
| **Assumed** | A prior implementation, comment, naming choice, or inference asserts the behavior without independent proof. It must not be used as a safety-critical fact. |
| **Unknown** | The inspected evidence does not determine the behavior. |

### Evidence hierarchy

1. Decompiled application behavior: `C:\Users\vagrant\Downloads\EnergyPod_RE\src`.
2. Original runtime material: `C:\Users\vagrant\Downloads\EnergyPod`, especially `cfg/AppConfig_ini.xml`, `cfg/MINI2 FultWarningInfo.xlsx`, and `Log/*.log`.
3. Prior integrations: `C:\Users\vagrant\Downloads\modbus`. These are useful operational corroboration, but they also contain contradictions and must not be treated as vendor documentation.

Principal source anchors:

| ID | Source |
|---|---|
| V-CONN | `EnergyPod_RE/src/MiniESapp/CommCfgForm.cs:137-285, 403-437` |
| V-LAYOUT | `EnergyPod_RE/src/MiniESapp/MiniESapp.cs:1277-1330` |
| V-POLL | `EnergyPod_RE/src/MiniESapp/SysControl.cs:103-370` |
| V-SYS | `EnergyPod_RE/src/MiniESapp/SysControl.cs:372-470` |
| V-IOT | `EnergyPod_RE/src/MiniESapp/SysControl.cs:472-928` |
| V-LEGACY | `EnergyPod_RE/src/MiniESapp/SysControl.cs:930-1439` |
| V-WRITE | `EnergyPod_RE/src/MiniESapp/SysControl.cs:1442-1582` |
| V-PQ-UI | `EnergyPod_RE/src/MiniESapp/MiniESapp.cs:2172-2237, 4744-4752` |
| V-MODES | `EnergyPod_RE/src/MiniESapp/GlobalFun.cs:152-250`; `MiniESapp.cs:2101-2169` |
| V-FAULTS | `EnergyPod_RE/src/MiniESapp/MiniESapp.cs:789-1078, 1484-1527, 1615-1645` |
| O-CONFIG | `EnergyPod/cfg/AppConfig_ini.xml` |
| O-FAULT-MAP | `EnergyPod/cfg/MINI2 FultWarningInfo.xlsx`, sheets `Fault Info` and `Warning Info` |
| O-LOGS | `EnergyPod/Log/*.log` |
| P-TRANSPORT | `modbus/battery.py:146-155`; `modbus/byd/battery.py:111-119` |
| P-CONTROL | `modbus/battery.py:207-245, 469-584`; `modbus/byd/battery.py:254-307` |
| P-MAP | `modbus/byd/battery.py:62-75, 339-382` |
| P-FLEET | `modbus/manager.py:151-165, 833-835`; `modbus/byd/config.py:19-59`; `modbus/byd/scheduler.py:241-252` |

## 2. Provenance: is the application actually from BYD?

| Fact | Classification | Evidence and conclusion |
|---|---|---|
| The executable and project identify themselves as `EnergyPod` / `MiniESapp`. | **Confirmed by vendor code** | The executable name is `EnergyPod.exe`; the decompiled assembly title/product are `MiniESapp`; the UI reports application version `1.10` (`SoftInfo.cs:3-7`). |
| The application contains BYD-specific account names. | **Confirmed by vendor code** | `LoginForm.cs:39-53` contains `BYDadmin` and `BYDuser`. This supports a BYD association. Credentials embedded there must not be reused. |
| The executable is cryptographically signed by BYD. | **Unknown** | `Get-AuthenticodeSignature` reports `NotSigned`. There is no signer certificate. |
| Assembly company metadata establishes BYD authorship. | **Unknown** | Metadata says `AssemblyCompany("Microsoft")` and `Copyright © Microsoft 2019`, which appears to be placeholder metadata and does not establish authorship (`Properties/AssemblyInfo.cs:7-17`). |
| The inspected binary is official BYD-authored software. | **Unknown** | The names, EnergyPod terminology, register model, and BYD login strings make BYD-associated tooling plausible, but the artifacts do not prove publisher identity or chain of custody. Refer to it below as the **vendor application** only as shorthand for the supplied reference application. |
| `.NET 4.5.2` is the application version. | **Assumed** | It is the target framework (`app.config` and `EnergyPod.csproj`), not the product version. The UI version is `1.10`; assembly/file version is `1.0.0.0`. |

## 3. Transport contracts and distinctions

| Fact | Classification | Evidence / precise contract |
|---|---|---|
| Vendor serial transport is Modbus RTU. | **Confirmed by vendor code** | `ModbusSerialMaster.CreateRtu(serialPort)` is used. Bundled/default settings are 19200 baud, 8 data bits, no parity, 1 stop bit, station/slave address 4, 5000 ms read/write timeout, and zero transport retries (V-CONN, O-CONFIG). |
| The vendor application's Ethernet mode is a reverse connection. | **Confirmed by vendor code** | The PC configures local `192.168.1.10`, listens on TCP port `507`, waits up to 30 seconds, and accepts a connection from the device assigned `192.168.1.100` (V-CONN; `DeviceInfo.cs:9-15`). The PC is the TCP listener; the device initiates the TCP connection. |
| Vendor Ethernet uses the NModbus IP master, not the serial RTU framer. | **Confirmed by vendor code** | The accepted `TcpClient` is passed to `ModbusIpMaster.CreateIp(...)` (V-CONN). This is distinct from the Waveshare path below and implies Modbus IP/MBAP framing in NModbus. |
| The deployed Waveshare path is client-initiated RTU framing carried over a TCP stream. | **Verified by live capture (2026-08-22)** | The production `WaveshareTransport` (PyModbus 3.15, `FramerType.RTU`, `device_id=4`, TCP 4196) connected outward to each gateway and read the full 13-block IoT plan cleanly on first attempt, three units, zero malformed responses. Raw vectors: `docs/evidence/live-capture-2026-08-22.json`. |
| The three gateway endpoints are MID `.11`, RHS `.12`, LHS `.13`, TCP port `4196`. | **Corroborated operationally** | Repeated in `modbus/manager.py:833-835`, `modbus/byd/config.py:19-48`, and the next-generation config. The physical identity behind each IP has not been cryptographically or register-identity verified. |
| Slave/unit ID is 4. | **Confirmed by vendor code** | Default and bundled serial configuration use 4; every NModbus read/write receives `DeviceInfo.devAddress`; prior Waveshare code also sends slave 4. |
| TCP unit ID is ignored by the device. | **Unknown** | The vendor code passes the unit ID on every TCP operation. Whether the firmware or gateway ignores it is not established. |
| Holding-register reads and writes use zero-based PDU addresses. | **Confirmed by vendor code** | All addresses below are the literal `startAddress` values supplied to NModbus and are corroborated by prior PyModbus calls. Human-facing `4xxxx` or “register 514” notation may be one-based; do not add one when an API expects a PDU address. |
| Function codes used by the vendor application are FC03 and FC16. | **Confirmed by vendor code** | Reads call `ReadHoldingRegisters`; all writes call `WriteMultipleRegisters`. No vendor call to FC06, input-register reads, coils, or discrete inputs was found. Prior integrations sometimes use single-register helpers; that is not the vendor application's write contract. |
| Polls and writes are serialized. | **Confirmed by vendor code** | All main polling and UI writes share `ShareRes.mutex`, a process-wide `System.Threading.Mutex` (`ShareRes.cs:5-8`; V-POLL; V-PQ-UI). |

### Required adapter distinction

The two Ethernet mechanisms are not interchangeable:

```text
Vendor Ethernet:
EnergyPod PC (192.168.1.10:507 listener) <- device TCP connection -> Modbus IP/MBAP

Observed Waveshare deployment:
new controller -> TCP client to 192.168.1.11/12/13:4196 -> RTU frame in TCP stream -> slave 4
```

Any implementation must select framing explicitly. A default `ModbusTcpClient` using MBAP is not evidence-equivalent to the prior Waveshare integration.

## 4. Layout detection

| Fact | Classification | Evidence / precise contract |
|---|---|---|
| Layout probe | **Confirmed by vendor code** | FC03, start `0x5000` (20480), count 7 (V-LAYOUT). |
| IoT/new layout discriminator | **Confirmed by vendor code** | If returned register offset 0 is greater than 10, `protocolFlag = 1`. Enable mask is offset 4 and BIC count is offset 5. |
| Legacy layout discriminator | **Confirmed by vendor code** | Otherwise `protocolFlag = 0`. Enable mask is offset 2 and BIC count is offset 6. |
| Negative BIC count handling | **Confirmed by vendor code** | The value is cast to signed 16-bit; if negative, the local copy is set to zero. No upper-bound validation is performed during detection. |
| Enable mask width | **Confirmed by vendor code** | The enable-mask register is cast to `byte` before being stored (`BmsInfo.battstringEnableStatus`, declared `byte`; `(byte)array[4]` for IoT and `(byte)array[2]` for legacy, `MiniESapp.cs:1289,1299`, `SysControl.cs:729,1150`). Only the low 8 bits are used, so at most 8 BECU enable bits exist; bits 8-15 of the raw register are discarded by the vendor. |
| BECU count | **Confirmed by vendor code** | It is the population count of the byte-truncated enable mask, forced to at least 1 (`GlobalFun.NumberOf1`, `SysControl.cs:90-99`). Because the mask is truncated to 8 bits, the legacy BECU count cannot exceed 8. |
| Deployed units use the IoT layout. | **Verified by live capture (2026-08-22)** | All three layout probes return register 0 = 536 (> 10 selects IoT). Per-unit commissioned topology: MID (192.168.1.11) 6 BIC, RHS (192.168.1.12) 5 BIC — differing topology across the fleet, validating the per-unit commissioning requirement — LHS (192.168.1.13) 6 BIC; enable mask 1 on all. Pinned device identities (RTU ID at 0x8106, low-word-first uint32): MID `0x2C225097`, RHS `0x2C225076`, LHS `0x2C225095`. Control remains observe-only until scaling, direction, freshness, and watchdog timing are validated per unit. |

## 4a. Live commissioning evidence (2026-08-22, authorized observe-only)

Authorized by the operator as a direct hookup; every operation below was a
read-only FC03 holding-register read through the production
`WaveshareTransport`; no write of any kind was issued.

- First live read: MID layout probe (0x5000, 7 registers) — succeeded first
  attempt, validating the previously unverified RTU-over-TCP framing end to
  end through our own adapter.
- Full 13-block IoT read plan captured once per unit (including the 0x8106
  identity pair): `docs/evidence/live-capture-2026-08-22.json` — the first
  E1 reference vectors taken from deployed hardware rather than handcrafted.
- Fleet topology: MID 6 BIC / RHS 5 BIC / LHS 6 BIC (enable mask 1 each).
- Still unknown and required before any actuation: telemetry scaling and
  field placement validation against the evidence matrix, power-direction
  sign on the real installation, watchdog expiry timing, and the
  string-identity binding strategy (the wire carries only the CRC32 RTU ID).

## 5. Exhaustive vendor call-site register inventory

All entries are holding registers. Counts are 16-bit registers. `i` is zero-based BECU index.

### Reads

| Layout / area | Start (hex / decimal) | Count | Meaning decoded by application | Classification | Source |
|---|---:|---:|---|---|---|
| Common | `0x0100` / 256 | 61 | System overview/history | **Confirmed by vendor code** | V-SYS |
| Common | `0x8100` / 33024 | 1 | Debug/maintenance mode readback | **Confirmed by vendor code** | V-SYS |
| Common | `0x8139` / 33081 | 1 | System network status | **Confirmed by vendor code** | V-SYS |
| Detection | `0x5000` / 20480 | 7 | Layout, enable mask, BIC count | **Confirmed by vendor code** | V-LAYOUT |
| Common | `0x8106` / 33030 | 2 | RTU ID | **Confirmed by vendor code** | V-LAYOUT |
| Common | `0x8102` / 33026 | 56 | Device/network/serial parameters | **Confirmed by vendor code** | V-WRITE |
| IoT | `0x1000` / 4096 | 21 | PCS live data | **Confirmed by vendor code** | V-IOT |
| IoT | `0x1040` / 4160 | 22 | PCS warning words 0-3 and fault words 0-5 | **Confirmed by vendor code** | V-IOT |
| IoT | `0x1060` / 4192 | 32 | PCS detailed state, objectives, limits, temperatures | **Confirmed by vendor code** | V-IOT |
| IoT | `0x2000` / 8192 | 13 | DCDC live data | **Confirmed by vendor code** | V-IOT |
| IoT | `0x2040` / 8256 | 22 | DCDC warning words 0-3 and fault words 0-5 | **Confirmed by vendor code** | V-IOT |
| IoT | `0x2060` / 8288 | 19 | DCDC detailed state, objectives, limits, temperatures | **Confirmed by vendor code** | V-IOT |
| IoT | `0x4101` / 16641 | 12 | Grid/load/PV/BMS cumulative energies | **Confirmed by vendor code** | V-IOT |
| IoT | `0x5000` / 20480 | 31 | BMS live data and extrema | **Confirmed by vendor code** | V-IOT |
| IoT | `0x5040` / 20544 | 22 | BMS/BECU warnings and faults | **Confirmed by vendor code** | V-IOT |
| IoT | `0x5200` / 20992 | `min(BIC×10,100)` | Cell voltages | **Confirmed by vendor code** | V-IOT |
| IoT | `0x523C` / 21052 | `BIC×3` | Pole/cell temperatures | **Confirmed by vendor code** | V-IOT |
| IoT | `0x524E` / 21070 | `BIC` | Package balance words | **Confirmed by vendor code** | V-IOT |
| Legacy | `0x1000` / 4096 | 60 | PCS live and detailed data | **Confirmed by vendor code** | V-LEGACY |
| Legacy | `0x1040` / 4160 | 60 | PCS warnings, faults, cumulative energies | **Confirmed by vendor code** | V-LEGACY |
| Legacy | `0x3000` / 12288 | 35 | DCDC live and detailed data | **Confirmed by vendor code** | V-LEGACY |
| Legacy | `0x3040` / 12352 | 22 | DCDC warnings and faults | **Confirmed by vendor code** | V-LEGACY |
| Legacy | `0x5000` / 20480 | 21 | BMS live data | **Confirmed by vendor code** | V-LEGACY |
| Legacy | `0x5040` / 20544 | 34 | BMS and four BECU warning/fault areas | **Confirmed by vendor code** | V-LEGACY |
| Legacy BECU `i` | `0x6000 + 0x800i` | 35 | BECU data/extrema/limits/energy/capacity | **Confirmed by vendor code** | V-LEGACY |
| Legacy BECU `i` | `0x6040 + 0x800i` | 20 | BECU warnings and faults | **Confirmed by vendor code** | V-LEGACY |
| Legacy BECU `i` | `0x6200 + 0x800i` | `min(BIC×10,100)` | Cell voltages | **Confirmed by vendor code** | V-LEGACY |
| Legacy BECU `i` | `0x6300 + 0x800i` | `min(BIC×10,100)` | Cell temperatures | **Confirmed by vendor code** | V-LEGACY |
| Legacy BECU `i` | `0x6400 + 0x800i` | `BIC` | Package balance words | **Confirmed by vendor code** | V-LEGACY |
| Legacy BECU `i` | `0x6500 + 0x800i` | `BIC×2` | Pole temperatures | **Confirmed by vendor code** | V-LEGACY |

### Writes

| Start (hex / decimal) | Count / payload | Purpose | Classification | Source |
|---|---|---|---|---|
| `0x0200` / 512 | 3: `[1, signed P bits, signed Q bits]` | PQ objective | **Confirmed by vendor code** | V-WRITE |
| `0x8000` / 32768 | 1: debug-mode value | Select debug/maintenance mode | **Confirmed by vendor code** | V-MODES |
| `0x8001` / 32769 | 1: `0xFF00` | Clear historical energy counters | **Confirmed by vendor code** | `MiniESapp.cs:2261-2274`, `2443-2457` |
| `0x8002` / 32770 | caller-provided list | RS485 parameters | **Confirmed by vendor code** | V-WRITE |
| `0x8008` / 32776 | caller-provided list | Network parameters | **Confirmed by vendor code** | V-WRITE |
| `0x8018` / 32792 | caller-provided list | Serial number | **Confirmed by vendor code** | V-WRITE |
| `0x8034` / 32820 | 1 | Grid standard | **Confirmed by vendor code** | `MiniESapp.cs:2488-2499` |
| `0x8035` / 32821 | 1 | Region | **Confirmed by vendor code** | `MiniESapp.cs:2502-2521` |
| `0x8036` / 32822 | 1 | Parameter-setting enable | **Confirmed by vendor code** | `MiniESapp.cs:2524-2573` |
| `0x8037` / 32823 | 1: `0xFF00` | Clear battery low-voltage protection latch | **Confirmed by vendor code** | `MiniESapp.cs:2576-2588` |

The maintenance/configuration writes are evidence only. They are not safe production-control interfaces.

## 6. Field decoding, signedness, scaling, and endianness

### Primitive rules

| Fact | Classification | Contract |
|---|---|---|
| Register byte order | **Confirmed by vendor code** | NModbus returns each register as a `ushort`. No byte swap is performed within a register. |
| Signed 16-bit fields | **Confirmed by vendor code** | Fields cast to `short` use two's-complement signed values. Fields cast to `byte` keep only the low 8 bits. |
| 32-bit composition helper | **Confirmed by vendor code** | `GetUintFromUshort(high, low) = (high << 16) | low` (`GlobalFun.cs:253-256`). Call-site argument order therefore determines word order. |
| Universal 32-bit word order | **Unknown** | There is no universal order. The source uses field-specific word orders listed below. |

### Corrected field-specific 32-bit word order

“Lower-address word” is the first register in the read block.

| Field(s) | Lower-address word | Scale | Classification | Source |
|---|---|---:|---|---|
| System history charge/discharge at `0x0103..0x0106` | High word | none applied | **Confirmed by vendor code** | `SysControl.cs:412-413` |
| Legacy PCS charge, discharge, buy, sell, PV energy at `0x1070..0x1079` | High word | `×0.1` | **Confirmed by vendor code** | `SysControl.cs:1027-1031` |
| Legacy BMS charge/discharge energy at `0x5010..0x5013` | High word | `×0.1` | **Confirmed by vendor code** | `SysControl.cs:1164-1165` |
| Legacy BECU charge/discharge energy | High word | `×0.001` | **Confirmed by vendor code** | `SysControl.cs:1272-1273` |
| Legacy BECU nominal/current capacity | High word | none applied | **Confirmed by vendor code** | `SysControl.cs:1274-1275` |
| IoT BMS charge/discharge energy in `0x500F..0x5012` | Low word | `×0.1` | **Confirmed by vendor code** | `SysControl.cs:740-741` |
| Entire IoT energy block `0x4101..0x410C` | Low word | `×0.1` | **Confirmed by vendor code** | `SysControl.cs:779-784` |
| RTU ID at `0x8106..0x8107` and parameter offsets 4-5 | Low word | none | **Confirmed by vendor code** | `MiniESapp.cs:1325-1330`; `SysControl.cs:1540-1545` |

The engineering unit for system-history values at `0x0103..0x0106` is **Unknown**. The vendor decoder does not multiply by `0.1`; any kWh scaling assigned there is an assumption.

### Common system overview: base `0x0100`

| Offset / address | Decode | Engineering meaning | Classification |
|---|---|---|---|
| `+0..+2` / `0x0100..0102` | low byte | system status, control mode, work mode | **Confirmed by vendor code** |
| `+3..+6` | two high-word-first `uint32` | charge/discharge history; unit unconfirmed | **Confirmed by vendor code** |
| `+7` | low byte | force-charge mode | **Confirmed by vendor code** |
| `+16,+17` | low byte | battery status, SOC (%) | **Confirmed by vendor code** |
| `+18,+19` | unsigned register `×0.1` | battery voltage (V), battery current (A) | **Confirmed by vendor code** |
| `+20,+21,+22` | `int16`, word, low byte | battery power (W), warning word, SOH (%) | **Confirmed by vendor code** |
| `+48` | low byte | PCS status | **Confirmed by vendor code** |
| `+49,+50` | register `×0.1` | PCS current (A), grid voltage (V) | **Confirmed by vendor code** |
| `+51,+52` | `int16` | PCS active/reactive power | **Confirmed by vendor code** |
| `+53` | register `×0.01` | grid frequency (Hz) | **Confirmed by vendor code** |
| `+54,+55` | `int16` | apparent-power limit, grid power | **Confirmed by vendor code** |
| `+57,+58,+59` | `int16` | system charge limit, discharge limit, PV power | **Confirmed by vendor code** |
| `+60` | `int16` | radiator temperature; no scale/offset applied | **Confirmed by vendor code** |

The prior map's system work-mode address `208` (`0x00D0`) is unsupported. The vendor decoder uses `0x0102` (258).

### IoT PCS: bases `0x1000` and `0x1060`

| Field group | Offset(s) | Decode / scale | Classification |
|---|---:|---|---|
| software version, status, run mode | `0x1000 +0..2` | packed version; low-byte enums | **Confirmed by vendor code** |
| DC/grid voltage | `+3,+4` | `int16 ×0.1 V` | **Confirmed by vendor code** |
| grid current/frequency | `+5,+6` | `int16 ×0.01 A/Hz` | **Confirmed by vendor code** |
| grid P/Q/S | `+7..+9` | `int16`, no scale | **Confirmed by vendor code** |
| inverter voltage/current/frequency | `+10..+12` | `×0.1 V`, `×0.01 A`, `×0.01 Hz` | **Confirmed by vendor code** |
| PCS P/Q/S | `+13..+15` | `int16`, no scale | **Confirmed by vendor code** |
| external grid current/power | `+16,+17` | `×0.01 A`, `int16` | **Confirmed by vendor code** |
| external PV current/power, load power | `+18..+20` | `×0.01 A`, `int16`, `int16` | **Confirmed by vendor code** |
| debug status/command, parameter version | `0x1060 +0..2` | low byte / packed version | **Confirmed by vendor code** |
| rated voltage/frequency, function selected, grid standard | `+3..+5` | low byte | **Confirmed by vendor code** |
| leakage current, ground voltage | `+6,+7` | `int16 ×0.1` | **Confirmed by vendor code** |
| internal temperature | `+8` | `int16`, no offset | **Confirmed by vendor code** |
| wave/fan/relay/LED/input/grid flags | `+9..+16` | low byte or `int16` exactly as decoded | **Confirmed by vendor code** |
| active/reactive power objectives | `+17,+18` | `int16`, no scale | **Confirmed by vendor code** |
| active/reactive current objectives | `+19,+20` | `int16 ×0.01 A` | **Confirmed by vendor code** |
| DC-voltage objective | `+21` | `int16 ×0.1 V` | **Confirmed by vendor code** |
| PF objective / matching PF | `+22,+23` | `int16 ×0.001` | **Confirmed by vendor code** |
| S/discharge/charge/reactive limits | `+24..+27` | `int16`, no scale | **Confirmed by vendor code** |
| radiator/inductor temperature | `+28,+29` | `int16`, no offset | **Confirmed by vendor code** |
| matching target state/off-grid target frequency | `+30,+31` | `int16`, no scale | **Confirmed by vendor code** |

Important correction: prior code labels `0x1001` (4097) “function selected.” In the IoT decoder it is PCS **status**. IoT `functionSelected` is `0x1064` (4196). A prior write to 4097 is therefore **Assumed** and must not be retained.

### IoT DCDC: bases `0x2000` and `0x2060`

| Field group | Offset(s) | Decode / scale | Classification |
|---|---:|---|---|
| version/status/run mode | `0x2000 +0..2` | packed version / low byte | **Confirmed by vendor code** |
| battery/output voltage | `+3,+4` | `int16 ×0.1 V` | **Confirmed by vendor code** |
| battery/branch currents | `+5..+8` | `int16 ×0.01 A` | **Confirmed by vendor code** |
| battery/branch powers | `+9..+12` | `int16`, no scale | **Confirmed by vendor code** |
| debug/version/temperatures | `0x2060 +0..4` | low byte / packed version / `int16` | **Confirmed by vendor code** |
| insulation voltage/frequency/voltage objective | `+5..+7` | `×0.1 V`, `×0.01 Hz`, `×0.1 V` | **Confirmed by vendor code** |
| power objective | `+8` | **low byte only** | **Confirmed by vendor code** |
| current objective | `+9` | **low byte only ×0.01** | **Confirmed by vendor code** |
| wave/fan/speed/LED | `+10..+13` | low byte or `int16` | **Confirmed by vendor code** |
| power limit/adapters/other inputs | `+15..+18` | `int16`, no scale | **Confirmed by vendor code** |

The low-byte-only objective decodes may be application defects. They describe what the application does, not a reliable firmware field definition.

### IoT BMS: base `0x5000`

| Offset | Decode / scale | Meaning | Classification |
|---:|---|---|---|
| `+0..+5` | packed version; low-byte status/run/charge state/enable; `int16` BIC count | identity/state/topology | **Confirmed by vendor code** |
| `+6,+7,+8` | `int16 ×0.1 V`, `int16 ×0.1 A`, `int16 W` | pack voltage/current/power | **Confirmed by vendor code** |
| `+9,+10` | `int16`, no scale | SOC, SOH (%) | **Confirmed by vendor code** |
| `+11,+12` | `int16 ×0.1 A` | charge/discharge current limits | **Confirmed by vendor code** |
| `+13,+14` | `int16 W` | charge/discharge power limits | **Confirmed by vendor code** |
| `+15..+18` | two low-word-first `uint32 ×0.1` | charge/discharge energy | **Confirmed by vendor code** |
| `+19,+20,+21` | cell number, raw mV, raw-40 °C | maximum-voltage cell | **Confirmed by vendor code** |
| `+22,+23,+24` | cell number, raw mV, raw-40 °C | minimum-voltage cell | **Confirmed by vendor code** |
| `+25,+26,+27` | cell number, raw-40 °C, raw mV | maximum-temperature cell | **Confirmed by vendor code** |
| `+28,+29,+30` | cell number, raw-40 °C, raw mV | minimum-temperature cell | **Confirmed by vendor code** |

Cell-voltage registers are raw millivolts; prior code's `×0.001 V` presentation is operationally consistent. Cell temperatures use `raw - 40 °C` where explicitly decoded.

### IoT cumulative energy: base `0x4101`

All six fields are low-word-first `uint32 ×0.1`: grid buy, grid sell, load consumption, PV production, BMS charge, and BMS discharge, in that order. This is **Confirmed by vendor code**.

### Legacy-specific corrections

| Fact | Classification | Correct interpretation |
|---|---|---|
| Legacy PCS energy order | **Confirmed by vendor code** | At fault-block offsets 48-57: charge, discharge, buy, sell, PV. All are high-word-first `uint32 ×0.1`. No legacy load-energy pair is decoded. |
| Legacy PCS inverter current | **Confirmed by vendor code** | The application reads array offset 12 again instead of the likely intended offset 18 (`SysControl.cs:950-959`). Treat as a vendor-application defect; actual field is **Unknown**. |
| Legacy BMS current/voltage/SOC | **Confirmed by vendor code** | Offsets 7/8 are `×0.1 A/V`; SOC at offset 10 is raw percent. Charge/discharge energies are high-word-first `×0.1`. |
| Legacy BECU SOC | **Confirmed by vendor code** | Offset 5 is `int16 ×0.01 %`. Currents are `×0.1 A`; temperatures use `raw - 40`; cell voltages are mV. |
| Legacy BECU energy/capacity | **Confirmed by vendor code** | Energy is high-word-first `×0.001`; capacities are high-word-first with no engineering scale established. |
| Legacy BECU balance acceptance | **Confirmed by vendor code** | It requests `BIC` words but compares response count with `becuNumber`, likely discarding valid data unless the counts happen to match (`SysControl.cs:1350-1369`). Firmware behavior is **Unknown**. |

### Scaling summary

| Quantity | Confirmed scale(s) |
|---|---|
| PCS and DCDC currents | `0.01 A` per count, except PCS leakage uses `0.1` and the DCDC detailed objectives have low-byte-only decoding |
| System, BMS, and BECU currents | `0.1 A` per count |
| Pack/grid/DC voltages | generally `0.1 V` per count where the decoder multiplies |
| Cell voltage | raw mV; display conversion may use `0.001 V` |
| Frequency | `0.01 Hz` per count |
| PF | `0.001` per count |
| IoT BMS and IoT total energy | `0.1` per count, low-word-first |
| Legacy PCS/BMS energy | `0.1` per count, high-word-first |
| Legacy BECU energy | `0.001` per count, high-word-first |
| Temperature | either raw signed value or `raw - 40 °C`; use the field-specific decoder, not a global rule |

## 7. PQ command and command renewal

| Fact | Classification | Evidence / precise contract |
|---|---|---|
| Full vendor PQ write | **Confirmed by vendor code** | FC16 to `0x0200`, three registers `[1, (ushort)(short)P, (ushort)(short)Q]` (V-WRITE). P and Q are signed 16-bit values transported as their two's-complement register bit patterns. |
| P/Q engineering scale | **Confirmed by vendor code** | The UI parses signed `short` values and labels P in W and Q in var; no multiplier is applied (V-PQ-UI). |
| Vendor application requires debug mode 0 before UI PQ dispatch. | **Confirmed by vendor code** | The send handler refuses the action when the `0x8100` debug readback is nonzero (`MiniESapp.cs:2179-2184`). |
| Vendor application proves PCS run mode 1 is required. | **Unknown** | The source defines PCS run mode 1 as “Remote PQ Power,” but the send handler does not check or change `PcsInfo.runMode`. Operational requirements may exist in firmware but are not proved here. |
| “Keep Send” default | **Confirmed by vendor code** | The checkbox is checked by default. When used, the application attempts a send every nominal 1000 ms through the shared mutex (`MiniESapp.cs:2191-2233, 4744-4752`). |
| Stop behavior | **Confirmed by vendor code** | Cancelling the keep-send loop is followed by a PQ write for P=0 and Q=0 (`MiniESapp.cs:2233-2236`). |
| The device has an approximately two-second lease/watchdog. | **Unknown** | Periodic renewal is confirmed, and the BMS warning dictionary includes `No Remote Dispatch`, but no vendor source defines an expiry interval. The user's and prior application's approximately two-second resend behavior is operational evidence, not a measured firmware contract. |
| Prior active-only write | **Corroborated operationally** | Prior integrations repeatedly write signed active power to PDU address `0x0201` (513), commonly every 2.0 or 1.5 seconds (P-CONTROL, P-FLEET). This writes only P and does not reproduce the vendor's full `[1,P,Q]` transaction. It needs byte capture and a controlled acceptance test before becoming normative. |
| Negative P means charging; positive P means discharging. | **Corroborated operationally** | Both prior implementations invoke charging with negative values and discharging with positive values. The vendor UI/source does not label either sign, and no raw measured trace was supplied. |
| External-grid power sign | **Unknown** | Prior code infers signs, but the vendor decoder only casts the register to `short`. Buy-positive/sell-negative is not proved. |
| Firmware fallback when renewals stop | **Unknown** | Prior use suggests autonomous grid-following resumes, but neither the device-side timeout nor fallback state is present in the inspected source. |
| `SendPQPower()` success return | **Confirmed by vendor code** | The method initializes `result=false` and never sets it true, even after a successful write (`SysControl.cs:1442-1460`). Callers ignore the return. A rewrite must validate the actual Modbus acknowledgement instead of copying this defect. |

No persistent charge/discharge override is established by the evidence. The least-assumptive model is a short-lived, renewable objective whose exact timeout must be measured without defeating the firmware fallback.

## 8. Debug / maintenance modes

The mode is written with FC16 to `0x8000` and read from `0x8100`. The UI immediately rereads after a selection. The names and values are confirmed; firmware-side behavior is not present in the application.

| Value | Vendor UI name | Classification of name/value | What the evidence proves about function |
|---:|---|---|---|
| 0 | Normal Mode | **Confirmed by vendor code** | Required by this UI before PQ dispatch. Exact autonomous strategy is **Unknown**. |
| 1 | Standby | **Confirmed by vendor code** | Label only; voltage, contactor, and trickle behavior are **Unknown**. |
| 2 | Charge | **Confirmed by vendor code** | Label only; power/current target, termination, and protections are **Unknown**. |
| 3 | Discharge | **Confirmed by vendor code** | Label only; power/current target, termination, and protections are **Unknown**. |
| 4 | Circulation | **Confirmed by vendor code** | Label only; whether this cycles charge/discharge and under what limits is **Unknown**. |
| 5 | Fixing SOC | **Confirmed by vendor code** | Label only. Calibration algorithm, prerequisites, duration, persistence, and interruption behavior are **Unknown**. It must not be exposed as a normal control. |
| 6 | Verify Capacity | **Confirmed by vendor code** | Label only. Test sequence, depth of discharge, termination, duration, and calibration effects are **Unknown**. It must not be exposed as a normal control. |

The source defines seven values, not eight independent debug values. “SOC” in prior discussion corresponds to **Fixing SOC**, not a separate eighth mode.

### Distinct mode/status fields

| Field | Confirmed meanings | Evidence note |
|---|---|---|
| System work mode at `0x0102` | 2 Economy, 6 Remote dispatch, 8 Timing | `GlobalFun.cs:167-175` |
| PCS run mode at IoT `0x1002` | 0 Matching Load, 1 Remote PQ Power, 2 Remote PF Power, 3 Remote PF Current, 4 Remote DC voltage | `GlobalFun.cs:178-188` |
| System control mode at `0x0101` | 1 Remote, 2 Local | `GlobalFun.cs:204-211` |
| PCS/system status | 0 Initialization, 1 Idle, 2 Standby, 3 On grid, 4 Off grid, 5 Fail, 6 Debugging | `GlobalFun.cs:224-236` |
| BMS status | 0 Initial, 1 Stop, 2 Starting, 3 Running, 4 Stopping, 5 Fail | `GlobalFun.cs:239-250` |

These enums must not be conflated with debug mode. In particular, prior `EssForceState` values written to address 512 are not the vendor debug-mode contract.

## 9. Faults and warnings

### Word locations

| Subsystem/layout | Warning words | Fault words | Classification |
|---|---|---|---|
| IoT PCS `0x1040`, 22 words | offsets 0-3 | offsets 16-21 | **Confirmed by vendor code** |
| Legacy PCS `0x1040`, 60 words | offsets 0-3 | offsets 16-21 | **Confirmed by vendor code** |
| IoT DCDC `0x2040`, 22 words | offsets 0-3 | offsets 16-21 | **Confirmed by vendor code** |
| Legacy DCDC `0x3040`, 22 words | offsets 0-3 | offsets 10-15 | **Confirmed by vendor code** |
| IoT BMS `0x5040`, 22 words | BMS offset 0; BECU0 offsets 1-2 | BMS offsets 16-17; BECU0 offsets 18-21 | **Confirmed by vendor code** |
| Legacy BMS `0x5040`, 34 words | BMS offset 0; BECU0-3 offsets 1-8 | BMS offsets 16-17; BECU0-3 offsets 18-33 | **Confirmed by vendor code** |
| Legacy per-BECU `0x6040 + 0x800i`, 20 words | offsets 0-1 | offsets 16-19 | **Confirmed by vendor code** |

Each is a 16-bit bitmask: bit 0 is least significant. The UI creates one `_Happen` event on a 0→1 transition and one `_Disappear` event on a 1→0 transition, retaining current and historical lists (V-FAULTS). Original logs contain both forms, operationally corroborating the lifecycle.

### Corrected key bit maps

The application loads descriptions from the bundled workbook. These mappings therefore describe what the supplied vendor application displays and logs.

| Word | Non-reserved bit meanings | Classification |
|---|---|---|
| BMS Fault0 | 0 No Battery BECU Available; 1 Start Fail; 2 Stop Fail; 3 PCS CAN disconnected; 4 DCDC CAN disconnected | **Confirmed by vendor code** |
| BMS Fault1 | bits 0-15: stack-to-BECU 0-15 CAN disconnected | **Confirmed by vendor code** |
| BMS Warning0 | 11 No Remote Dispatch; 12 No TCP Connection; 13 Electricity Meter Communication Disconnected; 14 DRED Communication Disconnected | **Confirmed by vendor code** |
| PCS Warning0 | 0 EE Configuration Parameter Out of Range; 1 EE Calibration Parameter Out of Range | **Confirmed by vendor code** |
| DCDC Warning0 | 0 EEPROM Configuration Parameter Out of Range; 1 EEPROM Calibration Parameter Out of Range | **Confirmed by vendor code** |
| BECU Warning0 | 0 high voltage; 1 low voltage; 2 high temperature; 3 low temperature; 4 charging over-current; 5 discharging over-current; 6 leakage; 7 BIC adaptive error | **Confirmed by vendor code** |
| PCS Fault1 | 0 inverter high voltage; 1 leakage current; 2 abnormal ground voltage; 3 overload; **4-13 sampling failures**; 14-15 reserved | **Confirmed by vendor code** |

The corrected BMS Fault0 map starts at bit 0 with “No Battery BECU Available”; it must not be shifted down. Logs corroborate `Stack_Fault0_0`, `_3`, and `_4`, including happen/disappear transitions (for example `SerialNo_BEP0005KXX11B10500029.log:1509-1559`).

The observed calibration events are also corroborated: many logs contain `PCS_Warning0_1 EE Calibration parameter Out of Range` and `DCDC_Warning0_1 EEPROM Calibration parameter Out of Range` (for example `SerialNo_BEP0005KXX11B10500055.log:1-3`). These labels do not establish root cause, severity, or safe reset procedure.

### Full non-reserved fault groups

| Subsystem / group | Non-reserved bits from workbook | Classification |
|---|---|---|
| PCS Fault0 | 0 high DC voltage; 1 EEPROM; 2 grid abnormal capture; 3 inverter abnormal capture; 4 hardware PDP; 5 solar DCDC shutdown; 6 software over-current; 7 low DC voltage; 8 DC disconnected; 9 zero drift; 10 temporary low grid voltage; 11 BMS CAN; 12 DCDC CAN; 13 solar CAN; 15 cabinet over-temperature | **Confirmed by vendor code** |
| PCS Fault1 | 0 inverter high voltage; 1 leakage; 2 abnormal ground voltage; 3 overload; 4 inverter-current sample; 5 grid-current sample; 6 grid-voltage sample; 7 busbar-voltage sample; 8 inverter-voltage sample; 9 external-solar-current sample; 10 external-grid-current sample; 11 ground-detection sample; 12 leakage-current sample; 13 cabinet-temperature sample | **Confirmed by vendor code** |
| PCS Fault3 | 0 grid power down; 1 low grid voltage; 2 high grid voltage; 3 low grid frequency; 4 high grid frequency | **Confirmed by vendor code** |
| DCDC Fault0 | 0 high DC voltage; 1 high battery current; 2 high DC1 input current; 3 high DC2 input current; 4 CPLD; 5 zero drift; 6 low battery-side voltage; 7 high battery-side voltage; 8 PDP; 9 BSMU CAN; 10 PCS CAN; 11 solar CAN; 14 EEPROM; 15 radiator over-temperature | **Confirmed by vendor code** |
| DCDC Fault1 | 0 DC2 inductor over-temperature; 1 DC1 inductor over-temperature; 4 battery-total-current sample; 5 branch-1-current sample; 6 branch-2-current sample; 7 battery-input-voltage sample; 8 output-voltage sample; 9 radiator-temperature sample; 10 DC2-inductor-temperature sample; 11 DC1-inductor-temperature sample | **Confirmed by vendor code** |
| BECU Fault0 | 0 BIC CAN disconnected; 1 BIC fault; 2 serious high voltage; 3 serious low voltage; 4 serious high temperature; 5 serious low temperature; 6 charge serious over-current; 7 discharge serious over-current; 8 charge current over limit; 9 discharge current over limit; 10 start fail/incomplete precharge; 11 stop fail/unsafe current; 12 serious leakage; 15 BSMU CAN disconnected | **Confirmed by vendor code** |
| BECU Fault2 | bits 0-15: BECU-to-BIC 0-15 CAN disconnected | **Confirmed by vendor code** |
| BECU Fault3 | bits 0-15: BIC 0-15 fault | **Confirmed by vendor code** |

Workbook entries marked reserved remain reserved. A new implementation should preserve raw words and bit numbers even when descriptions are unknown, rather than inventing meanings.

## 10. Cell-map constraints and defects

| Fact | Classification | Evidence / consequence |
|---|---|---|
| Vendor cell count formula | **Confirmed by vendor code** | IoT and legacy cell-voltage reads use `BIC×10`, capped at 100. |
| Prior deployment reads 59 cell registers. | **Corroborated operationally** | Multiple prior maps request 59 registers at `0x5200`. The reason for 59 rather than 60 is not documented. |
| Actual cell count per deployed unit | **Unknown** | No raw BIC-count response or device identity snapshot is in the evidence set. |
| IoT map is non-overlapping for six BICs / 60 cells. | **Confirmed by vendor code** | `0x5200 + 60 = 0x523C`, exactly the fixed temperature start; `BIC×3 = 18`, ending exactly at balance start `0x524E`. |
| IoT map remains valid above six BICs. | **Unknown** | For BIC > 6, the variable voltage read overlaps the fixed temperature area, and the temperature read overlaps the fixed balance area. The source permits up to 100 voltage registers but does not resolve this collision. |
| Balance-word bit encoding | **Unknown** | The application stores one raw word per BIC but does not decode its bits. |
| Reported 24 mV imbalance is a protocol threshold. | **Unknown** | The protocol exposes raw cell voltages; neither vendor code nor workbook defines a permitted imbalance threshold. The user's observed 0.024 V value is an operational observation, not a limit. |

## 11. Communication error behavior in the vendor application

| Fact | Classification | Evidence / caveat |
|---|---|---|
| Three consecutive failures disconnect. | **Assumed** | The counter reaches disconnected at 3 only when an exception reaches `SciExceptionDeal` (`SysControl.cs:460-468`). Short or malformed-length responses return false without incrementing it. |
| Successful poll resets failure count. | **Confirmed by vendor code** | A fully successful chain resets the counter to zero (V-POLL). |
| Split reads reliably preserve an earlier failure. | **Unknown** | Several methods perform multiple reads and overwrite `flag`; a later successful sub-read can mask an earlier failed sub-read. Do not copy this behavior. |
| Poll cadence | **Confirmed by vendor code** | Main loops gate on nominal 1000 ms intervals and busy-loop between gates. Actual cadence also includes every serialized read and timeout. |
| A 1000 ms command renewal is guaranteed with a 5000 ms transport timeout. | **Unknown** | Polls and writes contend on one mutex. A slow read can delay renewal beyond the nominal interval. |

## 12. Prior-attempt claims that must not become contracts

| Prior claim / implementation | Classification | Evidence-based disposition |
|---|---|---|
| Address 512 is a general “force state” register with values 1, 4, 5, 6. | **Assumed** | Vendor code identifies `0x0200` as the first word of the three-register PQ command and writes value 1 there. Vendor debug mode is written at `0x8000` with values 0-6. Reject the prior mapping. |
| `0x1001` is “function selected” and may be written. | **Assumed** | In the IoT decoder it is PCS status. Function selected is read at `0x1064`; the vendor application contains no write to `0x1001`. Reject. |
| System work mode is at decimal 208. | **Assumed** | Vendor source decodes it at `0x0102` / 258. Reject. |
| Exactly 59 cell voltages exist. | **Assumed** | Prior code reads 59; vendor code computes `BIC×10`. Require topology discovery and a captured response. |
| Registers 40120-40124 implement a safe passive-voltage control profile. | **Assumed** | No matching vendor read/write exists. Prior code also attempts values too large for a single 16-bit register. Exclude from the protocol until independently documented and captured. |
| Normal/standby/shutdown values from `EssForceState` are vendor modes. | **Assumed** | They conflict with the confirmed debug-mode table and were written to the PQ header address. Exclude. |
| All 32-bit values are high-word-first. | **Assumed** | Directly contradicted by RTU ID and IoT energy decoders. Use the field-specific table in section 6. |
| All currents use `0.01 A`. | **Assumed** | System, BMS, and BECU use `0.1 A`; PCS/DCDC generally use `0.01 A`. |
| The Waveshare endpoint is ordinary Modbus TCP. | **Assumed** | Prior working code explicitly selects the RTU framer over TCP. Framing must be explicit and byte-tested. |

## 13. Open uncertainties requiring new evidence

These are intentionally unresolved:

1. Exact firmware lease/watchdog timeout, allowed renewal jitter, and behavior after one or more missed renewals.
2. Exact autonomous fallback state after PQ renewal stops.
3. Whether full `[1,P,Q]` writes are required, or whether active-only `0x0201` writes are safely equivalent on the deployed firmware.
4. Active-power and reactive-power sign conventions verified against measured battery-side and grid-side power.
5. Whether PCS run mode 1 or system work mode 6 is a prerequisite, and how either is safely entered.
6. Reactive-power limits, apparent-power envelope, and whether Q control is supported at the site.
7. Per-unit identity, serial number, firmware versions, protocol layout, enable mask, BIC count, and actual cell count for `.11`, `.12`, and `.13`.
8. Exact Waveshare model/configuration, serial-side baud/parity settings, frame boundary behavior, idle timeout, and reconnect behavior.
9. Raw RTU-over-TCP request/response bytes for representative reads, full PQ write, active-only write, exception response, and timeout.
10. Meanings of reserved bits and whether maps differ by firmware version or layout.
11. Firmware semantics, entry conditions, targets, termination, persistence, and interruption behavior for Standby, Charge, Discharge, Circulation, Fixing SOC, and Verify Capacity debug modes.
12. Engineering unit for the system-history counters at `0x0103..0x0106`.
13. Actual legacy PCS inverter-current register (the application likely decodes the wrong offset).
14. IoT cell/temperature/balance layout for BIC counts greater than six.
15. Balance-word encoding and a defensible cell-imbalance safety threshold.
16. Meaning, severity, persistence, and vendor-approved remediation for PCS/DCDC calibration warnings.
17. Cause and validity handling for the observed large SOC jumps; the protocol source provides values but no plausibility or recalibration algorithm.
18. Whether another local or cloud controller writes the same objective registers and how competing writers can be detected.

## 14. Evidence-gated acceptance requirements for future work

This section is not an implementation design; it defines what evidence would be needed to promote uncertain facts:

| Claim to promote | Minimum new evidence |
|---|---|
| Waveshare framing and address contract | Byte-level capture of request and valid response for each unit, including CRC and slave 4 |
| Power direction | Low-power controlled test with command bytes plus simultaneous BMS/PCS/grid measurements |
| Lease duration and fallback | Timestamped renewal-stop test with objective readback and measured power, repeated per unit |
| Unit identity/layout | Read-only identity, firmware, `0x5000` probe, BIC count, and cell-range snapshot for each configured endpoint |
| Fault mapping | Injected or naturally observed raw word correlated with application log and workbook bit description |
| Active-only command equivalence | Side-by-side trace and measured behavior for vendor full write versus `0x0201`-only write |
| Any debug/maintenance mode | Official service documentation plus controlled, isolated procedure approved for that mode |

Until those gates are met, the only control facts suitable for implementation are the vendor application's full PQ frame shape, signed 16-bit encoding, normal-debug-mode precondition used by the UI, and explicit zero-on-stop behavior. Even those require a non-actuating byte-level simulator test before any live write.
