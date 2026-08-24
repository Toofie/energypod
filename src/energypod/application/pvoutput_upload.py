"""The PVOutput upload controller (the retiring Docker writer's replacement).

One application component in the TelemetryHistorian's exact shape: one
bounded, fully suppressed tick per fleet cycle -- AFTER the polls, the
accountant, and the historian, and BEFORE the kernel tick -- so the reporter
can never delay or crash the control loop.  It is observability only: it
READS the observation stream through the same port the energy accountant
reads and writes nothing but its own HTTP status posts.

Doctrine pinned here:

- **One POST per 5-minute slot, +30 s grace.** The tick attempts a slot only
  once the freshest poll can have landed (the slot's first ~30 s), at most
  ``retry_max`` retries per slot spaced at least 60 s apart (never bursts),
  and never a second successful post inside the configured ``interval_s``.
  A restart starts with no memory of past slots: the current slot posts
  fresh, past ones are gaps -- **no backfill, ever** (the old container's
  every-5-minutes cron had the same property; PVOutput slots are cheap,
  fabricated history is not).
- **Nulls are nulls, never zero.** Every field rides through its own quality
  gate: a unit's SoC posts only when the observation's ``bms_soc_pct``
  quality entry is GOOD and the value present, its power only when
  ``battery_watts`` holds the same bar.  A stale or bad field is OMITTED
  from the POST -- never zero-filled -- and a slot where no pod is fresh
  enough (``max_sample_age_s``) is skipped entirely: a gap on the dashboard,
  which is the honest record of a telemetry outage.
- **The pinned slot layout.** Each commissioned unit owns its config-declared
  ``[SoC slot, power slot]`` pair (the old container's exact layout for
  dashboard continuity); SoC is the REAL BMS word (``bms_soc_pct`` --
  strictly better than the old container's voltage-curve estimate), power is
  the signed ``battery_watts`` posted UNNEGATED (both words share the
  negative = charge orientation, PROTOCOL_EVIDENCE 4b).  The native fleet
  aggregates (``b1``/``b2``) ride the SAME POST: ``b1`` is the plain sum in
  pod-manager's convention (the adapter flips it to the spec's), ``b2`` the
  mean SoC over the identical pod capacities -- and because the
  specification makes ``b1`` mandatory whenever any battery field is sent,
  ``b2`` is omitted together with ``b1`` whenever ``b1`` cannot be computed
  fresh.
- **Failure semantics.** An auth-class refusal (bad/read-only key, donation
  mode) DISABLES the uploader with a LOUD persistent note on the status
  surface -- retrying a credential that may never write is noise; the
  operator's next enable clears the latch and retries.  The rate 403 backs
  off until PVOutput's own reset instant.  A 400 surfaces PVOutput's reason
  text verbatim and moves on (the slot is refused, not retryable).  A
  transient failure retries inside the slot's budget, then the slot is a
  gap.
- **The durable runtime toggle.** ``enabled`` boots from the config block
  and is overridden by the durable toggle row (schema v7): the operator's
  enable/disable is a machine fact that survives restarts, written by
  :meth:`set_enabled` BEFORE the in-memory flip (a failing durable write
  refuses the toggle; there is never an in-memory-only choice).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from energypod.domain.observations import DataQuality

__all__ = [
    "PVOUTPUT_CONFIRMATION",
    "PvOutputRefusal",
    "PvOutputUploader",
]

#: The guarded toggle's typed confirmation literal (the night/excess pattern).
PVOUTPUT_CONFIRMATION: Final[str] = "PVOUTPUT"

#: The slot grid (PVOutput's 5-minute status interval) and the posting grace:
#: a slot is attempted only once its first ~30 s have passed, so the freshest
#: poll of the slot has landed and the POST describes real current data.
_SLOT_SECONDS: Final[float] = 300.0
_SLOT_GRACE_S: Final[float] = 30.0

#: Same-slot retry spacing: a transient failure retries no sooner than this,
#: so the retry budget can never become a burst against a struggling endpoint.
_RETRY_SPACING_S: Final[float] = 60.0

#: The margin added to PVOutput's own rate-reset instant before the next
#: attempt (clock skew between two machines; small on purpose).
_RATE_MARGIN_S: Final[float] = 5.0

#: The failure-class vocabulary the uploader reads off the injected client's
#: typed errors (structural: the adapter's error classes carry the matching
#: ``pvoutput_failure`` attribute, so the application layer never imports the
#: adapter module).
_FAILURE_AUTH: Final[str] = "auth"
_FAILURE_RATE_LIMITED: Final[str] = "rate_limited"
_FAILURE_REJECTED: Final[str] = "rejected"
_FAILURE_UNAVAILABLE: Final[str] = "unavailable"


class PvOutputRefusal(Exception):
    """A guarded-surface refusal carrying its wire code and details.

    The facade raises exactly this for the 409 shapes; the guarded boundary
    maps ``code`` onto the error envelope verbatim.
    """

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class _ObservationsPort(Protocol):
    """The observation read the accountant itself uses."""

    async def all_latest(self) -> dict[str, Any]: ...


class _ClockPort(Protocol):
    def wall_now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class _StatusClientPort(Protocol):
    """The injected pvoutput client (the adapter satisfies this structurally).

    ``post_status`` raises the adapter's typed errors; the uploader classifies
    them through their structural ``pvoutput_failure`` attribute.
    """

    async def post_status(
        self, *, sample_at: datetime, fields: Mapping[str, float]
    ) -> Any: ...


class _ToggleStorePort(Protocol):
    """The durable runtime-toggle singleton (schema v7)."""

    def state(self) -> tuple[bool, str] | None: ...

    def store(self, *, enabled: bool, updated_at: str) -> None: ...


def _slot_floor(local: datetime) -> datetime:
    return local.replace(minute=(local.minute // 5) * 5, second=0, microsecond=0)


def _failure_class(error: BaseException) -> str | None:
    """The typed failure word off the adapter's error, or None (untreated)."""
    failure = getattr(error, "pvoutput_failure", None)
    return failure if isinstance(failure, str) and failure else None


def _gated(observation: Any, field: str) -> float | None:
    """One field's value when its own quality entry is GOOD, else None.

    The doctrine's whole teeth: a stale/bad/missing quality entry OMITS the
    field from the POST -- the dashboard keeps an honest gap, never a zero a
    degraded poll fabricated.
    """
    if observation is None:
        return None
    value = getattr(observation, field, None)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(float(value)):
        return None
    quality = getattr(observation, "quality", None)
    if not isinstance(quality, Mapping) or quality.get(field) != DataQuality.GOOD:
        return None
    return float(value)


class PvOutputUploader:
    """The uploader control: cadence gate, snapshot assembly, health state."""

    def __init__(
        self,
        *,
        unit_ids: tuple[str, ...],
        unit_slots: Mapping[str, tuple[str, str]],
        timezone_name: str,
        interval_s: float,
        max_sample_age_s: float,
        retry_max: int,
        native_battery_fields: bool,
        config_enabled: bool,
        client: _StatusClientPort | None,
        clock: _ClockPort,
        observations: _ObservationsPort,
        store: _ToggleStorePort | None = None,
        credentials_note: str | None = None,
    ) -> None:
        units = tuple(unit_ids)
        if not units or any(
            not isinstance(unit, str) or not unit or unit != unit.strip() for unit in units
        ):
            raise ValueError("unit_ids must be non-empty normalized identifiers")
        if len(set(units)) != len(units):
            raise ValueError("unit_ids must be unique")
        if set(unit_slots) != set(units):
            raise ValueError("unit_slots must cover exactly unit_ids")
        for name, value in (
            ("interval_s", interval_s),
            ("max_sample_age_s", max_sample_age_s),
        ):
            if not isinstance(value, int | float) or not float(value) > 0:
                raise ValueError(f"{name} must be positive")
        if not isinstance(retry_max, int) or isinstance(retry_max, bool) or retry_max < 0:
            raise ValueError("retry_max must be a non-negative integer")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be an IANA timezone") from exc
        self._unit_ids = units
        self._unit_slots = dict(unit_slots)
        self._zone = zone
        self._interval_s = float(interval_s)
        self._max_sample_age_s = float(max_sample_age_s)
        self._retry_max = int(retry_max)
        self._native = bool(native_battery_fields)
        self._client = client
        self._clock = clock
        self._observations = observations
        self._store = store
        self._credentials_note = credentials_note
        # --- the runtime toggle: the durable row overrides the config, the
        # config is the boot default, and the origin names which one speaks.
        self._enabled = bool(config_enabled)
        self._enabled_origin = "config"
        stored = store.state() if store is not None else None
        self._stored_updated_at: str | None = None
        if stored is not None:
            self._enabled = bool(stored[0])
            self._enabled_origin = "runtime"
            self._stored_updated_at = stored[1]
        # --- in-memory health state (the status surface's whole truth) ---
        self._auth_disabled = False
        self._disabled_reason: str | None = None
        self._last_success_at: datetime | None = None
        self._last_posted_slot: str | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._rate_remaining: int | None = None
        self._slots_skipped_stale = 0
        self._attempts_this_slot = 0
        self._slot_succeeded = False
        self._last_slot_key: datetime | None = None
        self._last_attempt_mono: float | None = None
        self._last_posted_slot_key: datetime | None = None
        self._backoff_until_mono: float | None = None
        # The lazy slot-close bookkeeping: a slot whose attempts all judged
        # the fleet stale counts once, when the NEXT slot proves it never
        # recovered inside its own window.
        self._pending_stale_close = False

    # --- the guarded toggle ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def enabled_origin(self) -> str:
        return self._enabled_origin

    def set_enabled(self, enabled: bool) -> None:
        """The runtime toggle: durable write FIRST, in-memory flip second.

        A failing durable write refuses the whole act (there is never an
        in-memory-only choice -- the toggle's entire point is surviving a
        restart).  The composition audits after this returns; an audit
        failure never rolls the flip back (the Impl-10 doctrine).  Enabling
        also clears an auth-disable latch: the operator's explicit re-enable
        is the one honest retry of a credential that may have been fixed.
        """
        if self._store is None:
            raise PvOutputRefusal(
                "pvoutput_toggle_failed",
                "the durable pvoutput toggle store is not composed on this deployment",
            )
        updated_at = self._clock.wall_now().astimezone(UTC).isoformat()
        try:
            self._store.store(enabled=bool(enabled), updated_at=updated_at)
        except PvOutputRefusal:
            raise
        except Exception as error:
            # The durable write is the act: a refused store write refuses
            # the whole toggle, and nothing in memory moved.
            raise PvOutputRefusal(
                "pvoutput_toggle_failed",
                "the durable pvoutput toggle could not be stored; the choice was not "
                "recorded and nothing changed",
                {"error": type(error).__name__},
            ) from error
        self._enabled = bool(enabled)
        self._enabled_origin = "runtime"
        self._stored_updated_at = updated_at
        if enabled:
            self._auth_disabled = False
            self._disabled_reason = None

    # --- the status surface --------------------------------------------------------

    def status_payload(self) -> dict[str, Any]:
        """The health snapshot ``GET /api/v1/pvoutput/status`` serves."""
        now = self._clock.wall_now()
        age = (
            None
            if self._last_success_at is None
            else max(0.0, (now - self._last_success_at).total_seconds())
        )
        disabled_reason = self._disabled_reason
        if disabled_reason is None and self._client is None:
            disabled_reason = "missing_credentials"
        return {
            "feature": "pvoutput",
            "enabled": self._enabled,
            "enabled_origin": self._enabled_origin,
            "disabled_reason": disabled_reason,
            "credentials_note": self._credentials_note,
            "interval_s": self._interval_s,
            "unit_slots": {
                unit: [soc, power] for unit, (soc, power) in sorted(self._unit_slots.items())
            },
            "native_battery_fields": self._native,
            "as_of": now.astimezone(UTC).isoformat(),
            "last_success_at": (
                None
                if self._last_success_at is None
                else self._last_success_at.astimezone(UTC).isoformat()
            ),
            "last_post_age_s": age,
            "last_posted_slot": self._last_posted_slot,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
            "rate_remaining": self._rate_remaining,
            "slots_skipped_stale": self._slots_skipped_stale,
        }

    # --- the fleet-cycle tick --------------------------------------------------------

    async def tick(self) -> None:
        """One suppressed posting tick; never raises into the fleet loop."""
        try:
            await self._tick()
        except Exception as error:
            # Survived but never invisible (the suppressed-heartbeat and
            # historian precedent): one process log line, nothing else.
            print(f"SUPERVISED PVOUTPUT TICK FAILURE: {error!r}", flush=True)

    async def _tick(self) -> None:
        client = self._client
        if client is None or self._auth_disabled or not self._enabled:
            return
        now = self._clock.wall_now()
        now_mono = float(self._clock.monotonic())
        if self._backoff_until_mono is not None and now_mono < self._backoff_until_mono:
            return
        local = now.astimezone(self._zone)
        slot = _slot_floor(local)
        # The grace: the slot's first ~30 s belong to the polls, not the POST.
        if (local - slot).total_seconds() < _SLOT_GRACE_S:
            return
        if self._last_slot_key is not None and slot < self._last_slot_key:
            # The wall clock moved backwards across a slot edge; never act on
            # a slot already judged under a later reading of time.
            return
        if slot != self._last_slot_key:
            self._close_pending_slot()
            self._last_slot_key = slot
            self._attempts_this_slot = 0
            self._slot_succeeded = False
            self._pending_stale_close = False
        if self._attempts_this_slot > self._retry_max:
            return  # the slot's budget is spent: it stays whatever it became
        if (
            self._attempts_this_slot > 0
            and self._last_attempt_mono is not None
            and now_mono - self._last_attempt_mono < _RETRY_SPACING_S
        ):
            return  # retries never burst
        if self._last_posted_slot_key is not None:
            # interval_s is the minimum spacing between POSTED slots: 300 s
            # (the grid) posts every slot; a coarser interval posts every
            # interval_s/300-th slot by slot arithmetic, immune to attempt
            # timing.
            spacing = (slot - self._last_posted_slot_key).total_seconds()
            if spacing < self._interval_s - _SLOT_SECONDS / 2:
                return
        self._attempts_this_slot += 1
        self._last_attempt_mono = now_mono
        await self._attempt_slot(client, now, now_mono)

    async def _attempt_slot(
        self, client: _StatusClientPort, now: datetime, now_mono: float
    ) -> None:
        latest: Mapping[str, Any] = {}
        store_read_failed = False
        try:
            latest = await self._observations.all_latest()
        except Exception:
            # An unreadable observation store is a stale judgment: this slot
            # attempt contributes nothing and the retry budget governs.
            store_read_failed = True
        fresh: list[Any] = []
        if not store_read_failed:
            for unit_id in self._unit_ids:
                observation = latest.get(unit_id)
                captured = getattr(observation, "captured_at_mono", None)
                if (
                    isinstance(captured, int | float)
                    and not isinstance(captured, bool)
                    and now_mono - float(captured) <= self._max_sample_age_s
                ):
                    fresh.append(observation)
        if not fresh:
            # No pod has fresh-enough observations: the slot is a gap, never
            # zero-filled -- recorded once, when the slot closes still stale.
            self._pending_stale_close = True
            return
        self._pending_stale_close = False
        fields: dict[str, float] = {}
        powers: list[float] = []
        socs: list[float] = []
        sample_at: datetime | None = None
        for observation in fresh:
            stamped_unit_id: Any = getattr(observation, "unit_id", None)
            slots = (
                self._unit_slots.get(stamped_unit_id)
                if isinstance(stamped_unit_id, str)
                else None
            )
            if slots is None:
                continue
            soc = _gated(observation, "bms_soc_pct")
            power = _gated(observation, "battery_watts")
            if soc is not None:
                fields[slots[0]] = soc
                socs.append(soc)
            # The power slots post UNNEGATED: both words share the negative
            # = charge orientation (PROTOCOL_EVIDENCE 4b), so the dashboard
            # graphs the old container drew stay continuous.
            if power is not None:
                fields[slots[1]] = power
                powers.append(power)
            stamped = getattr(observation, "wall_timestamp", None)
            if (
                isinstance(stamped, datetime)
                and stamped.tzinfo is not None
                and (sample_at is None or stamped > sample_at)
            ):
                sample_at = stamped
        if self._native and powers:
            # b1 is the fleet aggregate in POD-MANAGER's convention (the
            # adapter flips it onto the specification's); b2 rides only when
            # b1 could be computed fresh -- the spec makes b1 mandatory when
            # any battery field is sent, so the pair omits together.
            fields["b1"] = float(sum(powers))
            if socs:
                fields["b2"] = sum(socs) / len(socs)
        if not fields:
            self._last_error = (
                "slot skipped: no unit carried a fresh good SoC or power field "
                "(quality-gated omission, never zero-filled)"
            )
            return
        if sample_at is None:
            sample_at = now
        if sample_at > now:
            # A stamp from the future can never ride the wire (the spec
            # refuses future dates); the honest floor is this tick's now.
            sample_at = now
        try:
            result = await client.post_status(sample_at=sample_at, fields=fields)
        except Exception as error:
            self._record_failure(error, now_mono)
            return
        remaining = getattr(result, "rate_remaining", None)
        self._rate_remaining = remaining if isinstance(remaining, int) else self._rate_remaining
        self._last_success_at = now
        self._consecutive_failures = 0
        self._last_error = None
        self._slot_succeeded = True
        self._last_posted_slot_key = self._last_slot_key
        self._last_posted_slot = _slot_floor(sample_at.astimezone(self._zone)).strftime(
            "%Y-%m-%d %H:%M"
        )

    def _record_failure(self, error: BaseException, now_mono: float) -> None:
        """Map one typed adapter refusal onto the uploader's health state."""
        self._consecutive_failures += 1
        self._last_error = str(error) or type(error).__name__
        failure = _failure_class(error)
        if failure == _FAILURE_AUTH:
            # Loud and persistent: the status surface carries the note until
            # the operator acts (a new key, a donation, or an explicit
            # re-enable at the toggle).
            self._auth_disabled = True
            self._disabled_reason = "auth_failed"
            return
        if failure == _FAILURE_RATE_LIMITED:
            # Back off to PVOutput's own reset instant (Unix UTC) plus a
            # small skew margin; slots inside the window are gaps.
            reset = getattr(error, "reset_at_unix", None)
            wait = _RATE_MARGIN_S
            if isinstance(reset, int):
                wait = max(wait, reset - self._clock.wall_now().timestamp() + _RATE_MARGIN_S)
            self._backoff_until_mono = now_mono + max(0.0, wait)
            return
        if failure == _FAILURE_REJECTED:
            # A 400 refused THIS payload; retrying the identical bytes inside
            # the same slot is noise, so the slot's budget is spent and the
            # verbatim reason rides in ``last_error`` for the operator.
            self._attempts_this_slot = self._retry_max + 1
            return
        # Unavailable (timeout/5xx) consumes its attempt; the retry budget
        # and the slot grid carry the rest.

    def _close_pending_slot(self) -> None:
        """Judge the slot that just ended: a never-recovered stale gap counts."""
        if self._pending_stale_close and not self._slot_succeeded:
            self._slots_skipped_stale += 1
        self._pending_stale_close = False
