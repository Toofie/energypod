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

import importlib
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from types import BuiltinFunctionType, FunctionType, ModuleType
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from pydantic import ValidationError

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


def _side_effect_violations(
    events: list[tuple[str, tuple[Any, ...]]],
    *,
    allow_config_reads: bool,
    allow_database: bool,
) -> list[str]:
    """Classify recorded audit events into human-readable contract breaches."""
    issues: list[str] = []
    for event, args in events:
        if event.startswith(_NETWORK_EVENTS):
            issues.append(f"network activity: {event}")
        elif event.startswith(_PROCESS_EVENTS):
            issues.append(f"process activity: {event}")
        elif allow_database:
            continue
        elif event.startswith("sqlite3."):
            issues.append("database connection opened")
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
                issues.append(f"file created or written: {path}")
    return issues


@dataclass
class SideEffectAudit:
    """Scoped window over the process audit log; issues are asserted by tests."""

    allow_config_reads: bool = False
    allow_database: bool = False
    issues: list[str] = field(default_factory=list)
    _mark: int = field(default=0, init=False, repr=False)

    def __enter__(self) -> SideEffectAudit:
        self._mark = len(_AUDIT_EVENTS)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.issues = _side_effect_violations(
            _AUDIT_EVENTS[self._mark :],
            allow_config_reads=self.allow_config_reads,
            allow_database=self.allow_database,
        )


# ---------------------------------------------------------------------------
# CLI invocation harness.
# ---------------------------------------------------------------------------


@dataclass
class ServerRunner:
    """Injected serving seam: records the app and serving args instead of serving."""

    calls: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)
    failure: BaseException | None = None

    def __call__(self, app: Any, *args: Any, **kwargs: Any) -> None:
        self.calls.append((app, kwargs))
        if self.failure is not None:
            raise self.failure


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


def call_main(
    module: ModuleType,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    *,
    runner: ServerRunner | None = None,
) -> Invocation:
    """Call main in-process; a SystemExit escape is recorded, never honored."""
    effective = runner if runner is not None else ServerRunner()
    capsys.read_outerr()
    try:
        exit_code = module.main(argv, server_runner=effective)
    except SystemExit as exc:
        captured = capsys.read_outerr()
        code = exc.code if isinstance(exc.code, int) else None
        return Invocation(list(argv), code, True, effective, captured.out, captured.err)
    captured = capsys.read_outerr()
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
    """Try both conventional config spellings; return the one the CLI accepted."""
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

_IMPORT_SAFE_ATTRIBUTES = (
    ModuleType,
    type,
    FunctionType,
    BuiltinFunctionType,
    str,
    int,
    float,
    bool,
    bytes,
    type(None),
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
        if not name.startswith("_") and not isinstance(value, _IMPORT_SAFE_ATTRIBUTES)
    }
    assert constructed == {}, "importing energypod.main must not construct runtime state"


def test_missing_command_prints_usage_without_building_anything(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with SideEffectAudit() as audit:
        invocation = call_main(main_module, [], capsys)
    assert not invocation.system_exit_raised, (
        "main must return an exit code, not raise SystemExit: " + invocation.report()
    )
    assert isinstance(invocation.exit_code, int) and invocation.exit_code != 0
    assert "usage" in invocation.combined_output.lower(), invocation.report()
    assert invocation.runner.calls == []
    assert audit.issues == []


def test_unknown_command_prints_usage_without_building_anything(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with SideEffectAudit() as audit:
        invocation = call_main(main_module, ["teleport"], capsys)
    assert not invocation.system_exit_raised, invocation.report()
    assert isinstance(invocation.exit_code, int) and invocation.exit_code != 0
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
    assert not invocation.system_exit_raised, invocation.report()
    assert isinstance(invocation.exit_code, int) and invocation.exit_code != 0
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
    assert not invocation.system_exit_raised, invocation.report()
    assert isinstance(invocation.exit_code, int) and invocation.exit_code != 0
    assert invocation.combined_output.strip() != ""
    assert audit.issues == []


def test_check_config_reports_a_missing_configuration_file(
    main_module: ModuleType, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    template, valid = _discovered_argv(main_module, tmp_path, capsys)
    missing = tmp_path / "absent.yaml"
    with SideEffectAudit(allow_config_reads=True) as audit:
        invocation = call_main(main_module, _replace_path(template, valid, missing), capsys)
    assert not invocation.system_exit_raised, invocation.report()
    assert isinstance(invocation.exit_code, int) and invocation.exit_code != 0
    assert "absent.yaml" in invocation.combined_output, invocation.report()
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
    with SideEffectAudit(allow_config_reads=True, allow_database=True) as audit:
        invocation = _engaged_invocation(main_module, "run", path, capsys, runner, serving=True)
    assert not invocation.system_exit_raised, invocation.report()
    assert invocation.exit_code == 0, invocation.report()
    assert len(runner.calls) == 1, f"expected exactly one serving call: {invocation.report()}"
    app, serving_kwargs = runner.calls[0]
    assert isinstance(app, FastAPI)
    assert {getattr(route, "path", "") for route in app.routes} >= API_ROUTE_PATHS
    host = serving_kwargs.get("host")
    if host is not None:
        assert isinstance(host, str) and host
    port = serving_kwargs.get("port")
    if port is not None:
        assert isinstance(port, int) and 1 <= port <= 65535
    assert audit.issues == [], "serving must never start with an injected runner"

    args, kwargs = _composition_call(spy)
    config = args[0] if args else kwargs.get("config")
    assert Path(getattr(config, "storage", None).database_path) == database
    assert not kwargs.get("simulate"), "run must not force simulator mode"
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
    app, _ = runner.calls[0]
    assert isinstance(app, FastAPI)
    assert {getattr(route, "path", "") for route in app.routes} >= API_ROUTE_PATHS
    assert audit.issues == [], "the simulator never opens sockets or databases"
    assert not database.exists(), "simulate must force in-memory persistence"
    assert {child.name for child in tmp_path.iterdir()} == {"controller.yaml"}
    _, kwargs = _composition_call(spy)
    assert kwargs.get("simulate") is True, (
        "simulate must pass simulator mode through to build_runtime"
    )


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
    assert not invocation.system_exit_raised, invocation.report()
    assert isinstance(invocation.exit_code, int) and invocation.exit_code != 0, (
        "a supervisor/serving failure must fence and exit with a clean nonzero code: "
        + invocation.report()
    )
    assert len(runner.calls) == 1
