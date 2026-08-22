"""Diagnostic: reproduce the live-control stall and dump every task stack.

Composes the write-enabled runtime against the REAL gateways (operator-
authorized live commissioning), drives arm + charge through the facade, and
when telemetry goes stale (the observed stall signature) prints the await
point of every asyncio task plus the supervision halt state, then fences
everything and exits. No HTTP serving — the facade and supervision run
directly, so their state is inspectable in-process.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from energypod.runtime import composition
from energypod.runtime.config import ControllerConfig

UNIT = "mid"
STALL_AFTER_S = 12.0
CHARGE_W = 500
TTL_S = 180


async def main() -> None:
    import pathlib

    import yaml

    from energypod.runtime.credentials import FileCredentialStore

    payload = yaml.safe_load(
        pathlib.Path("config/config.live-write-example.yaml").read_text(encoding="utf-8")
    )
    config = ControllerConfig.model_validate(payload)
    store = FileCredentialStore(pathlib.Path("var/live-credentials.json"))
    runtime = composition.build_runtime(config, credential_store=store)
    supervision = composition._LAST_SUPERVISION
    assert supervision is not None
    principal = type(
        "P",
        (),
        {
            "subject": "operator:diagnostic",
            "scopes": frozenset(
                {"observe", "audit:read", "dispatch", "arm", "stop", "stop:acknowledge"}
            ),
            "interactive": True,
            "site_id": "home",
        },
    )()
    t0 = time.monotonic()

    def log(event: str) -> None:
        print(f"[{time.monotonic() - t0:7.2f}] {event}", flush=True)

    async def watch_for_stall() -> None:
        last_fresh = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            try:
                observation = await runtime.observations.latest(UNIT)
            except Exception as error:
                log(f"observation read failed: {error!r}")
                continue
            if observation is None:
                continue
            age = time.monotonic() - (
                observation.captured_at_mono
                if observation.captured_at_mono > 1_000_000
                else observation.captured_at_mono - runtime.clock.monotonic() + time.monotonic()
            )
            # captured_at_mono is on the process-relative clock; compare like with like
            age = max(0.0, runtime.clock.monotonic() - observation.captured_at_mono)
            if age < 5.0:
                last_fresh = time.monotonic()
                continue
            stale_for = time.monotonic() - last_fresh
            if stale_for >= STALL_AFTER_S:
                log(f"STALL DETECTED (telemetry age {age:.1f}s, stale {stale_for:.1f}s)")
                log(f"supervision stopped={supervision._stopped} tasks={len(supervision._tasks)}")
                for report_name, report in getattr(supervision, "_start_reports", {}).items():
                    log(f"  start[{report_name}] done={report.done()}")
                for task in asyncio.all_tasks():
                    if task is asyncio.current_task():
                        continue
                    stack = task.get_stack(limit=4)
                    frames = "".join(
                        f"\n      at {frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}"
                        for frame in reversed(list(stack))
                    )
                    log(
                        f"  task {task.get_name()!r} done={task.done()} "
                        f"cancel={task.cancelling()}{frames}"
                    )
                evidence = getattr(supervision, "halt_evidence", None)
                if evidence:
                    log("HALT EVIDENCE (first failing supervision task):")
                    for line in evidence.splitlines():
                        log(f"    {line}")
                for name, actor in runtime.actors.items():
                    log(
                        f"  actor {name}: lifecycle={actor.lifecycle} stopping={actor._stopping}"
                        f" owner={actor._owner and actor._owner.done()}"
                    )
                return

    watcher = asyncio.create_task(watch_for_stall())
    await supervision.start()
    log("supervision started")

    await asyncio.sleep(8.0)
    log("arming")
    arm = await runtime.facade.arm(
        principal=principal, unit_ids=[UNIT], idempotency_key="diag-arm-1", request_id="diag-1"
    )
    log(f"arm -> {arm}")
    dispatch = await runtime.facade.submit_intent(
        principal=principal,
        unit_ids=[UNIT],
        direction="charge",
        watts=CHARGE_W,
        ttl_s=TTL_S,
        reason="stall diagnostic",
        idempotency_key="diag-charge-1",
        request_id="diag-2",
    )
    log(f"dispatch -> {dispatch.get('intent_id')}")

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(watcher, timeout=STALL_AFTER_S + 90.0)
    watcher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watcher

    log("fencing down")
    await runtime.facade.emergency_stop(
        principal=principal,
        unit_ids=[UNIT],
        reason="stall diagnostic teardown",
        idempotency_key="diag-stop-1",
        request_id="diag-3",
    )
    await supervision.stop()
    log("done")


if __name__ == "__main__":
    asyncio.run(main())
