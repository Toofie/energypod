"""Per-unit, sole-owner actor for safety-critical EnergyPod I/O.

The actor deliberately depends on structural (duck-typed) ports.  Adapters and
repositories may therefore evolve independently, while all socket operations
remain owned by one mailbox task.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from energypod.domain import Observation, UnitLifecycle

from .generation import AuthorityGenerationCoordinator

# API_CONTRACTS "Excess-solar accelerated charging (advisory)": the advisory
# CT power words (grid/load at PCS 0x1000+17/+20) ride the observation's
# quality map but stay OUTSIDE every ordinary quality judgment.  Qualifying
# a unit is exactly such an ordinary decision: a read plan that does not
# serve the PCS block (advisory MISSING) must never keep a unit from
# qualifying, and the advisory words gate their own fail-closed export
# bound instead.
#
# SYNC_RESILIENCE_AUDIT B1 (2026-08-24): the system controller's SOC word is
# advisory for the same reason -- its block is the once-per-process tier, so
# a single BAD cycle-1 decode (or a SUSPECT blanket downgrade it can never
# refresh away) would otherwise permanently reset this actor's stable-sample
# counter while the battery's own BMS SOC reads fresh and GOOD every cycle.
_SAFETY_QUALITY_FIELDS: Final[frozenset[str]] = Observation.REQUIRED_SAFETY_QUALITY_FIELDS

_HEARTBEAT_PRIORITY: Final = 0
_CONTROL_PRIORITY: Final = 10
_POLL_PRIORITY: Final = 20
# An externally requested bounded zero outranks every scheduled operation
# except terminal shutdown; the facade enqueues it for emergency stops.
_ZERO_PRIORITY: Final = -1
# The served PQ objective readback window the arm-time external-writer
# preflight reads (PROTOCOL_EVIDENCE 4b: IoT PCS block 0x1060, active and
# reactive power objectives at +17/+18; the live -200 W commissioning write
# read back immediately at +17).  Exactly this pair is read, through the one
# transport this actor owns, inside the arm mailbox dispatch.
_OBJECTIVE_READBACK_COUNT: Final = 2


class InhibitCause(StrEnum):
    """Cause class recorded when the unit enters INHIBITED (API_CONTRACTS)."""

    TRANSIENT = "transient"
    QUALIFIED = "qualified"
    LATCHED = "latched"


@dataclass(slots=True)
class _Message:
    operation: str
    argument: Any
    reply: asyncio.Future[Any]


class EnergyPodActor:
    """Serialize one unit's lifecycle and protocol operations through a mailbox."""

    def __init__(
        self,
        *,
        unit_id: str,
        transport: Any,
        clock: Any,
        observations: Any,
        authorizations: Any,
        audit: Any,
        command_encoder: Any,
        generation_coordinator: AuthorityGenerationCoordinator | None = None,
        expected_identity: str,
        expected_profile: str,
        expected_cell_count: int,
        stable_observations_required: int,
        essential_read_address: int,
        essential_read_count: int,
        heartbeat_interval_s: float,
        heartbeat_safety_margin_s: float,
        objective_readback_address: int | None = None,
        blocking_fault_codes: frozenset[str] | None = None,
        mode_refresh_window: tuple[int, int] | None = None,
        telemetry: Any | None = None,
    ) -> None:
        if stable_observations_required < 1:
            raise ValueError("stable_observations_required must be positive")
        if heartbeat_interval_s <= 0:
            raise ValueError("heartbeat_interval_s must be positive")
        if not 0 <= heartbeat_safety_margin_s < heartbeat_interval_s:
            raise ValueError("heartbeat safety margin must be within the interval")
        if objective_readback_address is not None and objective_readback_address < 0:
            raise ValueError("objective_readback_address must be non-negative")
        if blocking_fault_codes is not None and any(
            not code or code != code.strip() for code in blocking_fault_codes
        ):
            raise ValueError("blocking_fault_codes must be non-empty and normalized")
        if mode_refresh_window is not None and (
            len(mode_refresh_window) != 2
            or type(mode_refresh_window[0]) is not int
            or type(mode_refresh_window[1]) is not int
            or mode_refresh_window[0] < 0
            or mode_refresh_window[1] < 3
        ):
            raise ValueError("mode_refresh_window must be an (address, count>=3) pair")

        self.unit_id = unit_id
        self._transport = transport
        self._clock = clock
        self._observations = observations
        self._authorizations = authorizations
        self._audit = audit
        self._command_encoder = command_encoder
        self._generation_coordinator = generation_coordinator or AuthorityGenerationCoordinator()
        self._expected_identity = expected_identity
        self._expected_profile = expected_profile
        self._expected_cell_count = expected_cell_count
        self._stable_required = stable_observations_required
        self._essential_address = essential_read_address
        self._essential_count = essential_read_count
        # Optional arm-time external-writer preflight port (API_CONTRACTS
        # "Write-enabled run mode", bullet 3): the composition root pins the
        # served PQ objective readback window's base address (IoT 0x1060+17)
        # in write-enabled mode.  ``None`` — the default — keeps the arm path
        # exactly as it is today, so observe-only wiring is unchanged.
        self._objective_readback_address = objective_readback_address
        self._heartbeat_interval = heartbeat_interval_s
        self._heartbeat_margin = heartbeat_safety_margin_s
        self._blocking_fault_codes = frozenset(blocking_fault_codes or ())
        # SYNC_RESILIENCE_AUDIT B5 (2026-08-24): the bounded fresh read of the
        # system-mode window (0x0100: ctrlMode +1, workMode +2) the dispatch
        # refusal path performs when the CACHED ctrlMode word would refuse.
        # The cached word rides the cold ring (~108 s), so without this port a
        # pod that was in Local at some earlier moment could refuse dispatch
        # on stale evidence.  ``None`` keeps the arm path exactly as today.
        self._mode_refresh_window = mode_refresh_window
        # Optional telemetry strategy (structural port): the composition root
        # may inject the poll->decode->deliver strategy so one telemetry cycle
        # reads the selected register-layout plan through this actor's sole
        # transport and delivers the decoded observation.  Without it the poll
        # stays the bare essential read and nothing is published.
        self._telemetry = telemetry

        self.lifecycle = UnitLifecycle.BOOT
        self.generation = 0
        # Inhibit cause class and latch, exposed for facade snapshots.  A
        # latched cause never auto-recovers and needs one explicit privileged
        # acknowledgement before stable samples may return the unit to
        # DISARMED (API_CONTRACTS "Inhibit acknowledgement").
        self.inhibit_cause: InhibitCause | None = None
        self.inhibit_latched = False
        # Operator-visible inhibit reason string (``external_writer``,
        # ``identity_mismatch``, ...): recorded with every inhibit next to the
        # cause class, cleared only by the same stable-sample recovery that
        # clears the cause, so the privileged acknowledgement path and the
        # facade surface can name why the unit latched.
        self.inhibit_reason: str | None = None
        self._connection_epoch: int | None = None
        self._latest_observation: Any | None = None
        self._stable_observations = 0
        self._used_cycles: set[tuple[int, int]] = set()
        # The last PQ objective (signed active, reactive power) this actor
        # itself applied, so the arm-time external-writer preflight can tell
        # its own standing objective — left applied across a disarm, for
        # example — from a foreign writer's.  ``None`` (nothing written by
        # this actor) makes any nonzero readback foreign: a fresh process
        # cannot inherit provenance.
        self._applied_objective: tuple[int, int] | None = None
        # B4 (SYNC_RESILIENCE_AUDIT): one-shot cell-tier promotion hint,
        # consumed by the next poll.
        self._cell_refresh_requested = False

        self._mailbox: asyncio.PriorityQueue[tuple[int, int, _Message]] = asyncio.PriorityQueue()
        self._sequence = itertools.count()
        self._owner: asyncio.Task[None] | None = None
        self._active_operation: str | None = None
        self._active_started_mono: float | None = None
        self._active_reply: asyncio.Future[Any] | None = None
        self._started = False
        self._stopping = False
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start disconnected authority in observe-only mode."""
        async with self._start_lock:
            if self._stopping:
                raise RuntimeError("actor is stopping")
            if self._started:
                return
            self._started = True
            self._owner = asyncio.create_task(self._run(), name=f"energypod-actor:{self.unit_id}")
            try:
                await self._submit("connect", None, _CONTROL_PRIORITY)
            except BaseException:
                self.lifecycle = UnitLifecycle.DISCONNECTED
                self._stopping = True
                await self._advance_generation("actor-start-failed")
                self._cancel_owner()
                if self._owner is not None:
                    await asyncio.gather(self._owner, return_exceptions=True)
                raise
            self.lifecycle = UnitLifecycle.OBSERVE_ONLY

    @property
    def qualified(self) -> bool | None:
        """Whether stable observations currently qualify the unit for arming.

        ``None`` models the honest unknown — no observation has ever been
        assessed — which callers must treat as a refusal, never as permission.
        A unit holding the configured count of consecutive qualifying
        observations reports ``True`` even while armed or inhibited; the
        lifecycle and the latch remain the separate arming gates.
        """
        if self._latest_observation is None:
            return None
        return self._stable_observations >= self._stable_required

    async def accept_observation(self, observation: Any) -> None:
        await self._submit("observation", observation, _CONTROL_PRIORITY)

    async def arm(self) -> None:
        await self._submit("arm", None, _CONTROL_PRIORITY)

    async def disarm(self) -> None:
        """Disarm through the mailbox; the inhibit latch is never cleared.

        Idempotent and fail-closed: disarming an unarmed, inhibited, or
        stopping unit is a no-op, an armed unit loses its armed lifecycles and
        its outstanding authorization, and re-arming always needs the full
        qualification path again.
        """
        await self._submit("disarm", None, _CONTROL_PRIORITY)

    async def poll_once(self) -> Any:
        if self._stopping or not self._started:
            return None
        return await self._submit("poll", None, _POLL_PRIORITY)

    async def heartbeat_once(self) -> None:
        if self._stopping or not self._started:
            return

        # A queued heartbeat may not sit behind a telemetry read beyond the
        # commissioned renewal budget.  Cancellation affects the mailbox task,
        # which catches it at the current await boundary before starting a write.
        preempt = asyncio.create_task(self._preempt_overdue_read())
        try:
            await self._submit("heartbeat", None, _HEARTBEAT_PRIORITY)
        finally:
            preempt.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await preempt

    async def request_bounded_zero(self, reason: str) -> None:
        """Enqueue exactly one bounded zero write through the mailbox.

        The service facade uses this for emergency stop after it has fenced
        the fleet generation itself; the actor only owns the serialized
        bounded write.  A stopping actor is dropped here because shutdown
        owes its own bounded zero before closing the transport.
        """
        await self._submit("zero", reason, _ZERO_PRIORITY)

    async def refresh_mode_words(self) -> tuple[int, int]:
        """One bounded fresh read of the system-mode words (B5).

        Reads exactly the wired system-mode window (0x0100, three words) once
        through this actor's sole transport, inside the mailbox dispatch, so
        the API refusal path can judge ctrlMode on FRESH evidence instead of
        the cold-ring cache.  The read is bounded by the heartbeat margin
        (wired from ``write_timeout_s``) and runs below heartbeat priority,
        so it can never delay a renewal; a failing or unwired read raises,
        and the caller's two-failures policy does the rest.
        """
        words = await self._submit("refresh_mode_words", None, _CONTROL_PRIORITY)
        return (int(words[0]) & 0xFFFF, int(words[1]) & 0xFFFF)

    async def acknowledge_inhibit(self) -> None:
        """Clear one latched inhibit cause.

        Idempotent and deliberately weak: acknowledgement only clears the
        latch.  The unit still needs the configured count of stable
        qualifying observations to reach DISARMED and then an explicit arm;
        it never bypasses the safety path.
        """
        await self._submit("acknowledge_inhibit", None, _CONTROL_PRIORITY)

    async def fence(self, reason: str) -> int:
        """Publish revocation before waiting for cancellation or durable work."""
        if self._stopping:
            return self.generation
        await self._advance_generation(reason)
        self._used_cycles.clear()
        self._stable_observations = 0
        if self.lifecycle is UnitLifecycle.ACTIVE:
            self.lifecycle = UnitLifecycle.ARMED_IDLE
        self._cancel_active_authority_work()
        # Publish cancellation to an in-flight heartbeat before this method
        # returns, without waiting for cancellation-resistant port work to end.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await self._revoke(reason)
        return self.generation

    async def shutdown(self) -> None:
        """Fence once, make one bounded zero attempt, and close once."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown_once(), name=f"energypod-stop:{self.unit_id}"
            )
        await asyncio.shield(self._shutdown_task)

    async def _shutdown_once(self) -> None:
        self._stopping = True
        await self._advance_generation("shutdown")
        self._used_cycles.clear()
        self.lifecycle = UnitLifecycle.STOPPING
        self._cancel_active_authority_work()

        if self._owner is None or self._owner.done():
            # The owner cannot serialize a stop, but the shutdown contract still
            # requires one bounded zero-command attempt before closing.  No
            # mailbox task is alive, so a direct bounded write is safe.
            await self._attempt_zero_owned()
            await self._close_directly()
            return

        # Start external revocation before stopping, but never let repository
        # latency postpone the bounded zero and close sequence.
        revoke_task = asyncio.create_task(
            self._revoke("shutdown"), name=f"energypod-revoke:{self.unit_id}"
        )
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await self._submit("stop", None, -100, allow_stopping=True)
        if self._owner is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._owner
        try:
            async with asyncio.timeout(self._heartbeat_margin or 0.1):
                await revoke_task
        except (Exception, asyncio.CancelledError):
            revoke_task.cancel()
            await asyncio.gather(revoke_task, return_exceptions=True)

    async def _close_directly(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._transport.close()

    async def _submit(
        self,
        operation: str,
        argument: Any,
        priority: int,
        *,
        allow_stopping: bool = False,
    ) -> Any:
        if self._owner is None or self._owner.done():
            if operation == "stop":
                return None
            raise RuntimeError("actor is not running")
        if self._stopping and not allow_stopping:
            return None

        reply: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        message = _Message(operation, argument, reply)
        await self._mailbox.put((priority, next(self._sequence), message))
        try:
            return await reply
        except asyncio.CancelledError:
            # Cancellation of a caller must cross the mailbox boundary.  This
            # prevents an abandoned nonzero write from completing silently.
            if self._active_reply is reply and self._owner is not None:
                self._owner.cancel()
            raise

    async def _run(self) -> None:
        terminal_error: BaseException | None = None
        try:
            while True:
                _, _, message = await self._mailbox.get()
                if message.reply.cancelled():
                    continue
                self._active_operation = message.operation
                self._active_reply = message.reply
                self._active_started_mono = self._clock.monotonic()
                try:
                    result = await self._dispatch(message.operation, message.argument)
                except asyncio.CancelledError:
                    if not message.reply.done():
                        message.reply.cancel()
                    # Cancellation is an operation-level fence, not actor death.
                    continue
                except Exception as error:
                    if not message.reply.done():
                        message.reply.set_exception(error)
                except BaseException:
                    if not message.reply.done():
                        message.reply.cancel()
                    raise
                else:
                    if not message.reply.done():
                        message.reply.set_result(result)
                finally:
                    self._active_operation = None
                    self._active_started_mono = None
                    self._active_reply = None
                if message.operation == "stop":
                    return
        except BaseException as error:
            terminal_error = error
            raise
        finally:
            if self._active_reply is not None and not self._active_reply.done():
                self._active_reply.cancel()
            self._active_operation = None
            self._active_started_mono = None
            self._active_reply = None
            self._fail_queued_replies(terminal_error)

    async def _dispatch(self, operation: str, argument: Any) -> Any:
        if operation == "connect":
            return await self._transport.connect()
        if operation == "observation":
            return await self._accept_observation_owned(argument)
        if operation == "arm":
            return await self._arm_owned()
        if operation == "disarm":
            return await self._disarm_owned()
        if operation == "poll":
            return await self._poll_owned()
        if operation == "heartbeat":
            return await self._heartbeat_owned()
        if operation == "zero":
            return await self._attempt_zero_owned()
        if operation == "refresh_mode_words":
            return await self._refresh_mode_words_owned()
        if operation == "acknowledge_inhibit":
            return self._acknowledge_inhibit_owned()
        if operation == "stop":
            return await self._stop_owned()
        raise RuntimeError(f"unknown actor operation: {operation}")

    async def _accept_observation_owned(self, observation: Any) -> None:
        await self._observations.append(observation)
        self._latest_observation = observation
        if self._latching_fault_present(observation):
            await self._accept_blocking_fault_owned()
            return
        if self._identity_mismatch(observation):
            await self._accept_identity_mismatch_owned()
            return
        if self._qualifies(observation):
            observation_epoch = observation.connection_epoch
            if self._connection_epoch not in {None, observation_epoch}:
                # Stable samples never span reconnects.  Publish the fence before
                # the revocation adapter can block or fail.
                await self._advance_generation("connection-epoch-changed")
                self._used_cycles.clear()
                self._stable_observations = 0
                self.lifecycle = UnitLifecycle.OBSERVE_ONLY
                await self._revoke("connection_epoch_changed")
            self._stable_observations += 1
            self._connection_epoch = observation_epoch
            # Non-latching recovery returns to DISARMED, never directly to
            # ACTIVE: nonzero power still requires an explicit arm.  A latched
            # cause additionally holds INHIBITED until the explicit
            # acknowledgement clears the latch; stable samples alone never
            # clear it, and good telemetry alone never clears an inhibit.
            if (
                self.lifecycle in {UnitLifecycle.OBSERVE_ONLY, UnitLifecycle.INHIBITED}
                and self._stable_observations >= self._stable_required
                and not self.inhibit_latched
            ):
                self.lifecycle = UnitLifecycle.DISARMED
                self.inhibit_cause = None
                self.inhibit_reason = None
        else:
            self._stable_observations = 0
            if self.lifecycle not in {
                UnitLifecycle.BOOT,
                UnitLifecycle.STOPPING,
                UnitLifecycle.INHIBITED,
            }:
                self.lifecycle = UnitLifecycle.OBSERVE_ONLY

    async def _poll_owned(self) -> Any:
        """One telemetry cycle: advance, read the plan, decode, and deliver.

        With no injected telemetry strategy the poll stays the bare essential
        read.  With one, the device advances exactly once per cycle, every
        register window of the plan is read through this actor's transport
        (still inside the one serialized mailbox dispatch, so sole socket
        ownership is unchanged), the decoded observation is delivered through
        the same accept-observation path the public mailbox operation uses,
        and the essential registers are returned to the caller.
        """
        telemetry = self._telemetry
        if telemetry is None:
            return await self._transport.read_holding(
                self._essential_address, self._essential_count
            )
        await telemetry.advance()
        if self._cell_refresh_requested:
            # B4 (SYNC_RESILIENCE_AUDIT): the fleet loop flagged this unit
            # after a cell-derived deny; forward the hint to the strategy and
            # consume it -- exactly one promoted cycle, then the tier phase
            # rules again unless the fresh read still denies.
            self._cell_refresh_requested = False
            hint = getattr(telemetry, "request_cell_refresh", None)
            if callable(hint):
                hint()
        essential = (self._essential_address, self._essential_count)
        blocks: dict[tuple[int, int], tuple[int, ...]] = {}
        for window in (essential, *telemetry.read_plan()):
            if window in blocks:
                continue
            blocks[window] = await self._transport.read_holding(*window)
        observation = telemetry.decode(blocks, self.lifecycle)
        await self._accept_observation_owned(observation)
        return blocks[essential]

    def request_cell_refresh(self) -> None:
        """Schedule this unit's NEXT telemetry cycle to include 0x5200 (B4).

        Set by the fleet loop after a control decision carried a cell-derived
        deny reason for this unit's fleet: the cell window normally rides the
        every-3rd-cycle tier, so the deny may have judged a stale cached
        window.  The promoted poll re-evaluates the deny on FRESH battery
        data (steady plan 8 -> 9 windows, ~1.0 s at the 0.1 s inter-frame
        gap, inside the 1.5 s control period / 1.60 s renewal budget).  The
        flag is consumed by exactly one poll and never bypasses a fresh
        violation: a persisting fresh violation re-denies and re-promotes.
        """
        self._cell_refresh_requested = True

    async def _refresh_mode_words_owned(self) -> tuple[int, int]:
        """The B5 bounded fresh read of (ctrlMode, workMode) served words."""
        window = self._mode_refresh_window
        if window is None:
            raise RuntimeError(f"{self.unit_id}: no mode refresh window is wired for this actor")
        address, count = window
        try:
            async with asyncio.timeout(self._heartbeat_margin or 0.1):
                words = tuple(await self._transport.read_holding(address, count))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise RuntimeError(f"{self.unit_id}: the mode-word refresh read failed") from error
        if len(words) < 3:
            raise RuntimeError(
                f"{self.unit_id}: the mode-word window did not serve ctrlMode and workMode"
            )
        return (int(words[1]) & 0xFFFF, int(words[2]) & 0xFFFF)

    def _latching_fault_present(self, observation: Any) -> bool:
        # Blocking-fault classification is the policy's: composition wires the
        # configured blocking fault codes, so an empty set disables the hook.
        if not self._blocking_fault_codes:
            return False
        faults = getattr(observation, "active_faults", None) or ()
        return bool(set(faults) & self._blocking_fault_codes)

    async def _accept_blocking_fault_owned(self) -> None:
        """Latch on a policy blocking fault; it never clears by itself."""
        if self.lifecycle is UnitLifecycle.INHIBITED and self.inhibit_latched:
            # The fault is still present: preserve the latch and reset the
            # stable-sample count so recovery cannot proceed underneath it.
            self._stable_observations = 0
            return
        await self._inhibit_owned("blocking_fault_active", InhibitCause.LATCHED)

    def _identity_mismatch(self, observation: Any) -> bool:
        """Whether the observation contradicts this unit's commissioned identity.

        An absent identity is the honest unknown, not a contradiction: it
        fails qualification without latching.  A presented-but-different
        device identity or protocol profile is ARCHITECTURE 8.1's latched
        "identity mismatch": another device is speaking on this transport, so
        only a privileged acknowledgement may re-qualify the unit.
        """
        identity = getattr(observation, "device_identity", None)
        if identity is not None and identity != self._expected_identity:
            return True
        profile = getattr(observation, "protocol_profile", None)
        return profile is not None and profile != self._expected_profile

    async def _accept_identity_mismatch_owned(self) -> None:
        """Latch on an identity mismatch; acknowledgement is the only exit."""
        if self.lifecycle is UnitLifecycle.INHIBITED and self.inhibit_latched:
            # The mismatch persists: hold the standing latch and keep the
            # stable-sample count at zero so recovery cannot proceed
            # underneath it.
            self._stable_observations = 0
            return
        await self._inhibit_owned("identity_mismatch", InhibitCause.LATCHED)

    def _qualifies(self, observation: Any) -> bool:
        complete = getattr(observation, "complete", None)
        if complete is None:
            # Domain observations carry the derived safety-completeness view
            # instead of a bare flag; incomplete safety data never qualifies.
            complete = getattr(observation, "safety_data_complete", False)
        quality = getattr(observation, "quality", None)
        quality_items = getattr(quality, "items", None)
        if callable(quality_items):
            # A quality map (domain Observation): every SAFETY-CRITICAL
            # telemetry field must be good — the advisory CT fields stay
            # outside this judgment (see _SAFETY_QUALITY_FIELDS) — and an
            # empty judgment is the absence of evidence, not proof.
            values = tuple(
                value for field, value in quality_items() if field in _SAFETY_QUALITY_FIELDS
            )
            quality_ok = bool(values) and all(
                getattr(value, "value", value) == "good" for value in values
            )
        else:
            quality_ok = getattr(quality, "value", quality) == "good"
        cells = getattr(observation, "cells", None)
        cell_count_ok = cells is None or len(cells) == self._expected_cell_count
        return bool(
            complete
            and quality_ok
            and getattr(observation, "unit_id", None) == self.unit_id
            and getattr(observation, "device_identity", None) == self._expected_identity
            and getattr(observation, "protocol_profile", None) == self._expected_profile
            and cell_count_ok
        )

    async def _arm_owned(self) -> None:
        if self._stopping:
            return
        if (
            self.lifecycle is not UnitLifecycle.DISARMED
            or self.inhibit_latched
            or self._stable_observations < self._stable_required
        ):
            raise RuntimeError("unit is not qualified for arming")
        # The external-writer preflight is arm-gated: it runs exactly once per
        # arm attempt, inside this mailbox dispatch, before ARMED_IDLE — never
        # per heartbeat.
        await self._verify_sole_writer_owned()
        self.lifecycle = UnitLifecycle.ARMED_IDLE

    async def _verify_sole_writer_owned(self) -> None:
        """Refuse the arm unless this actor is the sole PQ writer.

        API_CONTRACTS "Write-enabled run mode", bullet 3: at arm time the
        actor reads the served PQ objective readback through its own
        transport.  Any nonzero objective it did not itself write means
        another writer holds the unit, so the arm is refused and the unit
        latches INHIBITED with cause ``external_writer`` (privileged
        acknowledgement required, and the preflight re-latches while the
        foreign objective persists).  An unreadable readback also refuses the
        arm, fail-closed, but as a non-latched transient so ordinary
        stable-sample recovery suffices once it reads back zero.  The port is
        unset (``None``) in observe-only wiring and the probe is skipped
        entirely.

        An objective this actor itself applied — the standing objective left
        on the device across a disarm, or the bounded zero an inhibit or stop
        delivered — is not a foreign writer: the preflight compares the served
        pair against the last objective this actor wrote, and a fresh actor
        that has written nothing treats every nonzero readback as foreign
        because it cannot inherit provenance from a previous process.
        """
        address = self._objective_readback_address
        if address is None:
            return
        try:
            readback = tuple(await self._transport.read_holding(address, _OBJECTIVE_READBACK_COUNT))
            if len(readback) < _OBJECTIVE_READBACK_COUNT:
                raise ValueError("objective readback did not cover P and Q")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._inhibit_owned("objective_readback_unreadable", InhibitCause.TRANSIENT)
            raise RuntimeError(
                f"{self.unit_id}: arm refused, the served PQ objective readback is unreadable"
            ) from error
        served = (self._signed_objective(readback[0]), self._signed_objective(readback[1]))
        if served != (0, 0) and served != self._applied_objective:
            await self._inhibit_owned("external_writer", InhibitCause.LATCHED)
            raise RuntimeError(
                f"{self.unit_id}: arm refused, an external writer holds the PQ "
                f"objective (P={served[0]}, Q={served[1]})"
            )

    @staticmethod
    def _signed_objective(word: int) -> int:
        """Decode one readback word as the signed PQ objective being served.

        The firmware serves two's-complement int16 words (the live -200 W
        charge objective read back as 0xFF38); words already decoded as
        negative by an adapter pass through unchanged.
        """
        return word - 0x10000 if word > 0x7FFF else word

    async def _disarm_owned(self) -> None:
        # An unarmed or inhibited unit has nothing to drop; an armed unit loses
        # its armed lifecycle first, then its outstanding authority, so an
        # observer that sees DISARMED can never still see live capability.
        # The inhibit latch and its cause are deliberately untouched: only the
        # privileged acknowledgement may clear them.
        if self._stopping or self.lifecycle not in {
            UnitLifecycle.ARMED_IDLE,
            UnitLifecycle.ACTIVE,
        }:
            return
        self.lifecycle = UnitLifecycle.DISARMED
        await self._revoke("disarmed")

    def _acknowledge_inhibit_owned(self) -> None:
        # Acknowledgement clears only the latch; lifecycle and the stable-sample
        # recovery path are untouched, so it can never arm the unit directly.
        self.inhibit_latched = False

    async def _heartbeat_owned(self) -> None:
        if self._stopping or self.lifecycle not in {
            UnitLifecycle.ARMED_IDLE,
            UnitLifecycle.ACTIVE,
        }:
            return

        lookup_generation = (await self._generation_coordinator.snapshot()).epoch
        self.generation = lookup_generation
        lookup_now = self._clock.monotonic()
        try:
            authorization = await self._authorizations.current(self.unit_id, lookup_now)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._inhibit_owned("authorization_lookup_failed")
            return
        if authorization is None:
            await self._reject_authorization(None, "authorization_missing")
            return

        try:
            reason = await self._authorization_rejection(authorization, lookup_generation)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._inhibit_owned("observation_lookup_failed")
            return
        if reason is not None:
            await self._reject_authorization(authorization, reason)
            return

        cycle = (authorization.generation, authorization.cycle_id)
        if cycle in self._used_cycles:
            await self._reject_authorization(authorization, "authorization_already_used")
            return

        # Consumption deliberately precedes encoding and the transport await.
        self._used_cycles.add(cycle)
        try:
            encoded = self._command_encoder.encode(authorization)
        except Exception:
            # The authorized command could not be encoded: a control-data
            # mismatch, not a transport blip.
            await self._inhibit_owned("command_encoding_failed", InhibitCause.QUALIFIED)
            return
        try:
            await self._transport.write_registers(encoded.address, encoded.values)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._advance_generation("write-failed")
            self._used_cycles.clear()
            self._stable_observations = 0
            self.lifecycle = UnitLifecycle.INHIBITED
            # A transport failure is one failed renewal attempt: transient, so
            # the existing stable-sample recovery path is unchanged.
            self._record_inhibit_cause(InhibitCause.TRANSIENT, "write_failed")
            await self._attempt_zero_owned()
            await self._revoke("write_failed")
            return
        # The device now serves this objective; remembering it lets the next
        # arm-time preflight recognize this actor's own standing objective.
        self._record_applied_objective(encoded.values)

        # A cancellation-resistant adapter may acknowledge after replacement.
        # Never let that stale result restore ACTIVE authority.
        if (
            not self._stopping
            and lookup_generation == (await self._generation_coordinator.snapshot()).epoch
            and authorization.generation == lookup_generation
        ):
            self.lifecycle = UnitLifecycle.ACTIVE

    async def _authorization_rejection(
        self, authorization: Any, lookup_generation: int
    ) -> str | None:
        now = self._clock.monotonic()
        if getattr(authorization, "unit_id", None) != self.unit_id:
            return "unit_mismatch"
        current_generation = (await self._generation_coordinator.snapshot()).epoch
        self.generation = current_generation
        if authorization.generation != lookup_generation or lookup_generation != current_generation:
            return "generation_mismatch"
        if authorization.not_before_mono > now:
            return "authorization_not_yet_valid"
        if authorization.expires_at_mono <= now:
            return "authorization_expired"

        # Refresh evidence after the authorization lookup.  This is the final
        # awaited safety read before capability consumption and transport I/O.
        observation = await self._observations.latest(self.unit_id)
        current_generation = (await self._generation_coordinator.snapshot()).epoch
        self.generation = current_generation
        if lookup_generation != current_generation:
            return "generation_mismatch"
        now = self._clock.monotonic()
        if authorization.expires_at_mono <= now:
            return "authorization_expired"
        if authorization.not_before_mono > now:
            return "authorization_not_yet_valid"
        if observation is None or not self._qualifies(observation):
            return "observation_invalid"
        if observation.connection_epoch != authorization.connection_epoch:
            return "connection_epoch_mismatch"
        if self._connection_epoch != authorization.connection_epoch:
            return "connection_epoch_mismatch"
        if observation.sequence != authorization.observation_sequence:
            return "observation_sequence_mismatch"
        self._latest_observation = observation
        return None

    async def _reject_authorization(self, authorization: Any, reason: str) -> None:
        if self.lifecycle is UnitLifecycle.ACTIVE:
            self.lifecycle = UnitLifecycle.ARMED_IDLE
        await self._revoke(reason, authorization)

    async def _inhibit_owned(
        self, reason: str, cause: InhibitCause = InhibitCause.TRANSIENT
    ) -> None:
        """Fail closed locally before invoking any potentially blocking port."""
        await self._advance_generation(reason)
        self._used_cycles.clear()
        self._stable_observations = 0
        self.lifecycle = UnitLifecycle.INHIBITED
        self._record_inhibit_cause(cause, reason)
        await self._attempt_zero_owned()
        await self._revoke(reason)

    def _record_inhibit_cause(self, cause: InhibitCause, reason: str) -> None:
        # Entering INHIBITED always records a cause class and its operator-
        # visible reason string.  Only LATCHED sets the latch; TRANSIENT/
        # QUALIFIED keep the existing stable-sample recovery behavior
        # (ADR-0003 D5).  A standing latch is never downgraded by a later
        # non-latched cause: lifecycle ordering keeps transient inhibit paths
        # out of a latched unit today, and this guard keeps the latch — and
        # its reason — true even if a future path forgets that ordering.
        if self.inhibit_latched and cause is not InhibitCause.LATCHED:
            return
        self.inhibit_cause = cause
        self.inhibit_latched = cause is InhibitCause.LATCHED
        self.inhibit_reason = reason

    async def _advance_generation(self, reason: str) -> int:
        snapshot = await self._generation_coordinator.advance(reason=reason)
        self.generation = snapshot.epoch
        self._used_cycles.clear()
        return snapshot.epoch

    async def _revoke(self, reason: str, authorization: Any = None) -> None:
        with contextlib.suppress(Exception):
            await self._authorizations.revoke(
                (self.unit_id,), reason=reason, authorization=authorization
            )

    async def _attempt_zero_owned(self) -> None:
        zero = self._command_encoder.zero()
        try:
            async with asyncio.timeout(self._heartbeat_margin or 0.1):
                await self._transport.write_registers(zero.address, zero.values)
        except (Exception, asyncio.CancelledError):
            return
        self._record_applied_objective(zero.values)

    def _record_applied_objective(self, values: Sequence[int]) -> None:
        """Remember the [1, P, Q] objective frame this actor just applied."""
        if len(values) < 3:
            return
        self._applied_objective = (
            self._signed_objective(int(values[1])),
            self._signed_objective(int(values[2])),
        )

    async def _stop_owned(self) -> None:
        await self._attempt_zero_owned()
        if not self._closed:
            self._closed = True
            with contextlib.suppress(Exception):
                await self._transport.close()

    async def _preempt_overdue_read(self) -> None:
        threshold = self._heartbeat_interval - self._heartbeat_margin
        started = self._active_started_mono
        elapsed = 0.0 if started is None else max(0.0, self._clock.monotonic() - started)
        await self._clock.sleep(max(0.0, threshold - elapsed))
        if self._active_operation == "poll" and self._owner is not None:
            self._owner.cancel()

    def _cancel_active_authority_work(self) -> None:
        if self._active_operation == "heartbeat" and self._owner is not None:
            self._owner.cancel()

    def _cancel_owner(self) -> None:
        if self._owner is not None and not self._owner.done():
            self._owner.cancel()

    def _fail_queued_replies(self, error: BaseException | None) -> None:
        while True:
            try:
                _, _, message = self._mailbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if message.reply.done():
                continue
            if error is None or isinstance(error, asyncio.CancelledError):
                message.reply.cancel()
            else:
                message.reply.set_exception(RuntimeError("actor mailbox terminated unexpectedly"))
