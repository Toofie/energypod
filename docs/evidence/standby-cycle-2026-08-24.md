# Live standby-cycle evidence — 2026-08-24 (rhs)

Operator-authorized rung R3 execution (POD_RECOVERY_RESEARCH §R3), session
8645b050. First-ever live writes to the vendor debug-mode register on this
fleet. Subject unit: **rhs** (Right Pod, gateway 192.168.1.12:4196, device 4,
wire identity byd-2c225076, 50 cells, SOC 98% at start). Controller: live
write-enabled build (pre-366abe1), disarmed throughout; the controller's own
polling continued independently and cross-validated every observation.

Tooling: `var/r3_standby_cycle_20260824.py` (pymodbus direct, RTU framing,
fresh socket per stage, 0.1 s inter-frame gaps, FC16 echo validation, readback
at 0x8100, value whitelist {0,1}); stage log `var/r3_standby_cycle_20260824.log`.

## Observed transition table

| Time (local) | Operation | Request | Readback (0x8100) | Observed effect |
|---|---|---|---|---|
| 10:42:16 | dry read | FC03 0x8100, 0x0100×3, 0x1000×3 | 0 (Normal) | ctrl_mode 1 (Remote), work_mode 2, run_mode 0 (Matching Load) — matches controller decode exactly |
| 10:42:25 | no-op self-write | FC16 0x8000 ← [0] | 0 (unchanged) | write path proven: FC16 echo ACK (addr/count/dev match), readback stable |
| 10:42:37 | STANDBY | FC16 0x8000 ← [1] | **1 within ~2 s** | see standby observations below |
| 10:43:00–10:43:40 | standby observation (4 API samples, 10 s apart) | — | 1 throughout | measured −16 → +66 → +132 → **0.0 W** (CT-following contribution ramped off, I = 0.00 A); pack V steady 166.1; SOC steady 98; telemetry age ≤1.1 s; zero faults; controller decode showed debug_mode_w = 1 (independent cross-validation); health reason autonomous_self_charge cleared |
| 10:43:55 | NORMAL (exit) | FC16 0x8000 ← [0] | **0 within ~1 s** | no wedge, no retry needed |
| 10:44:00–10:44:30 | recovery observation (4 samples) | — | 0 throughout | CT-following float resumed (66 → 0 → 33 → 99 W), identical profile to pre-test baseline (~82 W avg); no faults; run_mode 0 |

## Standing facts established

1. **0x8000 accepts FC16 writes on this firmware** (echo-validated ACK; the
   write path is real, not silently ignored — proven by the 0→1→0 readback
   transitions).
2. **Standby (1) is a benign soft-park**: PCS stops participating (power → 0),
   while comms, telemetry, pack voltage, and SOC reporting stay alive. No
   contactor drop is implied or observed; the pack stays connected at full
   voltage (~166 V) — standby is NOT electrical isolation.
3. **The exit write (← 0) reliably returns the pod to Normal** — readback 0
   in ~1 s, autonomy resumes immediately, no wedge, no vendor-side
   auto-normalization involved (verified absent in the vendor decompile;
   every mode change is an explicit write).
4. **A parked pod still ACKs register writes** (the standby-stage writes and
   reads succeeded while parked; the controller's control-rate polling
   continued uninterrupted) — the ACK-then-ignore device model, adopted by
   the simulator in DESIGN_POD_PARKING §6.

## Companion actuation legs (same session, rhs, sanctioned API path)

Baseline and post-cycle 1,000 W discharge legs (arm → intent discharge 1000 W
→ 120 s → cancel → disarm) both completed cleanly: authorized 1,000 W within
~10 s, held through the window, watchdog handback ≤ ~10 s, clean disarm.
Systematic measured overshoot in both legs: settled 1,059–1,257 W (means
1,158 / 1,162 W, +15–16% vs command) — unchanged by the register cycle
(pre-existing bias; filed in docs/DEFERRED_FINDINGS.md with measurement plan).
Post-cycle leg armed with `sole_writer` classification — the register cycle
left no foreign objective residue.

## Classifications this evidence supports amending

(The amendments land with the parking round — DESIGN_POD_PARKING §11 lists
every site; until then the docs of record still read "label only," and this
file makes no claim they have changed.)

- PROTOCOL_EVIDENCE §8 rows for values 0 and 1: "label only" → live-observed
  on this fleet (enter, park behavior, exit). Values 2–6 remain label-only
  and permanently unexposed.
- POD_RECOVERY_RESEARCH R3: hypothesis → executed and verified; the
  asymmetric-failure concern (wedge-on-exit) did not materialize, though it
  remains the designed-for failure (park_readback_unverified).
