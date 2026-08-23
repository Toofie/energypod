"""Contract tests for the runtime composition root (ADR-0003 D1, D4, D6).

``energypod.runtime.composition.build_runtime`` is the only place that may
construct the whole object graph, and boot must be observe-only and disarmed
even when durable stores remember prior authority. The production module is
loaded lazily so this red-phase suite collects before the implementation
exists; every missing contract surfaces as an ordinary test failure, never as
a collection error. No test opens a socket, contacts hardware, or reads
ambient time: construction is the behavior under test, and simulator-backed
actors are the only things explicitly started.

Repository handles are driven through ``_settle`` because the composition may
lawfully expose the shipped synchronous stores; the suite never awaits a
synchronous attribute.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import itertools
import json
import math
import re
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from pydantic import ValidationError
from starlette.routing import WebSocketRoute

from energypod.adapters.persistence.sqlite import SQLiteAuditRepository, SQLiteScheduleRepository
from energypod.application.actor import EnergyPodActor
from energypod.application.audit import AuditEventFactory
from energypod.application.control_kernel import ControlKernel
from energypod.application.generation import AuthorityGenerationCoordinator
from energypod.domain import (
    ControlPolicy,
    Direction,
    IntentSource,
    PowerIntent,
    UnitLifecycle,
)
from energypod.domain.audit import AuditEvent
from energypod.domain.authorization import AuthorizationBatch, AuthorizedSetpoint
from energypod.runtime.config import ControllerConfig
from energypod.simulator import SimulatorTransport
from tests.unit.test_event_bus import close_subscription, drain

UNIT_IDS = ("mid", "rhs")
UNIT_IDENTITIES = {"mid": "BEP-MID", "rhs": "BEP-RHS"}
SITE_ID = "home"
_SQLITE_TYPES = (SQLiteAuditRepository, SQLiteScheduleRepository)
API_ROUTE_PATHS = frozenset(
    {
        "/api/v1/snapshot",
        "/api/v1/health",
        "/api/v1/audit",
        "/api/v1/intents",
        "/api/v1/intents/cancel",
        "/api/v1/arm",
        "/api/v1/emergency-stop",
        "/api/v1/emergency-stop/{stop_id}/acknowledge",
    }
)


@dataclass(frozen=True)
class OperatorPrincipal:
    """Authenticated operator identity used only as adapter input."""

    subject: str = "person:operator"
    scopes: frozenset[str] = frozenset(
        {"observe", "audit:read", "dispatch", "arm", "stop", "stop:acknowledge"}
    )
    interactive: bool = True
    site_id: str = SITE_ID


OPERATOR = OperatorPrincipal()


def _load(module_name: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        pytest.fail(f"composition contract dependency is not implemented: {module_name}: {error}")


def _load_class(module_name: str, attribute: str) -> Any:
    value = getattr(_load(module_name), attribute, None)
    if value is None:
        pytest.fail(f"{module_name}.{attribute} is not implemented", pytrace=False)
    return value


def _build_runtime(config: ControllerConfig, **overrides: Any) -> Any:
    factory = getattr(_load("energypod.runtime.composition"), "build_runtime", None)
    if not callable(factory):
        pytest.fail("energypod.runtime.composition.build_runtime is not implemented", pytrace=False)
    return factory(config, **overrides)


def _compose_with(
    config: ControllerConfig,
    *,
    simulate: bool,
    clock: Any | None = None,
    announce: Callable[[str], None] | None = None,
) -> Any:
    overrides: dict[str, Any] = {"simulate": simulate}
    if clock is not None:
        overrides["clock"] = clock
    if announce is not None:
        overrides["dev_credential_announce"] = announce
    return _build_runtime(config, **overrides)


async def _settle(value: Any) -> Any:
    """Await a repository result only when the composed handle is asynchronous."""
    if inspect.isawaitable(value):
        return await value
    return value


def _validate(payload: Mapping[str, Any]) -> ControllerConfig:
    return ControllerConfig.model_validate(dict(payload))


def _timing_payload() -> dict[str, Any]:
    # Commissioning fixture shared with the configuration contract suite; the
    # numbers satisfy every cross-validated timing budget.
    return {
        "device_command_expiry_s": 2.35,
        "device_command_expiry_evidence": "live-trial://direction-2026-08-22/rev-1",
        "control_period_s": 0.40,
        "essential_read_timeout_s": 0.10,
        "kernel_timeout_s": 0.05,
        "audit_timeout_s": 0.05,
        "write_timeout_s": 0.10,
        "acknowledgement_timeout_s": 0.10,
        "maximum_jitter_s": 0.10,
        "renewal_margin_s": 0.50,
    }


def _unit_payload(unit_id: str, identity: str, host: str) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "display_name": unit_id.capitalize(),
        "endpoint": {"host": host, "port": 4196},
        "transport_profile": "waveshare_rtu_over_tcp",
        "protocol_profile": "iot",
        "device_id": 4,
        "expected_identity": identity,
        "expected_cell_count": 59,
    }


def _config_payload(database: Path, *, unit_count: int = 2) -> dict[str, Any]:
    units = [
        _unit_payload(unit_id, UNIT_IDENTITIES[unit_id], f"192.168.1.{11 + index}")
        for index, unit_id in enumerate(UNIT_IDS[:unit_count])
    ]
    return {
        "schema_version": 1,
        "revision": 7,
        "mode": "observe_only",
        "site": {
            "site_id": SITE_ID,
            "timezone": "Australia/Brisbane",
            "expected_unit_count": unit_count,
        },
        "units": units,
        "timing": _timing_payload(),
        "storage": {"database_path": str(database), "busy_timeout_ms": 250},
    }


def _policy_payload() -> dict[str, Any]:
    # Commissioning numbers shared with the configuration contract suite.
    return {
        "version": 3,
        "threshold_provenance": "commissioning-record-2026-08",
        "max_fleet_charge_w": 6000,
        "max_fleet_discharge_w": 6000,
        "max_unit_charge_w": 2500,
        "max_unit_discharge_w": 2500,
        "minimum_soc_pct": 10.0,
        "maximum_soc_pct": 95.0,
        "minimum_cell_v": 2.80,
        "maximum_cell_v": 3.65,
        "maximum_cell_imbalance_v": 0.050,
        "minimum_temperature_c": 0.0,
        "maximum_temperature_c": 45.0,
        "maximum_soc_difference_pct": 5.0,
        "maximum_soc_jump_pct": 10.0,
        "maximum_telemetry_age_s": 1.0,
        "maximum_cell_data_age_s": 5.0,
        "authorization_lifetime_s": 0.75,
        "ramp_limit_w_per_s": 1000,
        "stable_samples_to_rearm": 5,
        "reactive_power_limit_var": 0,
        "blocking_fault_codes": [
            "PCS_EE_CALIBRATION_OUT_OF_RANGE",
            "DCDC_EE_CALIBRATION_OUT_OF_RANGE",
        ],
        "debug_modes_enabled": False,
    }


def _authentication_payload() -> dict[str, Any]:
    return {
        "enabled": True,
        "operator_credential_ref": "secret://energypod/composition-operator",
        "trusted_proxy_cidrs": ["192.168.1.0/24"],
    }


def _write_enabled_payload(database: Path, *, unit_count: int = 2) -> dict[str, Any]:
    payload = _config_payload(database, unit_count=unit_count)
    payload["mode"] = "write_enabled"
    payload["policy"] = _policy_payload()
    payload["authentication"] = _authentication_payload()
    return payload


def compose(
    database: Path,
    *,
    simulate: bool = False,
    unit_count: int = 2,
    clock: Any | None = None,
    announce: Callable[[str], None] | None = None,
) -> Any:
    config = _validate(_config_payload(database, unit_count=unit_count))
    return _compose_with(config, simulate=simulate, clock=clock, announce=announce)


def compose_write_enabled(
    database: Path,
    *,
    simulate: bool = False,
    clock: Any | None = None,
    announce: Callable[[str], None] | None = None,
    policy_overrides: dict[str, Any] | None = None,
) -> Any:
    payload = _write_enabled_payload(database)
    if policy_overrides:
        payload["policy"] = {**payload["policy"], **policy_overrides}
    config = _validate(payload)
    return _compose_with(config, simulate=simulate, clock=clock, announce=announce)


def _prior_active_audit_event(unit_id: str) -> AuditEvent:
    """Durable history claiming the previous process held armed authority."""
    return AuditEvent(
        event_id=f"prior-active-authority-{unit_id}",
        occurred_at=datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC),
        monotonic_offset_s=0.0,
        process_instance_id="prior-process",
        event_type="authorization.granted",
        unit_id=unit_id,
        connection_epoch=3,
        generation=2,
        cycle_id="cycle-00000000000000000001",
        principal="person:operator",
        source=IntentSource.MANUAL,
        correlation_id="intent:stale-command-1:revision:9",
        intent_id="stale-command-1",
        policy_version=3,
        configuration_version=7,
        observation_sequences={unit_id: 42},
        reason_codes=("safety_checks_passed",),
        requested_active_w=1500,
        authorized_active_w=1500,
        request_fingerprint="request-fingerprint-prior",
        response_fingerprint="response-fingerprint-prior",
        result="authorized",
        lifecycle=UnitLifecycle.ACTIVE,
    )


def _stale_command_intent(units: tuple[str, ...] = UNIT_IDS) -> PowerIntent:
    return PowerIntent(
        id="stale-command-1",
        source=IntentSource.MANUAL,
        selected_unit_ids=frozenset(units),
        direction=Direction.DISCHARGE,
        watts=1500,
        duration_s=3600.0,
        accepted_at_mono=50.0,
        acceptance_revision=9,
        actor_identity="person:operator",
    )


def _latched_stop_intent() -> PowerIntent:
    return PowerIntent(
        id="stale-stop-1",
        source=IntentSource.EMERGENCY_STOP,
        selected_unit_ids=frozenset(UNIT_IDS),
        direction=Direction.IDLE,
        watts=0,
        duration_s=3600.0,
        accepted_at_mono=50.0,
        acceptance_revision=10,
        actor_identity="person:operator",
    )


def _authorization_batch(
    units: tuple[str, ...] = UNIT_IDS,
    *,
    generation: int = 2,
    issued_at: float = 50.0,
) -> AuthorizationBatch:
    setpoints = tuple(
        AuthorizedSetpoint(
            unit_id=unit_id,
            connection_epoch=3,
            generation=generation,
            cycle_id="cycle-00000000000000000001",
            intent_id="stale-command-1",
            intent_revision=9,
            direction=Direction.DISCHARGE,
            watts=750,
            issued_at_mono=issued_at,
            not_before_mono=issued_at,
            expires_at_mono=issued_at + 3_950.0,
            observation_sequence=42,
            maximum_observation_age_s=1.0,
            policy_version="3",
            configuration_version=7,
            decision_id="decision-00000000000000000001",
        )
        for unit_id in units
    )
    return AuthorizationBatch(
        cycle_id="cycle-00000000000000000001",
        generation=generation,
        authorizations=setpoints,
    )


def _observation_seed(unit_id: str, sequence: int) -> Any:
    # The observation port is structural; seeding data need only carry the
    # attributes the repository contract reads.
    return SimpleNamespace(unit_id=unit_id, connection_epoch=1, sequence=sequence)


def _forbid_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the composition root must not open sockets")

    monkeypatch.setattr(socket, "socket", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "socketpair", refused)


async def _asgi_request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Drive one request through the composed ASGI app with no test server."""
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    body = b"" if json_body is None else json.dumps(dict(json_body)).encode("utf-8")
    if json_body is not None:
        raw_headers.append((b"content-type", b"application/json"))
        raw_headers.append((b"content-length", str(len(body)).encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return status, json.loads(body)


async def _shutdown_actors(runtime: Any) -> None:
    for actor in runtime.actors.values():
        await actor.shutdown()


_SCRIPT_START = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


@dataclass
class ScriptedClock:
    """Injectable deterministic clock: every sleep advances scripted time."""

    elapsed_s: float = 0.0

    def wall_now(self) -> datetime:
        return _SCRIPT_START + timedelta(seconds=self.elapsed_s)

    def monotonic(self) -> float:
        return self.elapsed_s

    async def sleep(self, seconds: float) -> None:
        self.elapsed_s += max(0.0, float(seconds))
        await asyncio.sleep(0)


class _LifespanSession:
    """Drives the composed app's ASGI lifespan protocol with no test server."""

    def __init__(self, app: FastAPI) -> None:
        self.events: list[dict[str, Any]] = []
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.app_task: asyncio.Task[None] = asyncio.create_task(self._drive(app))

    async def _drive(self, app: FastAPI) -> None:
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.3"}}

        async def receive() -> dict[str, Any]:
            return await self._incoming.get()

        async def record(message: dict[str, Any]) -> None:
            self.events.append(message)

        await app(scope, receive, record)

    def send(self, message_type: str) -> None:
        self._incoming.put_nowait({"type": message_type})

    def seen(self, message_type: str) -> bool:
        return any(message["type"] == message_type for message in self.events)

    async def pump_until(
        self, predicate: Callable[[], bool], *, message: str, attempts: int = 5000
    ) -> None:
        for _ in range(attempts):
            if predicate():
                return
            if self.app_task.done():
                break
            await asyncio.sleep(0)
        if not predicate():
            pytest.fail(f"{message}: lifespan events={self.events!r}", pytrace=False)

    async def close(self) -> None:
        if not self.app_task.done():
            self.app_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.app_task


async def test_minimal_observe_only_config_builds_the_whole_graph(tmp_path: Path) -> None:
    config = _validate(_config_payload(tmp_path / "fleet.sqlite3", unit_count=1))
    runtime = _build_runtime(config)

    assert runtime.config == config
    assert isinstance(runtime.kernel, ControlKernel)
    assert isinstance(runtime.generation_coordinator, AuthorityGenerationCoordinator)
    assert isinstance(runtime.audit_event_factory, AuditEventFactory)
    for repository in ("intents", "observations", "authorizations", "audit", "schedule"):
        assert getattr(runtime, repository, None) is not None

    # Boot constructs one disarmed actor per configured unit and fences nothing.
    assert set(runtime.actors) == {"mid"}
    actor = runtime.actors["mid"]
    assert isinstance(actor, EnergyPodActor)
    assert actor.unit_id == "mid"
    assert actor.lifecycle is UnitLifecycle.BOOT
    assert (await runtime.generation_coordinator.snapshot()).epoch == 0

    facade_class = _load_class("energypod.application.service", "EnergyServiceFacade")
    assert isinstance(runtime.facade, facade_class)
    for operation in (
        "snapshot",
        "health",
        "recent_audit",
        "submit_intent",
        "arm",
        "emergency_stop",
        "acknowledge_emergency_stop",
    ):
        assert callable(getattr(runtime.facade, operation, None))

    event_bus_class = _load_class("energypod.application.events", "EventBus")
    assert isinstance(runtime.event_bus, event_bus_class)
    snapshot_sequence = runtime.event_bus.snapshot_sequence()
    assert isinstance(snapshot_sequence, int) and not isinstance(snapshot_sequence, bool)

    assert isinstance(runtime.app, FastAPI)
    assert callable(runtime.mcp_server_factory)

    clock = runtime.clock
    assert callable(clock.wall_now)
    assert callable(clock.monotonic)
    assert callable(clock.sleep)
    monotonic = clock.monotonic()
    assert isinstance(monotonic, float) and math.isfinite(monotonic)
    wall = clock.wall_now()
    assert isinstance(wall, datetime) and wall.tzinfo is not None
    await clock.sleep(0)

    snapshot = await _settle(runtime.facade.snapshot(principal=OPERATOR))
    assert snapshot["site_id"] == SITE_ID
    assert isinstance(snapshot["snapshot_sequence"], int)

    # The audit factory's process identity is generated per build, so two
    # builds never share one audit-trail identity.
    other = _build_runtime(config)
    assert other.audit_event_factory is not runtime.audit_event_factory


async def test_observe_only_default_policy_matches_the_commissioned_imbalance_tier(
    tmp_path: Path,
) -> None:
    """The observe-only compose default follows the operator's 2026-08-23
    direction (a7297bf, live config rev 4): cell imbalance is an early-warning
    tier at 0.500 V, so a simulated or observe-only composition judges spread
    with the deployed policy's gate, not the retired 0.050 V commissioning
    one.  The absolute per-cell bounds stay the hard protection."""
    config = _validate(_config_payload(tmp_path / "fleet.sqlite3", unit_count=1))
    runtime = _build_runtime(config)

    assert runtime.policy.version == "observe-only-default"
    assert runtime.policy.min_cell_voltage_v == 2.80
    assert runtime.policy.max_cell_voltage_v == 3.65
    assert runtime.policy.max_cell_imbalance_v == 0.500


async def test_composed_app_serves_the_versioned_guarded_api(tmp_path: Path) -> None:
    runtime = compose(tmp_path / "fleet.sqlite3")
    app = runtime.app
    assert isinstance(app, FastAPI)

    paths = {route.path for route in app.routes}
    assert paths >= API_ROUTE_PATHS
    websocket_paths = {route.path for route in app.routes if isinstance(route, WebSocketRoute)}
    assert "/api/v1/events" in websocket_paths

    status, body = await _asgi_request(app, "GET", "/api/v1/snapshot")
    assert status == 401
    assert body["code"] == "authentication_required"
    assert body["request_id"]


async def test_mcp_server_factory_is_read_only_by_default(tmp_path: Path) -> None:
    runtime = compose(tmp_path / "fleet.sqlite3")
    factory = runtime.mcp_server_factory
    assert callable(factory)

    server = factory(principal=OPERATOR)
    assert isinstance(server, FastMCP)
    assert factory(principal=OPERATOR) is not server

    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert {"get_snapshot", "get_health", "get_recent_audit"} <= names
    # The operator principal holds dispatch scope; the composed default must
    # still expose a read-only MCP surface.
    assert "dispatch_intent" not in names


async def test_construction_opens_no_sockets_and_starts_no_tasks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _forbid_sockets(monkeypatch)

    def refused(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the composition root must not start tasks at construction")

    before = asyncio.all_tasks()
    # Any task start — however short-lived — fails loudly inside the ban, so a
    # fire-and-forget cycle that completes within the window cannot hide.
    with monkeypatch.context() as tasks_banned:
        tasks_banned.setattr(asyncio, "create_task", refused)
        tasks_banned.setattr(asyncio, "ensure_future", refused)
        runtime = compose(tmp_path / "fleet.sqlite3")
    assert set(runtime.actors) == set(UNIT_IDS)
    assert all(actor.lifecycle is UnitLifecycle.BOOT for actor in runtime.actors.values())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not asyncio.all_tasks() - before


async def test_run_mode_persists_only_audit_and_schedule(tmp_path: Path) -> None:
    database = tmp_path / "fleet.sqlite3"
    runtime = compose(database)

    assert database.exists()
    # The type pins are permitted to stay, but the durable stores are the
    # shipped synchronous classes, so the suite never awaits them directly.
    assert isinstance(runtime.audit, SQLiteAuditRepository)
    assert isinstance(runtime.schedule, SQLiteScheduleRepository)
    for repository in (runtime.intents, runtime.observations, runtime.authorizations):
        assert not isinstance(repository, _SQLITE_TYPES)

    # The volatile stores are live, in-process repositories.
    await _settle(runtime.observations.append(_observation_seed("mid", 1)))
    assert await _settle(runtime.observations.latest("mid")) is not None
    assert await _settle(runtime.schedule.get()) is None


async def test_missing_database_path_composes_fully_in_memory_persistence(
    tmp_path: Path,
) -> None:
    """API_CONTRACTS "Runtime composition and entry point": when no database
    path is configured, persistence is entirely in-memory.

    StorageConfig cannot express a missing database path today, so this test
    deliberately stays red until the configuration contract allows the branch.
    """
    payload = _config_payload(tmp_path / "never-created.sqlite3")
    payload.pop("storage")
    try:
        config = ControllerConfig.model_validate(payload)
    except ValidationError:
        pytest.fail(
            "API_CONTRACTS grants 'when no database path is configured ... persistence is "
            "entirely in-memory', but the configuration model cannot express a missing "
            "database path",
            pytrace=False,
        )
    runtime = _build_runtime(config)
    assert not isinstance(runtime.audit, _SQLITE_TYPES)
    assert not isinstance(runtime.schedule, _SQLITE_TYPES)
    assert not (tmp_path / "never-created.sqlite3").exists()


async def test_boot_restores_no_arming_authority_or_active_commands(tmp_path: Path) -> None:
    database = tmp_path / "fleet.sqlite3"
    prior = compose(database)

    # Durable history claims the fleet was ACTIVE with granted authority.
    for unit_id in UNIT_IDS:
        await _settle(prior.audit.append(_prior_active_audit_event(unit_id)))
    # Live volatile state in the previous runtime: an active command, a
    # latched stop, and current, unexpired authorizations.
    await _settle(prior.intents.add(_stale_command_intent()))
    await _settle(prior.intents.add(_latched_stop_intent()))
    await _settle(prior.authorizations.publish(_authorization_batch()))
    await _settle(prior.observations.append(_observation_seed("mid", 1)))

    # The seeds are genuinely live in the previous runtime, so the restarted
    # one cannot pass vacuously.
    assert await _settle(prior.intents.active(60.0))
    for unit_id in UNIT_IDS:
        assert await _settle(prior.authorizations.current(unit_id, 60.0)) is not None

    restarted = compose(database)

    # The durable audit trail really was reused; prior authority stays plainly
    # visible in it while the restarted process starts with nothing.
    recent = await _settle(restarted.audit.recent(limit=10))
    assert {f"prior-active-authority-{unit_id}" for unit_id in UNIT_IDS} <= {
        event.event_id for event in recent
    }
    for now in (0.0, 60.0, 1_000_000_000.0):
        assert not await _settle(restarted.intents.active(now))
        for unit_id in UNIT_IDS:
            assert await _settle(restarted.authorizations.current(unit_id, now)) is None
    for unit_id in UNIT_IDS:
        assert await _settle(restarted.observations.latest(unit_id)) is None
        assert restarted.actors[unit_id] is not prior.actors[unit_id]
        assert restarted.actors[unit_id].lifecycle is UnitLifecycle.BOOT


async def test_write_enabled_restart_boots_disarmed_and_restores_nothing(
    tmp_path: Path,
) -> None:
    """Boot-disarmed must also hold where restoration could reach hardware."""
    database = tmp_path / "fleet.sqlite3"
    prior = compose_write_enabled(database)

    # Durable history claims the previous write-enabled process held armed,
    # active authority — exactly the state a restart must not resurrect.
    for unit_id in UNIT_IDS:
        await _settle(prior.audit.append(_prior_active_audit_event(unit_id)))
    await _settle(prior.intents.add(_stale_command_intent()))
    await _settle(prior.intents.add(_latched_stop_intent()))
    await _settle(prior.authorizations.publish(_authorization_batch()))
    assert await _settle(prior.intents.active(60.0))
    for unit_id in UNIT_IDS:
        assert await _settle(prior.authorizations.peek(unit_id)) is not None

    restarted = compose_write_enabled(database)

    recent = await _settle(restarted.audit.recent(limit=10))
    assert {f"prior-active-authority-{unit_id}" for unit_id in UNIT_IDS} <= {
        event.event_id for event in recent
    }
    for now in (0.0, 60.0, 1_000_000_000.0):
        assert not await _settle(restarted.intents.active(now))
        for unit_id in UNIT_IDS:
            # peek is the granted non-consuming projection read.
            assert await _settle(restarted.authorizations.peek(unit_id)) is None
    for unit_id in UNIT_IDS:
        assert await _settle(restarted.observations.latest(unit_id)) is None
        actor = restarted.actors[unit_id]
        assert actor is not prior.actors[unit_id]
        assert actor.lifecycle is UnitLifecycle.BOOT


async def test_simulate_boot_stays_observe_only_despite_live_authority(tmp_path: Path) -> None:
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True)
    await _settle(runtime.intents.add(_stale_command_intent(("mid",))))
    await _settle(runtime.authorizations.publish(_authorization_batch(("mid",))))
    try:
        for actor in runtime.actors.values():
            await actor.start()
        assert all(
            actor.lifecycle is UnitLifecycle.OBSERVE_ONLY for actor in runtime.actors.values()
        )
        # Boot performed no heartbeat: the freshly published, still-valid
        # authorization was not consumed by any started actor.
        assert await _settle(runtime.authorizations.current("mid", 60.0)) is not None
        assert await _settle(runtime.intents.active(60.0))
    finally:
        await _shutdown_actors(runtime)


async def test_simulate_mode_forces_simulator_transports_and_memory_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _forbid_sockets(monkeypatch)
    database = tmp_path / "fleet.sqlite3"
    runtime = compose(database, simulate=True)

    assert not database.exists()
    assert not isinstance(runtime.audit, _SQLITE_TYPES)
    assert not isinstance(runtime.schedule, _SQLITE_TYPES)

    event = _prior_active_audit_event("mid")
    await _settle(runtime.audit.append(event))
    saved = await _settle(runtime.audit.recent(limit=5))
    assert [item.event_id for item in saved] == [event.event_id]

    # A second simulated build against the same configured database path
    # starts from empty in-memory stores and never touches the file.
    again = compose(database, simulate=True)
    assert not await _settle(again.audit.recent(limit=5))
    assert not database.exists()

    # The only transports that can start and poll under a socket ban are the
    # deterministic simulator ones.
    try:
        for actor in runtime.actors.values():
            await actor.start()
            registers = await actor.poll_once()
            assert isinstance(registers, tuple) and registers
            assert all(
                isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFF
                for value in registers
            )
        assert all(
            actor.lifecycle is UnitLifecycle.OBSERVE_ONLY for actor in runtime.actors.values()
        )
    finally:
        await _shutdown_actors(runtime)


async def test_unit_fencing_advances_the_one_fleet_wide_generation(tmp_path: Path) -> None:
    runtime = compose(tmp_path / "fleet.sqlite3")
    coordinator = runtime.generation_coordinator
    before = (await coordinator.snapshot()).epoch
    assert await runtime.actors["mid"].fence("composition-contract-first") == before + 1
    assert (await coordinator.snapshot()).epoch == before + 1
    assert await runtime.actors["rhs"].fence("composition-contract-second") == before + 2


async def test_kernel_tick_is_wired_to_the_runtime_stores_and_bus(tmp_path: Path) -> None:
    """The composed kernel drives THE runtime repositories, not private copies."""
    runtime = compose_write_enabled(
        tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock()
    )
    epoch = (await runtime.generation_coordinator.snapshot()).epoch
    await _settle(
        runtime.authorizations.publish(_authorization_batch(generation=epoch, issued_at=0.0))
    )
    for unit_id in UNIT_IDS:
        # peek is the granted non-consuming projection read.
        assert await _settle(runtime.authorizations.peek(unit_id)) is not None
    bus_before = runtime.event_bus.snapshot_sequence()

    # A latched stop forces the always-permitted fail-closed path: the tick
    # must revoke through the runtime's shared authorization repository.
    await _settle(runtime.intents.add(_latched_stop_intent()))
    await runtime.kernel.tick()

    for unit_id in UNIT_IDS:
        assert await _settle(runtime.authorizations.peek(unit_id)) is None
    recent = await _settle(runtime.audit.recent(limit=5))
    assert recent, "the kernel tick must audit through the runtime audit repository"
    # ADR-0003 D3: audit appends are events on the composed bus.
    for _ in range(100):
        if runtime.event_bus.snapshot_sequence() > bus_before:
            break
        await asyncio.sleep(0)
    assert runtime.event_bus.snapshot_sequence() > bus_before


async def test_facade_wiring_shares_the_runtime_bus_and_fleet_coordinator(
    tmp_path: Path,
) -> None:
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True)
    bus_before = runtime.event_bus.snapshot_sequence()
    epoch_before = (await runtime.generation_coordinator.snapshot()).epoch

    await _settle(
        runtime.facade.emergency_stop(
            unit_ids=list(UNIT_IDS), reason="composition-wiring-proof", principal=OPERATOR
        )
    )

    # API_CONTRACTS facade: emergency stop "immediately advances the fleet
    # generation", so the facade fences through THE runtime coordinator and
    # latches the stop in THE runtime intent repository.
    assert (await runtime.generation_coordinator.snapshot()).epoch > epoch_before
    active = await _settle(runtime.intents.active(runtime.clock.monotonic()))
    assert any(getattr(intent, "source", None) is IntentSource.EMERGENCY_STOP for intent in active)
    bus_after = runtime.event_bus.snapshot_sequence()
    assert bus_after > bus_before, "facade mutations must publish to the runtime event bus"

    snapshot = await _settle(runtime.facade.snapshot(principal=OPERATOR))
    assert snapshot["snapshot_sequence"] == bus_after
    assert runtime.event_bus.snapshot_sequence() == bus_after


def _actors_stopped(runtime: Any) -> bool:
    return all(
        actor.lifecycle in {UnitLifecycle.STOPPING, UnitLifecycle.DISCONNECTED}
        for actor in runtime.actors.values()
    )


# --- intent lifecycle and grant events on the composed bus ---------------------
#
# 2026-08-23 observability root cause: an intent whose TTL lapsed published
# nothing at all -- the kernel's no-winner revoke publishes only while a unit
# still holds a capability, which is never true at expiry -- and authority
# grants were store-only, so the console's request cards never saw either
# transition.  The console consumes `intent.expired` and
# `authorization.granted` by those exact names.


async def _bus_events(runtime: Any, *, limit: int = 64) -> list[dict[str, Any]]:
    iterator = runtime.event_bus.subscribe(after_sequence=0)
    try:
        return await drain(iterator, limit)
    finally:
        await close_subscription(iterator)


def _short_lived_intent(api: Any = None) -> PowerIntent:
    del api
    return PowerIntent(
        id="short-lived-1",
        source=IntentSource.MANUAL,
        selected_unit_ids=frozenset(UNIT_IDS),
        direction=Direction.DISCHARGE,
        watts=1200,
        duration_s=5.0,
        accepted_at_mono=0.0,
        acceptance_revision=21,
        actor_identity="person:operator",
    )


async def test_expired_intents_publish_on_the_event_bus_exactly_once(
    tmp_path: Path,
) -> None:
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock())
    intent = _short_lived_intent()
    await _settle(runtime.intents.add(intent))
    assert await _settle(runtime.intents.active(4.0))

    # The kernel's no-winner tick is the live trigger: its own intent read
    # crosses the TTL boundary, so the expiry the fleet actually acted on is
    # the one the bus carries.
    runtime.clock.elapsed_s = 10.0
    assert await runtime.kernel.tick() is None

    (event,) = [event for event in await _bus_events(runtime) if event["type"] == "intent.expired"]
    assert event["payload"] == {
        "intent_id": "short-lived-1",
        "source": "manual",
        "direction": "discharge",
        "watts": 1200,
        "unit_ids": sorted(UNIT_IDS),
    }

    # The transition fires once: later reads, ticks, and snapshots never
    # re-announce an already-published expiry.
    sequence_after = runtime.event_bus.snapshot_sequence()
    await _settle(runtime.intents.active(11.0))
    await runtime.kernel.tick()
    assert runtime.event_bus.snapshot_sequence() == sequence_after


async def test_removed_intents_never_publish_an_expiry(tmp_path: Path) -> None:
    """A cancelled or acknowledged intent leaves by removal, not by TTL: the
    bus must not announce an expiry for an intent that was deliberately
    taken out of the store."""
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock())
    intent = _short_lived_intent()
    await _settle(runtime.intents.add(intent))
    assert await _settle(runtime.intents.active(4.0))
    await _settle(runtime.intents.remove(intent.id))
    runtime.clock.elapsed_s = 10.0
    await _settle(runtime.intents.active(10.0))
    assert not any(event["type"] == "intent.expired" for event in await _bus_events(runtime))


async def test_published_authorizations_announce_the_grant_on_the_bus(
    tmp_path: Path,
) -> None:
    """Symmetry with authorization.revoked: a batch that lands in the store is
    announced as authorization.granted with the cycle, generation, units, and
    -- since concurrent cycles may mix directions per unit -- each unit's own
    authorized watts and direction."""
    runtime = compose_write_enabled(
        tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock()
    )
    epoch = (await runtime.generation_coordinator.snapshot()).epoch
    batch = _authorization_batch(generation=epoch, issued_at=0.0)
    await _settle(runtime.authorizations.publish(batch))

    (event,) = [
        item for item in await _bus_events(runtime) if item["type"] == "authorization.granted"
    ]
    assert event["payload"] == {
        "cycle_id": batch.cycle_id,
        "generation": epoch,
        "unit_ids": sorted(UNIT_IDS),
        "watts_by_unit": {unit_id: 750 for unit_id in sorted(UNIT_IDS)},
        "directions_by_unit": {unit_id: "discharge" for unit_id in sorted(UNIT_IDS)},
    }


async def test_lifespan_starts_and_stops_supervision(tmp_path: Path) -> None:
    """API_CONTRACTS: supervision starts and stops through the app lifespan."""
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock())
    baseline = set(asyncio.all_tasks())
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="the application lifespan never reported supervision startup",
        )
        assert session.seen("lifespan.startup.complete"), f"startup failed: {session.events!r}"
        await session.pump_until(
            lambda: bool(asyncio.all_tasks() - baseline - {session.app_task}),
            message="the application lifespan must start the supervision tasks",
        )
        supervision = asyncio.all_tasks() - baseline - {session.app_task}
        assert supervision, "the kernel, actor, and publication loops must be running tasks"
        await session.pump_until(
            lambda: all(
                actor.lifecycle is not UnitLifecycle.BOOT for actor in runtime.actors.values()
            ),
            message="the per-unit actor loops never started",
        )
        epoch_running = (await runtime.generation_coordinator.snapshot()).epoch

        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the application lifespan never reported supervision shutdown",
        )
        assert session.seen("lifespan.shutdown.complete"), f"shutdown failed: {session.events!r}"
        await session.pump_until(
            lambda: not asyncio.all_tasks() - baseline - {session.app_task},
            message="supervision tasks never stopped after lifespan shutdown",
        )
        assert _actors_stopped(runtime), "lifespan shutdown must run actor shutdown"
        # Actor shutdown fences: a stopped process never resumes an old epoch.
        assert (await runtime.generation_coordinator.snapshot()).epoch > epoch_running
    finally:
        await session.close()


async def test_supervisor_failure_fences_generations_and_runs_actor_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API_CONTRACTS: supervisor or task failure fences every generation and
    runs actor shutdown with the bounded-zero contract before exiting."""
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock())
    epoch = (await runtime.generation_coordinator.snapshot()).epoch
    await _settle(
        runtime.authorizations.publish(_authorization_batch(generation=epoch, issued_at=0.0))
    )
    for unit_id in UNIT_IDS:
        assert await _settle(runtime.authorizations.peek(unit_id)) is not None

    async def exploding_tick() -> None:
        raise RuntimeError("supervisor component failed")

    monkeypatch.setattr(runtime.kernel, "tick", exploding_tick)

    baseline = set(asyncio.all_tasks())
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.failed")
            or session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed")
            or _actors_stopped(runtime),
            message="a failed supervisor component must stop the application lifespan",
        )
        await session.pump_until(
            lambda: _actors_stopped(runtime),
            message="a failed supervisor component must run actor shutdown",
        )
        # Fencing every generation also revokes outstanding authority.
        assert (await runtime.generation_coordinator.snapshot()).epoch > epoch
        for unit_id in UNIT_IDS:
            assert await _settle(runtime.authorizations.peek(unit_id)) is None
        await session.pump_until(
            lambda: not asyncio.all_tasks() - baseline - {session.app_task},
            message="a failed supervisor component must not leave supervision tasks running",
        )
    finally:
        await session.close()


async def test_mid_run_supervisor_failure_watcher_halts_the_fleet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR-0003 D6 for the failure that happens *after* a healthy boot.

    The startup-window failure above never creates the failure watcher; this
    scenario starts supervision cleanly, lets it run, and only then fails a
    component, so the watcher-driven halt path -- fence, revoke, actor
    shutdown with the bounded-zero contract, no surviving supervision task --
    is what actually executes.
    """
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock())
    epoch = (await runtime.generation_coordinator.snapshot()).epoch
    await _settle(
        runtime.authorizations.publish(_authorization_batch(generation=epoch, issued_at=0.0))
    )
    for unit_id in UNIT_IDS:
        assert await _settle(runtime.authorizations.peek(unit_id)) is not None

    supervised_tick = runtime.kernel.tick
    ticks = itertools.count()

    async def failing_mid_run() -> None:
        if next(ticks) >= 3:
            raise RuntimeError("mid-run supervisor component failed")
        return await supervised_tick()

    monkeypatch.setattr(runtime.kernel, "tick", failing_mid_run)

    baseline = set(asyncio.all_tasks())
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="the application lifespan never reported supervision startup",
        )
        assert session.seen("lifespan.startup.complete"), f"startup failed: {session.events!r}"

        # The halt must come from the failure watcher alone: nothing else is
        # waiting on a mid-run component failure.
        await session.pump_until(
            lambda: _actors_stopped(runtime),
            message="a mid-run supervisor failure must run actor shutdown",
        )
        assert (await runtime.generation_coordinator.snapshot()).epoch > epoch
        for unit_id in UNIT_IDS:
            assert await _settle(runtime.authorizations.peek(unit_id)) is None
        recent = await _settle(runtime.audit.recent(limit=8))
        assert any(event.event_type == "authorization_revoked" for event in recent), (
            "the mid-run halt must durably record the fleet revocation"
        )
        await session.pump_until(
            lambda: not asyncio.all_tasks() - baseline - {session.app_task},
            message="a mid-run supervisor failure must not leave supervision tasks running",
        )

        # Supervision is one-shot: the lifespan still shuts down cleanly after
        # the halt instead of trying to restart anything.
        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the application lifespan never reported supervision shutdown",
        )
        assert session.seen("lifespan.shutdown.complete"), f"shutdown failed: {session.events!r}"
    finally:
        await session.close()


async def test_unreachable_gateway_fails_the_application_lifespan_startup(
    tmp_path: Path,
) -> None:
    """A unit whose transport cannot connect must fail startup, not serve.

    ``_await_startup`` may not read "left BOOT" as success while the actor's
    own task is failing: an unreachable gateway is a dead fleet, and the
    process must report a failed lifespan startup instead of serving with no
    polls, no observations, and no heartbeats.
    """
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock())
    assert runtime.simulators is not None
    runtime.simulators["mid"].drop_link()

    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="an unreachable gateway must resolve startup, not serve",
        )
        assert session.seen("lifespan.startup.failed"), (
            "a unit that cannot connect must fail the application lifespan startup: "
            f"{session.events!r}"
        )
    finally:
        await session.close()


async def test_simulate_mode_qualifies_the_evidenced_59_cell_topology(
    tmp_path: Path,
) -> None:
    """The corroborated 59-cell topology must be qualifiable in simulate mode.

    The evidenced IoT read plan serves a BIC*10-wide cell window; the composed
    decode reconciles it with the unit's configured expectation so a count
    that is not a multiple of ten still reaches DISARMED and arms.  Without
    that reconciliation the served window (60) can never equal the configured
    expectation (59) and the unit is silently, permanently uncontrollable.
    """
    runtime = compose_write_enabled(
        tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock()
    )
    actor = runtime.actors["mid"]
    assert runtime.simulators is not None
    pod = runtime.simulators["mid"]
    # The evidence-backed register plan is not narrowed: the pod still serves
    # the full six-BIC packing while the unit expects 59 cells.
    assert len(pod.read(0x5200, 60)) == 60

    await actor.start()
    try:
        for _ in range(runtime.policy.stable_samples_needed_to_rearm):
            await runtime.clock.sleep(0.05)
            await actor.poll_once()

        latest = await _settle(runtime.observations.latest("mid"))
        assert latest is not None, "every poll must deliver an observation"
        assert latest.expected_cell_count == 59
        assert len(latest.cell_voltages_v) == 59
        assert latest.cells_complete
        assert latest.temperatures_complete
        # The temperature expectation follows the served topology, not the
        # length of whatever window happened to be read.
        assert latest.expected_temperature_count == 18
        assert latest.protocol_profile == "iot"
        assert latest.safety_data_complete

        assert actor.qualified is True
        assert actor.lifecycle is UnitLifecycle.DISARMED
        result = await _settle(
            runtime.facade.arm(
                unit_ids=["mid"],
                principal=OPERATOR,
                idempotency_key="arm-59-cell-topology",
                request_id="request-59-cell-topology",
            )
        )
        outcomes = {unit["unit_id"]: unit["status"] for unit in result["units"]}
        assert outcomes == {"mid": "armed"}, outcomes
        assert actor.lifecycle is UnitLifecycle.ARMED_IDLE
    finally:
        await _shutdown_actors(runtime)


async def test_simulate_mode_rejects_a_cell_count_the_iot_packing_cannot_serve(
    tmp_path: Path,
) -> None:
    """A count the plan cannot serve is a wiring error, not a dead unit.

    Composing a simulated unit whose expected cell count exceeds the evidenced
    packing would boot a unit that can never present a complete cell window,
    so construction must fail loudly before any task starts.
    """
    payload = _config_payload(tmp_path / "fleet.sqlite3", unit_count=1)
    payload["units"][0]["expected_cell_count"] = 61
    config = _validate(payload)
    with pytest.raises(ValueError, match="expects 61 cells"):
        _compose_with(config, simulate=True)
    assert not (tmp_path / "fleet.sqlite3").exists()


async def test_simulate_mode_never_qualifies_a_unit_served_the_wrong_layout(
    tmp_path: Path,
) -> None:
    """A legacy-configured unit served an IoT bank must never qualify.

    The decode verifies the served layout probe against the configured
    profile instead of stamping the configuration into the observation, so a
    unit whose bank contradicts its configuration fails every poll closed and
    can never arm on fabricated self-consistent evidence.
    """
    payload = _config_payload(tmp_path / "fleet.sqlite3", unit_count=1)
    payload["units"][0]["protocol_profile"] = "legacy"
    payload["units"][0]["expected_cell_count"] = 40
    config = _validate(payload)
    runtime = _compose_with(config, simulate=True, clock=ScriptedClock())

    actor = runtime.actors["mid"]
    await actor.start()
    try:
        for _ in range(4):
            await runtime.clock.sleep(0.05)
            with pytest.raises(RuntimeError, match="configured for protocol profile 'legacy'"):
                await actor.poll_once()

        assert await _settle(runtime.observations.latest("mid")) is None
        assert actor.qualified is None
        assert actor.lifecycle is UnitLifecycle.OBSERVE_ONLY
        with pytest.raises(RuntimeError, match="not qualified for arming"):
            await actor.arm()
    finally:
        await _shutdown_actors(runtime)


def _fast_timing_payload() -> dict[str, Any]:
    # A commissioned budget with a short control period, so a stalled telemetry
    # read outruns the renewal deadline quickly in wall-clock terms.
    return {
        "device_command_expiry_s": 2.35,
        "device_command_expiry_evidence": "live-trial://direction-2026-08-22/rev-1",
        "control_period_s": 0.06,
        "essential_read_timeout_s": 0.10,
        "kernel_timeout_s": 0.05,
        "audit_timeout_s": 0.05,
        "write_timeout_s": 0.02,
        "acknowledgement_timeout_s": 0.10,
        "maximum_jitter_s": 0.10,
        "renewal_margin_s": 0.50,
    }


async def test_overdue_telemetry_read_does_not_delay_heartbeat_renewal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API_CONTRACTS "Unit actor": an overdue read is abandoned, never renewed late.

    A telemetry cycle that stalls past the interval-minus-margin deadline must
    be abandoned by the composed runtime instead of serializing the next
    heartbeat behind it; the actor's own preemption can only engage while a
    heartbeat is actually awaiting dispatch, so the composed loop must keep
    the heartbeat and the overdue poll concurrent.
    """
    payload = _write_enabled_payload(tmp_path / "fleet.sqlite3", unit_count=1)
    payload["timing"] = _fast_timing_payload()
    config = _validate(payload)
    interval_s = float(config.timing.control_period_s)
    margin_s = float(config.timing.write_timeout_s)
    stall_s = interval_s * 5

    served_read = SimulatorTransport.read_holding

    async def stalling_read(self: Any, address: int, count: int) -> tuple[int, ...]:
        if address == 0x5200:
            await asyncio.sleep(stall_s)
        return await served_read(self, address, count)

    monkeypatch.setattr(SimulatorTransport, "read_holding", stalling_read)

    runtime = _compose_with(config, simulate=True)
    actor = runtime.actors["mid"]
    renewals: list[float] = []
    supervised_heartbeat = actor.heartbeat_once

    async def observed_heartbeat() -> None:
        await supervised_heartbeat()
        renewals.append(time.monotonic())

    actor.heartbeat_once = observed_heartbeat  # type: ignore[method-assign]

    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="the application lifespan never reported supervision startup",
        )
        assert session.seen("lifespan.startup.complete"), f"startup failed: {session.events!r}"

        # Two hundred 5 ms pumps bound the wait at one second of wall clock,
        # far beyond the six renewals a healthy cadence needs.
        for _ in range(200):
            if len(renewals) >= 6:
                break
            await asyncio.sleep(0.005)
        assert len(renewals) >= 6, (
            f"only {len(renewals)} heartbeat renewals completed while the telemetry "
            "read was overdue"
        )
        gaps = [second - first for first, second in itertools.pairwise(renewals)]
        assert max(gaps) < stall_s / 2, (
            f"a heartbeat waited {max(gaps):.3f}s behind an overdue telemetry read; the "
            f"commissioned budget is {interval_s}s with a {margin_s}s safety margin"
        )
        # The overdue read was abandoned, never completed into an observation
        # that the safety path could then mistake for fresh evidence.
        assert await _settle(runtime.observations.latest("mid")) is None

        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the application lifespan never reported supervision shutdown",
        )
        assert session.seen("lifespan.shutdown.complete"), f"shutdown failed: {session.events!r}"
    finally:
        await session.close()


def _paginated_audit_events(count: int) -> tuple[AuditEvent, ...]:
    return tuple(
        _prior_active_audit_event("mid").model_copy(update={"event_id": f"page-{number:02d}"})
        for number in range(count)
    )


async def _assert_pages_without_repeats(runtime: Any, *, limit: int, total: int) -> None:
    page = await _settle(runtime.facade.recent_audit(principal=OPERATOR, limit=limit))
    seen: list[int] = []
    while page["events"]:
        for event in page["events"]:
            assert isinstance(event, AuditEvent)
            assert type(event.sequence) is int and not isinstance(event.sequence, bool)
        sequences = [event.sequence for event in page["events"]]
        assert sequences == sorted(sequences, reverse=True), "pages must stay newest-first"
        seen.extend(sequences)
        cursor = page["next_cursor"]
        if not page["events"] or len(page["events"]) < limit:
            assert cursor is None, "a short terminal page leaves no further cursor"
            break
        assert type(cursor) is int and cursor == sequences[-1]
        page = await _settle(
            runtime.facade.recent_audit(principal=OPERATOR, limit=limit, cursor=cursor)
        )
    assert seen == list(range(total, 0, -1)), f"pagination must cover every fact once: {seen!r}"


async def test_composed_audit_cursor_pages_in_every_deployment_mode(tmp_path: Path) -> None:
    """API_CONTRACTS facade: ``recent_audit`` keeps a stable, resumable cursor.

    The composed stores project their own ordering key onto the audit read
    model, so a page is never silently the last page while strictly more
    durable facts exist, and an explicit cursor resumes with strictly older
    facts instead of raising.
    """
    volatile = compose(tmp_path / "fleet.sqlite3", simulate=True)
    for event in _paginated_audit_events(5):
        await _settle(volatile.audit.append(event))
    await _assert_pages_without_repeats(volatile, limit=2, total=5)

    # The durable deployment pages the same way, and its cursor survives a
    # restart because the sequences belong to the database rows.
    database = tmp_path / "durable-fleet.sqlite3"
    durable = compose(database)
    assert isinstance(durable.audit, SQLiteAuditRepository)
    for event in _paginated_audit_events(5):
        await _settle(durable.audit.append(event))
    await _assert_pages_without_repeats(durable, limit=2, total=5)

    reopened = compose(database)
    await _settle(
        reopened.audit.append(
            _prior_active_audit_event("mid").model_copy(update={"event_id": "page-after-restart"})
        )
    )
    fresh = await _settle(reopened.facade.recent_audit(principal=OPERATOR, limit=1))
    assert [event.event_id for event in fresh["events"]] == ["page-after-restart"]
    assert fresh["events"][0].sequence == 6, "durable sequences must continue across restarts"


async def test_timing_validation_rejects_a_write_timeout_the_control_period_cannot_fit(
    tmp_path: Path,
) -> None:
    """``check-config`` and ``build_runtime`` must agree on the timing budget.

    The write timeout is wired as the actor's heartbeat safety margin inside
    the heartbeat interval (the control period), so a configuration whose
    write timeout cannot fit is rejected at validation time instead of
    passing ``check-config`` and failing every composition attempt.
    """
    payload = _config_payload(tmp_path / "fleet.sqlite3", unit_count=1)
    payload["timing"] = {**_timing_payload(), "control_period_s": 0.50, "write_timeout_s": 1.00}
    with pytest.raises(ValidationError, match="write timeout must fit strictly inside"):
        _validate(payload)


async def test_build_runtime_fails_closed_and_leaves_no_database_behind(
    tmp_path: Path,
) -> None:
    """A rejected composition is a wiring error with no filesystem effect.

    The eager actor-wiring check runs before the durable store opens, so even
    a configuration that bypassed validation cannot leave an open database --
    or its live WAL siblings -- behind on disk.
    """
    database = tmp_path / "leftover.sqlite3"
    config = _validate(_config_payload(database, unit_count=1))
    unbudgeted = config.model_copy(
        update={"timing": config.timing.model_copy(update={"write_timeout_s": 5.0})}
    )
    with pytest.raises(ValueError, match="heartbeat safety margin"):
        _build_runtime(unbudgeted)
    assert not database.exists()
    assert not (tmp_path / "leftover.sqlite3-wal").exists()
    assert not (tmp_path / "leftover.sqlite3-shm").exists()


# --- Operations surface (Milestone C): the simulator development principal --
#
# API_CONTRACTS: "`energypod simulate` mints one deterministic development
# principal (full scopes, interactive, token printed once to stdout at
# startup) when no credential store is configured — simulator deployments
# only, never `run` mode, never against hardware.  `run` mode without a
# credential store stays fail-closed (all bearer auth refused)."


async def test_simulate_mode_prints_one_dev_credential_to_stdout_at_startup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No credential store configured + simulate mode: one dev credential is
    minted and its token printed exactly once to stdout at startup, and that
    printed token is a working bearer credential for the composed API."""
    capsys.readouterr()
    runtime = compose(tmp_path / "sim-fleet.sqlite3", simulate=True)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, "the development credential must be printed exactly once"
    match = re.search(r"[A-Za-z0-9_-]{32,}", lines[0])
    assert match is not None, "the startup line must carry the token"
    token = match.group(0)
    assert token == token.strip()

    status, body = await _asgi_request(
        runtime.app, "GET", "/api/v1/snapshot", headers={"Authorization": f"Bearer {token}"}
    )
    assert status == 200, body
    assert body["site_id"] == SITE_ID

    # Anything else — including a tampered copy of the genuine token — is
    # refused with the ordinary structured 401 envelope.
    for candidate in ("", "dev", f"{token}x", token.upper(), "x" * 64):
        status, body = await _asgi_request(
            runtime.app,
            "GET",
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {candidate}"},
        )
        assert status == 401, candidate
        assert body["code"] == "authentication_required"


async def test_simulate_dev_principal_holds_full_scopes_and_is_interactive(
    tmp_path: Path,
) -> None:
    """The minted principal is the full-scope interactive operator the guarded
    surface can express, and its identity is deterministic across builds."""
    announced: list[str] = []
    runtime = compose(tmp_path / "dev-fleet.sqlite3", simulate=True, announce=announced.append)
    assert len(announced) == 1
    bearer = {"Authorization": f"Bearer {announced[0]}"}

    # observe, and audit:read on top of it.
    status, snapshot = await _asgi_request(runtime.app, "GET", "/api/v1/snapshot", headers=bearer)
    assert status == 200, snapshot
    status, _audit = await _asgi_request(runtime.app, "GET", "/api/v1/audit", headers=bearer)
    assert status == 200

    # dispatch: a real accepted intent carrying the principal's identity.
    status, accepted = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/intents",
        headers={**bearer, "Idempotency-Key": "dev-dispatch-probe"},
        json_body={"unit_ids": ["mid"], "direction": "charge", "watts": 500, "ttl_s": 30},
    )
    assert status == 202, accepted
    active = await _settle(runtime.intents.active(runtime.clock.monotonic()))
    subjects = {intent.actor_identity for intent in active}
    assert len(subjects) == 1

    # stop and stop:acknowledge: the full latch/acknowledge round trip.
    status, stopped = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/emergency-stop",
        headers={**bearer, "Idempotency-Key": "dev-stop-probe"},
        json_body={"unit_ids": ["mid"], "reason": "dev-principal scope probe"},
    )
    assert status == 202, stopped
    status, acknowledged = await _asgi_request(
        runtime.app,
        "POST",
        f"/api/v1/emergency-stop/{stopped['stop_id']}/acknowledge",
        headers={**bearer, "Idempotency-Key": "dev-stop-ack-probe"},
        json_body={"confirmation": "ACKNOWLEDGE"},
    )
    assert status == 200, acknowledged

    # arm scope plus an interactive principal: the ghost-unit inhibit
    # acknowledgement passes both gates and fails on the unit lookup instead
    # (404 unit_not_found, not a 403).
    status, body = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/units/pod-ghost/inhibit/acknowledge",
        headers={**bearer, "Idempotency-Key": "dev-interactive-probe"},
        json_body={"confirmation": "ACKNOWLEDGE"},
    )
    assert status == 404, body
    assert body["code"] == "unit_not_found"

    # The identity is the deterministic part: a second simulated build mints a
    # different per-process token but the very same principal subject.
    second_announced: list[str] = []
    second = compose(
        tmp_path / "dev-fleet-2.sqlite3", simulate=True, announce=second_announced.append
    )
    assert len(second_announced) == 1
    assert second_announced[0] != announced[0], "tokens are per-process credentials"
    status, _ = await _asgi_request(
        second.app,
        "POST",
        "/api/v1/intents",
        headers={
            "Authorization": f"Bearer {second_announced[0]}",
            "Idempotency-Key": "dev-dispatch-probe",
        },
        json_body={"unit_ids": ["mid"], "direction": "charge", "watts": 500, "ttl_s": 30},
    )
    assert status == 202
    second_active = await _settle(second.intents.active(second.clock.monotonic()))
    assert {intent.actor_identity for intent in second_active} == subjects


async def test_run_mode_without_a_credential_store_refuses_every_bearer(
    tmp_path: Path,
) -> None:
    """`run` mode stays fail-closed with no credential store: every bearer is
    refused — including a token genuinely minted by a simulator deployment —
    while /healthz keeps answering unauthenticated liveness."""
    announced: list[str] = []
    simulator = compose(tmp_path / "sim-grant.sqlite3", simulate=True, announce=announced.append)
    assert len(announced) == 1
    minted = announced[0]
    status, _ = await _asgi_request(
        simulator.app, "GET", "/api/v1/snapshot", headers={"Authorization": f"Bearer {minted}"}
    )
    assert status == 200, "the minted token must be a real simulator credential"

    refused: list[str] = []
    runtime = compose(tmp_path / "run-fleet.sqlite3", announce=refused.append)
    assert refused == [], "run mode must never mint or announce a dev credential"
    for candidate in (minted, f"dev-{'x' * 40}", "Bearer", "anything"):
        status, body = await _asgi_request(
            runtime.app,
            "GET",
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {candidate}"},
        )
        assert status == 401, candidate
        assert body["code"] == "authentication_required"

    status, body = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/intents",
        headers={"Authorization": f"Bearer {minted}", "Idempotency-Key": "run-mode-refusal"},
        json_body={"unit_ids": ["mid"], "direction": "charge", "watts": 500, "ttl_s": 30},
    )
    assert status == 401
    assert body["code"] == "authentication_required"

    # The authenticated three-fact health view stays guarded; /healthz stays
    # the one unauthenticated endpoint and answers liveness only.
    status, _body = await _asgi_request(runtime.app, "GET", "/api/v1/health")
    assert status == 401
    status, body = await _asgi_request(runtime.app, "GET", "/healthz")
    assert status == 200
    assert body == {"ok": True}


async def test_a_configured_credential_reference_stays_fail_closed_even_in_simulate(
    tmp_path: Path,
) -> None:
    """A configured credential reference names a store that does not exist in
    this milestone; the simulator grant never fabricates a principal from it,
    so even simulate mode with authentication configured refuses every bearer."""
    announced: list[str] = []
    runtime = compose_write_enabled(
        tmp_path / "cred-fleet.sqlite3", simulate=True, announce=announced.append
    )
    assert announced == []
    for candidate in (f"dev-{'y' * 40}", "operator-token", "x" * 60):
        status, body = await _asgi_request(
            runtime.app,
            "GET",
            "/api/v1/snapshot",
            headers={"Authorization": f"Bearer {candidate}"},
        )
        assert status == 401, candidate
        assert body["code"] == "authentication_required"
    status, body = await _asgi_request(runtime.app, "GET", "/healthz")
    assert status == 200
    assert body == {"ok": True}


# --- excess-solar export bound (API_CONTRACTS "Excess-solar accelerated
# --- charging (advisory)") ----------------------------------------------------
#
# The deterministic bound is ONE additional min() term on the allocation
# demand for an OPTIMIZER-sourced charge intent: margin-subtracted fleet grid
# export, capped by max_charge_from_export_w, collapsing to 0 whenever any
# fleet unit's grid evidence is missing, quality-bad, or stale.  It can never
# raise power above today's limits, and it never applies to any other source
# or direction.


def _export_control_policy(
    *, export_limit_w: int | None = 2_000, margin_w: int = 200, max_age_s: float = 3.0
) -> Any:
    """A real ControlPolicy with the export triple armed (all-or-none)."""
    values: dict[str, Any] = {
        "version": "export-1",
        "static_charge_limit_w_by_unit": {"mid": 2_500, "rhs": 2_500},
        "static_discharge_limit_w_by_unit": {"mid": 2_500, "rhs": 2_500},
        "fleet_charge_limit_w": 6_000,
        "fleet_discharge_limit_w": 6_000,
        "min_soc_pct": 10.0,
        "max_soc_pct": 95.0,
        "max_soc_jump_pct": 10.0,
        "max_soc_disagreement_pct": 5.0,
        "min_cell_voltage_v": 2.80,
        "max_cell_voltage_v": 3.65,
        "max_cell_imbalance_v": 0.050,
        "expected_cell_count_by_unit": {"mid": 59, "rhs": 59},
        "min_temperature_c": 0.0,
        "max_temperature_c": 45.0,
        "max_temperature_spread_c": 45.0,
        "max_telemetry_age_s": 5.0,
        "max_cell_age_s": 15.0,
        "authorization_lifetime_s": 2.0,
        "heartbeat_interval_s": 1.5,
        "ramp_limit_w_per_s_by_unit": {"mid": 10_000, "rhs": 10_000},
        "apparent_power_limit_va_by_unit": {"mid": 5_000, "rhs": 5_000},
        "reactive_limit_var": 0,
        "stable_samples_needed_to_rearm": 3,
        "blocking_fault_codes": frozenset(),
        "blocking_warning_codes": frozenset(),
    }
    if export_limit_w is not None:
        values.update(
            export_charge_limit_w=export_limit_w,
            export_headroom_margin_w=margin_w,
            export_telemetry_max_age_s=max_age_s,
        )
    try:
        return ControlPolicy(**values)
    except ValidationError as error:
        pytest.fail(f"the ControlPolicy export triple is not implemented: {error}", pytrace=False)
        raise  # pragma: no cover - pytest.fail never returns


def _export_observation(
    *, grid_power_w: float | None, captured_at_mono: float = 100.0
) -> SimpleNamespace:
    return SimpleNamespace(
        dynamic_charge_limit_w=2_500.0,
        dynamic_discharge_limit_w=2_500.0,
        grid_power_w=grid_power_w,
        captured_at_mono=captured_at_mono,
    )


def _optimizer_charge_intent(watts: int = 3_000, units: frozenset[str] = frozenset({"mid"})) -> Any:
    return PowerIntent(
        id="excess-1",
        source=IntentSource.OPTIMIZER,
        selected_unit_ids=frozenset(units),
        direction=Direction.CHARGE,
        watts=watts,
        duration_s=10.0,
        accepted_at_mono=100.0,
        acceptance_revision=1,
        actor_identity="energypod:excess-adviser",
    )


def _allocate_export(intent: Any, observations: dict[str, Any], policy: Any) -> tuple[Any, ...]:
    adapter = _load_class("energypod.runtime.composition", "_FleetAllocatorAdapter")()
    try:
        return tuple(adapter.allocate(intent, observations, policy, 101.0))
    except TypeError as error:
        pytest.fail(
            "the allocator port does not yet accept now_mono (the export bound's freshness "
            f"input): {error}",
            pytrace=False,
        )
        raise  # pragma: no cover - pytest.fail never returns


@pytest.mark.parametrize(
    ("grids", "captured", "export_limit_w", "expected_watts"),
    [
        # Sum 1700 W export - 200 W margin = 1500 W eligible, demand 3000 W.
        ({"mid": 800.0, "rhs": 900.0}, {}, 2_000, 1_500),
        # The margin dominates: only 200 W of export survives it.
        ({"mid": 300.0, "rhs": 100.0}, {}, 2_000, 200),
        # The cap dominates: 4800 W eligible, capped at 2000 W.
        ({"mid": 2_500.0, "rhs": 2_500.0}, {}, 2_000, 2_000),
        # Net import across the fleet: no eligible charge at all.
        ({"mid": -400.0, "rhs": -100.0}, {}, 2_000, 0),
        # One stale phase collapses the whole bound fail-closed.
        ({"mid": 800.0, "rhs": 900.0}, {"rhs": 96.0}, 2_000, 0),
        # Without grid evidence anywhere: no advisory charge.
        ({"mid": None, "rhs": None}, {}, 2_000, 0),
    ],
)
def test_allocator_bounds_optimizer_charge_by_measured_export(
    grids: dict[str, float | None],
    captured: dict[str, float],
    export_limit_w: int | None,
    expected_watts: int,
) -> None:
    observations = {
        unit: _export_observation(grid_power_w=grid, captured_at_mono=captured.get(unit, 100.0))
        for unit, grid in grids.items()
    }
    policy = _export_control_policy(export_limit_w=export_limit_w)

    proposals = _allocate_export(_optimizer_charge_intent(), observations, policy)

    assert [proposal.unit_id for proposal in proposals] == ["mid"]
    assert proposals[0].watts == expected_watts
    assert getattr(proposals[0], "export_bounded", False) is True, (
        "the proposal must carry the export-bounded flag so the kernel's evidence "
        "denial can apply to it"
    )


def test_export_bound_is_zero_when_the_policy_triple_is_not_armed() -> None:
    """No armed export triple means no advisory charging: the fail-closed default."""
    observations = {unit: _export_observation(grid_power_w=2_500.0) for unit in ("mid", "rhs")}
    policy = _export_control_policy(export_limit_w=None)

    proposals = _allocate_export(_optimizer_charge_intent(), observations, policy)

    assert proposals[0].watts == 0


def test_export_bound_never_touches_manual_or_discharge_intents() -> None:
    """The bound scopes to OPTIMIZER charge intents only: a manual charge or an
    optimizer discharge allocates exactly as today, importing fleet or not."""
    observations = {unit: _export_observation(grid_power_w=-400.0) for unit in ("mid", "rhs")}
    policy = _export_control_policy()

    manual = PowerIntent(
        id="manual-1",
        source=IntentSource.MANUAL,
        selected_unit_ids=frozenset({"mid"}),
        direction=Direction.CHARGE,
        watts=2_000,
        duration_s=60.0,
        accepted_at_mono=100.0,
        acceptance_revision=2,
        actor_identity="operator:local",
    )
    discharge = PowerIntent(
        id="excess-2",
        source=IntentSource.OPTIMIZER,
        selected_unit_ids=frozenset({"mid"}),
        direction=Direction.DISCHARGE,
        watts=2_000,
        duration_s=10.0,
        accepted_at_mono=100.0,
        acceptance_revision=3,
        actor_identity="energypod:excess-adviser",
    )

    manual_proposals = _allocate_export(manual, observations, policy)
    discharge_proposals = _allocate_export(discharge, observations, policy)

    assert manual_proposals[0].watts == 2_000
    assert getattr(manual_proposals[0], "export_bounded", True) is False
    assert discharge_proposals[0].watts == 2_000
    assert getattr(discharge_proposals[0], "export_bounded", True) is False


def test_allocator_adapter_distributes_multi_unit_requests_by_headroom() -> None:
    """The composed headroom path feeds the capacity-weighted distributor.

    The adapter's per-unit headroom is min(static policy limit, served dynamic
    BMS limit); a fleet request under that combined headroom must reach every
    selected unit, not just the first sorted one (2026-08-23 operator
    complaint: one unit absorbed each whole request while the rest sat at
    zero-watt proposals).
    """
    observations = {
        "mid": SimpleNamespace(dynamic_charge_limit_w=8_056.0, dynamic_discharge_limit_w=8_056.0),
        "rhs": SimpleNamespace(dynamic_charge_limit_w=6_752.0, dynamic_discharge_limit_w=6_752.0),
    }
    policy = _export_control_policy()

    fleet = PowerIntent(
        id="fleet-1",
        source=IntentSource.MANUAL,
        selected_unit_ids=frozenset({"mid", "rhs"}),
        direction=Direction.DISCHARGE,
        watts=3_000,
        duration_s=60.0,
        accepted_at_mono=100.0,
        acceptance_revision=1,
        actor_identity="operator:local",
    )

    proposals = _allocate_export(fleet, observations, policy)

    by_unit = {proposal.unit_id: proposal.watts for proposal in proposals}
    # Static caps dominate both units (2500 W < 8056/6752 W dynamic), so the
    # 3000 W split is 1500/1500 with neither unit at a zero-watt proposal.
    assert by_unit == {"mid": 1_500, "rhs": 1_500}
    assert sum(by_unit.values()) == 3_000


def test_disabled_excess_charging_composes_no_adviser(tmp_path: Path) -> None:
    """Default configuration: no adviser handle is composed at all."""
    runtime = compose_write_enabled(tmp_path / "no-adviser.sqlite3")

    assert getattr(runtime, "excess_adviser", "__missing__") is None, (
        "an unconfigured deployment must compose no excess-charge adviser"
    )
    assert getattr(runtime.policy, "export_charge_limit_w", None) is None


async def test_enabled_excess_charging_composes_the_adviser_and_armed_policy(
    tmp_path: Path,
) -> None:
    payload = _write_enabled_payload(tmp_path / "adviser.sqlite3")
    payload["excess_charging"] = {"enabled": True}
    try:
        config = _validate(payload)
    except ValidationError as error:
        pytest.fail(f"the excess_charging configuration block is not implemented: {error}")
        raise  # pragma: no cover
    runtime = _compose_with(config, simulate=True)

    assert getattr(runtime, "excess_adviser", None) is not None, (
        "an enabled deployment must compose the excess-charge adviser"
    )
    assert runtime.policy.export_charge_limit_w == 2_500
    assert runtime.policy.export_headroom_margin_w == 200
    assert runtime.policy.export_telemetry_max_age_s == 3.0
    await _shutdown_actors(runtime)


# --- P6 composition rescope + the P3 boot-loaded gate (DESIGN_EXCESS_ACTIVATION) --
#
# A PRESENT block composes the machinery and `enabled` gates participation:
# export triple armed, PCS block promoted, adviser + controller + projection
# composed -- suspended at boot when `enabled: false` or the acknowledgement
# is missing, which is what makes the console's first enable possible without
# a config edit.  An ABSENT block composes nothing, byte-identical to today.


async def test_present_but_disabled_excess_charging_composes_suspended_machinery(
    tmp_path: Path,
) -> None:
    """P6: `enabled: false` (explicit) composes but suspends at boot."""
    payload = _write_enabled_payload(tmp_path / "adviser-suspended.sqlite3")
    payload["excess_charging"] = {"enabled": False}
    runtime = _compose_with(_validate(payload), simulate=True)

    assert runtime.excess_adviser is not None, "a present block composes the adviser"
    assert runtime.excess_controller is not None
    assert runtime.policy.export_charge_limit_w == 2_500, "the triple arms on block presence"
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert "adviser_state" in snapshot
    state = snapshot["adviser_state"]
    assert state["enabled"] is False
    assert state["enabled_origin"] == "config"
    assert state["hysteresis_state"] == "inactive"
    assert state["charge_cap_w"] == 2_500
    assert "disabled_by_config" in state["reason_codes"]
    # The toggle is commissioned on a present block: disable answers (noop
    # semantics land with the facade family; here it must not be refused as
    # not-commissioned).
    result = await runtime.facade.set_excess_charging(
        action="disable",
        confirmation="EXCESS",
        principal=OPERATOR,
        idempotency_key="excess-composed-disable",
        request_id="excess-composed-disable-request",
    )
    assert result["enabled"] is False
    await _shutdown_actors(runtime)


async def test_absent_excess_block_composes_nothing_and_refuses_the_toggle(
    tmp_path: Path,
) -> None:
    """P6: an ABSENT block composes nothing -- no adviser, no controller, no
    triple, no projection key, and the toggle answers not-commissioned.
    Byte-identical to today's absent-block behavior."""
    runtime = compose_write_enabled(tmp_path / "no-adviser.sqlite3")

    assert runtime.excess_adviser is None
    assert runtime.excess_controller is None
    assert runtime.policy.export_charge_limit_w is None
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert "adviser_state" not in snapshot
    with pytest.raises(Exception) as caught:
        await runtime.facade.set_excess_charging(
            action="enable",
            confirmation="EXCESS",
            economics="NET_BILLED",
            principal=OPERATOR,
            idempotency_key="excess-absent",
            request_id="excess-absent-request",
        )
    assert getattr(caught.value, "code", "") == "excess_charging_not_commissioned"


async def test_an_enabled_block_without_the_acknowledgement_composes_suspended(
    tmp_path: Path,
) -> None:
    """P3's fail-closed gate at the composition level: even a config
    `enabled: true` cannot silently participate without the captured fact --
    the site composes suspended with `economics_acknowledgement_required`."""
    payload = _write_enabled_payload(tmp_path / "adviser-unacked.sqlite3")
    payload["excess_charging"] = {"enabled": True}
    runtime = _compose_with(_validate(payload), simulate=True)

    assert runtime.excess_adviser is not None
    controller = runtime.excess_controller
    assert controller is not None
    assert controller.enabled is True, "the desired participation reads enabled"
    assert controller.acknowledged_economics is False
    assert controller.participation_verdict() == "economics_acknowledgement_required"
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    state = snapshot["adviser_state"]
    assert state["enabled"] is True
    assert state["acknowledged_economics"] is False
    assert state["hysteresis_state"] == "inactive"
    assert "economics_acknowledgement_required" in state["reason_codes"]
    # The gate holds at the surface too: the first enable without the
    # acknowledgement is refused with exactly what to send.
    with pytest.raises(Exception) as caught:
        await runtime.facade.set_excess_charging(
            action="enable",
            confirmation="EXCESS",
            principal=OPERATOR,
            idempotency_key="excess-unacked",
            request_id="excess-unacked-request",
        )
    assert getattr(caught.value, "code", "") == "economics_acknowledgement_required"
    await _shutdown_actors(runtime)


async def test_the_acknowledgement_is_durable_and_boot_loads_into_the_gate(
    tmp_path: Path,
) -> None:
    """P3: the once-ever fact is a durable audit row (deterministic event id)
    loaded at boot into the gate -- the first enable of the NEXT process needs
    no economics field.  Simulator persistence is in-memory by design; this
    pins the durable run-mode path."""
    database = tmp_path / "adviser-ack.sqlite3"
    payload = _write_enabled_payload(database)
    payload["excess_charging"] = {"enabled": False}
    first = _compose_with(_validate(payload), simulate=False)
    captured = await first.facade.set_excess_charging(
        action="enable",
        confirmation="EXCESS",
        economics="NET_BILLED",
        principal=OPERATOR,
        idempotency_key="excess-ack-capture",
        request_id="excess-ack-capture-request",
    )
    assert captured["acknowledged_economics"] is True
    await _shutdown_actors(first)

    second = _compose_with(_validate(payload), simulate=False)
    controller = second.excess_controller
    assert controller is not None
    assert controller.acknowledged_economics is True, "boot loads the durable fact"
    # P1: the participation toggle itself never persisted -- boot recomposes
    # from the config default (disabled), acknowledged.
    assert controller.enabled is False
    assert controller.enabled_origin == "config"
    result = await second.facade.set_excess_charging(
        action="enable",
        confirmation="EXCESS",
        principal=OPERATOR,
        idempotency_key="excess-ack-restart",
        request_id="excess-ack-restart-request",
    )
    assert result["enabled"] is True, "no economics field needed ever again"
    assert result["acknowledged_economics"] is True
    await _shutdown_actors(second)


async def test_supervision_drives_the_adviser_projection_and_publishes_state_events(
    tmp_path: Path,
) -> None:
    """DESIGN_EXCESS_ACTIVATION §2 (publication wiring): the composed fleet
    loop itself drives the post-tick projection update and publishes
    ``excess_adviser.state_changed`` on the shared bus — no external caller,
    no separate publisher task (publication rides with the tick exactly like
    every other composed event path)."""
    payload = _write_enabled_payload(tmp_path / "adviser-events.sqlite3")
    payload["policy"]["maximum_cell_imbalance_v"] = 0.50
    payload["excess_charging"] = {"enabled": True}
    runtime = _compose_with(_validate(payload), simulate=True, clock=ScriptedClock())

    controller = getattr(runtime, "excess_controller", None)
    assert controller is not None, "a composed adviser must expose its controller"

    seen: list[dict[str, Any]] = []

    async def consume(iterator: Any) -> None:
        async for event in iterator:
            if event.get("type") == "excess_adviser.state_changed":
                seen.append(event)
                return

    subscription = runtime.event_bus.subscribe(after_sequence=None)
    consumer = asyncio.create_task(consume(subscription))
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="the application lifespan never reported supervision startup",
        )
        assert session.seen("lifespan.startup.complete"), f"startup failed: {session.events!r}"
        await session.pump_until(
            lambda: bool(seen), message="supervision never published an adviser state event"
        )
    finally:
        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the application lifespan never reported supervision shutdown",
        )
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        await close_subscription(subscription)

    state = seen[0]["payload"]
    assert state["enabled"] is True
    assert state["enabled_origin"] == "config"
    assert state["active"] is False
    assert state["heartbeat"] is False
    assert state["export_evidence"] in {"good", "missing", "stale"}
    assert state["reason_codes"], "the projection never carries an empty vocabulary"
    # The single-writer discipline is structural: the controller the loop
    # drives is the same object the composed runtime exposes.
    assert controller.state_payload()["enabled"] is True


async def test_cancel_intent_endpoint_cancels_the_active_intent(tmp_path: Path) -> None:
    """API_CONTRACTS "Cancel intent" (2026-08-23): POST /api/v1/intents/cancel
    is dispatch-scoped with no interactive requirement, demands an
    Idempotency-Key, cancels by exact id or "current", and answers with the
    structured envelope for unknown ids and empty fleets."""
    announced: list[str] = []
    runtime = compose(tmp_path / "cancel.sqlite3", simulate=True, announce=announced.append)
    bearer = {"Authorization": f"Bearer {announced[0]}"}

    # No key: the structured refusal, never a silent pass-through.
    status, body = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/intents/cancel",
        headers=bearer,
        json_body={"intent_id": "current"},
    )
    assert status == 400, body
    assert body["code"] == "idempotency_key_required"

    # Nothing active: the precise conflict, not a fabricated success.
    status, body = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/intents/cancel",
        headers={**bearer, "Idempotency-Key": "cancel-empty"},
        json_body={"intent_id": "current"},
    )
    assert status == 409, body

    status, accepted = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/intents",
        headers={**bearer, "Idempotency-Key": "cancel-source-dispatch"},
        json_body={"unit_ids": ["mid"], "direction": "charge", "watts": 500, "ttl_s": 30},
    )
    assert status == 202, accepted
    intent_id = accepted["intent_id"]

    status, cancelled = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/intents/cancel",
        headers={**bearer, "Idempotency-Key": "cancel-current-1"},
        json_body={"intent_id": "current"},
    )
    assert status == 200, cancelled
    assert cancelled == {
        "intent_id": intent_id,
        "status": "cancelled",
        "unit_ids": ["mid"],
        "degraded": [],
    }
    assert not await _settle(runtime.intents.active(runtime.clock.monotonic()))

    # Idempotent replay of the same key returns the same result.
    status, replay = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/intents/cancel",
        headers={**bearer, "Idempotency-Key": "cancel-current-1"},
        json_body={"intent_id": "current"},
    )
    assert status == 200
    assert replay == cancelled

    # An exact id that is no longer active is the structured 404.
    status, body = await _asgi_request(
        runtime.app,
        "POST",
        "/api/v1/intents/cancel",
        headers={**bearer, "Idempotency-Key": "cancel-gone"},
        json_body={"intent_id": intent_id},
    )
    assert status == 404, body
    assert body["code"] == "intent_not_found"


async def test_suppressed_heartbeat_failures_are_audited_and_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """W5 (2026-08-23): the fleet loop survives a failing heartbeat per cycle,
    but total actuation loss must never be invisible -- every suppressed
    failure is audited as heartbeat_failed for its unit and named in the
    process log, while supervision keeps running."""
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True, clock=ScriptedClock())
    actor = runtime.actors[UNIT_IDS[0]]
    original = actor.heartbeat_once
    heartbeats = itertools.count()

    async def failing_heartbeat() -> None:
        if next(heartbeats) >= 1:
            raise OSError("gateway write stalled")
        await original()

    monkeypatch.setattr(actor, "heartbeat_once", failing_heartbeat)
    baseline = set(asyncio.all_tasks())
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="the application lifespan never reported supervision startup",
        )
        assert session.seen("lifespan.startup.complete"), session.events

        def suppressed_rows() -> list[Any]:
            return [
                event
                for event in runtime.audit.recent(limit=16)
                if getattr(event, "event_type", None) == "heartbeat_failed"
            ]

        await session.pump_until(
            lambda: len(suppressed_rows()) >= 2,
            message="a failing heartbeat must be audited every suppressed cycle",
        )
        rows = suppressed_rows()
        assert {row.unit_id for row in rows} == {UNIT_IDS[0]}
        assert all(row.result == "suppressed" for row in rows)
        # Supervision survived the failures: the fleet task is still running.
        assert any(
            task.get_name() == "energypod-supervision:fleet-cycle"
            for task in asyncio.all_tasks() - baseline - {session.app_task}
        ), "a suppressed per-cycle heartbeat failure must never halt supervision"
    finally:
        await session.close()

    captured = capsys.readouterr()
    assert "HEARTBEAT FAILURE" in captured.out and UNIT_IDS[0] in captured.out, (
        "the process log must name the unit whose heartbeat was suppressed"
    )


# --- concurrent per-unit operation over the composed simulated fleet ------------
#
# The operator's exact requirement (2026-08-24): two accepted intents on
# DISJOINT batteries must run in the SAME control cycle -- MID charging while
# RHS discharges, both authorized and measured simultaneously, one cycle's
# decision row carrying both units' directions and watts.

_PCS_APPLIED_ACTIVE_ADDRESS = 0x1060 + 17
_SYSTEM_MEASURED_ACTIVE_ADDRESS = 0x0100 + 20


class ManualClock:
    """Deterministic clock that moves only through explicit ``advance`` calls.

    Unlike ``ScriptedClock`` (whose every sleep advances scripted time), a
    heartbeat's preemption timer sleeps without moving time, so the mint-to-
    renewal gap of a manually driven fleet cycle is exactly the scripted
    control period -- the same cadence the supervised loop holds.
    """

    def __init__(self) -> None:
        self.elapsed_s = 0.0

    def wall_now(self) -> datetime:
        return _SCRIPT_START + timedelta(seconds=self.elapsed_s)

    def monotonic(self) -> float:
        return self.elapsed_s

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


def _applied_active_w(pod: Any) -> int:
    from energypod.adapters.modbus.protocol_codec import decode_signed16

    return decode_signed16(pod.read(_PCS_APPLIED_ACTIVE_ADDRESS, 1)[0])


def _measured_active_w(pod: Any) -> int:
    from energypod.adapters.modbus.protocol_codec import decode_signed16

    return decode_signed16(pod.read(_SYSTEM_MEASURED_ACTIVE_ADDRESS, 1)[0])


async def _drive_one_fleet_cycle(runtime: Any) -> None:
    """The supervision loop's own ordering: heartbeats, then polls, then tick."""
    runtime.clock.elapsed_s += 0.4
    for actor in runtime.actors.values():
        await actor.heartbeat_once()
    for actor in runtime.actors.values():
        await actor.poll_once()
    await runtime.kernel.tick()


async def test_disjoint_intents_run_concurrently_over_the_simulated_fleet(
    tmp_path: Path,
) -> None:
    """MID charges 800 W while RHS discharges 400 W in ONE cycle: one audit
    row carries both directions, one batch carries both capabilities, both
    devices serve their own signed objective at the same moment."""
    runtime = compose_write_enabled(
        tmp_path / "fleet.sqlite3",
        simulate=True,
        clock=ManualClock(),
        # The simulator's seeded cells spread wider than the live fleet's
        # 50 mV commissioning bound (the golden scenarios commission the same
        # 0.50 V spread); everything else stays the write-enabled policy.
        policy_overrides={"maximum_cell_imbalance_v": 0.50},
    )
    assert runtime.simulators is not None
    for actor in runtime.actors.values():
        await actor.start()
    try:
        for _ in range(runtime.policy.stable_samples_needed_to_rearm):
            runtime.clock.elapsed_s += 0.05
            for actor in runtime.actors.values():
                await actor.poll_once()
        for actor in runtime.actors.values():
            assert actor.lifecycle is UnitLifecycle.DISARMED
        await runtime.facade.arm(
            unit_ids=list(UNIT_IDS),
            principal=OPERATOR,
            idempotency_key="concurrent-arm-0001",
            request_id="concurrent-arm-0001-request",
        )
        runtime.clock.elapsed_s += 0.4
        for actor in runtime.actors.values():
            await actor.poll_once()

        # The operator's two dispatches, in quick succession.
        mid_view = await runtime.facade.submit_intent(
            unit_ids=["mid"],
            direction="charge",
            watts=800,
            ttl_s=30.0,
            reason="concurrent: mid charge",
            principal=OPERATOR,
            idempotency_key="concurrent-mid-0001",
            request_id="concurrent-mid-0001-request",
        )
        rhs_view = await runtime.facade.submit_intent(
            unit_ids=["rhs"],
            direction="discharge",
            watts=400,
            ttl_s=30.0,
            reason="concurrent: rhs discharge",
            principal=OPERATOR,
            idempotency_key="concurrent-rhs-0001",
            request_id="concurrent-rhs-0001-request",
        )
        assert mid_view["status"] == rhs_view["status"] == "accepted"

        # Ramp allowance (1000 W/s x 0.4 s) means the charge reaches its full
        # 800 W on the second renewal; drive four supervised cycles.
        for _ in range(4):
            await _drive_one_fleet_cycle(runtime)

        decisions = [
            event
            for event in await _settle(runtime.audit.recent(limit=12))
            if getattr(event, "event_type", None) == "control_decision"
        ]
        assert decisions, "the concurrent cycle must be audited"
        latest = decisions[0]
        assert dict(latest.directions_by_unit) == {"mid": "charge", "rhs": "discharge"}
        assert dict(latest.authorized_watts_by_unit) == {"mid": 800, "rhs": 400}
        assert latest.intent_id is None, "a composed row names its cycle, not one intent"
        assert latest.correlation_id == f"cycle:{latest.cycle_id}"
        # EVERY supervised cycle composed both intents, not just the newest.
        for event in decisions:
            assert dict(event.directions_by_unit) == {"mid": "charge", "rhs": "discharge"}

        grants = [
            event
            for event in await _bus_events(runtime, limit=256)
            if event["type"] == "authorization.granted"
        ]
        assert grants, "each concurrent cycle must announce its grant"
        latest_grant = grants[-1]["payload"]
        assert latest_grant["unit_ids"] == ["mid", "rhs"]
        assert latest_grant["watts_by_unit"] == {"mid": 800, "rhs": 400}
        assert latest_grant["directions_by_unit"] == {"mid": "charge", "rhs": "discharge"}

        # BOTH devices serve their own signed objective at the same moment:
        # negative = charge, positive = discharge (live-proven convention).
        mid_pod, rhs_pod = runtime.simulators["mid"], runtime.simulators["rhs"]
        assert _applied_active_w(mid_pod) == -800
        assert _applied_active_w(rhs_pod) == 400
        assert _measured_active_w(mid_pod) == -800
        assert _measured_active_w(rhs_pod) == 400

        snapshot = await _settle(runtime.facade.snapshot(principal=OPERATOR))
        units = {view["unit_id"]: view for view in snapshot["units"]}
        assert units["mid"]["lifecycle"] == "active"
        assert units["rhs"]["lifecycle"] == "active"
    finally:
        for actor in runtime.actors.values():
            with contextlib.suppress(Exception):
                await actor.shutdown()


# --- self-healing awareness layer (R4): supervision drives the monitor -----------


async def test_supervision_drives_the_recovery_monitor_every_cycle(tmp_path: Path) -> None:
    """API_CONTRACTS "Self-healing awareness layer": the composed fleet loop
    itself drives the detection pass -- the peeked authority, the poll
    outcomes, the fresh observations -- without any external caller, and the
    facade projects the derived view.  A healthy simulated fleet never alarms."""
    runtime = compose_write_enabled(
        tmp_path / "fleet.sqlite3",
        simulate=True,
        clock=ScriptedClock(),
        # The simulated fleet's top-of-charge balancing spread sits above the
        # 0.050 V commissioning gate; the operator-relaxed 0.500 V tier
        # (live config rev 4) is the policy this fleet actually dispatches
        # under, so the kernel mints authority for the scenario to observe.
        policy_overrides={"maximum_cell_imbalance_v": 0.50},
    )
    session = _LifespanSession(runtime.app)
    try:
        session.send("lifespan.startup")
        await session.pump_until(
            lambda: session.seen("lifespan.startup.complete")
            or session.seen("lifespan.startup.failed"),
            message="the application lifespan never reported supervision startup",
        )
        assert session.seen("lifespan.startup.complete"), f"startup failed: {session.events!r}"
        await session.pump_until(
            lambda: all(actor.qualified is True for actor in runtime.actors.values()),
            message="the simulated fleet never qualified",
        )
        # The monitor is driven by the loop alone: its records hold fresh
        # facts (observe_cycle is the only writer of these).
        await session.pump_until(
            lambda: all(
                runtime.recovery._records[unit_id].measured_watts is not None
                for unit_id in UNIT_IDS
            ),
            message="supervision never handed the recovery monitor an observation",
        )
        await runtime.facade.arm(
            unit_ids=list(UNIT_IDS),
            principal=OPERATOR,
            idempotency_key="recovery-arm",
            request_id="recovery-arm-request",
        )
        await runtime.facade.submit_intent(
            unit_ids=["mid"],
            direction="charge",
            watts=600,
            ttl_s=30.0,
            reason="recovery supervision wiring",
            principal=OPERATOR,
            idempotency_key="recovery-dispatch",
            request_id="recovery-dispatch-request",
        )
        # The loop peeks the authority the heartbeat consumes and hands it to
        # the monitor: the record's authorized figure must go positive.
        await session.pump_until(
            lambda: runtime.recovery._records["mid"].authorized_watts > 0,
            message="supervision never reported the authorized watts to the monitor",
        )

        states = await runtime.recovery.unit_health_states()
        assert set(states) == set(UNIT_IDS)
        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        units = {view["unit_id"]: view for view in snapshot["units"]}
        for unit_id in UNIT_IDS:
            assert units[unit_id]["health_state"] in {"healthy", "self_healing"}, units[unit_id]
            assert units[unit_id]["health_state"] == states[unit_id].state.value

        # A healthy simulated fleet never raises a detection fact.
        for event in runtime.audit.recent(limit=128):
            assert event.event_type not in {
                "actuation_incoherent",
                "objective_echo",
                "unexpected_autonomy",
            }, event.event_type

        session.send("lifespan.shutdown")
        await session.pump_until(
            lambda: session.seen("lifespan.shutdown.complete")
            or session.seen("lifespan.shutdown.failed"),
            message="the application lifespan never reported supervision shutdown",
        )
        assert session.seen("lifespan.shutdown.complete"), f"shutdown failed: {session.events!r}"
    finally:
        await session.close()


# --- DESIGN_SCHEDULES §2/§5 B5: the composed schedule surface --------------------


def _schedule_payload(
    database: Path,
    *,
    windows: list[list[str]] | None = None,
    ttl_s: float = 10.0,
    excess: dict[str, Any] | None = None,
    write_enabled: bool = False,
) -> dict[str, Any]:
    payload = _write_enabled_payload(database) if write_enabled else _config_payload(database)
    payload["schedule"] = {
        "allowed_windows_local": windows or [["06:00", "20:00"]],
        "intent_ttl_s": ttl_s,
    }
    if excess is not None:
        payload["excess_charging"] = excess
    return payload


def compose_schedule(
    database: Path,
    *,
    clock: Any | None = None,
    announce: Callable[[str], None] | None = None,
    windows: list[list[str]] | None = None,
    ttl_s: float = 10.0,
    excess: dict[str, Any] | None = None,
    write_enabled: bool = False,
) -> Any:
    config = _validate(
        _schedule_payload(
            database, windows=windows, ttl_s=ttl_s, excess=excess, write_enabled=write_enabled
        )
    )
    return _compose_with(config, simulate=True, clock=clock, announce=announce)


async def test_a_present_schedule_block_composes_the_surface(tmp_path: Path) -> None:
    runtime = compose_schedule(tmp_path / "sched.sqlite3")

    assert runtime.schedule_surface is not None
    assert runtime.schedule_runner is not None
    assert runtime.schedule_surface.policy.posture == "yield"
    assert runtime.schedule_surface.policy.wire_windows() == [["06:00", "20:00"]]
    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert "schedule_state" in snapshot
    projection = snapshot["schedule_state"]
    assert projection["active"] is False
    assert projection["reason_codes"] == ["no_plan"]
    assert projection["posture"] == "yield"


async def test_an_absent_schedule_block_composes_nothing_and_refuses_both_routes(
    tmp_path: Path,
) -> None:
    announced: list[str] = []
    runtime = compose(tmp_path / "no-sched.sqlite3", simulate=True, announce=announced.append)
    assert runtime.schedule_surface is None
    assert runtime.schedule_runner is None

    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
    assert "schedule_state" not in snapshot, "absent block = byte-identical snapshot"

    bearer = {"Authorization": f"Bearer {announced[0]}"}
    status, body = await _asgi_request(runtime.app, "GET", "/api/v1/schedule", headers=bearer)
    assert status == 409
    assert body["code"] == "schedule_not_commissioned"
    status, put_body = await _asgi_request(
        runtime.app,
        "PUT",
        "/api/v1/schedule",
        headers={**bearer, "Idempotency-Key": "absent-1"},
        json_body={"expected_version": None, "timezone": "Australia/Brisbane", "entries": []},
    )
    assert status == 409
    assert put_body["code"] == "schedule_not_commissioned"


async def test_the_schedule_routes_serve_the_policy_when_composed(tmp_path: Path) -> None:
    announced: list[str] = []
    runtime = compose_schedule(tmp_path / "sched-routes.sqlite3", announce=announced.append)
    bearer = {"Authorization": f"Bearer {announced[0]}"}

    status, view = await _asgi_request(runtime.app, "GET", "/api/v1/schedule", headers=bearer)

    assert status == 200, view
    assert view["plan"] is None
    assert view["policy"] == {
        "posture": "yield",
        "allowed_windows_local": [["06:00", "20:00"]],
        "intent_ttl_s": 10.0,
    }
    assert view["acknowledged_night_windows"] is False
    assert view["next_action"] is None


async def test_the_runner_ticks_before_the_adviser_in_the_fleet_cycle(tmp_path: Path) -> None:
    """DESIGN_SCHEDULES §2 ordering, pinned: the schedule's claim is a
    published fact and the adviser is the opportunist — the runner ticks
    BEFORE the adviser step so a yield resolves within one cycle."""
    runtime = compose_schedule(
        tmp_path / "sched-order.sqlite3",
        clock=ScriptedClock(),
        excess={"enabled": False},
        write_enabled=True,
    )
    assert runtime.excess_adviser is not None
    order: list[str] = []
    runner_tick = runtime.schedule_runner.tick
    adviser_tick = runtime.excess_adviser.tick

    async def traced_schedule() -> None:
        order.append("schedule")
        await runner_tick()

    async def traced_adviser() -> Any:
        order.append("adviser")
        return await adviser_tick()

    runtime.schedule_runner.tick = traced_schedule  # type: ignore[method-assign]
    runtime.excess_adviser.tick = traced_adviser  # type: ignore[method-assign]

    session = _LifespanSession(runtime.app)
    session.send("lifespan.startup")
    await session.pump_until(lambda: order.count("adviser") >= 2, message="two fleet cycles")
    await session.close()

    assert order.index("schedule") < order.index("adviser"), "schedule first, adviser second"
    from itertools import pairwise

    assert all(earlier == "schedule" for earlier, later in pairwise(order) if later == "adviser"), (
        "every adviser tick follows a schedule tick in the same cycle"
    )


async def test_a_published_window_runs_the_full_composed_path(tmp_path: Path) -> None:
    """Publish (per-battery watts) -> runner opens the window -> a live
    SCHEDULE intent with per-unit targets -> the projection says so -> the
    window ends and the closing transition publishes."""
    clock = ScriptedClock()  # 2026-08-21 12:00 UTC = Friday 22:00 Brisbane
    announced: list[str] = []
    runtime = compose_schedule(
        tmp_path / "sched-live.sqlite3",
        clock=clock,
        announce=announced.append,
        windows=[["20:00", "00:00"]],  # a night-granting partition site
    )
    bearer = {**{"Authorization": f"Bearer {announced[0]}"}, "Idempotency-Key": "publish-1"}

    status, published = await _asgi_request(
        runtime.app,
        "PUT",
        "/api/v1/schedule",
        headers=bearer,
        json_body={
            "expected_version": None,
            "timezone": "Australia/Brisbane",
            "night_posture": "PARTITION_ACKNOWLEDGED",
            "entries": [
                {
                    "entry_id": "night-charge",
                    "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                    "start_local": "21:00",
                    "end_local": "23:59",
                    "action": "charge",
                    "watts_by_unit": {"mid": 700, "rhs": 800},
                    "unit_ids": ["mid", "rhs"],
                    "effective_from": "2020-01-01",
                    "effective_until": "2035-12-31",
                    "priority": 0,
                    "enabled": True,
                }
            ],
        },
    )
    assert status == 200, published
    assert published["version"] == 1
    assert published["acknowledged_night_windows"] is True

    session = _LifespanSession(runtime.app)
    session.send("lifespan.startup")
    try:
        await session.pump_until(
            lambda: runtime.schedule_runner.held_intent_id is not None,
            message="the runner opens the published window",
        )
        active = await _settle(runtime.intents.active(runtime.clock.monotonic()))
        schedule_intents = [intent for intent in active if intent.source.value == "schedule"]
        assert len(schedule_intents) == 1, "exactly one live SCHEDULE intent"
        live = schedule_intents[0]
        assert dict(live.watts_by_unit or {}) == {"mid": 700, "rhs": 800}
        assert live.watts == 1500
        assert live.actor_identity == "energypod:schedule-runner"

        snapshot = await runtime.facade.snapshot(principal=OPERATOR)
        projection = snapshot["schedule_state"]
        assert projection["active"] is True
        assert projection["entry_id"] == "night-charge"
        assert projection["version"] == 1
        assert projection["ends_at"].startswith("2026-08-21T23:59")
        assert projection["reason_codes"] == ["window_open"]

        # The clock rolls past the window end: non-renewal + the closing event.
        clock.elapsed_s += 2 * 3600 + 1800  # 14:30 UTC = 00:30 Saturday
        await session.pump_until(
            lambda: runtime.schedule_runner.held_intent_id is None,
            message="the window ends by non-renewal",
        )
        remaining = await _settle(runtime.intents.active(runtime.clock.monotonic()))
        assert all(intent.source.value != "schedule" for intent in remaining)
        published_events = await _bus_events(runtime)
        closing = [e for e in published_events if e["type"] == "schedule_window.closing"]
        assert closing and closing[-1]["payload"]["reason"] == "window_ended"
        opened = [e for e in published_events if e["type"] == "schedule_window.opened"]
        assert len(opened) == 1, "opened is a transition, never a renewal heartbeat"
        replaced = [e for e in published_events if e["type"] == "schedule.replaced"]
        assert len(replaced) == 1
        assert replaced[0]["payload"]["diff"]["added"] == ["night-charge"]
    finally:
        await session.close()
