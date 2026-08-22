"""S0 contract tests for the energypod.main CLI entry point (ADR-0003 D6).

``energypod.main`` is the console script pyproject already declares. Importing
it must load nothing but code: no configuration, no server start, no I/O.
``main(argv, server_runner=...)`` returns a process exit code and accepts an
injected server runner so tests capture the composed ASGI app and its serving
arguments without ever binding a port or touching hardware. The production
module is loaded lazily so this red-phase suite collects before the
implementation exists; every missing contract surfaces as an ordinary test
failure, never a collection error. Determinism is enforced with a process
audit hook over import/CLI activity and tmp_path fixtures for every file.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import os
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from types import BuiltinFunctionType, FunctionType, ModuleType, SimpleNamespace
from typing import Any, ClassVar

import pytest
import yaml
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.datastructures import State

from energypod.runtime.config import ControllerConfig

SITE_ID = "entry-site"
TIMEZONE = "Australia/Brisbane"
UNIT_IDS = ("entry-mid", "entry-rhs")
API_ROUTE_PATHS = frozenset({"/api/v1/snapshot", "/api/v1/health"})

# ---------------------------------------------------------------------------
# Side-effect audit: a process-level audit hook records every network,
# subprocess, database, and file-write attempt during the window under test.
# ---------------------------------------------------------------------------

_NETWORK_EVENTS = ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto")
_PROCESS_EVENTS = ("subprocess.", "os.system", "os.posix_spawn")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_TEMPORARY_FLAG = getattr(os, "O_TEMPORARY", 0)
_COMPILED_SUFFIXES = frozenset({".pyc", ".pyo"})
_CONFIGURATION_SUFFIXES = frozenset(
    {".yaml", ".yml", ".toml", ".json", ".ini", ".cfg", ".db", ".sqlite", ".sqlite3"}
)
_AUDIT_EVENTS: list[tuple[str, tuple[Any, ...]]] = []


def _record_audit_event(event: str, args: tuple[Any, ...]) -> None:
    _AUDIT_EVENTS.append((event, args))


sys.addaudithook(_record_audit_event)


def _suffix(text: str) -> str:
    try:
        return PurePath(text).suffix.lower()
    except ValueError:
        return ""


def _inside_pycache(text: str) -> bool:
    try:
        return "__pycache__" in PurePath(text).parts
    except ValueError:
        return False


def _has_write_intent(args: tuple[Any, ...]) -> bool:
    mode = args[1] if len(args) > 1 and isinstance(args[1], str) else None
    if mode is not None:
        return any(flag in mode for flag in ("w", "a", "x", "+"))
    flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
    return bool(flags & _WRITE_FLAGS)


def _open_flags(args: tuple[Any, ...]) -> int:
    return args[2] if len(args) > 2 and isinstance(args[2], int) else 0


def _database_paths(database: Path) -> frozenset[str]:
    """The configured database file plus its sqlite journal/WAL siblings."""
    text = str(database)
    candidates = (text, f"{text}-journal", f"{text}-wal", f"{text}-shm")
    return frozenset(os.path.normcase(candidate) for candidate in candidates)


def _side_effect_violations(
    events: list[tuple[str, tuple[Any, ...]]],
    *,
    allow_config_reads: bool,
    allowed_database: Path | None,
) -> list[str]:
    """Classify recorded audit events into human-readable contract breaches."""
    allowed_paths = (
        _database_paths(allowed_database) if allowed_database is not None else frozenset()
    )
    issues: list[str] = []
    for event, args in events:
        if event.startswith(_NETWORK_EVENTS):
            issues.append(f"network activity: {event}")
        elif event.startswith(_PROCESS_EVENTS):
            issues.append(f"process activity: {event}")
        elif event.startswith("sqlite3."):
            if allowed_database is None:
                issues.append("database connection opened")
            elif event == "sqlite3.connect":
                target = args[0] if args and isinstance(args[0], str | os.PathLike) else None
                if target is None or os.path.normcase(str(target)) not in allowed_paths:
                    issues.append(f"database connection outside the configured store: {target}")
        elif event == "open":
            if not args or not isinstance(args[0], str | os.PathLike):
                # fd-based opens duplicate a path-based open already classified.
                continue
            path = str(args[0])
            suffix = _suffix(path)
            if not allow_config_reads and suffix in _CONFIGURATION_SUFFIXES:
                issues.append(f"configuration artifact opened: {path}")
            if (
                _has_write_intent(args)
                and not _inside_pycache(path)
                and suffix not in (_COMPILED_SUFFIXES)
            ):
                if os.path.normcase(path) in allowed_paths:
                    continue
                if allowed_database is not None and _open_flags(args) & _TEMPORARY_FLAG:
                    # sqlite scratch files vanish on close; only their
                    # connection target is attributable.
                    continue
                issues.append(f"file created or written: {path}")
    return issues


@dataclass
class SideEffectAudit:
    """Scoped window over the process audit log; issues are asserted by tests."""

    allow_config_reads: bool = False
    allowed_database: Path | None = None
    issues: list[str] = field(default_factory=list)
    _mark: int = field(default=0, init=False, repr=False)

    def __enter__(self) -> SideEffectAudit:
        self._mark = len(_AUDIT_EVENTS)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.issues = _side_effect_violations(
            _AUDIT_EVENTS[self._mark :],
            allow_config_reads=self.allow_config_reads,
            allowed_database=self.allowed_database,
        )


# ---------------------------------------------------------------------------
# CLI invocation harness.
# ---------------------------------------------------------------------------


class _Completed:
    """Zero-value awaitable: the injected runner may be called or awaited."""

    def __await__(self) -> Any:
        yield
        return None


@dataclass
class ServerRunner:
    """Injected serving seam: records the app and serving args instead of serving.

    API_CONTRACTS "Runtime composition and entry point" grants
    ``main(argv, server_runner=None)`` with an injected async server runner
    receiving the built app and serving parameters, so the spy tolerates being
    called, awaited, or both, and records positional spellings too.
    """

    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    failure: BaseException | None = None

    def __call__(self, app: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((app, args, kwargs))
        if self.failure is not None:
            raise self.failure
        return _Completed()


@dataclass
class Invocation:
    argv: list[str]
    exit_code: int | None
    system_exit_raised: bool
    runner: ServerRunner
    stdout: str
    stderr: str

    @property
    def combined_output(self) -> str:
        return self.stdout + self.stderr

    def report(self) -> str:
        return (
            f"argv={self.argv!r} exit_code={self.exit_code!r} "
            f"system_exit_raised={self.system_exit_raised} "
            f"stdout={self.stdout!r} stderr={self.stderr!r}"
        )


def assert_failed_cleanly(invocation: Invocation) -> None:
    """Error paths may return a nonzero code or raise SystemExit carrying one."""
    assert isinstance(invocation.exit_code, int) and invocation.exit_code != 0, invocation.report()


def call_main(
    module: ModuleType,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    *,
    runner: ServerRunner | None = None,
) -> Invocation:
    """Call main in-process; a SystemExit escape is recorded, never honored."""
    effective = runner if runner is not None else ServerRunner()
    capsys.readouterr()
    try:
        exit_code = module.main(argv, server_runner=effective)
    except SystemExit as exc:
        captured = capsys.readouterr()
        code = exc.code if isinstance(exc.code, int) else None
        return Invocation(list(argv), code, True, effective, captured.out, captured.err)
    captured = capsys.readouterr()
    code = exit_code if isinstance(exit_code, int) else None
    return Invocation(list(argv), code, False, effective, captured.out, captured.err)


def _engaged_invocation(
    module: ModuleType,
    command: str,
    path: Path,
    capsys: pytest.CaptureFixture[str],
    runner: ServerRunner,
    *,
    serving: bool,
) -> Invocation:
    """Try both granted config spellings; return the one the CLI accepted.

    API_CONTRACTS "Runtime composition and entry point": both ``--config PATH``
    and a positional ``PATH`` are accepted spellings.
    """
    attempts: list[Invocation] = []
    for argv in ([command, "--config", str(path)], [command, str(path)]):
        before = len(runner.calls)
        invocation = call_main(module, argv, capsys, runner=runner)
        attempts.append(invocation)
        engaged = len(runner.calls) > before if serving else invocation.exit_code == 0
        if engaged:
            return invocation
    return attempts[0]


@dataclass
class CompositionSpy:
    """Wraps the real build_runtime; recording only, never faking composition."""

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)


def _wrap_build_runtime(monkeypatch: pytest.MonkeyPatch) -> CompositionSpy | None:
    try:
        composition = importlib.import_module("energypod.runtime.composition")
    except ImportError:
        return None
    real = getattr(composition, "build_runtime", None)
    if not callable(real):
        return None
    spy = CompositionSpy()

    def recording_build(*args: Any, **kwargs: Any) -> Any:
        spy.calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(composition, "build_runtime", recording_build)
    entry = sys.modules.get("energypod.main")
    if entry is not None and getattr(entry, "build_runtime", None) is real:
        monkeypatch.setattr(entry, "build_runtime", recording_build)
    return spy


def _forbid_real_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any reach of the default uvicorn path ignored the injected runner."""
    import uvicorn

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("main ignored the injected server_runner and attempted real serving")

    monkeypatch.setattr(uvicorn, "run", explode)
    monkeypatch.setattr(uvicorn.Server, "serve", explode)
    monkeypatch.setattr(uvicorn.Server, "run", explode)


def _composition_call(spy: CompositionSpy | None) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if spy is None or not spy.calls:
        pytest.fail(
            "run/simulate must compose through energypod.runtime.composition.build_runtime",
            pytrace=False,
        )
    return spy.calls[0]


def _serving_parameter(
    positional: tuple[Any, ...], keyword: dict[str, Any], name: str, index: int
) -> Any:
    """Read one serving parameter from either spelling of the runner call."""
    if name in keyword:
        return keyword[name]
    return positional[index] if len(positional) > index else None


def _simulate_argument(positional: tuple[Any, ...], keyword: dict[str, Any]) -> Any:
    """build_runtime's simulate flag, positional (second argument) or keyword."""
    if "simulate" in keyword:
        return keyword["simulate"]
    return positional[1] if len(positional) > 1 else None


# ---------------------------------------------------------------------------
# Configuration fixtures written through the real strict configuration model.
# ---------------------------------------------------------------------------


def _timing_payload() -> dict[str, Any]:
    # Commissioning fixture shared with the composition contract suite; the
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


def _unit_payload(unit_id: str, port: int) -> dict[str, Any]:
    # Loopback endpoints refuse instantly; nothing may ever dial them here.
    return {
        "unit_id": unit_id,
        "display_name": unit_id.capitalize(),
        "endpoint": {"host": "127.0.0.1", "port": port},
        "transport_profile": "waveshare_rtu_over_tcp",
        "protocol_profile": "iot",
        "device_id": 4,
        "expected_identity": f"BEP-{unit_id.upper()}",
        "expected_cell_count": 59,
    }


def _valid_payload(database: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": 7,
        "mode": "observe_only",
        "site": {
            "site_id": SITE_ID,
            "timezone": TIMEZONE,
            "expected_unit_count": len(UNIT_IDS),
        },
        "units": [_unit_payload(unit_id, 4196 + index) for index, unit_id in enumerate(UNIT_IDS)],
        "timing": _timing_payload(),
        "storage": {"database_path": str(database), "busy_timeout_ms": 250},
    }


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _write_valid_config(path: Path, database: Path) -> ControllerConfig:
    """Round-trip a genuinely valid config through the real model before writing."""
    config = ControllerConfig.model_validate(_valid_payload(database))
    _write_yaml(path, config.model_dump(mode="json"))
    return config


def _invalid_payload(database: Path, flaw: str) -> dict[str, Any]:
    payload = _valid_payload(database)
    if flaw == "unit_count_mismatch":
        payload["site"]["expected_unit_count"] = len(UNIT_IDS) + 1
    elif flaw == "unknown_site_key":
        payload["site"]["surprise"] = "not part of the schema"
    else:
        payload["mode"] = "write_enabled"
    return payload


def _discovered_argv(
    module: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[list[str], Path]:
    """Learn the config spelling check-config accepts, using a known-valid file."""
    valid = tmp_path / "discovery.yaml"
    _write_valid_config(valid, tmp_path / "discovery-unused.sqlite3")
    invocation = _engaged_invocation(
        module, "check-config", valid, capsys, ServerRunner(), serving=False
    )
    if invocation.exit_code != 0:
        pytest.fail(
            f"check-config never accepted a valid configuration: {invocation.report()}",
            pytrace=False,
        )
    return invocation.argv, valid


def _replace_path(argv: list[str], old: Path, new: Path) -> list[str]:
    return [str(new) if part == str(old) else part for part in argv]


@pytest.fixture
def main_module() -> ModuleType:
    try:
        return importlib.import_module("energypod.main")
    except ImportError as exc:
        pytest.fail(f"entry-point contract is not implemented: energypod.main: {exc}")


# ---------------------------------------------------------------------------
# Import purity (ADR-0003 D6: importable with no side effects at import time).
# ---------------------------------------------------------------------------

_RUNTIME_CONSTRUCTION_TYPES: tuple[type, ...] = (
    FastAPI,
    asyncio.AbstractEventLoop,
    socket.socket,
    sqlite3.Connection,
)
_CODE_ATTRIBUTES = (FunctionType, BuiltinFunctionType, type, ModuleType, enum.Enum)


def _is_runtime_construction(value: Any) -> bool:
    """Behavioral test: is this module global a constructed runtime object?

    Module constants and ``from __future__`` imports are explicitly permitted
    by the grant; only runtime constructions fail the pin.
    """
    if isinstance(value, _RUNTIME_CONSTRUCTION_TYPES):
        return True
    module = getattr(value, "__module__", "")
    return (
        isinstance(module, str)
        and module.startswith("energypod.")
        and not isinstance(value, _CODE_ATTRIBUTES)
    )


def test_import_is_side_effect_free_and_constructs_nothing() -> None:
    sys.modules.pop("energypod.main", None)
    importlib.invalidate_caches()
    with SideEffectAudit() as audit:
        try:
            module = importlib.import_module("energypod.main")
        except ImportError as exc:
            pytest.fail(f"entry-point contract is not implemented: energypod.main: {exc}")
    assert audit.issues == []
    assert callable(getattr(module, "main", None))
    constructed = {
        name: type(value).__name__
        for name, value in vars(module).items()
        if not name.startswith("_") and _is_runtime_construction(value)
    }
    assert constructed == {}, "importing energypod.main must not construct runtime state"


def test_missing_command_prints_usage_without_building_anything(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with SideEffectAudit() as audit:
        invocation = call_main(main_module, [], capsys)
    assert_failed_cleanly(invocation)
    assert "usage" in invocation.combined_output.lower(), invocation.report()
    assert invocation.runner.calls == []
    assert audit.issues == []


def test_unknown_command_prints_usage_without_building_anything(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with SideEffectAudit() as audit:
        invocation = call_main(main_module, ["teleport"], capsys)
    assert_failed_cleanly(invocation)
    assert "usage" in invocation.combined_output.lower(), invocation.report()
    assert invocation.runner.calls == []
    assert audit.issues == []


# ---------------------------------------------------------------------------
# check-config: validate and print the effective configuration, zero side
# effects.
# ---------------------------------------------------------------------------


def test_check_config_prints_effective_configuration_with_zero_side_effects(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "controller.sqlite3"
    path = tmp_path / "controller.yaml"
    _write_valid_config(path, database)
    with SideEffectAudit(allow_config_reads=True) as audit:
        invocation = _engaged_invocation(
            main_module, "check-config", path, capsys, ServerRunner(), serving=False
        )
    assert not invocation.system_exit_raised, invocation.report()
    assert invocation.exit_code == 0, invocation.report()
    for marker in (SITE_ID, TIMEZONE, *UNIT_IDS, "observe_only", "controller.sqlite3"):
        assert marker in invocation.combined_output, f"effective config missing {marker}"
    assert invocation.runner.calls == []
    assert audit.issues == []
    assert not database.exists()
    assert {child.name for child in tmp_path.iterdir()} == {"controller.yaml"}


@pytest.mark.parametrize(
    "flaw", ["unit_count_mismatch", "unknown_site_key", "write_enabled_without_policy"]
)
def test_check_config_rejects_invalid_configuration(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    flaw: str,
) -> None:
    template, valid = _discovered_argv(main_module, tmp_path, capsys)
    database = tmp_path / "must-not-exist.sqlite3"
    payload = _invalid_payload(database, flaw)
    with pytest.raises(ValidationError):
        ControllerConfig.model_validate(payload)
    path = tmp_path / "invalid.yaml"
    _write_yaml(path, payload)
    with SideEffectAudit(allow_config_reads=True) as audit:
        invocation = call_main(main_module, _replace_path(template, valid, path), capsys)
    assert_failed_cleanly(invocation)
    assert invocation.combined_output.strip() != ""
    assert audit.issues == []
    assert not database.exists()
    assert {child.name for child in tmp_path.iterdir()} == {"discovery.yaml", "invalid.yaml"}


def test_check_config_rejects_malformed_yaml(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    template, valid = _discovered_argv(main_module, tmp_path, capsys)
    path = tmp_path / "garbage.yaml"
    path.write_text("units: [unclosed", encoding="utf-8")
    with SideEffectAudit(allow_config_reads=True) as audit:
        invocation = call_main(main_module, _replace_path(template, valid, path), capsys)
    assert_failed_cleanly(invocation)
    assert invocation.combined_output.strip() != ""
    assert audit.issues == []


def test_check_config_reports_a_missing_configuration_file(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    template, valid = _discovered_argv(main_module, tmp_path, capsys)
    missing = tmp_path / "absent.yaml"
    with SideEffectAudit(allow_config_reads=True) as audit:
        invocation = call_main(main_module, _replace_path(template, valid, missing), capsys)
    assert_failed_cleanly(invocation)
    assert "absent.yaml" in invocation.combined_output, invocation.report()
    assert audit.issues == []


def test_check_config_rejects_a_non_utf8_configuration_file(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A UTF-16 BOM artifact fails closed through the structured error path.

    UnicodeDecodeError is a ValueError, not an OSError: without an explicit
    catch it escapes main() as a raw traceback instead of the contracted
    ``configuration error: ...`` message and exit code.
    """
    template, valid = _discovered_argv(main_module, tmp_path, capsys)
    path = tmp_path / "utf16.yaml"
    path.write_bytes("schema_version: 1\n".encode("utf-16"))  # writes a 0xFF 0xFE BOM
    with SideEffectAudit(allow_config_reads=True) as audit:
        invocation = call_main(main_module, _replace_path(template, valid, path), capsys)
    assert not invocation.system_exit_raised, invocation.report()
    assert invocation.exit_code == 1, invocation.report()
    assert "configuration error" in invocation.stderr, invocation.report()
    assert "not valid UTF-8" in invocation.combined_output, invocation.report()
    assert audit.issues == []


# ---------------------------------------------------------------------------
# run: compose the runtime from the config and hand the app to the runner.
# ---------------------------------------------------------------------------


def test_run_hands_the_composed_app_to_the_injected_server_runner(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_real_serving(monkeypatch)
    spy = _wrap_build_runtime(monkeypatch)
    database = tmp_path / "controller.sqlite3"
    path = tmp_path / "controller.yaml"
    _write_valid_config(path, database)
    runner = ServerRunner()
    with SideEffectAudit(allow_config_reads=True, allowed_database=database) as audit:
        invocation = _engaged_invocation(main_module, "run", path, capsys, runner, serving=True)
    assert not invocation.system_exit_raised, invocation.report()
    assert invocation.exit_code == 0, invocation.report()
    assert len(runner.calls) == 1, f"expected exactly one serving call: {invocation.report()}"
    app, serving_args, serving_kwargs = runner.calls[0]
    assert isinstance(app, FastAPI)
    assert {getattr(route, "path", "") for route in app.routes} >= API_ROUTE_PATHS
    host = _serving_parameter(serving_args, serving_kwargs, "host", 0)
    if host is not None:
        assert isinstance(host, str) and host
    port = _serving_parameter(serving_args, serving_kwargs, "port", 1)
    if port is not None:
        assert isinstance(port, int) and 1 <= port <= 65535
    # Granted: supervision starts and stops through the application lifespan
    # the runner drives, so an injected runner performs no network activity.
    assert audit.issues == [], "serving must never start with an injected runner"

    args, kwargs = _composition_call(spy)
    config = args[0] if args else kwargs.get("config")
    assert Path(getattr(config, "storage", None).database_path) == database
    assert not _simulate_argument(args, kwargs), "run must not force simulator mode"
    assert database.exists(), "run must honor the configured durable database path"


def test_simulate_passes_simulator_mode_through_and_forces_in_memory_persistence(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_real_serving(monkeypatch)
    spy = _wrap_build_runtime(monkeypatch)
    database = tmp_path / "controller.sqlite3"
    path = tmp_path / "controller.yaml"
    _write_valid_config(path, database)
    runner = ServerRunner()
    with SideEffectAudit(allow_config_reads=True) as audit:
        invocation = _engaged_invocation(
            main_module, "simulate", path, capsys, runner, serving=True
        )
    assert not invocation.system_exit_raised, invocation.report()
    assert invocation.exit_code == 0, invocation.report()
    assert len(runner.calls) == 1, invocation.report()
    app, _, _ = runner.calls[0]
    assert isinstance(app, FastAPI)
    assert {getattr(route, "path", "") for route in app.routes} >= API_ROUTE_PATHS
    assert audit.issues == [], "the simulator never opens sockets or databases"
    assert not database.exists(), "simulate must force in-memory persistence"
    assert {child.name for child in tmp_path.iterdir()} == {"controller.yaml"}
    args, kwargs = _composition_call(spy)
    assert _simulate_argument(args, kwargs) is True, (
        "simulate must pass simulator mode through to build_runtime"
    )


@pytest.mark.parametrize("command", ["run", "simulate"])
@pytest.mark.parametrize("kind", ["invalid", "malformed", "missing", "non_utf8"])
def test_run_and_simulate_reject_bad_configurations_fail_closed(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    kind: str,
) -> None:
    """A bad configuration must fail before any composition or serving."""
    _forbid_real_serving(monkeypatch)
    spy = _wrap_build_runtime(monkeypatch)
    template, valid = _discovered_argv(main_module, tmp_path, capsys)
    database = tmp_path / "must-not-exist.sqlite3"
    if kind == "invalid":
        payload = _invalid_payload(database, "unit_count_mismatch")
        with pytest.raises(ValidationError):
            ControllerConfig.model_validate(payload)
        path = tmp_path / "invalid.yaml"
        _write_yaml(path, payload)
    elif kind == "malformed":
        path = tmp_path / "garbage.yaml"
        path.write_text("units: [unclosed", encoding="utf-8")
    elif kind == "non_utf8":
        path = tmp_path / "utf16.yaml"
        path.write_bytes(yaml.safe_dump(_valid_payload(database)).encode("utf-16"))
    else:
        path = tmp_path / "absent.yaml"
    runner = ServerRunner()
    with SideEffectAudit(allow_config_reads=True) as audit:
        invocation = call_main(
            main_module, _replace_path(template, valid, path), capsys, runner=runner
        )
    assert_failed_cleanly(invocation)
    assert invocation.combined_output.strip() != "", invocation.report()
    assert runner.calls == [], "an invalid configuration must never reach serving"
    if spy is not None:
        assert spy.calls == [], "an invalid configuration must never compose a runtime"
    assert not database.exists()
    assert audit.issues == []


@pytest.mark.parametrize("command", ["run", "simulate"])
def test_serving_failure_exits_cleanly_with_a_nonzero_code(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    _forbid_real_serving(monkeypatch)
    _wrap_build_runtime(monkeypatch)
    path = tmp_path / "controller.yaml"
    _write_valid_config(path, tmp_path / "controller.sqlite3")
    runner = ServerRunner(failure=RuntimeError("supervisor failure: kernel task died"))
    invocation = _engaged_invocation(main_module, command, path, capsys, runner, serving=True)
    assert_failed_cleanly(invocation)
    assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# Supervision failure must stop serving and make main exit nonzero
# (API_CONTRACTS "Runtime composition and entry point": supervisor or task
# failure fences every generation and runs actor shutdown ... before the
# process exits — the process exits; it may not keep serving the guarded API
# with control authority dead).
# ---------------------------------------------------------------------------


def _with_exploding_first_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose for real, then make the kernel die on its very first tick.

    The first-tick failure lands inside the supervision startup window, the
    one supervisor-failure path that is deterministic under the real clock.
    """
    composition = importlib.import_module("energypod.runtime.composition")
    real_build = composition.build_runtime

    def exploding_build(*args: Any, **kwargs: Any) -> Any:
        runtime = real_build(*args, **kwargs)

        async def exploding_tick() -> None:
            raise RuntimeError("supervisor component failed: audit store unavailable")

        monkeypatch.setattr(runtime.kernel, "tick", exploding_tick)
        return runtime

    monkeypatch.setattr(composition, "build_runtime", exploding_build)


class _FakeListener:
    """Stands in for the bound listener socket; no test may ever bind a port."""

    sockets: ClassVar[list[Any]] = []

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def _prevent_listener_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give uvicorn a fake listener so the default runner serves socketless."""

    async def fake_create_server(*args: Any, **kwargs: Any) -> _FakeListener:
        return _FakeListener()

    monkeypatch.setattr(asyncio.BaseEventLoop, "create_server", fake_create_server)


class _ServingAudit:
    """Side-effect window that ignores only the loop's own wakeup pipe.

    ``asyncio.run`` on Windows unavoidably builds the event loop's self-pipe
    from a loopback ``socket.socketpair`` (one ephemeral bind plus a connect
    back) — internal bookkeeping, not serving traffic. The pair is tagged at
    creation so every other network, process, database, or write attempt in
    the window still fails the test.
    """

    def __init__(
        self, *, allow_config_reads: bool = False, allowed_database: Path | None = None
    ) -> None:
        self.allow_config_reads = allow_config_reads
        self.allowed_database = allowed_database
        self.issues: list[str] = []
        self._mark = 0
        self._bookkeeping: set[int] = set()
        self._real_socketpair = socket.socketpair

    def _tagging_socketpair(self, *args: Any, **kwargs: Any) -> Any:
        # Every network event raised while CPython assembles the pair — the
        # fallback's short-lived listener included — is wakeup bookkeeping.
        start = len(_AUDIT_EVENTS)
        try:
            return self._real_socketpair(*args, **kwargs)
        finally:
            for index in range(start, len(_AUDIT_EVENTS)):
                if _AUDIT_EVENTS[index][0].startswith(_NETWORK_EVENTS):
                    self._bookkeeping.add(index)

    def __enter__(self) -> _ServingAudit:
        self._mark = len(_AUDIT_EVENTS)
        socket.socketpair = self._tagging_socketpair
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        socket.socketpair = self._real_socketpair
        events = [
            (event, args)
            for offset, (event, args) in enumerate(_AUDIT_EVENTS[self._mark :])
            if self._mark + offset not in self._bookkeeping
        ]
        self.issues = _side_effect_violations(
            events,
            allow_config_reads=self.allow_config_reads,
            allowed_database=self.allowed_database,
        )


def test_supervision_failure_through_the_lifespan_exits_nonzero(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real supervision failure ends the lifespan loudly; main returns nonzero.

    The injected runner drives the composed app through the real ASGI lifespan
    protocol — the seam the default uvicorn runner uses — and refuses to report
    a healthy serve when supervision failed during startup.
    """
    _forbid_real_serving(monkeypatch)
    _with_exploding_first_tick(monkeypatch)
    path = tmp_path / "controller.yaml"
    _write_valid_config(path, tmp_path / "controller.sqlite3")
    events: list[dict[str, Any]] = []

    def lifespan_runner(app: Any, *, host: str, port: int) -> None:
        async def drive() -> None:
            incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            incoming.put_nowait({"type": "lifespan.startup"})

            async def receive() -> dict[str, Any]:
                return await incoming.get()

            async def record(message: dict[str, Any]) -> None:
                events.append(message)

            await app(
                {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.3"}},
                receive,
                record,
            )

        asyncio.run(drive())
        if not any(message["type"] == "lifespan.startup.complete" for message in events):
            raise RuntimeError("the application lifespan never completed startup")

    with _ServingAudit(allow_config_reads=True) as audit:
        invocation = call_main(main_module, ["simulate", str(path)], capsys, runner=lifespan_runner)
    assert_failed_cleanly(invocation)
    assert any(message["type"] == "lifespan.startup.failed" for message in events), events
    assert "serving failed" in invocation.stderr, invocation.report()
    assert audit.issues == []
    assert not (tmp_path / "controller.sqlite3").exists()


def test_default_runner_exits_nonzero_when_supervision_fails_during_startup(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default uvicorn path: supervision failure stops serving, exit 1.

    Drives ``main`` with ``server_runner=None`` — the real uvicorn runner —
    against a genuinely composed runtime whose kernel dies on its first tick.
    uvicorn itself exits on a failed lifespan startup; this pins that the
    process outcome is a structured nonzero return, not a healthy serve.
    """
    _with_exploding_first_tick(monkeypatch)
    _prevent_listener_sockets(monkeypatch)
    path = tmp_path / "controller.yaml"
    _write_valid_config(path, tmp_path / "controller.sqlite3")
    with _ServingAudit(allow_config_reads=True) as audit:
        capsys.readouterr()
        exit_code = main_module.main(["simulate", str(path)])
        captured = capsys.readouterr()
    assert exit_code == 1, captured.err
    assert "serving failed" in captured.err
    assert "supervision failed during startup" in captured.err
    assert audit.issues == [], "the startup-failure path must never bind a port"
    assert not (tmp_path / "controller.sqlite3").exists()


class _HaltingLifespanApp:
    """Pure-ASGI app whose supervision dies mid-run, after a healthy startup.

    Models the three ways a halted supervisor becomes observable to serving:
    ending its lifespan with a failure message, ending it unrequested, or
    staying suspended while only recording the halt on application state.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.state = State()
        self.shutdown_received = False
        self.served_types: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            self.served_types.append(scope["type"])
            return
        first = await receive()
        assert first["type"] == "lifespan.startup"
        await send({"type": "lifespan.startup.complete"})
        if self.mode == "halt_marker":
            # The lifespan stays suspended: a dead supervisor can still record
            # the halt on application state, nothing more.
            self.state.energypod_supervision_halt_reason = "supervisor_failure"
            message = await receive()
            assert message["type"] == "lifespan.shutdown"
            self.shutdown_received = True
            await send({"type": "lifespan.shutdown.complete"})
            return
        await asyncio.sleep(0.05)
        if self.mode == "shutdown_failed":
            await send(
                {
                    "type": "lifespan.shutdown.failed",
                    "message": "supervision halted: supervisor_failure",
                }
            )
            return
        await send({"type": "lifespan.shutdown.complete"})


@pytest.mark.parametrize("mode", ["shutdown_failed", "unsolicited_complete", "halt_marker"])
def test_default_runner_stops_serving_when_the_lifespan_halts_mid_run(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """The default runner may not wait forever: a lifespan that ends or halts
    by itself must stop serving and return a nonzero exit code."""
    app = _HaltingLifespanApp(mode)
    composition = importlib.import_module("energypod.runtime.composition")

    def stub_build(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(app=app, actors={})

    monkeypatch.setattr(composition, "build_runtime", stub_build)
    _prevent_listener_sockets(monkeypatch)
    path = tmp_path / "controller.yaml"
    _write_valid_config(path, tmp_path / "controller.sqlite3")
    with _ServingAudit(allow_config_reads=True) as audit:
        capsys.readouterr()
        exit_code = main_module.main(["simulate", str(path)])
        captured = capsys.readouterr()
    assert exit_code == 1, f"serving outlived supervision (mode={mode}): {captured.err}"
    assert "serving failed" in captured.err
    if mode == "halt_marker":
        assert "supervision halted: supervisor_failure" in captured.err
        assert app.shutdown_received, "stopping serving must still shut the lifespan down cleanly"
    assert audit.issues == []
    assert not (tmp_path / "controller.sqlite3").exists()


@pytest.mark.parametrize(
    ("attribute", "reason", "must_fail"),
    [
        ("energypod_supervision_halt_reason", "supervisor_failure", True),
        ("energypod_supervision_halt_reason", "supervision_shutdown", False),
        ("supervision_state", "supervisor_failure", True),
        ("supervision_state", "running", False),
    ],
)
def test_recorded_supervision_halt_decides_the_exit_code(
    main_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
    reason: str,
    must_fail: bool,
) -> None:
    """A halt recorded on the app decides the exit even if the runner settled.

    Composition owns recording the halt on application state; main must fail
    closed on it (and only an operator-driven halt may still exit zero). A
    supervision-named value that names a failure halts too, while healthy
    supervision state may never read as a halt.
    """
    _forbid_real_serving(monkeypatch)
    _wrap_build_runtime(monkeypatch)
    path = tmp_path / "controller.yaml"
    _write_valid_config(path, tmp_path / "controller.sqlite3")
    calls: list[Any] = []

    def recording_runner(app: Any, *, host: str, port: int) -> Any:
        calls.append(app)
        setattr(app.state, attribute, reason)
        return _Completed()

    with _ServingAudit(allow_config_reads=True) as audit:
        invocation = call_main(
            main_module, ["simulate", str(path)], capsys, runner=recording_runner
        )
    assert len(calls) == 1
    if must_fail:
        assert invocation.exit_code == 1, invocation.report()
        assert "supervision halted: supervisor_failure" in invocation.stderr, invocation.report()
    else:
        assert invocation.exit_code == 0, invocation.report()
    assert audit.issues == []
