"""Commissioning trial: live power-direction confirmation (single unit, 200 W).

Authorized by the operator 2026-08-22 ("just implement it") after the
three-source sign reconciliation (PROTOCOL_EVIDENCE section 4b). This is the
first write issued by this codebase.

Safety envelope:
- ONE unit (MID, SOC ~10%: charging is the benign direction), 200 W charge
  (~2.6% of its 7696 W charge limit), single-shot, no renewal.
- ABORT before writing if: debug-mode readback nonzero, the unit's PQ
  objective readback is nonzero (competing writer), or charge limit < 500 W.
- Explicit stop [1,0,0] at the end; post-stop verification; everything
  logged (raw frames) to docs/evidence/live-direction-trial-2026-08-22.json.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from energypod.adapters.modbus import (
    RegisterCatalog,
    WaveshareTransport,
    WaveshareTransportConfig,
    encode_pq_registers,
    encode_stop_registers,
    protocol_codec,
)

HOST = "192.168.1.11"
TRIAL_WATTS = -200
OBSERVE_SECONDS = 20.0
POLL_INTERVAL_S = 0.5
OUT = Path("docs/evidence/live-direction-trial-2026-08-22.json")

CATALOG = RegisterCatalog()


async def main() -> None:
    config = WaveshareTransportConfig(
        host=HOST, port=4196, device_id=4, timeout_s=3.0, inter_request_delay_s=0.1
    )
    transport = WaveshareTransport(config=config)
    journal: list[dict[str, object]] = []
    t0 = time.monotonic()

    def log(event: str, **data: object) -> None:
        journal.append({"t": round(time.monotonic() - t0, 3), "event": event, **data})
        print(f"[{journal[-1]['t']:7.3f}] {event} {data if data else ''}")

    async def read(address: int, count: int) -> tuple[int, ...]:
        regs = await transport.read_holding(address, count)
        log("read", address=hex(address), count=count, registers=list(regs))
        return regs

    await transport.connect()
    log("connected", host=HOST)
    try:
        # --- pre-flight: state that gates the write -------------------------
        debug_mode = (await read(0x8100, 1))[0]
        pcs_detail = await read(0x1060, 32)
        objective_p = protocol_codec.decode_signed16(pcs_detail[17])
        objective_q = protocol_codec.decode_signed16(pcs_detail[18])
        bms = await read(0x5000, 31)
        battery_w = protocol_codec.decode_signed16(bms[8])
        soc = bms[9]
        charge_limit = protocol_codec.decode_signed16(bms[13])
        system = await read(0x0100, 61)
        log(
            "baseline",
            debug_mode=debug_mode,
            objective_p=objective_p,
            objective_q=objective_q,
            battery_w=battery_w,
            soc=soc,
            charge_limit_w=charge_limit,
            system_status_word=system[0],
        )

        if debug_mode != 0:
            log("ABORT", reason="debug mode active (vendor precondition for PQ violated)")
            return
        if objective_p != 0 or objective_q != 0:
            log("ABORT", reason="competing writer: nonzero PQ objective already active")
            return
        if charge_limit < 500:
            log("ABORT", reason="charge limit too low for a safe trial", charge_limit=charge_limit)
            return

        # --- the trial: one write, then observation --------------------------
        frame = encode_pq_registers(TRIAL_WATTS, 0)
        log("WRITE objective", frame=list(frame), meaning=f"{TRIAL_WATTS} W (expected CHARGE)")
        await transport.write_registers(0x0200, frame)

        applied_at: float | None = None
        reverted_at: float | None = None
        samples: list[dict[str, object]] = []
        while (time.monotonic() - t0) < OBSERVE_SECONDS:
            await asyncio.sleep(POLL_INTERVAL_S)
            bms = await read(0x5000, 31)
            pcs_detail = await read(0x1060, 32)
            power = protocol_codec.decode_signed16(bms[8])
            current = round(protocol_codec.decode_signed16(bms[7]) * 0.1, 2)
            soc_now = bms[9]
            obj = protocol_codec.decode_signed16(pcs_detail[17])
            samples.append(
                {
                    "t": round(time.monotonic() - t0, 3),
                    "battery_w": power,
                    "pack_current_a": current,
                    "soc": soc_now,
                    "objective_readback_w": obj,
                }
            )
            if applied_at is None and power <= -50:
                applied_at = time.monotonic() - t0
                log("OBJECTIVE APPLIED", battery_w=power, pack_current_a=current)
            if applied_at is not None and reverted_at is None and abs(power) <= 10:
                reverted_at = time.monotonic() - t0
                log("OBJECTIVE REVERTED (watchdog?)", battery_w=power)
                break

        # --- explicit stop ----------------------------------------------------
        stop = encode_stop_registers()
        log("WRITE stop", frame=list(stop))
        await transport.write_registers(0x0200, stop)
        await asyncio.sleep(1.5)
        final_bms = await read(0x5000, 31)
        final_power = protocol_codec.decode_signed16(final_bms[8])
        log("post-stop", battery_w=final_power)

        verdict = {
            "objective_applied": applied_at is not None,
            "applied_at_s": applied_at,
            "watchdog_reverted": reverted_at is not None,
            "watchdog_expiry_observed_s": (
                round(reverted_at - applied_at, 2)
                if applied_at is not None and reverted_at is not None
                else None
            ),
            "commanded_w": TRIAL_WATTS,
            "observed_battery_w_range": [
                min(s["battery_w"] for s in samples),  # type: ignore[type-var]
                max(s["battery_w"] for s in samples),  # type: ignore[type-var]
            ],
            "post_stop_battery_w": final_power,
            "direction_confirmed_charge_negative": applied_at is not None,
        }
        log("VERDICT", **verdict)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(
            json.dumps(
                {
                    "when": datetime.now(UTC).isoformat(),
                    "unit": "MID",
                    "host": HOST,
                    "journal": journal,
                    "samples": samples,
                    "verdict": verdict,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"written: {OUT}")
    finally:
        await transport.close()
        log("disconnected")


if __name__ == "__main__":
    asyncio.run(main())
