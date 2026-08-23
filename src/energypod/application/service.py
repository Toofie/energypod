"""Application service facade: the sole application surface behind REST and MCP.

The facade composes the intent, observation, authorization, and audit
repositories, the fleet-wide generation fence, the event bus, and per-unit
actor handles into the operations the guarded adapters call.  It owns no
transport and imports no protocol adapter: every hardware effect travels
through an actor handle's bounded-zero request, and authorization is only
ever published by the control kernel, never by this module.

The safety order inside ``emergency_stop`` is fixed and may not be reordered:
fence the fleet generation first, then revoke outstanding fleet
authorizations, then fence every affected actor (cancelling any in-flight
nonzero heartbeat write), then request the bounded zero through those actors,
and only then audit and publish.  A degraded dependency anywhere in that
sequence may turn the response into an error, but it never removes a step.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from energypod.domain import Direction, IntentSource, Observation, PowerIntent, UnitLifecycle
from energypod.domain.audit import AuditEvent

from .arbiter import IntentArbiter

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# API_CONTRACTS: a latched stop carries a fixed duration of at least 24 hours
# and is removed only by acknowledgement, never by TTL expiry.
_LATCHED_STOP_DURATION_S: Final[float] = 86_400.0

# The control-side telemetry freshness limit is policy-owned
# (ControlPolicy.max_telemetry_age_s) and enforced by the safety kernel.  The
# facade has no policy, so its snapshot view applies only this conservative
# display bound: telemetry older than this is never labelled "good".
_SNAPSHOT_GOOD_TELEMETRY_MAX_AGE_S: Final[float] = 30.0

# API_CONTRACTS "Application service facade" (2026-08-24 cold-load fix): the
# snapshot's per-unit intent view mirrors the FRESHEST control-decision row for
# its authorized figures.  One bounded newest-first read is scanned for the
# newest decision; when the window holds none, the authorized map is null --
# never fabricated and never dug out of older history.
_SNAPSHOT_DECISION_SCAN_LIMIT: Final[int] = 25

_ARMED_LIFECYCLES: Final[frozenset[str]] = frozenset({"armed_idle", "active"})
_FACADE_POLICY_VERSION: Final[str] = "facade"
_MAX_REASON_LENGTH: Final[int] = 500

# API_CONTRACTS "Application service facade": the nullable per-unit telemetry
# summary field set.  Every field is null when the observation lacks that
# datum, never zero-filled or fabricated.
_TELEMETRY_SUMMARY_FIELDS: Final[tuple[str, ...]] = (
    "soc_pct",
    "bms_soc_pct",
    "soh_pct",
    "pack_voltage_v",
    "pack_current_a",
    "battery_watts",
    "dynamic_charge_limit_w",
    "dynamic_discharge_limit_w",
    "cell_count",
    "cell_min_v",
    "cell_max_v",
    "cell_spread_mv",
    "temperature_min_c",
    "temperature_max_c",
    "active_faults",
    "active_warnings",
    "grid_power_w",
    "load_power_w",
    "debug_mode_w",
    "ctrl_mode_w",
    "work_mode_w",
    "run_mode_w",
)

# API_CONTRACTS "Excess-solar accelerated charging (advisory)": the advisory
# CT power words stay OUTSIDE every ordinary quality judgment.  The fleet
# view's aggregate quality therefore judges only the safety-critical fields;
# a read plan that does not serve the PCS block (advisory MISSING) must not
# degrade the fleet view, and the advisory words gate their own fail-closed
# export bound instead.
_SAFETY_QUALITY_FIELDS: Final[frozenset[str]] = Observation.QUALITY_FIELDS


class Principal(Protocol):
    """Authenticated caller identity, as validated by the guarded boundary."""

    @property
    def subject(self) -> str: ...

    @property
    def scopes(self) -> frozenset[str]: ...

    @property
    def interactive(self) -> bool: ...

    @property
    def site_id(self) -> str: ...


class Clock(Protocol):
    """Injected deterministic time source; the facade never sleeps."""

    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class IntentRepository(Protocol):
    async def add(self, intent: Any) -> None: ...

    async def active(self, now_mono: float) -> tuple[Any, ...]: ...

    async def remove(self, intent_id: str) -> None: ...


class ObservationRepository(Protocol):
    async def latest(self, unit_id: str) -> Any | None: ...

    async def all_latest(self) -> dict[str, Any]: ...


class AuthorizationRepository(Protocol):
    async def peek(self, unit_id: str) -> Any | None: ...

    async def revoke(self, **kwargs: Any) -> None: ...


class AuditRepository(Protocol):
    async def append(self, event: Any) -> None: ...

    async def recent(self, limit: int, after_sequence: int | None = None) -> tuple[Any, ...]: ...


class EventPublisher(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...

    def snapshot_sequence(self) -> int: ...


class GenerationCoordinator(Protocol):
    async def snapshot(self) -> Any: ...

    async def advance(self, *, reason: str) -> Any: ...


class RecoveryView(Protocol):
    """The recovery monitor's derived per-unit health projection (R4).

    ``energypod.application.recovery.RecoveryMonitor`` is the composed
    implementation; the facade only ever PROJECTS from it (the monitor owns
    the classification, the transitions, and the ``unit.health_changed``
    publications).  An unavailable projection degrades to explicit nulls --
    detection must never gate a read.
    """

    async def unit_health_states(self) -> Mapping[str, Any]: ...


class ActorHandle(Protocol):
    """Per-unit control handle owned by the actor (or its composition wrapper).

    ``qualified`` and ``inhibit_latched`` are read defensively through
    ``getattr`` because a handle that cannot report them is itself a reason to
    refuse: an unknown state is never treated as permission.  ``fence`` is
    read the same way: a handle that cannot fence cannot cancel an in-flight
    nonzero write, so an emergency stop reports the unit as degraded instead
    of assuming the cancellation happened.
    """

    @property
    def unit_id(self) -> str: ...

    @property
    def lifecycle(self) -> Any: ...

    async def arm(self, *, takeover_acknowledged: bool = False) -> None: ...

    async def disarm(self) -> None: ...

    async def acknowledge_inhibit(self) -> None: ...

    async def request_bounded_zero(self, reason: str) -> None: ...

    # B5 (SYNC_RESILIENCE_AUDIT): the bounded fresh mode-word read the
    # dispatch refusal path performs before denying on a cached non-Remote
    # word.  Read defensively through ``getattr`` -- a handle without the
    # port fails its refresh reads and the two-failure policy refuses.
    async def refresh_mode_words(self) -> tuple[int, int]: ...


@dataclass(frozen=True)
class _LatchedStop:
    stop_id: str
    unit_ids: frozenset[str]
    created_at_mono: float
    fenced_generation: int | None
    # Snapshot truth for consoles (2026-08-23): a latch observed only through
    # events leaves a console opened after the latch showing nothing.
    principal: str = ""
    latched_at_wall: datetime = datetime(1970, 1, 1, tzinfo=UTC)
    reason_codes: tuple[str, ...] = ("latched",)


def _enum_value(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else str(raw)


def _fingerprint(facts: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(facts), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_units(unit_ids: Any) -> list[str]:
    if isinstance(unit_ids, str | bytes) or not isinstance(unit_ids, Sequence):
        raise TypeError("unit_ids must be a sequence of unit identifiers")
    units = list(unit_ids)
    if not units:
        raise ValueError("at least one unit must be selected")
    for unit_id in units:
        if not isinstance(unit_id, str) or _ID_PATTERN.fullmatch(unit_id) is None:
            raise ValueError("unit identifiers must be canonical")
    if len(set(units)) != len(units):
        raise ValueError("unit identifiers must be unique")
    return units


def _dispatch_direction(direction: Any) -> Direction:
    if isinstance(direction, Direction):
        resolved = direction
    elif isinstance(direction, str):
        try:
            resolved = Direction(direction)
        except ValueError as error:
            raise ValueError("unknown dispatch direction") from error
    else:
        raise TypeError("direction must be charge or discharge")
    if resolved is Direction.IDLE:
        raise ValueError("dispatch accepts charge or discharge only")
    return resolved


def _positive_watts(watts: Any) -> int:
    if isinstance(watts, bool) or type(watts) is not int:
        raise TypeError("watts must be an integer")
    if watts <= 0:
        raise ValueError("watts must be positive")
    return int(watts)


def _per_unit_watts(watts_by_unit: Any, units: Sequence[str]) -> dict[str, int] | None:
    """Validate an optional per-unit watt-target map against the selection.

    The 2026-08-23 operator ruling makes per-unit the primary mental model:
    each value is THAT battery's own request.  The keys must match the
    selected units exactly and every value is a positive integer.  ``None``
    means the dispatch used the scalar fleet-total form.  A target above a
    unit's static cap is not an error here: the policy's headroom bounds the
    allocation exactly as it bounds a scalar request today (clamped, with the
    shortfall reported unallocated), never reversed and never fabricated.
    """
    if watts_by_unit is None:
        return None
    if isinstance(watts_by_unit, str | bytes) or not isinstance(watts_by_unit, Mapping):
        raise TypeError("watts_by_unit must be a mapping of unit identifiers to watts")
    values = dict(watts_by_unit)
    if not values:
        raise ValueError("watts_by_unit must name every selected unit")
    if set(values) != set(units):
        raise ValueError("watts_by_unit keys must match the selected units exactly")
    for unit_id, watts in values.items():
        if not isinstance(unit_id, str):
            raise TypeError("watts_by_unit keys must be unit identifiers")
        if isinstance(watts, bool) or type(watts) is not int:
            raise TypeError("per-unit watts must be integers")
        if watts <= 0:
            raise ValueError("per-unit watts must be positive")
    return values


def _dispatch_watts(
    watts: Any, watts_by_unit: Any, units: Sequence[str]
) -> tuple[int, dict[str, int] | None]:
    """Resolve the exactly-one watt form (scalar fleet total or per-unit map)."""
    resolved_per_unit = _per_unit_watts(watts_by_unit, units)
    if resolved_per_unit is None:
        return _positive_watts(watts), None
    if watts is not None:
        raise ValueError("send watts or watts_by_unit, never both")
    return sum(resolved_per_unit.values()), resolved_per_unit


def _positive_duration(ttl_s: Any) -> float:
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int | float):
        raise TypeError("ttl_s must be a number")
    if not math.isfinite(float(ttl_s)) or ttl_s <= 0:
        raise ValueError("ttl_s must be finite and positive")
    return float(ttl_s)


def _reason_text(reason: Any, *, required: bool) -> str | None:
    if reason is None:
        if required:
            raise ValueError("a reason is required")
        return None
    if not isinstance(reason, str):
        raise TypeError("reason must be a string")
    if (
        not reason
        or reason != reason.strip()
        or len(reason) > _MAX_REASON_LENGTH
        or any(character in reason for character in "\r\n")
    ):
        raise ValueError("reason must be canonical and at most 500 characters")
    return reason


def _correlation_key(value: Any, name: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical identifier")
    return value


def _attach_stop_id(error: BaseException, stop_id: str) -> None:
    """Carry a latched stop's id on an error so it stays correlateable.

    ``emergency_stop`` completes its safety sequence before any caller-facing
    error surfaces; the guarded boundary must be able to register the id for
    a later acknowledgement.  Slot-bound exceptions cannot carry the
    attribute; the id is then simply unavailable to the caller, never
    fabricated.
    """
    with contextlib.suppress(AttributeError, TypeError):
        error.stop_id = stop_id  # type: ignore[attr-defined]


def _requested_power(unit_id: str, intents: Sequence[Any]) -> dict[str, Any]:
    selected = [
        intent for intent in intents if unit_id in getattr(intent, "selected_unit_ids", frozenset())
    ]
    if not selected:
        return {"direction": "idle", "watts": 0}
    newest = max(selected, key=lambda intent: (intent.acceptance_revision, intent.id))
    return {"direction": _enum_value(newest.direction), "watts": int(newest.watts)}


def _scalar_unit_shares(total_watts: int, units: Sequence[str]) -> dict[str, int]:
    """Exact integer shares of one scalar fleet total over its surviving scope.

    A scalar intent names no per-unit attribution, so the snapshot projects its
    total as the headroom-blind request-time share: largest-remainder over the
    sorted surviving scope, ties by unit id, summing exactly to the intent's
    own total.  The decision's capacity-weighted split stays the authorized
    map's job (the freshest decision row); the two are deliberately different
    projections of the same request.
    """
    ordered = sorted(units)
    if not ordered:
        return {}
    base, remainder = divmod(int(total_watts), len(ordered))
    return {
        unit_id: base + (1 if index < remainder else 0) for index, unit_id in enumerate(ordered)
    }


def _authorized_projection(capability: Any) -> dict[str, Any] | None:
    if capability is None:
        return None
    return {"direction": _enum_value(capability.direction), "watts": int(capability.watts)}


def _optional_float(raw: Any) -> float | None:
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    return None


def _optional_int(raw: Any) -> int | None:
    if type(raw) is int:
        return raw
    return None


def _optional_text(raw: Any) -> str | None:
    return raw if isinstance(raw, str) else None


def _optional_codes(raw: Any) -> list[str] | None:
    """Sorted code list for a present fault/warning set; null when absent."""
    if isinstance(raw, frozenset | set | tuple | list):
        return sorted(str(code) for code in raw)
    return None


def _numeric_series(raw: Any) -> tuple[float, ...] | None:
    """Numeric view of a cell/temperature array; empty arrays project null."""
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return None
    values = tuple(
        float(item) for item in raw if isinstance(item, int | float) and not isinstance(item, bool)
    )
    return values or None


def _telemetry_summary(observation: Any) -> dict[str, Any] | None:
    """Nullable summary projection of one latest observation.

    ``None`` means the unit has no observation at all.  Inside the block every
    field is null when that datum is absent from the observation, never
    zero-filled: an absent cell array is not a 0 V pack.
    """
    if observation is None:
        return None
    cells = _numeric_series(getattr(observation, "cell_voltages_v", None))
    cell_min = min(cells, default=None) if cells is not None else None
    cell_max = max(cells, default=None) if cells is not None else None
    spread_v = None if cell_min is None or cell_max is None else cell_max - cell_min
    temperatures = _numeric_series(getattr(observation, "temperatures_c", None))
    return {
        "soc_pct": _optional_float(getattr(observation, "system_soc_pct", None)),
        "bms_soc_pct": _optional_float(getattr(observation, "bms_soc_pct", None)),
        "soh_pct": _optional_float(getattr(observation, "soh_pct", None)),
        "pack_voltage_v": _optional_float(getattr(observation, "pack_voltage_v", None)),
        "pack_current_a": _optional_float(getattr(observation, "pack_current_a", None)),
        "battery_watts": _optional_float(getattr(observation, "battery_watts", None)),
        "dynamic_charge_limit_w": _optional_float(
            getattr(observation, "dynamic_charge_limit_w", None)
        ),
        "dynamic_discharge_limit_w": _optional_float(
            getattr(observation, "dynamic_discharge_limit_w", None)
        ),
        "cell_count": len(cells) if cells is not None else None,
        "cell_min_v": cell_min,
        "cell_max_v": cell_max,
        "cell_spread_mv": None if spread_v is None else spread_v * 1000.0,
        "temperature_min_c": (
            min(temperatures, default=None) if temperatures is not None else None
        ),
        "temperature_max_c": (
            max(temperatures, default=None) if temperatures is not None else None
        ),
        "active_faults": _optional_codes(getattr(observation, "active_faults", None)),
        "active_warnings": _optional_codes(getattr(observation, "active_warnings", None)),
        # Advisory per-pod CT power, readthrough-style: null when the poll did
        # not serve the PCS live block, never zero-filled or fabricated.
        "grid_power_w": _optional_float(getattr(observation, "grid_power_w", None)),
        "load_power_w": _optional_float(getattr(observation, "load_power_w", None)),
        # Advisory device-mode words (2026-08-23 incident 1), same doctrine:
        # the raw registers read through, null when the poll served no such
        # block.  They are evidence for the console and the dispatch gate,
        # never a quality judgment.
        "debug_mode_w": _optional_int(getattr(observation, "debug_mode_w", None)),
        "ctrl_mode_w": _optional_int(getattr(observation, "ctrl_mode_w", None)),
        "work_mode_w": _optional_int(getattr(observation, "work_mode_w", None)),
        "run_mode_w": _optional_int(getattr(observation, "run_mode_w", None)),
    }


def _health_projection(
    states: Mapping[str, Any] | None, unit_id: str, *, summary_field: str
) -> dict[str, Any]:
    """Project one unit's derived recovery view; nulls when it is absent.

    ``summary_field`` names the reasons key on the target view (``health_
    reasons`` beside the snapshot's other per-unit fields, ``reasons`` inside
    the health view's recovery-only unit block).  An absent or unusable
    projection is explicit nulls, never a fabricated state.
    """
    view = states.get(unit_id) if states is not None else None
    if view is None:
        return {"health_state": None, summary_field: None, "remediation_hint": None}
    state = getattr(view, "state", None)
    reasons = getattr(view, "reasons", None)
    hint = getattr(view, "remediation_hint", None)
    return {
        "health_state": None if state is None else _enum_value(state),
        summary_field: (None if reasons is None else [str(reason) for reason in reasons]),
        "remediation_hint": hint if isinstance(hint, str) else None,
    }


def _unit_projection(unit_id: str, observation: Any) -> dict[str, Any]:
    """Full single-unit projection served by ``unit_detail``.

    A commissioned unit that has not published an observation yet is known but
    silent: the projection exists and every datum is null, never fabricated.
    """
    cells = _numeric_series(getattr(observation, "cell_voltages_v", None))
    temperatures = _numeric_series(getattr(observation, "temperatures_c", None))
    wall = getattr(observation, "wall_timestamp", None)
    lifecycle = getattr(observation, "lifecycle", None)
    quality = getattr(observation, "quality", None)
    return {
        "unit_id": unit_id,
        "device_identity": _optional_text(getattr(observation, "device_identity", None)),
        "protocol_profile": _optional_text(getattr(observation, "protocol_profile", None)),
        "connection_epoch": _optional_int(getattr(observation, "connection_epoch", None)),
        "lifecycle": None if lifecycle is None else _enum_value(lifecycle),
        "sequence": _optional_int(getattr(observation, "sequence", None)),
        "captured_at_mono": _optional_float(getattr(observation, "captured_at_mono", None)),
        "cell_sequence": _optional_int(getattr(observation, "cell_sequence", None)),
        "cell_captured_at_mono": _optional_float(
            getattr(observation, "cell_captured_at_mono", None)
        ),
        "wall_timestamp": wall.isoformat() if isinstance(wall, datetime) else None,
        **(_telemetry_summary(observation) or dict.fromkeys(_TELEMETRY_SUMMARY_FIELDS)),
        "cell_voltages_v": list(cells) if cells is not None else None,
        "temperatures_c": list(temperatures) if temperatures is not None else None,
        "quality": (
            {str(field): _enum_value(value) for field, value in sorted(quality.items())}
            if isinstance(quality, Mapping)
            else None
        ),
    }


class EnergyServiceFacade:
    """Fleet-level application service behind the guarded REST and MCP adapters."""

    def __init__(
        self,
        *,
        site_id: str,
        clock: Clock,
        intents: IntentRepository,
        observations: ObservationRepository,
        authorizations: AuthorizationRepository,
        audit: AuditRepository,
        events: EventPublisher,
        coordinator: GenerationCoordinator,
        actors: Mapping[str, ActorHandle],
        recovery: RecoveryView | None = None,
    ) -> None:
        if not isinstance(site_id, str) or _ID_PATTERN.fullmatch(site_id) is None:
            raise ValueError("site_id must be a canonical identifier")
        handles = dict(actors)
        if any(
            not isinstance(unit_id, str) or _ID_PATTERN.fullmatch(unit_id) is None
            for unit_id in handles
        ):
            raise ValueError("actor handles must be keyed by canonical unit identifiers")
        self._site_id = site_id
        self._clock = clock
        self._intents = intents
        self._observations = observations
        self._authorizations = authorizations
        self._audit = audit
        self._events = events
        self._coordinator = coordinator
        self._actors = handles
        self._recovery = recovery
        self._revision = 0
        self._advisory_correlations = itertools.count(1)
        self._latched_stops: dict[str, _LatchedStop] = {}
        self._acknowledged_stops: set[str] = set()
        self._process_instance_id = f"facade-{uuid.uuid4().hex}"
        self._process_origin_mono = float(clock.monotonic())

    # --- read views ---------------------------------------------------------

    async def snapshot(self, *, principal: Principal) -> dict[str, Any]:
        """Assemble one immutable fleet view from repository reads only."""
        self._admit(principal, "observe")
        now_mono = float(self._clock.monotonic())
        sequence = self._events.snapshot_sequence()
        latest = await self._observations.all_latest()
        active = await self._intents.active(now_mono)
        recovery_states = await self._recovery_states()
        units: list[dict[str, Any]] = []
        for unit_id, handle in self._actors.items():
            telemetry = latest.get(unit_id) if isinstance(latest, Mapping) else None
            units.append(
                await self._unit_view(unit_id, handle, telemetry, active, now_mono, recovery_states)
            )
        return {
            "site_id": self._site_id,
            "snapshot_sequence": sequence,
            "captured_at": self._clock.wall_now().isoformat(),
            "units": units,
            # Per-unit intent figures (2026-08-24 cold-load fix): null when no
            # live intent claims any unit.
            "intent": await self._intent_projection(active, now_mono),
            # Console truth (2026-08-23): a latched emergency stop must be
            # visible in a snapshot taken after the latch event, not only on
            # the event stream.  Only non-acknowledged latches appear -- an
            # acknowledged stop leaves the list, exactly as it leaves the
            # registry -- and a null unit_ids means the stop fenced the whole
            # fleet this facade serves.
            "active_stops": [
                {
                    "stop_id": stop.stop_id,
                    "latched_at": stop.latched_at_wall.isoformat(),
                    "principal": stop.principal,
                    "reason_codes": list(stop.reason_codes),
                    "unit_ids": (
                        None if self._is_fleet_wide(stop.unit_ids) else sorted(stop.unit_ids)
                    ),
                }
                for stop in self._latched_stops.values()
            ],
        }

    async def unit_detail(self, *, principal: Principal, unit_id: Any) -> dict[str, Any]:
        """Project one unit's latest observation; a read-only repository view.

        Unknown unit ids are refused.  A known unit that has not published an
        observation yet projects nulls for every datum, never zero-filled or
        fabricated values.  This performs no I/O beyond the one repository
        read and never triggers control.
        """
        self._admit(principal, "observe")
        canonical_unit = _correlation_key(unit_id, "unit_id")
        if canonical_unit not in self._actors:
            raise LookupError(f"no unit with id {canonical_unit!r}")
        observation = await self._observations.latest(canonical_unit)
        return _unit_projection(canonical_unit, observation)

    async def health(self, *, principal: Principal) -> dict[str, Any]:
        """Separate process liveness, dependency readiness, and control readiness."""
        self._admit(principal, "observe")
        now_mono = float(self._clock.monotonic())
        service_reasons: list[str] = []
        await self._probe(
            service_reasons, "intent_repository_unavailable", lambda: self._intents.active(now_mono)
        )
        await self._probe(
            service_reasons,
            "observation_repository_unavailable",
            lambda: self._observations.all_latest(),
        )
        probe_unit = next(iter(self._actors), "")
        if probe_unit:
            await self._probe(
                service_reasons,
                "authorization_repository_unavailable",
                lambda: self._authorizations.peek(probe_unit),
            )
        await self._probe(
            service_reasons, "audit_repository_unavailable", lambda: self._audit.recent(limit=1)
        )
        await self._probe(
            service_reasons,
            "authority_coordinator_unavailable",
            lambda: self._coordinator.snapshot(),
        )
        recovery_states = await self._recovery_states()
        control_reasons = await self._control_readiness_reasons(recovery_states)
        return {
            "liveness": {
                "ok": True,
                # Identity of THIS process (2026-08-23 false-stall lesson): a
                # console watching these can distinguish a deployment — the
                # instance id changes — from a genuine stall, where the id is
                # stable while data ages climb.
                "process_instance_id": self._process_instance_id,
                "uptime_s": round(now_mono - self._process_origin_mono, 3),
            },
            "service_readiness": {"ready": not service_reasons, "reasons": service_reasons},
            "control_readiness": {"ready": not control_reasons, "reasons": control_reasons},
            # The self-healing awareness layer's derived per-unit recovery
            # view (R4): informational by design, with the R5 honest-terminal
            # remediation hint riding next to the state it belongs to.
            "units": [
                {
                    "unit_id": unit_id,
                    **_health_projection(recovery_states, unit_id, summary_field="reasons"),
                }
                for unit_id in self._actors
            ],
        }

    async def recent_audit(
        self, *, principal: Principal, limit: int, cursor: int | None = None
    ) -> dict[str, Any]:
        """Bounded, newest-first audit read with a stable pagination cursor."""
        self._admit(principal, "observe")
        if isinstance(limit, bool) or type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        if cursor is not None and (
            isinstance(cursor, bool) or type(cursor) is not int or cursor < 0
        ):
            raise ValueError("cursor must be a non-negative integer")
        # The cursor is the audit port's own ordering key: it is passed
        # through as ``after_sequence`` and the next cursor is derived from
        # what the store returned, never from facade-side bookkeeping.
        events = list(await self._audit.recent(limit=limit, after_sequence=cursor))
        # The cursor names the oldest delivered event so pagination resumes
        # with strictly older facts; a short terminal page leaves no cursor.
        next_cursor = None
        if len(events) == limit:
            oldest = getattr(events[-1], "sequence", None)
            if type(oldest) is int:
                next_cursor = oldest
        return {"events": events, "next_cursor": next_cursor}

    # --- mutations ----------------------------------------------------------

    async def submit_intent(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        reason: Any = None,
        principal: Principal,
        idempotency_key: Any,
        request_id: Any,
        watts_by_unit: Any = None,
    ) -> dict[str, Any]:
        """Accept one intent with a server-assigned revision; grant nothing.

        Acceptance is atomic with its audit and publication: if either fails,
        the stored intent is rolled back and the error surfaces, so power can
        never flow from a dispatch the caller saw fail.

        The watt form is exactly one of: scalar ``watts`` (the fleet total) or
        ``watts_by_unit`` (one target per selected unit, from which the fleet
        total is derived as the sum).  The per-unit breakdown travels onto the
        stored intent, the audit fact set, the published event, and the
        acceptance view.
        """
        self._admit(principal, "dispatch")
        units = _validated_units(unit_ids)
        unknown = [unit_id for unit_id in units if unit_id not in self._actors]
        if unknown:
            raise ValueError(f"unknown units requested: {unknown}")
        await self._refuse_undispatchable_modes(units)
        resolved_direction = _dispatch_direction(direction)
        resolved_watts, resolved_per_unit = _dispatch_watts(watts, watts_by_unit, units)
        duration_s = _positive_duration(ttl_s)
        _reason_text(reason, required=False)
        _correlation_key(idempotency_key, "idempotency_key")
        request = _correlation_key(request_id, "request_id")

        now_mono = float(self._clock.monotonic())
        revision = self._next_revision()
        intent_id = f"intent-{revision}-{now_mono:.6f}"
        intent = PowerIntent(
            id=intent_id,
            source=IntentSource.MANUAL,
            selected_unit_ids=frozenset(units),
            direction=resolved_direction,
            watts=resolved_watts,
            watts_by_unit=resolved_per_unit,
            duration_s=duration_s,
            accepted_at_mono=now_mono,
            acceptance_revision=revision,
            actor_identity=principal.subject,
        )
        await self._intents.add(intent)
        try:
            await self._append_audit(
                self._mutation_audit(
                    event_type="intent_accepted",
                    subject=principal.subject,
                    result="accepted",
                    request_id=request,
                    source=IntentSource.MANUAL,
                    intent_id=intent_id,
                    reason_codes=("accepted",),
                    # Acceptance alone moves no unit; authority comes only from a
                    # kernel tick, so the fleet stays in its non-active state.
                    lifecycle=UnitLifecycle.DISARMED,
                    payload={
                        "direction": resolved_direction.value,
                        "unit_ids": sorted(units),
                        "watts": resolved_watts,
                        **(
                            {"watts_by_unit": dict(sorted(resolved_per_unit.items()))}
                            if resolved_per_unit is not None
                            else {}
                        ),
                    },
                )
            )
            await self._publish(
                "intent.accepted",
                {
                    "principal": principal.subject,
                    "intent_id": intent_id,
                    "direction": resolved_direction.value,
                    "watts": resolved_watts,
                    **(
                        {"watts_by_unit": dict(sorted(resolved_per_unit.items()))}
                        if resolved_per_unit is not None
                        else {}
                    ),
                    "unit_ids": sorted(units),
                },
            )
        except Exception:
            # Acceptance is atomic with its audit and publication: a dispatch
            # the caller saw fail must leave nothing stored for the kernel to
            # arbitrate on the next tick.  A rollback failure cannot be
            # allowed to mask the original error; the stored intent is then
            # the residual risk the operator still has to see reported.
            with contextlib.suppress(Exception):
                await self._intents.remove(intent_id)
            raise
        return {
            "intent_id": intent_id,
            "acceptance_revision": revision,
            "accepted_at_monotonic": now_mono,
            "status": "accepted",
            "requested": {
                "direction": resolved_direction.value,
                "watts": resolved_watts,
                **(
                    {"watts_by_unit": dict(sorted(resolved_per_unit.items()))}
                    if resolved_per_unit is not None
                    else {}
                ),
            },
            "authorized": None,
            "measured": None,
            "expires_in_s": duration_s,
        }

    async def submit_advisory_intent(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        reason: Any = None,
        principal: Principal,
        idempotency_key: Any = None,
        request_id: Any = None,
    ) -> dict[str, Any]:
        """Accept one internal OPTIMIZER intent; the advisory twin of submit_intent.

        API_CONTRACTS "Excess-solar accelerated charging (advisory)": same
        validation, audit event type, idempotency/correlation contract, and
        publication as ``submit_intent``, with the mintage source pinned to
        ``OPTIMIZER`` and its own intent-id prefix — so audit attribution
        separates the automation principal plus ``optimizer`` tag from every
        console (``manual``) or agent traffic.  This method is composition-
        only wiring: it is never routed on REST or MCP, and only the composed
        ``energypod:excess-adviser`` principal ever reaches it.  Absent
        caller identifiers get deterministic facade-owned ones so a direct
        internal drive still travels the correlated, audited path.
        """
        self._admit(principal, "dispatch")
        units = _validated_units(unit_ids)
        unknown = [unit_id for unit_id in units if unit_id not in self._actors]
        if unknown:
            raise ValueError(f"unknown units requested: {unknown}")
        await self._refuse_undispatchable_modes(units)
        resolved_direction = _dispatch_direction(direction)
        resolved_watts = _positive_watts(watts)
        duration_s = _positive_duration(ttl_s)
        _reason_text(reason, required=False)
        resolved_idempotency = (
            self._advisory_key("idempotency") if idempotency_key is None else idempotency_key
        )
        _correlation_key(resolved_idempotency, "idempotency_key")
        request_source = self._advisory_key("request") if request_id is None else request_id
        request = _correlation_key(request_source, "request_id")

        now_mono = float(self._clock.monotonic())
        revision = self._next_revision()
        intent_id = f"excess-{revision}-{now_mono:.6f}"
        intent = PowerIntent(
            id=intent_id,
            source=IntentSource.OPTIMIZER,
            selected_unit_ids=frozenset(units),
            direction=resolved_direction,
            watts=resolved_watts,
            duration_s=duration_s,
            accepted_at_mono=now_mono,
            acceptance_revision=revision,
            actor_identity=principal.subject,
        )
        await self._intents.add(intent)
        try:
            await self._append_audit(
                self._mutation_audit(
                    event_type="intent_accepted",
                    subject=principal.subject,
                    result="accepted",
                    request_id=request,
                    source=IntentSource.OPTIMIZER,
                    intent_id=intent_id,
                    reason_codes=("accepted",),
                    lifecycle=UnitLifecycle.DISARMED,
                    payload={
                        "direction": resolved_direction.value,
                        "unit_ids": sorted(units),
                        "watts": resolved_watts,
                    },
                )
            )
            await self._publish(
                "intent.accepted",
                {
                    "principal": principal.subject,
                    "intent_id": intent_id,
                    "direction": resolved_direction.value,
                    "watts": resolved_watts,
                    "unit_ids": sorted(units),
                },
            )
        except Exception:
            # Atomic with its audit and publication exactly like submit_intent:
            # an advisory drive that failed here must leave nothing stored.
            with contextlib.suppress(Exception):
                await self._intents.remove(intent_id)
            raise
        return {
            "intent_id": intent_id,
            "acceptance_revision": revision,
            "accepted_at_monotonic": now_mono,
            "status": "accepted",
            "requested": {
                "direction": resolved_direction.value,
                "watts": resolved_watts,
            },
            "authorized": None,
            "measured": None,
            "expires_in_s": duration_s,
        }

    async def cancel_intent(
        self,
        *,
        intent_id: Any,
        principal: Principal,
        idempotency_key: Any,
        request_id: Any,
    ) -> dict[str, Any]:
        """Cancel the active intent by exact id or ``"current"``.

        Stopping power is safety-positive, so this shares the dispatch scope
        with no interactive requirement: the intent is removed from the
        repository, the kernel's next tick finds no winner and revokes, and
        the device watchdog hands power back.  A latched emergency stop is
        not an intent here -- it leaves only through its privileged
        acknowledgement -- and every refusal is loud, never a silent no-op.
        """
        self._admit(principal, "dispatch")
        _correlation_key(idempotency_key, "idempotency_key")
        request = _correlation_key(request_id, "request_id")
        if (
            not isinstance(intent_id, str)
            or not intent_id
            or intent_id != intent_id.strip()
            or len(intent_id) > 128
        ):
            raise ValueError("intent_id must be a canonical identifier or 'current'")
        now_mono = float(self._clock.monotonic())
        active = await self._intents.active(now_mono)
        if intent_id == "current":
            candidates = [
                intent
                for intent in active
                if _enum_value(getattr(intent, "source", None)) != "emergency_stop"
            ]
            if not candidates:
                raise ValueError("no active intent to cancel")
            target = max(
                candidates,
                key=lambda intent: (
                    getattr(intent, "acceptance_revision", 0),
                    getattr(intent, "accepted_at_mono", 0.0),
                ),
            )
        else:
            target = next(
                (intent for intent in active if getattr(intent, "id", None) == intent_id), None
            )
            if target is None:
                raise LookupError(f"no active intent with id {intent_id!r}")
            if _enum_value(getattr(target, "source", None)) == "emergency_stop":
                raise ValueError("an emergency stop is released by acknowledgement, not cancelled")
        selected = sorted(getattr(target, "selected_unit_ids", ()) or ())
        await self._intents.remove(target.id)
        await self._append_audit(
            self._mutation_audit(
                event_type="intent_cancelled",
                subject=principal.subject,
                result="cancelled",
                request_id=request,
                source=getattr(target, "source", None),
                intent_id=target.id,
                reason_codes=("cancelled",),
                lifecycle=self._handle_lifecycle(selected[0])
                if selected
                else UnitLifecycle.DISARMED,
                payload={"intent_id": target.id, "unit_ids": selected},
            )
        )
        await self._publish(
            "intent.cancelled",
            {"principal": principal.subject, "intent_id": target.id, "unit_ids": selected},
        )
        return {"intent_id": target.id, "status": "cancelled", "unit_ids": selected}

    async def arm(
        self,
        *,
        unit_ids: Any,
        principal: Principal,
        idempotency_key: Any,
        request_id: Any,
        takeover: Any = None,
    ) -> dict[str, Any]:
        """Arm exactly the requested qualified, disarmed units; report each outcome.

        ADD-1 layer (b) (2026-08-24 live blocker): ``takeover="ACKNOWLEDGE"``
        is the operator's explicit acknowledgement that arming may REPLACE a
        served PQ objective outside the pod-autonomy signature band — a
        deliberate, audited takeover of another writer.  Any other value is
        refused before any unit is touched; the acknowledgement is per-request
        and never persisted (boot stays observe-only, no provenance across
        restarts).
        """
        self._admit(principal, "arm", interactive=True)
        units = _validated_units(unit_ids)
        _correlation_key(idempotency_key, "idempotency_key")
        request = _correlation_key(request_id, "request_id")
        if takeover not in (None, "ACKNOWLEDGE"):
            raise ValueError("takeover acknowledgement must be the literal 'ACKNOWLEDGE'")
        takeover_acknowledged = takeover == "ACKNOWLEDGE"
        outcomes: list[dict[str, str]] = []
        for unit_id in units:
            outcome = await self._arm_one(unit_id, takeover_acknowledged=takeover_acknowledged)
            outcomes.append(outcome)
            # ADD-1: the preflight classification is a first-class audit fact
            # -- an acknowledged takeover or a pod-autonomy arm must be
            # visible on the durable row's reason codes, not only its
            # fingerprint.
            classification = outcome.get("objective_classification")
            classification_codes = (
                (f"arm_{classification}",) if isinstance(classification, str) else ()
            )
            await self._append_audit(
                self._mutation_audit(
                    event_type="unit_armed",
                    subject=principal.subject,
                    result=outcome["status"],
                    request_id=request,
                    reason_codes=(outcome["reason"], *classification_codes),
                    unit_id=unit_id,
                    lifecycle=self._handle_lifecycle(unit_id),
                    payload=dict(outcome),
                )
            )
        await self._publish("unit.armed", {"principal": principal.subject, "units": outcomes})
        return {"units": outcomes}

    async def disarm(
        self,
        *,
        unit_ids: Any,
        principal: Principal,
        idempotency_key: Any,
        request_id: Any,
    ) -> dict[str, Any]:
        """Disarm the requested known units; partial refusal stays visible."""
        self._admit(principal, "arm")
        units = _validated_units(unit_ids)
        _correlation_key(idempotency_key, "idempotency_key")
        request = _correlation_key(request_id, "request_id")
        outcomes: list[dict[str, str]] = []
        for unit_id in units:
            outcome = await self._disarm_one(unit_id)
            outcomes.append(outcome)
            await self._append_audit(
                self._mutation_audit(
                    event_type="unit_disarmed",
                    subject=principal.subject,
                    result=outcome["status"],
                    request_id=request,
                    reason_codes=(outcome["reason"],),
                    unit_id=unit_id,
                    lifecycle=self._handle_lifecycle(unit_id),
                    payload=dict(outcome),
                )
            )
        await self._publish("unit.disarmed", {"principal": principal.subject, "units": outcomes})
        return {"units": outcomes}

    async def emergency_stop(
        self,
        *,
        unit_ids: Any,
        reason: Any,
        principal: Principal,
        idempotency_key: Any,
        request_id: Any,
    ) -> dict[str, Any]:
        """Latch one fleet stop: fence, revoke, fence actors, zero, audit, publish."""
        self._admit(principal, "stop")
        units = _validated_units(unit_ids)
        stop_reason = _reason_text(reason, required=True)
        if stop_reason is None:  # pragma: no cover - guarded by required=True
            raise ValueError("a reason is required")
        _correlation_key(idempotency_key, "idempotency_key")
        request = _correlation_key(request_id, "request_id")

        now_mono = float(self._clock.monotonic())
        revision = self._next_revision()
        stop_id = f"stop-{revision}-{now_mono:.6f}"
        selected = frozenset(units)
        intent = PowerIntent(
            id=stop_id,
            source=IntentSource.EMERGENCY_STOP,
            selected_unit_ids=selected,
            direction=Direction.IDLE,
            watts=0,
            duration_s=_LATCHED_STOP_DURATION_S,
            accepted_at_mono=now_mono,
            acceptance_revision=revision,
            actor_identity=principal.subject,
        )
        unknown = [unit_id for unit_id in units if unit_id not in self._actors]
        degraded: list[str] = []

        # 1. Fence first: no potentially blocking work may precede the fence.
        fenced_generation: int | None = None
        try:
            snapshot = await self._coordinator.advance(reason=f"emergency_stop:{stop_id}")
            epoch = getattr(snapshot, "epoch", None)
            if type(epoch) is int:
                fenced_generation = epoch
        except Exception:
            # The fence itself may already have landed; a lost acknowledgement
            # must never abandon the remaining stop work.
            degraded.append("fence_unconfirmed")
        # 2. Revoke every outstanding authorization: the fenced generation is
        # dead fleet-wide, so no capability may survive the stop.
        try:
            await self._authorizations.revoke(reason=f"emergency_stop:{stop_id}")
        except Exception:
            degraded.append("revocation_unconfirmed")
        # 3. Fence every affected actor before any potentially blocking store
        # work: the actor's fence cancels an in-flight nonzero heartbeat write
        # and publishes its own revocation, so a dispatched nonzero command
        # can never complete on the wire after this stop returns.  The bounded
        # zero alone cannot promise that; it only queues behind whatever the
        # actor is already doing.
        for unit_id in units:
            handle = self._actors.get(unit_id)
            if handle is None:
                continue
            fence = getattr(handle, "fence", None)
            if fence is None:
                # A handle that cannot fence cannot cancel in-flight authority
                # work; the stop stays visible as degraded, never assumed safe.
                degraded.append(f"actor_fence_unavailable:{unit_id}")
                continue
            try:
                await fence(f"emergency_stop:{stop_id}")
            except Exception:
                degraded.append(f"actor_fence_failed:{unit_id}")
        # 4. Store the latched intent, then record the latch here only once
        # the store holds it: a registry entry without its stored intent is a
        # phantom latch no acknowledgement could ever satisfy, so a degraded
        # store leaves nothing half-latched.  The caller sees the failure and
        # re-issues the stop; the safety work above has already landed.
        store_error: Exception | None = None
        try:
            await self._intents.add(intent)
        except Exception as error:
            store_error = error
            degraded.append("intent_store_unavailable")
        else:
            self._latched_stops[stop_id] = _LatchedStop(
                stop_id=stop_id,
                unit_ids=selected,
                created_at_mono=now_mono,
                fenced_generation=fenced_generation,
                principal=principal.subject,
                latched_at_wall=self._clock.wall_now().astimezone(UTC),
                reason_codes=("latched",),
            )
        # 5. Bounded zero through every affected actor handle.
        for unit_id in units:
            handle = self._actors.get(unit_id)
            if handle is None:
                degraded.append(f"unknown_unit:{unit_id}")
                continue
            try:
                await handle.request_bounded_zero(stop_reason)
            except Exception:
                degraded.append(f"bounded_zero_failed:{unit_id}")
        # 6/7. Audit and publish only after the safety work has landed.
        try:
            await self._append_audit(
                self._mutation_audit(
                    event_type="emergency_stop",
                    subject=principal.subject,
                    result="latched",
                    request_id=request,
                    source=IntentSource.EMERGENCY_STOP,
                    intent_id=stop_id,
                    reason_codes=tuple(degraded) if degraded else ("latched",),
                    # A latched stop inhibits the fleet, matching the control
                    # aggregate the kernel assigns to stop intents.
                    lifecycle=UnitLifecycle.INHIBITED,
                    generation=fenced_generation,
                    payload={
                        "degraded": list(degraded),
                        "reason": stop_reason,
                        "stop_id": stop_id,
                        "unit_ids": sorted(selected),
                    },
                )
            )
        except Exception:
            degraded.append("audit_unavailable")
        try:
            await self._publish(
                "emergency_stop.latched",
                {
                    "principal": principal.subject,
                    "stop_id": stop_id,
                    "unit_ids": sorted(selected),
                    "reason": stop_reason,
                    "generation": fenced_generation,
                    "degraded": list(degraded),
                },
            )
        except Exception:
            degraded.append("publish_unavailable")

        # The latch record the snapshot serves carries the FINAL degraded
        # state of the stop, exactly as its audit event does.
        latched = self._latched_stops.get(stop_id)
        if latched is not None:
            self._latched_stops[stop_id] = dataclass_replace(
                latched, reason_codes=tuple(degraded) if degraded else ("latched",)
            )

        # The safety sequence is complete; only now may caller-facing errors
        # surface, and never by undoing any step above.  Every error path
        # carries the stop id: the store-refused stop never latched (so the
        # operator correlates and re-issues it), while the unknown-unit stop
        # latched for the known units and stays acknowledgeable by id.
        if store_error is not None:
            _attach_stop_id(store_error, stop_id)
            raise store_error
        if unknown:
            unknown_error = ValueError(f"unknown units requested for emergency stop: {unknown}")
            _attach_stop_id(unknown_error, stop_id)
            raise unknown_error
        return {
            "stop_id": stop_id,
            "status": "latched",
            "unit_ids": sorted(selected),
            "fenced_generation": fenced_generation,
            "degraded": degraded,
        }

    async def acknowledge_emergency_stop(
        self,
        *,
        stop_id: Any,
        principal: Principal,
        idempotency_key: Any,
        request_id: Any,
    ) -> dict[str, Any]:
        """Remove exactly one latched stop so it cannot relatch."""
        self._admit(principal, "stop:acknowledge")
        _correlation_key(idempotency_key, "idempotency_key")
        request = _correlation_key(request_id, "request_id")
        if not isinstance(stop_id, str):
            raise LookupError("stop_id must be a string")
        record = self._latched_stops.get(stop_id)
        if record is None or stop_id in self._acknowledged_stops:
            raise LookupError(f"no latched emergency stop with id {stop_id!r}")
        if not self._is_fleet_wide(record.unit_ids):
            # A stop that fenced only part of the fleet leaves the remaining
            # units untouched, so its latch must still be confirmed live in
            # the intent repository before the acknowledgement consumes it.
            # A fleet-wide stop fenced every unit in one generation and is
            # authoritative in this facade's own registry.
            active = await self._intents.active(float(self._clock.monotonic()))
            live = any(
                getattr(intent, "id", None) == stop_id
                and _enum_value(getattr(intent, "source", None)) == "emergency_stop"
                for intent in active
            )
            if not live:
                raise LookupError(f"no latched emergency stop with id {stop_id!r}")
        # A fleet-wide registry latch outranks the stored copy: if the intent
        # was already removed out-of-band, the acknowledgement must still
        # clear the latch rather than leave a registry entry no retry could
        # ever satisfy.  Any other store failure propagates before the
        # registry is consumed, leaving the latch for a retried call.
        with contextlib.suppress(LookupError):
            await self._intents.remove(stop_id)
        self._latched_stops.pop(stop_id, None)
        self._acknowledged_stops.add(stop_id)
        await self._append_audit(
            self._mutation_audit(
                event_type="stop_acknowledged",
                subject=principal.subject,
                result="acknowledged",
                request_id=request,
                source=IntentSource.EMERGENCY_STOP,
                intent_id=stop_id,
                reason_codes=("acknowledged",),
                # The latch is gone; units re-qualify through stable samples
                # back to DISARMED, never directly to ACTIVE.
                lifecycle=UnitLifecycle.DISARMED,
                payload={"stop_id": stop_id, "unit_ids": sorted(record.unit_ids)},
            )
        )
        await self._publish(
            "emergency_stop.acknowledged",
            {"principal": principal.subject, "stop_id": stop_id},
        )
        return {"stop_id": stop_id, "status": "acknowledged"}

    async def acknowledge_inhibit(
        self,
        *,
        unit_id: Any,
        principal: Principal,
        idempotency_key: Any,
        request_id: Any,
    ) -> dict[str, Any]:
        """Clear exactly one unit's latched inhibit; never arm and never bypass recovery."""
        self._admit(principal, "arm", interactive=True)
        canonical_unit = _correlation_key(unit_id, "unit_id")
        _correlation_key(idempotency_key, "idempotency_key")
        request = _correlation_key(request_id, "request_id")
        handle = self._actors.get(canonical_unit)
        if handle is None:
            raise LookupError(f"no unit with id {canonical_unit!r}")
        latched = bool(getattr(handle, "inhibit_latched", False))
        latch_cleared = False
        if latched:
            await handle.acknowledge_inhibit()
            latch_cleared = True
        # A non-latched inhibit needs no acknowledgement: it recovers through
        # stable qualifying samples, and a repeated call stays a no-op success.
        await self._append_audit(
            self._mutation_audit(
                event_type="inhibit_acknowledged",
                subject=principal.subject,
                result="acknowledged",
                request_id=request,
                reason_codes=("latch_cleared",) if latch_cleared else ("not_latched",),
                unit_id=canonical_unit,
                # Acknowledgement only clears the latch; the unit still needs
                # stable samples to reach DISARMED, so report the unit as-is.
                lifecycle=self._handle_lifecycle(canonical_unit),
                payload={"latch_cleared": latch_cleared, "unit_id": canonical_unit},
            )
        )
        await self._publish(
            "inhibit.acknowledged",
            {
                "principal": principal.subject,
                "unit_id": canonical_unit,
                "latch_cleared": latch_cleared,
            },
        )
        return {
            "unit_id": canonical_unit,
            "status": "acknowledged",
            "latch_cleared": latch_cleared,
        }

    # --- internal helpers ---------------------------------------------------

    async def _refuse_undispatchable_modes(self, units: Sequence[str]) -> None:
        """Refuse dispatch onto units whose mode words say it would be ignored.

        Vendor precedent (MiniESapp.cs:2180-2184, 2026-08-23 incident 1): the
        pod ignores external PQ objectives while its debug-mode readback is
        nonzero, and only accepts them under Remote control (ctrlMode 1,
        GlobalFun.cs:204-211).  Decoded positive evidence refuses the intent
        with the explicit reason before anything is stored; ABSENT evidence
        (a read plan without the mode blocks, or a unit yet to publish)
        changes nothing -- the advisory doctrine, and the safety kernel's own
        staleness gates remain the backstop.

        SYNC_RESILIENCE_AUDIT B5 (2026-08-24): the debug-mode word rides the
        control-rate core, but ctrlMode rides the cold ring (~108 s), so a
        cached non-Remote word may be minutes stale.  The CACHED word alone
        never refuses: before denying, the path performs ONE bounded fresh
        read of the mode words through the owning actor (one retry on a
        failed read), and only a fresh-confirmed non-Remote -- or two failed
        refresh reads, genuinely-unreadable class D -- refuses.
        """
        debugging: list[str] = []
        local: list[str] = []
        for unit_id in units:
            observation = await self._latest_observation(unit_id)
            if getattr(observation, "debug_mode_active", None) is True:
                debugging.append(unit_id)
            elif getattr(observation, "ctrl_mode_remote", None) is False and (
                await self._fresh_confirmed_not_remote(unit_id)
            ):
                local.append(unit_id)
        if debugging:
            raise ValueError(f"device_debug_mode_active: {sorted(debugging)}")
        if local:
            raise ValueError(f"device_mode_not_remote: {sorted(local)}")

    async def _fresh_confirmed_not_remote(self, unit_id: str) -> bool:
        """B5: re-read the mode words once (bounded) before refusing on them.

        Returns whether the unit is CONFIRMED non-Remote on fresh evidence.
        A fresh ctrlMode 1 (Remote) clears the cached refusal -- the operator
        flipping the pod to Remote takes effect at the very next dispatch,
        not at the next cold-ring rotation.  Any other fresh value, or two
        failed refresh reads, confirms the refusal: the mode word is then
        either genuinely Local or genuinely unreadable (class D).
        """
        handle = self._actors.get(unit_id)
        refresh = getattr(handle, "refresh_mode_words", None)
        served: list[tuple[int, int]] = []
        failures: list[BaseException] = []
        for _ in range(2):
            try:
                if not callable(refresh):
                    raise RuntimeError(f"{unit_id}: no mode refresh port is wired")
                served.append(await refresh())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # One bounded retry; a second failure leaves ``served`` empty
                # and the refusal below stands (genuinely unreadable, class D).
                failures.append(error)
            else:
                break
        del failures
        if not served:
            return True
        return int(served[-1][0]) != 1

    async def _latest_observation(self, unit_id: str) -> Any | None:
        """The unit's latest observation, or ``None`` when it cannot be read.

        An unreadable observation never grants: the kernel's own evidence
        gates refuse the mint, so absence is not evidence here either.
        """
        try:
            return await self._observations.latest(unit_id)
        except Exception:
            return None

    def _admit(self, principal: Principal, scope: str, *, interactive: bool = False) -> None:
        """Reject malformed and cross-site principals before any port is touched."""
        subject = getattr(principal, "subject", None)
        site_id = getattr(principal, "site_id", None)
        scopes = getattr(principal, "scopes", None)
        is_interactive = getattr(principal, "interactive", None)
        if not isinstance(subject, str) or _ID_PATTERN.fullmatch(subject) is None:
            raise ValueError("principal subject must be a canonical identifier")
        if not isinstance(site_id, str) or _ID_PATTERN.fullmatch(site_id) is None:
            raise ValueError("principal site must be a canonical identifier")
        if type(scopes) is not frozenset or any(
            not isinstance(item, str) or _ID_PATTERN.fullmatch(item) is None for item in scopes
        ):
            raise TypeError("principal scopes must be a frozenset of canonical identifiers")
        if type(is_interactive) is not bool:
            raise TypeError("principal interactivity must be boolean")
        if site_id != self._site_id:
            raise PermissionError("principal belongs to another site")
        if scope not in scopes:
            raise PermissionError(f"principal lacks the required scope {scope!r}")
        if interactive and not is_interactive:
            raise PermissionError("an interactive operator principal is required")

    def _next_revision(self) -> int:
        revision = self._revision
        self._revision += 1
        return revision

    def _advisory_key(self, prefix: str) -> str:
        """Deterministic facade-owned correlation for an internal advisory drive."""
        return f"excess-{prefix}-{next(self._advisory_correlations):08d}"

    async def _unit_view(
        self,
        unit_id: str,
        handle: ActorHandle,
        telemetry: Any,
        active_intents: Sequence[Any],
        now_mono: float,
        recovery_states: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        age_s: float | None = None
        measured_watts: float | None = None
        if telemetry is not None:
            captured = getattr(telemetry, "captured_at_mono", None)
            if isinstance(captured, int | float) and not isinstance(captured, bool):
                age_s = max(0.0, float(now_mono - float(captured)))
            watts = getattr(telemetry, "battery_watts", None)
            if isinstance(watts, int | float) and not isinstance(watts, bool):
                measured_watts = float(watts)
        # peek() is the granted non-consuming projection read; current() is
        # reserved for control and a snapshot must never burn a capability.
        capability = await self._authorizations.peek(unit_id)
        inhibit_latched = bool(getattr(handle, "inhibit_latched", False))
        inhibit_cause = getattr(handle, "inhibit_cause", None)
        return {
            "unit_id": unit_id,
            "lifecycle": _enum_value(getattr(handle, "lifecycle", None)),
            "telemetry_age_s": age_s,
            "quality": self._quality_projection(telemetry, age_s),
            "requested_power": _requested_power(unit_id, active_intents),
            "authorized_power": _authorized_projection(capability),
            "measured_watts": measured_watts,
            "telemetry": _telemetry_summary(telemetry),
            # Inhibit truth for the console's latch affordance: the boolean is
            # the actor's live latch state; the cause names WHY it latched and
            # is null whenever the unit is not latched.
            "inhibit_latched": inhibit_latched,
            "inhibit_cause": _enum_value(inhibit_cause)
            if inhibit_latched and inhibit_cause is not None
            else None,
            # Self-healing awareness (R4): the derived recovery state, its
            # reasons, and -- only where a wedge is proven -- the honest
            # terminal remediation hint.
            **_health_projection(recovery_states, unit_id, summary_field="health_reasons"),
        }

    async def _intent_projection(
        self, active: Sequence[Any], now_mono: float
    ) -> dict[str, Any] | None:
        """The live request's per-unit figures, composed across every winner.

        Concurrent per-unit arbitration (2026-08-24) means several intents can
        hold different batteries at once, so the view is composed with the SAME
        per-unit winner-set arbitration the kernel's cycle uses -- a fresh,
        throwaway arbiter per read, since the facade only ever projects.  Each
        claimed unit's entry comes from ITS OWN winning intent: the requested
        figure is that unit's target when the winner carried per-unit targets,
        else its exact share of the winner's scalar total over its surviving
        scope; the direction is the winner's direction.  The authorized map
        mirrors the freshest control-decision row, restricted to the units a
        live intent still claims so an ended request's figures never linger.
        """
        selection = IntentArbiter().arbitrate(active, now_mono)
        requested: dict[str, int] = {}
        directions: dict[str, str] = {}
        for intent in selection.ranked:
            scope = sorted(selection.scopes.get(intent.id, frozenset()))
            targets = getattr(intent, "watts_by_unit", None)
            target_map = targets if isinstance(targets, Mapping) else None
            shares = (
                None
                if target_map is not None
                else _scalar_unit_shares(int(getattr(intent, "watts", 0)), scope)
            )
            for unit_id in scope:
                target = target_map.get(unit_id) if target_map is not None else None
                requested[unit_id] = (
                    int(target) if type(target) is int else int((shares or {}).get(unit_id, 0))
                )
                directions[unit_id] = _enum_value(intent.direction)
        if not requested:
            # No live intent claims any unit (an expired intent claims none):
            # the whole view is null, and the audit trail was never read.
            return None
        freshest = await self._latest_decision_authorized_watts()
        authorized = (
            None
            if freshest is None
            else (
                {unit_id: watts for unit_id, watts in freshest.items() if unit_id in requested}
                or None
            )
        )
        return {
            "requested_watts_by_unit": dict(sorted(requested.items())),
            "authorized_watts_by_unit": (
                None if authorized is None else dict(sorted(authorized.items()))
            ),
            "directions_by_unit": dict(sorted(directions.items())),
        }

    async def _latest_decision_authorized_watts(self) -> dict[str, int] | None:
        """The freshest control-decision row's per-unit authorized watts.

        One bounded newest-first audit read; the first ``control_decision`` row
        in the window decides, exactly as the kernel's own per-tick audit does.
        A row that minted no batch carries no map, and a window without a
        decision or an unreadable audit trail both mean the same thing here:
        no decision to mirror, so ``None`` -- never an older row dug out of
        history and never a fabricated figure.
        """
        try:
            events = await self._audit.recent(limit=_SNAPSHOT_DECISION_SCAN_LIMIT)
        except Exception:
            # A degraded audit read is the absence of a freshest decision, not
            # a snapshot failure: the requested targets still render.
            return None
        for event in events:
            if getattr(event, "event_type", None) != "control_decision":
                continue
            raw = getattr(event, "authorized_watts_by_unit", None)
            if not isinstance(raw, Mapping):
                return None
            return {
                str(unit_id): int(watts)
                for unit_id, watts in sorted(raw.items())
                if type(watts) is int
            }
        return None

    def _quality_projection(self, telemetry: Any, age_s: float | None) -> str:
        if telemetry is None:
            return "missing"
        quality = getattr(telemetry, "quality", None)
        if not isinstance(quality, Mapping):
            return "degraded"
        # Only the safety-critical fields decide the aggregate (see
        # _SAFETY_QUALITY_FIELDS): advisory fields are readthrough data whose
        # absence or badness never degrades an ordinary fleet view.
        values = [
            _enum_value(item) for field, item in quality.items() if field in _SAFETY_QUALITY_FIELDS
        ]
        if "bad" in values:
            return "bad"
        fresh = age_s is not None and age_s <= _SNAPSHOT_GOOD_TELEMETRY_MAX_AGE_S
        if fresh and values and all(value == "good" for value in values):
            return "good"
        return "degraded"

    async def _probe(
        self, reasons: list[str], code: str, action: Callable[[], Awaitable[Any]]
    ) -> None:
        try:
            await action()
        except Exception:
            # Cancellation is a BaseException and is never swallowed here.
            reasons.append(code)

    async def _recovery_states(self) -> Mapping[str, Any] | None:
        """The recovery monitor's derived view, or None when unavailable.

        A failing or unwired projection never fails a read: the health
        fields degrade to explicit nulls instead.
        """
        if self._recovery is None:
            return None
        try:
            return await self._recovery.unit_health_states()
        except Exception:
            return None

    async def _control_readiness_reasons(
        self, recovery_states: Mapping[str, Any] | None = None
    ) -> list[str]:
        reasons: list[str] = []
        any_armed = False
        for unit_id, handle in self._actors.items():
            lifecycle = _enum_value(getattr(handle, "lifecycle", None))
            # Unknown qualification is a reason, never an assumption.
            qualified = getattr(handle, "qualified", None)
            if bool(getattr(handle, "inhibit_latched", False)):
                reasons.append(f"{unit_id}:inhibit_latched")
            if lifecycle == "inhibited":
                reasons.append(f"{unit_id}:inhibited")
            # P1 vi: a unit authorizing without actuating is not ready to
            # act, whatever its lifecycle says.
            view = recovery_states.get(unit_id) if recovery_states is not None else None
            if _enum_value(getattr(view, "state", None)) == "actuation_incoherent":
                reasons.append(f"{unit_id}:actuation_incoherent")
            if qualified is False:
                reasons.append(f"{unit_id}:not_qualified")
            elif qualified is None:
                reasons.append(f"{unit_id}:qualification_unknown")
            if lifecycle in _ARMED_LIFECYCLES:
                any_armed = True
        if not any_armed:
            reasons.append("no_unit_armed")
        return reasons

    async def _arm_one(
        self, unit_id: str, *, takeover_acknowledged: bool = False
    ) -> dict[str, str]:
        handle = self._actors.get(unit_id)
        if handle is None:
            return {"unit_id": unit_id, "status": "refused", "reason": "unknown_unit"}
        if bool(getattr(handle, "inhibit_latched", False)):
            return {"unit_id": unit_id, "status": "refused", "reason": "inhibit_latched"}
        qualified = getattr(handle, "qualified", None)
        if qualified is False:
            return {"unit_id": unit_id, "status": "refused", "reason": "not_qualified"}
        if qualified is None:
            # The owning handle would accept this unit; only the facade's own
            # unknown-state gate refuses it.
            return {"unit_id": unit_id, "status": "refused", "reason": "qualification_unknown"}
        try:
            await handle.arm(takeover_acknowledged=takeover_acknowledged)
        except Exception:
            # An actor-side refusal can only be discovered by attempting it.
            # An attempt that left the unit holding a latched inhibit (the
            # arm-time external-writer preflight refusing a foreign PQ
            # objective, API_CONTRACTS "Write-enabled run mode") reports the
            # standing latch as the operator reason: the refusal cause is the
            # latch, and the privileged acknowledgement is the documented exit.
            if bool(getattr(handle, "inhibit_latched", False)):
                return {"unit_id": unit_id, "status": "refused", "reason": "inhibit_latched"}
            return {"unit_id": unit_id, "status": "refused", "reason": "actor_failure"}
        outcome: dict[str, str] = {"unit_id": unit_id, "status": "armed", "reason": "armed"}
        # ADD-1: the preflight's classification rides the outcome (and so the
        # unit_armed audit payload) whenever the owning actor reports one --
        # pod_autonomy arms and acknowledged takeovers are auditable facts.
        classification = getattr(handle, "last_arm_classification", None)
        if isinstance(classification, str) and classification:
            outcome["objective_classification"] = classification
            if classification == "takeover_acknowledged":
                outcome["takeover"] = "acknowledged"
        return outcome

    async def _disarm_one(self, unit_id: str) -> dict[str, str]:
        handle = self._actors.get(unit_id)
        if handle is None:
            return {"unit_id": unit_id, "status": "refused", "reason": "unknown_unit"}
        try:
            await handle.disarm()
        except Exception:
            return {"unit_id": unit_id, "status": "refused", "reason": "actor_failure"}
        return {"unit_id": unit_id, "status": "disarmed", "reason": "disarmed"}

    def _handle_lifecycle(self, unit_id: str) -> UnitLifecycle:
        handle = self._actors.get(unit_id)
        if handle is None:
            # An unknown unit has no place in this site's control; reporting
            # it as disconnected never claims controllability it cannot have.
            return UnitLifecycle.DISCONNECTED
        return UnitLifecycle(_enum_value(getattr(handle, "lifecycle", None)))

    def _is_fleet_wide(self, unit_ids: frozenset[str]) -> bool:
        return set(self._actors).issubset(unit_ids)

    def _mutation_audit(
        self,
        *,
        event_type: str,
        subject: str,
        result: str,
        request_id: str,
        lifecycle: UnitLifecycle,
        reason_codes: tuple[str, ...],
        unit_id: str | None = None,
        source: IntentSource | None = None,
        intent_id: str | None = None,
        generation: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        wall = self._clock.wall_now()
        if not isinstance(wall, datetime) or wall.tzinfo is None or wall.utcoffset() is None:
            raise ValueError("clock wall time must be timezone-aware")
        facts = {
            "event_type": event_type,
            "principal": subject,
            "result": result,
            **dict(payload or {}),
        }
        return AuditEvent(
            event_id=f"facade-{uuid.uuid4().hex}",
            occurred_at=wall.astimezone(UTC),
            monotonic_offset_s=now_mono - self._process_origin_mono,
            process_instance_id=self._process_instance_id,
            event_type=event_type,
            unit_id=unit_id,
            generation=generation,
            principal=subject,
            source=source,
            correlation_id=f"facade:{event_type}:{request_id}",
            intent_id=intent_id,
            # Facade events carry no policy decision; the kernel alone speaks
            # for a policy version when it grants authority.
            policy_version=_FACADE_POLICY_VERSION,
            configuration_version=0,
            observation_sequences={},
            reason_codes=reason_codes,
            requested_active_w=0,
            authorized_active_w=0,
            request_fingerprint=_fingerprint(facts),
            response_fingerprint=_fingerprint({"result": result}),
            result=result,
            lifecycle=lifecycle,
        )

    async def _append_audit(self, event: AuditEvent) -> None:
        await self._audit.append(event)

    async def _publish(self, event_type: str, payload: Mapping[str, Any]) -> int:
        return await self._events.publish({"type": event_type, "payload": dict(payload)})


__all__ = ["EnergyServiceFacade"]
