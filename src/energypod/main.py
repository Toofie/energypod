"""Command-line entry point for the EnergyPod controller (ADR-0003 D6).

Importing this module is side-effect free: no configuration is loaded, no
runtime is composed, and no server is started. ``main`` validates the strict
configuration, composes the object graph exclusively through
``energypod.runtime.composition.build_runtime`` — the only composition point —
and hands the composed, lifespan-carrying application to a server runner.
Supervision (kernel tick loop, per-unit actor loops, event publication) starts
and stops through that application lifespan, never here.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import yaml  # type: ignore[import-untyped]  # types-PyYAML is not pinned yet
from pydantic import ValidationError

from energypod.runtime.config import ControllerConfig

CHECK_CONFIG_COMMAND: Final = "check-config"
RUN_COMMAND: Final = "run"
SIMULATE_COMMAND: Final = "simulate"
_COMMANDS: Final = (CHECK_CONFIG_COMMAND, RUN_COMMAND, SIMULATE_COMMAND)

# Serving defaults. The strict configuration model does not describe the HTTP
# listener yet, and a fleet control plane must not become reachable on every
# interface by accident, so the default bind stays on loopback until serving
# configuration is explicitly contracted.
DEFAULT_SERVE_HOST: Final = "127.0.0.1"
DEFAULT_SERVE_PORT: Final = 8080

_EXIT_OK: Final = 0
_EXIT_FAILURE: Final = 1
_EXIT_USAGE: Final = 2

_USAGE: Final = """usage: energypod [--config PATH | PATH] COMMAND

commands:
  check-config  validate the configuration and print the effective values
  run           compose the runtime and serve the guarded API
  simulate      compose with simulator transports and in-memory persistence

The configuration path is given either as `--config PATH` or as a single
positional `PATH`; exactly one spelling is accepted."""


class ServerRunner(Protocol):
    """Structural serving seam: receive the composed app and serving parameters."""

    def __call__(self, app: Any, *, host: str, port: int) -> Any: ...


@dataclass(frozen=True)
class _Invocation:
    command: str
    config_path: Path


class _UsageError(Exception):
    """The command line itself is malformed; nothing was loaded or composed."""


class _ConfigurationFileError(Exception):
    """The configuration is absent, unreadable, malformed, or invalid."""


def _parse_arguments(argv: Sequence[str]) -> _Invocation:
    """Parse one command plus exactly one configuration-path spelling."""
    if not argv:
        raise _UsageError("no command given")
    command, *operands = argv
    if command not in _COMMANDS:
        raise _UsageError(f"unknown command: {command!r}")
    flagged: Path | None = None
    positional: Path | None = None
    index = 0
    while index < len(operands):
        operand = operands[index]
        if operand == "--config":
            if flagged is not None:
                raise _UsageError("--config may be given at most once")
            if index + 1 == len(operands):
                raise _UsageError("--config requires a configuration path")
            flagged = Path(operands[index + 1])
            index += 2
            continue
        if operand.startswith("-"):
            raise _UsageError(f"unknown option: {operand!r}")
        if positional is not None:
            raise _UsageError("give exactly one configuration path")
        positional = Path(operand)
        index += 1
    if flagged is not None and positional is not None:
        raise _UsageError("use either --config PATH or a positional PATH, not both")
    config_path = flagged if flagged is not None else positional
    if config_path is None:
        raise _UsageError(f"{command} requires a configuration file path")
    return _Invocation(command=command, config_path=config_path)


def _load_configuration(path: Path) -> ControllerConfig:
    """Load and strictly validate one configuration file, failing closed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        reason = getattr(error, "strerror", None) or error.__class__.__name__
        raise _ConfigurationFileError(f"cannot read configuration file {path}: {reason}") from error
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise _ConfigurationFileError(
            f"configuration file {path} is not valid YAML: {error}"
        ) from error
    try:
        return ControllerConfig.model_validate(payload)
    except ValidationError as error:
        raise _ConfigurationFileError(f"invalid configuration in {path}: {error}") from error


def _check_config_command(path: Path) -> int:
    """Validate and print the effective configuration with zero side effects."""
    try:
        config = _load_configuration(path)
    except _ConfigurationFileError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    # Nothing is composed, opened, or started: the printed document is the
    # whole effect of the command.
    print("effective configuration:")
    print(config.model_dump_json(indent=2))
    return _EXIT_OK


async def _uvicorn_server_runner(app: Any, *, host: str, port: int) -> None:
    """Serve the app with uvicorn, driving the application lifespan.

    Imported lazily so importing energypod.main never loads a server stack and
    tests never bind a port. The programmatic ``Server.serve()`` form is used
    because the runner executes inside the one event loop that also composed
    the runtime; ``uvicorn.run`` would try to nest a second loop.
    """
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, lifespan="on"))
    await server.serve()
    if not server.started:
        # Fail closed: supervision never came up, so the process must not
        # report a healthy exit.
        raise RuntimeError("the server never started serving")


async def _shut_down_runtime(runtime: Any) -> None:
    """Best-effort bounded-zero shutdown after a serving failure.

    Supervision normally stops through the application lifespan the runner
    drives. When the runner itself failed, the lifespan may never have started
    or died halfway, so every actor gets its idempotent shutdown: fence the
    generation, attempt one bounded zero command, close the transport. An
    actor that never started has no transport to dial, so this path opens no
    sockets.
    """
    for actor in dict(getattr(runtime, "actors", None) or {}).values():
        with contextlib.suppress(Exception):
            await actor.shutdown()


def _compose_runtime(config: ControllerConfig, *, simulate: bool) -> Any:
    """Build the object graph through the only composition point.

    Imported here — never at module import — because composing pulls in the
    whole adapter stack (including third-party modules that read packaged
    JSON schemas at import), and importing energypod.main must stay free of
    any I/O. Construction validates wiring eagerly, so a misconfiguration
    fails before any task starts.
    """
    from energypod.runtime import composition

    return composition.build_runtime(config, simulate=simulate)


async def _await_outcome(outcome: Awaitable[Any]) -> None:
    await outcome


def _settle_runner_outcome(outcome: Any) -> None:
    """Conclude an injected runner's outcome without starting supervision.

    A coroutine — a genuinely asynchronous runner — is driven on a fresh event
    loop. A bare awaitable such as the contract's zero-value test seam is
    driven directly, because loop setup itself performs socket bookkeeping and
    an injected runner must be able to serve with no network activity at all.
    """
    if not inspect.isawaitable(outcome):
        return
    if inspect.iscoroutine(outcome):
        asyncio.run(_await_outcome(outcome))
        return
    generator = outcome.__await__()
    while True:
        try:
            generator.send(None)
        except StopIteration:
            return


def _compose_and_hand_off(
    config: ControllerConfig, *, simulate: bool, runner: ServerRunner
) -> None:
    """Compose on the caller's thread, then hand the app to the runner.

    The injected runner owns serving and with it the application lifespan, so
    supervision starts and stops only if the runner drives that lifespan.
    """
    runtime = _compose_runtime(config, simulate=simulate)
    try:
        outcome = runner(runtime.app, host=DEFAULT_SERVE_HOST, port=DEFAULT_SERVE_PORT)
        _settle_runner_outcome(outcome)
    except Exception:
        # Fail closed: a failed runner must not leave half-started authority.
        with contextlib.suppress(Exception):
            asyncio.run(_shut_down_runtime(runtime))
        raise


async def _compose_and_serve(
    config: ControllerConfig, *, simulate: bool, runner: ServerRunner
) -> None:
    """Compose and serve on one shared event loop.

    Production transports capture the event loop that constructs them, so
    composition must happen on the very loop that later serves and runs the
    supervision tasks.
    """
    runtime = _compose_runtime(config, simulate=simulate)
    try:
        outcome = runner(runtime.app, host=DEFAULT_SERVE_HOST, port=DEFAULT_SERVE_PORT)
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        # Fail closed: a failed runner must not leave half-started authority.
        await _shut_down_runtime(runtime)
        raise


def _serve_command(path: Path, *, simulate: bool, server_runner: ServerRunner | None) -> int:
    """Compose the runtime and serve the composed application."""
    try:
        config = _load_configuration(path)
    except _ConfigurationFileError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    try:
        if server_runner is None:
            asyncio.run(
                _compose_and_serve(config, simulate=simulate, runner=_uvicorn_server_runner)
            )
        else:
            _compose_and_hand_off(config, simulate=simulate, runner=server_runner)
    except Exception as error:
        print(f"serving failed: {error}", file=sys.stderr)
        return _EXIT_FAILURE
    return _EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    server_runner: ServerRunner | None = None,
) -> int:
    """Run the controller CLI and return a process exit code.

    ``server_runner`` is the serving seam: it receives the built application
    and the serving parameters (host, port) instead of main binding a port.
    """
    operands = list(sys.argv[1:] if argv is None else argv)
    try:
        invocation = _parse_arguments(operands)
    except _UsageError as error:
        print(f"error: {error}", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return _EXIT_USAGE
    if invocation.command == CHECK_CONFIG_COMMAND:
        return _check_config_command(invocation.config_path)
    return _serve_command(
        invocation.config_path,
        simulate=invocation.command == SIMULATE_COMMAND,
        server_runner=server_runner,
    )


if __name__ == "__main__":
    raise SystemExit(main())
