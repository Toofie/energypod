"""Contract tests for the runtime composition root (ADR-0003 D1, D4, D6).

``energypod.runtime.composition.build_runtime`` is the only place that may
construct the whole object graph, and boot must be observe-only and disarmed
even when durable stores remember prior authority. The production module is
loaded lazily so this red-phase suite collects before the implementation
exists; every missing contract surfaces as an ordinary test failure, never as
a collection error. No test opens a socket, contacts hardware, or reads
ambient time: construction is the behavior under test, and simulator-backed
actors are the only things explicitly started.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastmcp import Client, FastMCP
from starlette.routing import WebSocketRoute

from energypod.adapters.persistence.sqlite import SQLiteAuditRepository, SQLiteScheduleRepository
from energypod.application.actor import EnergyPodActor
from energypod.application.audit import AuditEventFactory
from energypod.application.control_kernel import ControlKernel
from energypod.application.generation import AuthorityGenerationCoordinator
from energypod.domain import Direction, IntentSource, PowerIntent, UnitLifecycle
from energypod.domain.audit import AuditEvent
from energypod.domain.authorization import AuthorizationBatch, AuthorizedSetpoint
from energypod.runtime.config import ControllerConfig, EndpointConfig

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


def compose(database: Path, *, simulate: bool = False, unit_count: int = 2) -> Any:
    config = _validate(_config_payload(database, unit_count=unit_count))
    return _build_runtime(config, simulate=simulate)


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


def _authorization_batch(units: tuple[str, ...] = UNIT_IDS) -> AuthorizationBatch:
    setpoints = tuple(
        AuthorizedSetpoint(
            unit_id=unit_id,
            connection_epoch=3,
            generation=2,
            cycle_id="cycle-00000000000000000001",
            intent_id="stale-command-1",
            intent_revision=9,
            direction=Direction.DISCHARGE,
            watts=750,
            issued_at_mono=50.0,
            not_before_mono=50.0,
            expires_at_mono=4_000.0,
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
        generation=2,
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

    snapshot = await runtime.facade.snapshot(principal=OPERATOR)
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
    before = asyncio.all_tasks()
    runtime = compose(tmp_path / "fleet.sqlite3")
    assert set(runtime.actors) == set(UNIT_IDS)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not asyncio.all_tasks() - before


def _corrupted(base: ControllerConfig, units: tuple[Any, ...]) -> ControllerConfig:
    values = base.model_dump()
    values["units"] = units
    return ControllerConfig.model_construct(**values)


@pytest.mark.parametrize("kind", ["duplicate_unit_id", "empty_fleet", "malformed_unit_id"])
async def test_invalid_fleet_configuration_raises_eagerly(tmp_path: Path, kind: str) -> None:
    base = _validate(_config_payload(tmp_path / "fleet.sqlite3"))
    first, second = base.units
    twin_endpoint = EndpointConfig(host="192.168.1.99", port=4196)
    if kind == "duplicate_unit_id":
        units: tuple[Any, ...] = (
            first,
            first.model_copy(update={"endpoint": twin_endpoint}),
        )
    elif kind == "empty_fleet":
        units = ()
    else:
        units = (
            second,
            first.model_copy(update={"unit_id": " mid ", "endpoint": twin_endpoint}),
        )
    corrupted = _corrupted(base, units)

    before = asyncio.all_tasks()
    with pytest.raises(ValueError):
        _build_runtime(corrupted)
    await asyncio.sleep(0)
    assert not asyncio.all_tasks() - before


async def test_run_mode_persists_only_audit_and_schedule(tmp_path: Path) -> None:
    database = tmp_path / "fleet.sqlite3"
    runtime = compose(database)

    assert database.exists()
    assert isinstance(runtime.audit, SQLiteAuditRepository)
    assert isinstance(runtime.schedule, SQLiteScheduleRepository)
    for repository in (runtime.intents, runtime.observations, runtime.authorizations):
        assert not isinstance(repository, _SQLITE_TYPES)

    # The volatile stores are live, in-process repositories.
    await runtime.observations.append(_observation_seed("mid", 1))
    assert await runtime.observations.latest("mid") is not None
    assert await runtime.schedule.get() is None


async def test_boot_restores_no_arming_authority_or_active_commands(tmp_path: Path) -> None:
    database = tmp_path / "fleet.sqlite3"
    prior = compose(database)

    # Durable history claims the fleet was ACTIVE with granted authority.
    for unit_id in UNIT_IDS:
        await prior.audit.append(_prior_active_audit_event(unit_id))
    # Live volatile state in the previous runtime: an active command, a
    # latched stop, and current, unexpired authorizations.
    await prior.intents.add(_stale_command_intent())
    await prior.intents.add(_latched_stop_intent())
    await prior.authorizations.publish(_authorization_batch())
    await prior.observations.append(_observation_seed("mid", 1))

    # The seeds are genuinely live in the previous runtime, so the restarted
    # one cannot pass vacuously.
    assert await prior.intents.active(60.0)
    for unit_id in UNIT_IDS:
        assert await prior.authorizations.current(unit_id, 60.0) is not None

    restarted = compose(database)

    # The durable audit trail really was reused; prior authority stays plainly
    # visible in it while the restarted process starts with nothing.
    recent = await restarted.audit.recent(limit=10)
    assert {f"prior-active-authority-{unit_id}" for unit_id in UNIT_IDS} <= {
        event.event_id for event in recent
    }
    for now in (0.0, 60.0, 1_000_000_000.0):
        assert not await restarted.intents.active(now)
        for unit_id in UNIT_IDS:
            assert await restarted.authorizations.current(unit_id, now) is None
    for unit_id in UNIT_IDS:
        assert await restarted.observations.latest(unit_id) is None
        assert restarted.actors[unit_id] is not prior.actors[unit_id]
        assert restarted.actors[unit_id].lifecycle is UnitLifecycle.BOOT


async def test_simulate_boot_stays_observe_only_despite_live_authority(tmp_path: Path) -> None:
    runtime = compose(tmp_path / "fleet.sqlite3", simulate=True)
    await runtime.intents.add(_stale_command_intent(("mid",)))
    await runtime.authorizations.publish(_authorization_batch(("mid",)))
    try:
        for actor in runtime.actors.values():
            await actor.start()
        assert all(
            actor.lifecycle is UnitLifecycle.OBSERVE_ONLY for actor in runtime.actors.values()
        )
        # Boot performed no heartbeat: the freshly published, still-valid
        # authorization was not consumed by any started actor.
        assert await runtime.authorizations.current("mid", 60.0) is not None
        assert await runtime.intents.active(60.0)
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
    await runtime.audit.append(event)
    assert [saved.event_id for saved in await runtime.audit.recent(limit=5)] == [event.event_id]

    # A second simulated build against the same configured database path
    # starts from empty in-memory stores and never touches the file.
    again = compose(database, simulate=True)
    assert not await again.audit.recent(limit=5)
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
