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
from energypod.domain import Direction, IntentSource, PowerIntent, UnitLifecycle
from energypod.domain.audit import AuditEvent
from energypod.domain.authorization import AuthorizationBatch, AuthorizedSetpoint
from energypod.runtime.config import ControllerConfig
from energypod.simulator import SimulatorTransport

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


def _compose_with(config: ControllerConfig, *, simulate: bool, clock: Any | None = None) -> Any:
    overrides: dict[str, Any] = {"simulate": simulate}
    if clock is not None:
        overrides["clock"] = clock
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
        "device_command_expiry_evidence": "commissioning://watchdog-trial-2026-08/rev-1",
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
) -> Any:
    config = _validate(_config_payload(database, unit_count=unit_count))
    return _compose_with(config, simulate=simulate, clock=clock)


def compose_write_enabled(
    database: Path,
    *,
    simulate: bool = False,
    clock: Any | None = None,
) -> Any:
    config = _validate(_write_enabled_payload(database))
    return _compose_with(config, simulate=simulate, clock=clock)


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


async def _asgi_request(app: FastAPI, method: str, path: str) -> tuple[int, dict[str, Any]]:
    """Drive one request through the composed ASGI app with no test server."""
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
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

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
        "device_command_expiry_evidence": "commissioning://watchdog-trial-2026-08/rev-1",
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
