"""Capstone end-to-end scenario: the composed simulate runtime, driven as an
operator drives it (API_CONTRACTS "Operations surface (Milestone C)" and
"Runtime composition and entry point").

The whole controller is booted through ``energypod.main.main`` with an injected
server runner, exactly like the CLI contract suite does — no port is ever bound
and no hardware is touched.  The only production seam the test wraps is the
composition point (a recording pass-through over ``build_runtime``, the same
spy the main-entry suite uses), and the only drivable handles it reads beyond
the served HTTP surface are the ones the composition contract already pins:
``runtime.app`` (handed to the runner) and ``runtime.simulators`` (the
per-unit simulator scenario handles).

Everything else travels the public boundary:

- ``GET /healthz`` is the one unauthenticated liveness endpoint;
- ``energypod simulate`` prints one development bearer token to stdout at
  startup, and that token is the credential for the whole scenario;
- ``TestClient`` drives the application lifespan, so real supervision (kernel
  tick loop, per-unit actor loops) runs at the commissioned cadence under the
  ambient clock — the scenario waits in bounded wall-clock windows instead of
  stepping time manually;
- the applied setpoint is observed where the device model latched it (the
  evidenced ``0x0200`` objective surfaced in the PCS detail block), never in
  controller-internal state;
- the durable audit trail is read back through ``GET /api/v1/audit``; and
- leaving the ``TestClient`` context stops supervision, whose shutdown contract
  drives the simulated fleet to zero.
"""

from __future__ import annotations

import importlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from energypod.adapters.modbus.protocol_codec import decode_signed16
from energypod.runtime.config import ControllerConfig

SITE_ID = "e2e-home"
UNIT_ID = "pod-e2e"
UNIT_IDENTITY = "SIM-POD-E2E-0007"

# Commissioning numbers shared with the golden scenarios.  The timing block
# satisfies the cross-validated control budget; the ramp limit makes one
# heartbeat's allowance 300 W, so the 250 W dispatch is inside every safety
# bound (static, dynamic, fleet, ramp) and must be authorized in full.
CONTROL_PERIOD_S = 0.40
STABLE_SAMPLES_TO_REARM = 2
DISPATCH_W = 250
INTENT_TTL_S = 20.0

# Bounded wall-clock windows for supervision to make progress under the
# ambient clock.  Healthy runs need roughly (samples + a few ticks) * period
# (about two seconds); the bounds only exist so a broken runtime fails the
# test instead of hanging the suite.
QUALIFICATION_TIMEOUT_S = 30.0
APPLIED_TIMEOUT_S = 30.0
SHUTDOWN_ZERO_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.05

# The applied PQ objective word (PCS detail block, 0x1060 + 17) and the
# measured battery-power word (system block, 0x0100 + 20) — both int16 W on
# the evidenced wire convention (positive discharge, negative charge).
_PCS_DETAIL_BASE = 0x1060
_APPLIED_ACTIVE_OFFSET = 17
_SYSTEM_BASE = 0x0100
_MEASURED_ACTIVE_OFFSET = 20

# A credential-shaped candidate on a stdout line that mentions a token: a
# contiguous run of URL-safe characters, long enough that no plausible
# English word on such a line can match.
_CREDENTIAL_RUN = re.compile(r"[A-Za-z0-9._~+-]{20,}")
_CREDENTIAL_PREFIXES = ("token", "bearer", "credential")


def _load_main() -> Any:
    try:
        return importlib.import_module("energypod.main")
    except ImportError as error:
        pytest.fail(f"entry-point contract is not implemented: energypod.main: {error}")


def _timing_payload() -> dict[str, Any]:
    return {
        "device_command_expiry_s": 2.35,
        "device_command_expiry_evidence": "commissioning://e2e-watchdog-trial-2026-08/rev-1",
        "control_period_s": CONTROL_PERIOD_S,
        "essential_read_timeout_s": 0.10,
        "kernel_timeout_s": 0.05,
        "audit_timeout_s": 0.05,
        "write_timeout_s": 0.10,
        "acknowledgement_timeout_s": 0.10,
        "maximum_jitter_s": 0.10,
        "renewal_margin_s": 0.50,
    }


def _config_payload(database: Path) -> dict[str, Any]:
    # Simulator-deployment shape (API_CONTRACTS "Operations surface"): the
    # site declares observe-only intent, names no credential store (so the
    # simulate-only development principal is minted), and carries the training
    # policy the safety chain needs for a realistic dispatch — a configuration
    # that named a credential store would stay fail-closed even in simulate.
    return {
        "schema_version": 1,
        "revision": 5,
        "mode": "observe_only",
        "site": {
            "site_id": SITE_ID,
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 1,
        },
        "units": [
            {
                "unit_id": UNIT_ID,
                "display_name": "E2E Pod",
                "endpoint": {"host": "192.168.1.11", "port": 4196},
                "transport_profile": "waveshare_rtu_over_tcp",
                "protocol_profile": "iot",
                "device_id": 4,
                "expected_identity": UNIT_IDENTITY,
                "expected_cell_count": 60,
            }
        ],
        "timing": _timing_payload(),
        "policy": {
            "version": 3,
            "threshold_provenance": "e2e://commissioning-baseline-2026-08",
            "max_fleet_charge_w": 3000,
            "max_fleet_discharge_w": 3000,
            "max_unit_charge_w": 3000,
            "max_unit_discharge_w": 3000,
            "minimum_soc_pct": 5.0,
            "maximum_soc_pct": 95.0,
            "minimum_cell_v": 2.80,
            "maximum_cell_v": 3.65,
            "maximum_cell_imbalance_v": 0.50,
            "minimum_temperature_c": -20.0,
            "maximum_temperature_c": 60.0,
            "maximum_soc_difference_pct": 5.0,
            "maximum_soc_jump_pct": 5.0,
            "maximum_telemetry_age_s": 2.0,
            "maximum_cell_data_age_s": 10.0,
            "authorization_lifetime_s": 1.2,
            "ramp_limit_w_per_s": 750,
            "stable_samples_to_rearm": STABLE_SAMPLES_TO_REARM,
            "reactive_power_limit_var": 0,
            "blocking_fault_codes": ["Stack_Fault0_3"],
            "debug_modes_enabled": False,
        },
        # Simulate mode forces in-memory persistence regardless of this path;
        # keeping a path configured proves the contract rather than vacuously
        # omitting the store.
        "storage": {"database_path": str(database), "busy_timeout_ms": 250},
    }


def _write_config(path: Path) -> None:
    payload = _config_payload(path.parent / "never-opened.sqlite3")
    # Round-trip through the strict model first: the scenario must fail on a
    # configuration the product itself would refuse, not on hand-written YAML.
    config = ControllerConfig.model_validate(payload)
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")


class _Completed:
    """Zero-value awaitable: the injected runner may be called or awaited."""

    def __await__(self) -> Any:
        yield
        return None


@dataclass
class _CompositionSpy:
    """Recording pass-through over the only composition point."""

    runtimes: list[Any] = field(default_factory=list)

    def wrap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        composition = importlib.import_module("energypod.runtime.composition")
        real = composition.build_runtime
        spy = self

        def recording_build(*args: Any, **kwargs: Any) -> Any:
            runtime = real(*args, **kwargs)
            spy.runtimes.append(runtime)
            return runtime

        monkeypatch.setattr(composition, "build_runtime", recording_build)


def _boot_simulate(
    path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any, str]:
    """Run ``energypod simulate`` to completion with a capturing runner.

    Returns the composed runtime, the composed ASGI application, and the whole
    stdout the command produced.
    """
    entry = _load_main()
    spy = _CompositionSpy()
    spy.wrap(monkeypatch)
    served: list[Any] = []

    def runner(app: Any, *, host: str, port: int) -> _Completed:
        served.append(app)
        return _Completed()

    capsys.readouterr()
    exit_code = entry.main(["simulate", str(path)], server_runner=runner)
    captured = capsys.readouterr()
    assert exit_code == 0, (
        f"energypod simulate must exit zero with an injected runner: "
        f"exit={exit_code} stderr={captured.err!r}"
    )
    assert len(served) == 1, "the runner must receive exactly one composed application"
    assert spy.runtimes, "simulate must compose through energypod.runtime.composition.build_runtime"
    return spy.runtimes[0], served[0], captured.out


def _extract_dev_token(stdout: str) -> str:
    """The single development bearer credential printed to stdout.

    ``energypod simulate`` prints the development principal's token once at
    startup.  The line spelling is not contracted, so the credential is found
    structurally: on lines that mention a token, the credential-shaped run
    (URL-safe characters, at least twenty of them, with any ``token=``-style
    prefix stripped).  Exactly one candidate may exist.
    """
    candidates: set[str] = set()
    for line in stdout.splitlines():
        if "token" not in line.lower():
            continue
        for run in _CREDENTIAL_RUN.findall(line):
            candidate = run
            if "=" in candidate:
                candidate = candidate.rsplit("=", 1)[1]
            lowered = candidate.lower()
            if any(lowered.startswith(prefix) for prefix in _CREDENTIAL_PREFIXES):
                continue
            if len(candidate) >= 20:
                candidates.add(candidate)
    if not candidates:
        pytest.fail(
            "energypod simulate must print its development bearer token to stdout "
            f"at startup; stdout was {stdout!r}",
            pytrace=False,
        )
    if len(candidates) > 1:
        pytest.fail(
            f"the development token line is ambiguous: {sorted(candidates)!r} in {stdout!r}",
            pytrace=False,
        )
    token = candidates.pop()
    assert stdout.count(token) == 1, "the development token must be printed exactly once"
    return token


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    description: str,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"timed out after {timeout_s}s waiting for {description}", pytrace=False)


def _wait_until_value(
    read: Callable[[], Any],
    *,
    expected: Any,
    timeout_s: float,
    description: str,
) -> Any:
    deadline = time.monotonic() + timeout_s
    latest: Any = None
    while time.monotonic() < deadline:
        latest = read()
        if latest == expected:
            return latest
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"timed out after {timeout_s}s waiting for {description} "
        f"(expected {expected!r}, last saw {latest!r})",
        pytrace=False,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mutation_headers(token: str, key: str) -> dict[str, str]:
    return {**_bearer(token), "Idempotency-Key": key}


def _applied_active_w(pod: Any) -> int:
    """The objective the device model latched, read from served registers."""
    word = pod.read(_PCS_DETAIL_BASE + _APPLIED_ACTIVE_OFFSET, 1)[0]
    return decode_signed16(word)


def _measured_active_w(pod: Any) -> int:
    word = pod.read(_SYSTEM_BASE + _MEASURED_ACTIVE_OFFSET, 1)[0]
    return decode_signed16(word)


def _unit_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    units = snapshot["units"]
    assert len(units) == 1, f"the single-unit site must serve one unit view: {units!r}"
    return units[0]


def _audit_events(client: TestClient, token: str) -> list[dict[str, Any]]:
    response = client.get("/api/v1/audit", params={"limit": 200}, headers=_bearer(token))
    assert response.status_code == 200, response.text
    page = response.json()
    assert isinstance(page["events"], list)
    return list(page["events"])


def test_simulate_runtime_serves_an_authenticated_dispatch_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "controller.yaml"
    _write_config(config_path)

    runtime, app, stdout = _boot_simulate(config_path, capsys, monkeypatch)
    assert runtime.simulators is not None, "simulate mode must expose simulator scenario handles"
    pod = runtime.simulators[UNIT_ID]
    token = _extract_dev_token(stdout)

    with TestClient(app) as client:
        # --- the one unauthenticated endpoint ----------------------------
        healthz = client.get("/healthz")
        assert healthz.status_code == 200, healthz.text
        healthz_body = healthz.json()
        for forbidden in ("units", "service_readiness", "control_readiness"):
            assert forbidden not in healthz_body, (
                f"/healthz is liveness only and must never serve readiness or "
                f"fleet data: {healthz_body!r}"
            )

        # --- fail-closed authentication ----------------------------------
        anonymous = client.get("/api/v1/snapshot")
        assert anonymous.status_code == 401, anonymous.text
        assert anonymous.json()["code"] == "authentication_required"
        wrong = client.get("/api/v1/snapshot", headers=_bearer("not-the-development-token"))
        assert wrong.status_code == 401, wrong.text

        # --- the development principal reaches the guarded surface -------
        snapshot = client.get("/api/v1/snapshot", headers=_bearer(token))
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert body["site_id"] == SITE_ID
        assert _unit_view(body)["unit_id"] == UNIT_ID

        health = client.get("/api/v1/health", headers=_bearer(token))
        assert health.status_code == 200, health.text
        three_fact = health.json()
        assert set(three_fact) >= {"liveness", "service_readiness", "control_readiness"}

        # --- supervision qualifies the simulated unit on its own ---------
        _wait_until(
            lambda: _unit_view(client.get("/api/v1/snapshot", headers=_bearer(token)).json())[
                "lifecycle"
            ]
            == "disarmed",
            timeout_s=QUALIFICATION_TIMEOUT_S,
            description=f"{UNIT_ID} to qualify to DISARMED through supervised polling",
        )

        # --- arm: full scopes, interactive, per-unit outcome -------------
        arm = client.post(
            "/api/v1/arm",
            json={"unit_ids": [UNIT_ID], "confirmation": "ARM"},
            headers=_mutation_headers(token, "e2e-arm-0001"),
        )
        assert arm.status_code == 200, arm.text
        assert {unit["unit_id"]: unit["status"] for unit in arm.json()["units"]} == {
            UNIT_ID: "armed"
        }, arm.text

        # --- dispatch: acceptance grants nothing yet ---------------------
        dispatch = client.post(
            "/api/v1/intents",
            json={
                "unit_ids": [UNIT_ID],
                "direction": "discharge",
                "watts": DISPATCH_W,
                "ttl_s": INTENT_TTL_S,
                "reason": "e2e capstone dispatch",
            },
            headers=_mutation_headers(token, "e2e-intent-0001"),
        )
        assert dispatch.status_code == 202, dispatch.text
        accepted = dispatch.json()
        assert accepted["status"] == "accepted", accepted
        assert accepted["requested"] == {"direction": "discharge", "watts": DISPATCH_W}
        assert accepted["authorized"] is None, "acceptance never grants authority"
        intent_id = accepted["intent_id"]

        # --- the supervised tick carries it to the simulated pod ---------
        _wait_until(
            lambda: _applied_active_w(pod) == DISPATCH_W,
            timeout_s=APPLIED_TIMEOUT_S,
            description=(
                f"the supervised kernel tick and actor heartbeat to apply the "
                f"{DISPATCH_W} W discharge objective to the simulated pod"
            ),
        )
        # Measured telemetry follows the applied objective (deterministic and
        # directionally correct, the simulator contract).
        assert _measured_active_w(pod) == DISPATCH_W

        # The served snapshot reports the measured power the device serves.
        measured = _wait_until_value(
            lambda: _unit_view(client.get("/api/v1/snapshot", headers=_bearer(token)).json())[
                "measured_watts"
            ],
            expected=DISPATCH_W,
            timeout_s=APPLIED_TIMEOUT_S,
            description="the fleet snapshot to report the dispatched measured watts",
        )
        assert measured == float(DISPATCH_W)

        # --- the durable audit trail records the whole authority chain ----
        events = _audit_events(client, token)
        by_type = {}
        for event in events:
            by_type.setdefault(event["event_type"], []).append(event)
        accepted_events = by_type.get("intent_accepted", [])
        assert any(event.get("intent_id") == intent_id for event in accepted_events), (
            f"the accepted dispatch {intent_id} must be audited: "
            f"{[event['event_type'] for event in events]!r}"
        )
        decisions = by_type.get("control_decision", [])
        authorized = [
            event
            for event in decisions
            if event.get("intent_id") == intent_id
            and event.get("authorized_active_w") == DISPATCH_W
        ]
        decision_summary = [
            (event.get("intent_id"), event.get("authorized_active_w")) for event in decisions
        ]
        assert authorized, (
            "a control_decision audit fact must record the granted authority for "
            f"{intent_id} at {DISPATCH_W} W; saw {decision_summary!r}"
        )

    # --- leaving the client stopped supervision: bounded zero, then quiet --
    _wait_until(
        lambda: _applied_active_w(pod) == 0,
        timeout_s=SHUTDOWN_ZERO_TIMEOUT_S,
        description="supervision shutdown to drive the simulated pod to zero",
    )
