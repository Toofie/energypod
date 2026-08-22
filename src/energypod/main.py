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

# How often the serving bridge checks whether supervision halted mid-run. The
# check rides next to uvicorn's own 0.1 s main-loop tick, so serving stops
# within one loop tick of a halt instead of waiting forever.
_HALT_POLL_INTERVAL_S: Final = 0.05

# A halt reason naming an operator-driven stop is the only halt that is not a
# serving failure; every other spelling fails closed (API_CONTRACTS "Runtime
# composition and entry point": supervisor or task failure ... before the
# process exits).
_BENIGN_HALT_TOKENS: Final = (
    "shutdown",
    "operator",
    "normal",
    "graceful",
    "requested",
    "signal",
    "sigterm",
    "sigint",
    "stop",
)

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
    except UnicodeError as error:
        # A UTF-16/ANSI artifact (Notepad's "Unicode" writes a 0xFF 0xFE BOM)
        # is an unreadable configuration file, not a traceback: it must leave
        # through the same structured configuration-error exit as every other
        # unreadable file. UnicodeDecodeError is a ValueError, so the OSError
        # clause above never sees it.
        raise _ConfigurationFileError(
            f"cannot read configuration file {path}: not valid UTF-8 text"
        ) from error
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


def _app_state_items(app: Any) -> tuple[tuple[str, Any], ...]:
    """Enumerate the composed app's state store without importing Starlette.

    Starlette keeps application state in ``State._state``; anything else falls
    back to the object's own ``__dict__``.
    """
    state = getattr(app, "state", None)
    if state is None:
        return ()
    store = getattr(state, "_state", None)
    if isinstance(store, dict):
        return tuple(store.items())
    try:
        return tuple(vars(state).items())
    except TypeError:
        return ()


def _halted_reason(app: Any) -> str | None:
    """The supervision halt recorded on the app, whatever spelling was used.

    Composition owns recording (``app.state``); serving only reacts. The exact
    attribute name is not pinned by the contract, so any state attribute whose
    name mentions a halt is honored: a string reason (or exception text) is
    reported verbatim, and a flag or event reports its own name. A
    supervision-named attribute counts only when its value itself names a
    failure, so healthy supervision state can never read as a halt. Anything
    that cannot be proven un-halted counts as halted — this is the fail-stop
    link between a dead supervisor and the serving process.
    """
    for name, value in _app_state_items(app):
        lowered_name = name.lower()
        if "halt" in lowered_name:
            halted, reason = _halt_flag_value(name, value)
        elif "supervis" in lowered_name and _names_failure(value):
            halted, reason = True, value.strip() if isinstance(value, str) else repr(value)
        else:
            continue
        if halted:
            return reason
    return None


def _halt_flag_value(name: str, value: Any) -> tuple[bool, str]:
    """Read one halt-named state entry as (halted?, reason)."""
    if isinstance(value, bool):
        return value, name
    if isinstance(value, BaseException):
        return True, str(value) or repr(value)
    is_set = getattr(value, "is_set", None)
    if callable(is_set):
        return bool(is_set()), name
    if isinstance(value, str):
        return bool(value.strip()), value.strip()
    if value is None:
        return False, name
    return bool(value), name


def _names_failure(value: Any) -> bool:
    """Does a supervision-named value itself say the supervisor failed?"""
    if isinstance(value, BaseException):
        return True
    return isinstance(value, str) and any(
        token in value.lower() for token in ("fail", "halt", "error", "fatal")
    )


def _halt_is_failure(reason: str) -> bool:
    """Only an explicitly operator-driven halt avoids the failure exit code."""
    lowered = reason.lower()
    return not any(token in lowered for token in _BENIGN_HALT_TOKENS)


def _assert_supervision_healthy(app: Any) -> None:
    """Fail closed when a runner settled but supervision had already halted."""
    reason = _halted_reason(app)
    if reason is not None and _halt_is_failure(reason):
        raise RuntimeError(f"supervision halted: {reason}")


class _LifespanBridge:
    """Observe the composed app's lifespan from inside the serving loop.

    Wrapping the app — instead of reaching into uvicorn internals — keeps the
    fail-stop contract at the ASGI protocol level: the only signals a halted
    supervisor can still emit are its lifespan ending, the lifespan failure
    messages, or a halt recorded on ``app.state``. Whichever appears first
    mid-run must stop serving; a guarded API must never keep answering
    intents, arm, and emergency-stop with control authority dead.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self.startup_failed: str | None = None
        self.shutdown_failed: str | None = None
        self.shutdown_requested = False
        self.lifespan_finished = False
        self.lifespan_exception: BaseException | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            await self._app(scope, receive, send)
            return

        async def observed_receive() -> Any:
            message = await receive()
            if message.get("type") == "lifespan.shutdown":
                self.shutdown_requested = True
            return message

        async def observed_send(message: Any) -> None:
            kind = message.get("type")
            if kind == "lifespan.startup.failed":
                self.startup_failed = str(message.get("message") or "application startup failed")
            elif kind == "lifespan.shutdown.failed":
                self.shutdown_failed = str(message.get("message") or "application shutdown failed")
            await send(message)

        try:
            await self._app(scope, observed_receive, observed_send)
        except BaseException as error:
            self.lifespan_exception = error
            raise
        finally:
            self.lifespan_finished = True

    def _stopped_serving(self) -> bool:
        """Has the lifespan ended, failed, or recorded a halt on its own?"""
        if self.startup_failed is not None or self.shutdown_failed is not None:
            return True
        if self.lifespan_finished and not self.shutdown_requested:
            return True
        return _halted_reason(self._app) is not None

    async def bridge_to(self, server: Any) -> None:
        """Set the server's exit flag as soon as supervision halts mid-run."""
        while not server.should_exit:
            if self._stopped_serving():
                server.should_exit = True
                return
            await asyncio.sleep(_HALT_POLL_INTERVAL_S)

    def failure_message(self, server: Any) -> str | None:
        """Translate a halted lifespan into the serving failure main reports."""
        if self.startup_failed is not None:
            return f"supervision failed during startup: {self.startup_failed}"
        if self.shutdown_failed is not None:
            return f"the application lifespan failed during shutdown: {self.shutdown_failed}"
        if self.lifespan_exception is not None:
            return f"the application lifespan raised: {self.lifespan_exception!r}"
        if self.lifespan_finished and not self.shutdown_requested:
            return "the application lifespan ended before serving was shut down"
        halt = _halted_reason(self._app)
        if halt is not None and _halt_is_failure(halt):
            return f"supervision halted: {halt}"
        if not server.started:
            return "the server never started serving"
        return None


async def _uvicorn_server_runner(app: Any, *, host: str, port: int) -> None:
    """Serve the app with uvicorn, driving the application lifespan.

    Imported lazily so importing energypod.main never loads a server stack and
    tests never bind a port. The programmatic ``Server.serve()`` form is used
    because the runner executes inside the one event loop that also composed
    the runtime; ``uvicorn.run`` would try to nest a second loop.

    Supervision failure must stop serving: ``serve()`` alone would wait
    forever, because uvicorn exits on signals and startup failures but never
    on an application lifespan that ends by itself. The bridge watches for
    exactly that and sets the server's exit flag, and a lifespan that ended
    or halted in failure becomes a nonzero outcome for ``main``.
    """
    import uvicorn

    bridge = _LifespanBridge(app)
    server = uvicorn.Server(uvicorn.Config(bridge, host=host, port=port, lifespan="on"))
    watcher = asyncio.create_task(bridge.bridge_to(server))
    try:
        await server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
    failure = bridge.failure_message(server)
    if failure is not None:
        raise RuntimeError(failure)


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
        _assert_supervision_healthy(runtime.app)
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
        _assert_supervision_healthy(runtime.app)
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
