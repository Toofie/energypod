"""Deterministic evaluation of versioned civil-time schedules."""

from __future__ import annotations

import contextlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Final, Literal, Protocol
from zoneinfo import ZoneInfo

from energypod.domain.intents import Direction, IntentSource
from energypod.domain.schedule import (
    OPEN_EFFECTIVE_FROM,
    OPEN_EFFECTIVE_UNTIL,
    ScheduleEntry,
    SchedulePlan,
    ScheduleValidationError,
    Weekday,
)


@dataclass(frozen=True, slots=True)
class ScheduleIntent:
    id: str
    source: IntentSource
    direction: Direction
    watts: int
    unit_ids: frozenset[str]
    created_at_monotonic: float
    duration_s: float
    schedule_version: int
    # DESIGN_SCHEDULES §1: the entry's watt form carries verbatim — the
    # per-unit mapping when the entry was published per battery, None for the
    # scalar fleet-total form.  Per-unit targets stay per-unit caps at
    # allocation exactly as on REST dispatch.
    watts_by_unit: Mapping[str, int] | None = None
    entry_id: str = ""

    @property
    def selected_unit_ids(self) -> frozenset[str]:
        return self.unit_ids

    @property
    def expires_at_monotonic(self) -> float:
        return self.created_at_monotonic + self.duration_s


class ScheduleEvaluator:
    def __init__(self, *, intent_ttl_s: float) -> None:
        if isinstance(intent_ttl_s, bool) or not isinstance(intent_ttl_s, int | float):
            raise ScheduleValidationError("intent TTL must be a finite positive number")
        if not math.isfinite(intent_ttl_s) or intent_ttl_s <= 0:
            raise ScheduleValidationError("intent TTL must be a finite positive number")
        self._intent_ttl_s = float(intent_ttl_s)

    def evaluate(
        self, *, schedule: SchedulePlan, at: datetime, now_monotonic: float
    ) -> ScheduleIntent | None:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ScheduleValidationError("evaluation instant must be timezone-aware")
        if isinstance(now_monotonic, bool) or not isinstance(now_monotonic, int | float):
            raise ScheduleValidationError("monotonic time must be finite")
        if not math.isfinite(now_monotonic):
            raise ScheduleValidationError("monotonic time must be finite")
        local = at.astimezone(ZoneInfo(schedule.timezone))
        matches = [entry for entry in schedule.entries if self._matches(entry, local)]
        if not matches:
            return None
        selected = max(matches, key=lambda item: (item.priority, item.entry_id))
        return ScheduleIntent(
            id=f"schedule:{schedule.version}:{selected.entry_id}",
            source=IntentSource.SCHEDULE,
            direction=selected.action,
            watts=selected.watts,
            unit_ids=selected.unit_ids,
            created_at_monotonic=float(now_monotonic),
            duration_s=self._intent_ttl_s,
            schedule_version=schedule.version,
            watts_by_unit=selected.watts_by_unit,
            entry_id=selected.entry_id,
        )

    @staticmethod
    def _matches(entry: ScheduleEntry, local: datetime) -> bool:
        if not entry.enabled:
            return False
        local_time = local.timetz().replace(tzinfo=None)
        start_date = local.date()
        if entry.crosses_midnight and local_time < entry.end_local:
            start_date -= timedelta(days=1)
            inside = True
        elif entry.crosses_midnight:
            inside = local_time >= entry.start_local
        else:
            inside = entry.start_local <= local_time < entry.end_local
        if not inside:
            return False
        return (
            Weekday(start_date.weekday()) in entry.days
            and entry.effective_from <= start_date <= entry.effective_until
        )


# --- the pure next-occurrence helpers (DESIGN_SCHEDULES §5.4) -------------------
#
# ``next_start`` and ``window_end`` are the SINGLE implementation of every
# countdown the surface serves (GET's ``next_action``, the ``schedule_state``
# projection, the Home card); no client reimplements civil-time arithmetic.
# Both are pure, deterministic, and DST-honest through the plan's zone.

# The next-occurrence horizon: 7 weekdays plus the boundary on each side, so a
# weekly window is always found inside the search and a cross-midnight window
# that started on the boundary day is not missed.
_NEXT_OCCURRENCE_HORIZON_DAYS = 8


def next_start(plan: SchedulePlan, at: datetime) -> tuple[ScheduleEntry, datetime] | None:
    """The earliest future start among enabled entries, in the plan's zone.

    Searched over the next 8 days; only starts strictly after ``at`` count (a
    window already open belongs to the evaluator, not to this countdown);
    effective date bounds are honoured on the start date; ties at one instant
    resolve by higher priority then entry id.  ``None`` when no enabled entry
    ever occurs again (all disabled, or past every ``effective_until``).
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ScheduleValidationError("next-occurrence instant must be timezone-aware")
    zone = ZoneInfo(plan.timezone)
    local = at.astimezone(zone)
    # (start instant, -priority, entry id, entry): ties at one instant resolve
    # by HIGHER priority first, then the stable entry id.
    best: tuple[datetime, int, str, ScheduleEntry] | None = None
    horizon = local.date() + timedelta(days=_NEXT_OCCURRENCE_HORIZON_DAYS)
    for entry in plan.entries:
        if not entry.enabled:
            continue
        for offset in range(_NEXT_OCCURRENCE_HORIZON_DAYS + 1):
            day = local.date() + timedelta(days=offset)
            if day > horizon:
                break
            if Weekday(day.weekday()) not in entry.days:
                continue
            if not entry.effective_from <= day <= entry.effective_until:
                continue
            candidate = datetime.combine(day, entry.start_local, tzinfo=zone)
            if candidate <= local:
                continue
            key = (candidate, -entry.priority, entry.entry_id, entry)
            if best is None or key[:3] < best[:3]:
                best = key
    if best is None:
        return None
    return best[3], best[0]


def window_end(entry: ScheduleEntry, at: datetime) -> datetime:
    """The local end instant of the currently matching window (aware).

    ``at`` is the aware evaluation instant, already in the plan's zone (the
    evaluator's own contract); the returned end instant carries the same zone.
    A midnight-crossing window that is currently in its tail ends TODAY at
    ``end_local``; one in its head ends TOMORROW at ``end_local`` — one entry,
    one continuous window, never two.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ScheduleValidationError("window-end instant must be timezone-aware")
    local_time = at.timetz().replace(tzinfo=None)
    end_date = at.date()
    if entry.crosses_midnight and local_time >= entry.start_local:
        end_date += timedelta(days=1)
    return datetime.combine(end_date, entry.end_local, tzinfo=at.tzinfo)


def schedule_wire_entry(entry: ScheduleEntry) -> dict[str, Any]:
    """The §5 wire shape of one entry (the editor renders straight from it)."""

    def _wire_bound(bound: Any, open_bound: Any) -> str | None:
        # An open bound (published without a date, DESIGN_SCHEDULES §1) echoes
        # as null — GET shows exactly what the operator published, and the
        # editor's optional date fields stay empty on reload.
        return None if bound == open_bound else bound.isoformat()

    wire: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "days": [_WIRE_DAYS[int(day)] for day in sorted(entry.days)],
        "start_local": entry.start_local.strftime("%H:%M"),
        "end_local": entry.end_local.strftime("%H:%M"),
        "action": entry.action.value,
        "unit_ids": sorted(entry.unit_ids),
        "effective_from": _wire_bound(entry.effective_from, OPEN_EFFECTIVE_FROM),
        "effective_until": _wire_bound(entry.effective_until, OPEN_EFFECTIVE_UNTIL),
        "priority": entry.priority,
        "enabled": entry.enabled,
    }
    if entry.watts_by_unit is not None:
        wire["watts_by_unit"] = dict(sorted(entry.watts_by_unit.items()))
    else:
        wire["watts"] = entry.watts
    return wire


def schedule_wire_plan(plan: SchedulePlan) -> dict[str, Any]:
    return {
        "version": plan.version,
        "timezone": plan.timezone,
        "entries": [schedule_wire_entry(entry) for entry in plan.entries],
    }


_WIRE_DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --- the night-writer posture policy (DESIGN_SCHEDULES §3, as pure logic) -------
#
# The allowed windows are POLICY WALLS: local civil "HH:MM" pairs, no zone of
# their own, union = the allowed command set.  "Night" is any civil second
# outside DAY_DEFAULT (06:00-20:00) — independent of how the operator narrows
# or widens the policy, so the acknowledgement and the console copy always
# mean the same thing by "night".

DAY_DEFAULT: Final[tuple[time, time]] = (time(6, 0), time(20, 0))

_SECONDS_PER_DAY = 86_400


def parse_hhmm(value: str) -> time:
    """Parse one policy wall clock "HH:MM" (minute precision, 00:00-23:59)."""
    if not isinstance(value, str):
        raise ScheduleValidationError("allowed window bounds must be HH:MM civil times")
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 2 for part in parts):
        raise ScheduleValidationError("allowed window bounds must be HH:MM civil times")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ScheduleValidationError("allowed window bounds must be HH:MM civil times")
    return time(hour, minute)


def _second_of_day(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _covers(second: int, start: int, end: int) -> bool:
    """Whether one civil second lies inside [start, end), wrapping midnight."""
    if start < end:
        return start <= second < end
    return second >= start or second < end


def _union_covers(second: int, windows: tuple[tuple[time, time], ...]) -> bool:
    return any(_covers(second, _second_of_day(a), _second_of_day(b)) for a, b in windows)


def window_inside_union(start: time, end: time, windows: tuple[tuple[time, time], ...]) -> bool:
    """Whether every civil second of [start, end) lies inside the union.

    A crossing window (start > end) is judged as its two halves — the head to
    midnight and the tail from midnight — exactly the split the facade's
    containment gate names offending entries by.
    """
    head_end = _SECONDS_PER_DAY if end < start else _second_of_day(end)
    if not all(_union_covers(second, windows) for second in range(_second_of_day(start), head_end)):
        return False
    # A crossing window's tail (from midnight to ``end``) is judged too: the
    # window lies inside the union only when BOTH halves do.
    tail_covered = end >= start or all(
        _union_covers(second, windows) for second in range(0, _second_of_day(end))
    )
    return tail_covered


def covers_night(windows: tuple[tuple[time, time], ...]) -> bool:
    """Whether the allowed union covers any civil second outside DAY_DEFAULT."""
    day_start, day_end = _second_of_day(DAY_DEFAULT[0]), _second_of_day(DAY_DEFAULT[1])
    return any(
        _union_covers(second, windows)
        for second in range(_SECONDS_PER_DAY)
        if not day_start <= second < day_end
    )


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    """The commissioned posture facts of a PRESENT ``schedule:`` block.

    ``timezone`` is the site zone the policy walls are rendered against in
    operator copy; the windows themselves are civil walls with no zone of
    their own.
    """

    allowed_windows_local: tuple[tuple[time, time], ...]
    intent_ttl_s: float
    timezone: str

    def __post_init__(self) -> None:
        if not self.allowed_windows_local:
            raise ScheduleValidationError("allowed_windows_local must not be empty")
        for start, end in self.allowed_windows_local:
            if start == end:
                raise ScheduleValidationError("allowed windows must not be zero-length")
        if not math.isfinite(self.intent_ttl_s) or self.intent_ttl_s <= 0:
            raise ScheduleValidationError("intent TTL must be a finite positive number")

    @property
    def posture(self) -> str:
        """Derived, read-only, never stored: ``partition`` when the allowed
        set grants any night second, else the shipped day-only ``yield``."""
        return "partition" if covers_night(self.allowed_windows_local) else "yield"

    def wire_windows(self) -> list[list[str]]:
        return [
            [start.strftime("%H:%M"), end.strftime("%H:%M")]
            for start, end in self.allowed_windows_local
        ]

    def contains_entry(self, entry: ScheduleEntry) -> bool:
        """The §3 containment rule: the entry's window (split across midnight
        when it crosses) lies entirely inside the allowed union."""
        return window_inside_union(entry.start_local, entry.end_local, self.allowed_windows_local)

    def entry_is_night(self, entry: ScheduleEntry) -> bool:
        """Whether any civil second of the entry's window falls in the night
        (outside DAY_DEFAULT) — the one-time acknowledgement's trigger."""
        return not window_inside_union(entry.start_local, entry.end_local, (DAY_DEFAULT,))


# --- the evaluation loop (DESIGN_SCHEDULES §2) ----------------------------------


class ScheduleStorePort(Protocol):
    """The plan repository read (one singleton row; no cache to invalidate).

    The composed surface control implements this beside its facade port, so
    the runner and the facade read the same singleton through one object.
    """

    async def get_plan(self) -> SchedulePlan | None: ...


class ScheduleClockPort(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class ScheduleSubmitPort(Protocol):
    """The composition-internal facade twin (source pinned to SCHEDULE)."""

    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> Any: ...


class ScheduleIntentPort(Protocol):
    async def active(self, now_mono: float) -> tuple[Any, ...]: ...

    async def remove(self, intent_id: str) -> None: ...


class ScheduleObservationsPort(Protocol):
    """The latest-observation read the disarmed-window honesty code needs.

    Structural, like every runner port: composition wires the SAME observation
    repository the fleet loop reads.  ``None`` (the default) keeps the
    projection exactly as it is today — the runner then never says
    ``units_disarmed``, so isolated compositions are unchanged.
    """

    async def all_latest(self) -> Mapping[str, Any]: ...


class ScheduleBusPort(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


# The projection's ONE pinned reason vocabulary (DESIGN_SCHEDULES §5).
REASON_NO_PLAN: Final[str] = "no_plan"
REASON_NO_WINDOW_OPEN: Final[str] = "no_window_open"
REASON_WINDOW_OPEN: Final[str] = "window_open"
REASON_WAITING: Final[str] = "waiting_for_higher_priority"
REASON_WINDOW_ENDED: Final[str] = "window_ended"
REASON_PLAN_CHANGED: Final[str] = "plan_changed"
# The 2026-08-23 live finding (the operator's own publish test): the runner
# submitted every tick while the fleet sat disarmed, the pre-arm refusals were
# audit-only (lifecycle_not_controllable), and the projection could not say
# "window open but the units are disarmed" — the operator had to discover the
# arm requirement by trying.  The word rides WITH ``window_open`` (like
# ``waiting_for_higher_priority``) when NO unit of the window's scope is in a
# controllable lifecycle, and outranks it: a disarmed unit claimed by a higher
# source is disarmed first (the night strategy's own precedence).
REASON_UNITS_DISARMED: Final[str] = "units_disarmed"

# The lifecycles a unit can actuate from (the safety kernel's
# ``lifecycle_not_controllable`` deny set, the night strategy's set).
_CONTROLLABLE_LIFECYCLES: Final[frozenset[str]] = frozenset({"armed_idle", "active"})

WINDOW_OPENED_EVENT: Final[str] = "schedule_window.opened"
WINDOW_CLOSING_EVENT: Final[str] = "schedule_window.closing"

# The sources ranked above SCHEDULE in the arbiter's pinned order; a window
# whose every unit such a source claims is WAITING (honesty only — the runner
# never checks claims to decide control).
_HIGHER_THAN_SCHEDULE: Final[frozenset[IntentSource]] = frozenset(
    {
        IntentSource.EMERGENCY_STOP,
        IntentSource.MANUAL,
        IntentSource.AGENT,
        IntentSource.OPTIMIZER,
    }
)

ScheduleAction = Literal["idle", "submit", "renew", "remove"]
ClosingReason = Literal["window_ended", "plan_replaced", "no_plan"]


class ScheduleRunner:
    """Exactly one live SCHEDULE intent, keyed ``(plan.version, entry_id)``.

    Per tick (DESIGN_SCHEDULES §2): read the plan through the repository port,
    evaluate at the clock's wall instant in the plan's zone, and maintain the
    held intent — submit on open, remove-then-submit renewal while the window
    holds (the adviser's exact discipline, so never two live), remove on
    window end / entry disable / plan change.  The runner is the LOWEST-
    priority source and never special-cases arbitration: no claim checks and
    no withdrawals against higher sources — a window that opens under one
    WAITS, keeps renewing, and is represented the cycle after the claimer
    lapses.  Window end is non-renewal; the intent TTL plus the firmware
    watchdog are the designed hand-back (and the fail-safe if this runner
    dies mid-window).  A tick failure is survivable per cycle and never halts
    the fleet; the composition wraps ``tick`` accordingly.
    """

    def __init__(
        self,
        *,
        store: ScheduleStorePort,
        evaluator: ScheduleEvaluator,
        clock: ScheduleClockPort,
        submit: ScheduleSubmitPort,
        intents: ScheduleIntentPort,
        observations: ScheduleObservationsPort | None = None,
        bus: ScheduleBusPort | None = None,
        posture: str = "yield",
        initial_plan: SchedulePlan | None = None,
    ) -> None:
        self._store = store
        self._evaluator = evaluator
        self._clock = clock
        self._submit = submit
        self._intents = intents
        self._observations = observations
        self._bus = bus
        self._posture = posture
        # Held-intent state: the live intent id plus the window key it serves.
        self._held_intent_id: str | None = None
        self._held_key: tuple[int, str] | None = None
        self._held_entry: ScheduleEntry | None = None
        self._held_ends_at: datetime | None = None
        self._plan: SchedulePlan | None = initial_plan
        # The projection's tick-derived fields (single writer: this runner).
        # The boot frame is honest about what boot knows — the plan from a
        # synchronous store read and NO window held — and the first tick (one
        # fleet cycle later) replaces it.
        self._last_action: ScheduleAction = "idle"
        self._last_reasons: tuple[str, ...] = (
            (REASON_NO_WINDOW_OPEN,) if initial_plan is not None else (REASON_NO_PLAN,)
        )
        self._last_tick_wall = clock.wall_now()

    @property
    def held_intent_id(self) -> str | None:
        """The live schedule intent id, or None while holding nothing.

        The projection derives ``active`` from THIS fact — never a lifecycle
        guess — so it can never claim inactive while a schedule intent is
        live.
        """
        return self._held_intent_id

    async def tick(self) -> None:
        """One evaluation: maintain the held intent, update the projection."""
        plan = await self._store.get_plan()
        self._plan = plan
        now_mono = float(self._clock.monotonic())
        wall = self._clock.wall_now()
        if plan is None:
            action = await self._close_held(reason="no_plan")
            self._record(action, (REASON_NO_PLAN,), wall)
            return
        evaluated = self._evaluator.evaluate(schedule=plan, at=wall, now_monotonic=now_mono)
        if evaluated is None:
            if self._held_intent_id is None:
                self._record("idle", (REASON_NO_WINDOW_OPEN,), wall)
                return
            # A publish that landed mid-window removed the matching entry;
            # the window merely ended; either way the command stops now.
            reason: ClosingReason = (
                "plan_replaced"
                if self._held_key is not None and self._held_key[0] != plan.version
                else "window_ended"
            )
            action = await self._close_held(
                reason=reason,
                projection_reason=REASON_PLAN_CHANGED
                if reason == "plan_replaced"
                else REASON_WINDOW_ENDED,
            )
            self._record(
                action,
                (REASON_PLAN_CHANGED if reason == "plan_replaced" else REASON_WINDOW_ENDED,),
                wall,
            )
            return
        entry = self._entry_for(plan, evaluated.entry_id)
        ends_at = window_end(entry, wall.astimezone(ZoneInfo(plan.timezone)))
        key = (plan.version, evaluated.entry_id)
        if self._held_key == key and self._held_intent_id is not None:
            await self._remove_held()
            await self._submit_window(plan, evaluated, entry, ends_at, announce=False)
            action = "renew"
        else:
            if self._held_intent_id is not None:
                await self._remove_held()
            await self._submit_window(plan, evaluated, entry, ends_at, announce=True)
            action = "submit"
        reasons: tuple[str, ...] = (REASON_WINDOW_OPEN,)
        if await self._fleet_disarmed(evaluated.unit_ids):
            reasons = (REASON_WINDOW_OPEN, REASON_UNITS_DISARMED)
        elif await self._waiting_for_higher_priority(evaluated.unit_ids, now_mono):
            reasons = (REASON_WINDOW_OPEN, REASON_WAITING)
        self._record(action, reasons, wall, entry=entry, ends_at=ends_at)

    # --- internals -------------------------------------------------------

    def _entry_for(self, plan: SchedulePlan, entry_id: str) -> ScheduleEntry:
        for entry in plan.entries:
            if entry.entry_id == entry_id:
                return entry
        raise ScheduleValidationError(f"evaluated entry {entry_id!r} is absent from the plan")

    async def _submit_window(
        self,
        plan: SchedulePlan,
        evaluated: ScheduleIntent,
        entry: ScheduleEntry,
        ends_at: datetime,
        *,
        announce: bool,
    ) -> None:
        result = await self._submit(
            unit_ids=sorted(evaluated.unit_ids),
            direction=evaluated.direction,
            # Exactly one watt form on the submit port, as on REST dispatch:
            # the per-unit map when the entry was published per battery (the
            # facade derives the fleet total as the sum), else the scalar.
            watts=None if evaluated.watts_by_unit is not None else evaluated.watts,
            ttl_s=evaluated.duration_s,
            watts_by_unit=(
                None if evaluated.watts_by_unit is None else dict(evaluated.watts_by_unit)
            ),
        )
        submitted = result.get("intent_id") if isinstance(result, Mapping) else None
        # Exactly one live SCHEDULE intent, ever: the held slot flips to the
        # submission's own id only on success, so a failed renewal lapses by
        # TTL (the designed hand-back) and the next tick re-submits.
        self._held_intent_id = submitted if isinstance(submitted, str) else None
        self._held_key = (plan.version, evaluated.entry_id)
        self._held_entry = entry
        self._held_ends_at = ends_at
        if announce:
            # "opened" is a TRANSITION, not a heartbeat: it publishes on the
            # first submit for a window key only — renewals ride the
            # snapshot's countdowns, never the event stream.
            await self._publish(
                WINDOW_OPENED_EVENT,
                {
                    "entry_id": evaluated.entry_id,
                    "version": plan.version,
                    "action": evaluated.direction.value,
                    **(
                        {"watts": int(evaluated.watts)}
                        if evaluated.watts_by_unit is None
                        else {
                            "watts": int(evaluated.watts),
                            "watts_by_unit": dict(sorted(evaluated.watts_by_unit.items())),
                        }
                    ),
                    "unit_ids": sorted(evaluated.unit_ids),
                    "ends_at": ends_at.isoformat(),
                },
            )

    async def _remove_held(self) -> None:
        """Remove the held intent by id (never a stop triple, no event)."""
        held = self._held_intent_id
        self._held_intent_id = None
        if held is None:
            return
        # Removal is opportunistic churn control, never the safety path: an
        # intent the store no longer knows (already expired and evicted) must
        # not fail the tick — the TTL lapse is the designed hand-back.
        with contextlib.suppress(Exception):
            await self._intents.remove(held)

    async def _close_held(
        self, *, reason: ClosingReason, projection_reason: str | None = None
    ) -> ScheduleAction:
        """Remove the held intent and publish the closing transition."""
        entry_id = self._held_key[1] if self._held_key is not None else None
        version = self._held_key[0] if self._held_key is not None else None
        unit_ids = sorted(self._held_entry.unit_ids) if self._held_entry is not None else []
        closing = self._held_intent_id is not None
        await self._remove_held()
        self._held_key = None
        self._held_entry = None
        self._held_ends_at = None
        if closing:
            await self._publish(
                WINDOW_CLOSING_EVENT,
                {"entry_id": entry_id, "version": version, "unit_ids": unit_ids, "reason": reason},
            )
        return "remove" if closing else "idle"

    async def _fleet_disarmed(self, unit_ids: frozenset[str]) -> bool:
        """Honesty only: no unit of the window's scope can actuate right now.

        The 2026-08-23 live finding: a window that opens over a disarmed fleet
        submits (correctly — the claim is a published fact) but can never
        actuate, and the pre-arm refusals were audit-only, so the operator had
        to discover the arm requirement by trying.  This read-only check names
        that state on the projection: ``units_disarmed`` when NO unit of the
        window's scope is in a controllable lifecycle (a unit with no
        observation yet is not controllable — boot is disarmed).  A read
        failure omits the code (unknown is not disarmed); the answer NEVER
        gates the submit/remove above — arming is the operator's act.
        """
        port = self._observations
        if port is None or not unit_ids:
            return False
        try:
            latest = await port.all_latest()
        except Exception:
            return False
        for unit_id in unit_ids:
            lifecycle = getattr(latest.get(unit_id), "lifecycle", None)
            value = getattr(lifecycle, "value", lifecycle)
            if value in _CONTROLLABLE_LIFECYCLES:
                return False
        return True

    async def _waiting_for_higher_priority(self, unit_ids: frozenset[str], now_mono: float) -> bool:
        """Honesty only: every unit claimed by a higher-priority live intent.

        A read failure omits the code (unknown is not waiting); the answer
        NEVER gates the submit/remove above — waiting is the arbiter's job.
        """
        try:
            active = await self._intents.active(now_mono)
        except Exception:
            return False
        if not active:
            return False
        claimed: set[str] = set()
        for intent in active:
            source = getattr(intent, "source", None)
            if source not in _HIGHER_THAN_SCHEDULE:
                continue
            selected = getattr(intent, "selected_unit_ids", None)
            if selected:
                claimed.update(unit for unit in selected if unit in unit_ids)
        return bool(unit_ids) and set(unit_ids) <= claimed

    async def _publish(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._bus is None:
            return
        with contextlib.suppress(Exception):
            await self._bus.publish({"type": event_type, "payload": dict(payload)})

    def _record(
        self,
        action: ScheduleAction,
        reasons: tuple[str, ...],
        wall: datetime,
        *,
        entry: ScheduleEntry | None = None,
        ends_at: datetime | None = None,
    ) -> None:
        self._last_action = action
        self._last_reasons = reasons
        self._last_tick_wall = wall
        if ends_at is not None:
            self._held_ends_at = ends_at
        elif action != "renew" and action != "submit":
            self._held_ends_at = None
        if entry is not None:
            self._held_entry = entry

    # --- the projection (single writer: this runner, post-tick) ----------

    def state_payload(self) -> dict[str, Any]:
        """The ``schedule_state`` snapshot projection (DESIGN_SCHEDULES §5).

        ``active`` derives from ``held_intent_id``, never a lifecycle guess,
        so it can never claim inactive while a schedule intent is live; the
        countdowns are computed at read time so they stay fresh between
        ticks on the console's snapshot cadence.
        """
        wall = self._clock.wall_now()
        ends_at = self._held_ends_at
        ends_in_s = max(0, int((ends_at - wall).total_seconds())) if ends_at is not None else None
        return {
            "version": None if self._plan is None else self._plan.version,
            "active": self._held_intent_id is not None,
            "entry_id": self._held_key[1] if self._held_key is not None else None,
            "held_intent_id": self._held_intent_id,
            "ends_at": ends_at.isoformat() if ends_at is not None else None,
            "ends_in_s": ends_in_s,
            "next": self._next_action_payload(wall),
            "posture": self._posture,
            "last_action": self._last_action,
            "last_tick_at": self._last_tick_wall.isoformat(),
            "reason_codes": list(self._last_reasons),
        }

    def _next_action_payload(self, wall: datetime) -> dict[str, Any] | None:
        """The next occurrence AFTER the running window (or after now)."""
        plan = self._plan
        if plan is None:
            return None
        search_from = wall if self._held_ends_at is None else max(wall, self._held_ends_at)
        found = next_start(plan, search_from)
        if found is None:
            return None
        entry, starts_at = found
        return {
            **schedule_wire_entry(entry),
            "starts_at": starts_at.isoformat(),
            "starts_in_s": max(0, int((starts_at - wall).total_seconds())),
        }


# --- the refusal family and the facade-facing surface control (§3/§5) ----------


class ScheduleRefusal(Exception):
    """A schedule publish/read refusal carrying its wire code and details.

    The facade raises exactly this for the surface's 409 shapes (the
    ``ExcessChargingRefusal`` pattern); the guarded boundary maps ``code``
    onto the error envelope verbatim.
    """

    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


class SchedulePublishValidationError(ScheduleValidationError):
    """The 422 shape: domain rule violations naming the offending entries."""

    def __init__(self, errors: Sequence[Mapping[str, Any]]) -> None:
        summary = "; ".join(
            f"{item.get('entry_id') or 'plan'}: {item.get('message')}" for item in errors
        )
        super().__init__(summary or "the schedule plan is not valid")
        self.entry_errors: list[dict[str, Any]] = [dict(item) for item in errors]


class SchedulePlanStorePort(Protocol):
    """The durable singleton plan store (the SQLite/in-memory CAS contract)."""

    def get(self) -> SchedulePlan | None: ...

    def replace(self, *, expected_version: int, replacement: SchedulePlan) -> None: ...


class ScheduleSurfaceControl:
    """The facade-facing half of the composed schedule surface (the P6 block-
    presence doctrine): the commissioned policy facts, the durable-once night
    acknowledgement latch, the async plan-store adapter, and the projection
    read off the bound runner.

    Writers are split by ownership, never by race (the excess-controller
    pattern): the facade's publish is the only writer of the plan store and
    the acknowledgement latch; the fleet loop's runner is the single writer
    of every projection field.  Composition builds this FIRST (the facade
    projects through it), then the runner, then binds it.
    """

    def __init__(
        self,
        *,
        policy: SchedulePolicy,
        store: SchedulePlanStorePort,
        acknowledged_night_windows: bool,
    ) -> None:
        self._policy = policy
        self._store = store
        self._acknowledged = bool(acknowledged_night_windows)
        self._runner: ScheduleRunner | None = None

    def bind_runner(self, runner: ScheduleRunner) -> None:
        """Bind the runner for the projection read (exactly once)."""
        if self._runner is not None:
            raise RuntimeError("the schedule surface control is already bound")
        self._runner = runner

    @property
    def policy(self) -> SchedulePolicy:
        return self._policy

    @property
    def acknowledged_night_windows(self) -> bool:
        """The once-ever durable night-partition fact, boot-loaded from the
        audit store's keyed existence check and latched by the first audited
        night publish; never re-prompted."""
        return self._acknowledged

    def mark_night_acknowledged(self) -> None:
        """Latch the captured fact (after its durable append)."""
        self._acknowledged = True

    async def get_plan(self) -> SchedulePlan | None:
        return self._store.get()

    async def replace_plan(self, *, expected_version: int, replacement: SchedulePlan) -> None:
        self._store.replace(expected_version=expected_version, replacement=replacement)

    def state_payload(self) -> dict[str, Any]:
        """The projection read; the bound runner is the single writer."""
        if self._runner is None:  # pragma: no cover - composition binds before serving
            raise RuntimeError("the schedule surface control has no bound runner")
        return self._runner.state_payload()
