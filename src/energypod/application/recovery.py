"""Self-healing awareness: detection and honest surfacing of recovery states.

The operator's principle (docs/POD_RECOVERY_RESEARCH.md): the batteries
self-heal from command-state, communication, and estimation problems --
watchdog reversion, autonomy resumption, cell balancing, SOC re-estimation,
stable-sample requalification -- and all of those are TRUSTED by design.  What
the controller owes the operator is detection of three things:

1. self-healing in progress (quiet, informational -- ``self_healing``);
2. ambiguous anomalous behavior (evidence capture -- ``unexpected_autonomy``);
3. self-healing FAILURE: the firmware-wedge class that historically required
   a physical power cycle (``not_responding``, and ``actuation_incoherent``
   once the objective echo proves the defect is pod-side).

This module is the ladder's R4 rung plus the promoted P1 items vi
(actuation-coherence watchdog) and iii (objective echo read-back).  It is
PASSIVE by construction: it appends audit facts and publishes bus events and
nothing else.  No write path, no latch, no block, no refusal originates here;
the existing actor latch (``external_writer``) remains the only latching
mechanism and is only ever READ by the classifier.

Every health state is a DERIVED read: ``observe_cycle`` records the latest
cycle facts and ``unit_health_states`` recomputes the view from them, so
nothing is remembered beyond the current evidence (boot is
``healthy``-by-observation).  The monitor is driven once per fleet cycle by
the single supervision loop, so it needs no locking.

DESIGN_BATTERY_HEALTH_WATCH §3 (Wave 0) landed here first, before any stage
of that program composes, because both of its fixes correct the evidence the
program would act on: the self-charge float deadband (W0-1) and coherence
judged on delivery with a gap-surviving baseline (W0-2).  Detection work
only -- Wave 0 adds no write of any kind, and per the contract's invariant
I10 no automated response anywhere may key on ``actuation_incoherent``
alone.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from energypod.domain import UnitLifecycle
from energypod.domain.audit import AuditEvent

# The share of an authorized figure the measured battery power is expected to
# move once the unit is actuating.  Mid legitimately delivers 75-87 % of a
# charge command (solar self-charge offsetting inside its uncommanded band)
# and discharge runs +150-250 W over command (firmware serving local load), so
# the coherent band deliberately starts at half the command.
_MOVEMENT_FRACTION = 0.5

# The operator-directed cell early-warning line (2026-08-23 imbalance
# incident: 0.050 V demoted from a hard gate to an early-warning tier, the
# ABSOLUTE per-cell bounds remain the hard gates).  A spread above it while
# the battery is otherwise fine is top-of-charge balancing -- self-healing.
_CELL_BALANCING_SPREAD_V = 0.050

# R5 honest terminal guidance (docs/POD_RECOVERY_RESEARCH.md R5 and the
# vendor's own remediation string, CommCfgForm.cs:249): when the pod itself
# is the wedged party, no remote remedy exists and saying so is the product.
PHYSICAL_RESTART_HINT = (
    "pod not responding — remote recovery exhausted; physical restart required "
    "(power-cycle the pod, then verify telemetry resumes, Debug Mode reads "
    "Normal Mode and SysControlMode reads Remote in the vendor MiniES app — see "
    "docs/POD_RECOVERY_RESEARCH.md R5)"
)

# R2a mode-conflict guidance: the objective is not being served at all, the
# vendor-documented precondition class (MiniESapp.cs:2179-2184 refuses sends
# while the pod is not in Normal Mode / Remote).
MODE_CHECK_HINT = (
    "pod is not serving the commanded objective — open the vendor MiniES app, "
    "check Debug Mode = Normal Mode and SysControlMode = Remote, then "
    "re-dispatch (docs/POD_RECOVERY_RESEARCH.md R2a)"
)

# DESIGN_POD_PARKING sections 1/3: the expiry hint (the pinned sentence) and
# the terminal write-unverified posture's hint.  Resuming a device is an
# enabling act -- an interactive operator act, never a timer.
PARK_EXPIRY_HINT = "lease expired — Resume is an operator act"
PARK_WRITE_UNVERIFIED_HINT = (
    "resume write unverified — the pod may still be parked; repeat RESUME and "
    "watch the readback, and verify the mode word in the vendor app before any "
    "physical work (parking is not electrical isolation)"
)


class HealthState(StrEnum):
    """The per-unit recovery vocabulary served on snapshot and health views.

    Classification precedence, highest first: ``unreachable``,
    ``not_responding``, ``foreign_writer``, ``inhibited``,
    ``actuation_incoherent``, ``parked``, ``self_healing``, ``healthy``.
    The fault classes (everything above ``parked``) legitimately outrank a
    park -- fault beats operator state, pinned so nobody "fixes" it
    (DESIGN_POD_PARKING section 3).
    """

    HEALTHY = "healthy"
    SELF_HEALING = "self_healing"
    ACTUATION_INCOHERENT = "actuation_incoherent"
    NOT_RESPONDING = "not_responding"
    UNREACHABLE = "unreachable"
    FOREIGN_WRITER = "foreign_writer"
    INHIBITED = "inhibited"
    # DESIGN_POD_PARKING section 3: PARKED is a COMPOSABLE state -- the
    # healing-reason list is computed first (pure), then `("parked",
    # *healing)`, so a parked+balancing pod keeps its balancing visibility.
    PARKED = "parked"


# Objective-echo classifications (P1 iii; vendor precedent MiniESapp.cs:2166
# reads the mode register back immediately after every write).
ECHO_MATCHES_WRITE = "echo_matches_write"
ECHO_EXTERNAL_WRITER = "external_writer"
ECHO_OBJECTIVE_NOT_SERVED = "objective_not_served"
ECHO_UNREADABLE = "echo_unreadable"

# Per-cycle bus/read outcomes the classifier consumes (the runtime maps the
# concrete transport errors onto this vocabulary; no protocol type leaks here).
READ_OK = "ok"
READ_FAILED = "read_failed"
CONNECT_FAILED = "connect_failed"

_RECOVERY_PRINCIPAL = "energypod:recovery"
_RECOVERY_POLICY_VERSION = "recovery"


def _enum_text(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else str(raw)


def _fingerprint(facts: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(facts), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_float(raw: Any) -> float | None:
    if isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw):
        return float(raw)
    return None


@dataclass(frozen=True, slots=True)
class RecoverySettings:
    """Commissioning knobs for the detection layer (config policy keys)."""

    actuation_coherence_cycles: int = 4
    actuation_coherence_min_movement_w: int = 150
    # DESIGN_BATTERY_HEALTH_WATCH §3.1 (Wave 0, W0-1): the self-charge float
    # deadband.  At 96-99% SoC the pods float ACROSS zero (observed rhs
    # straddle -16/0/+33 W), so an exact-zero test on the self-charge
    # classifier flapped the health state 158 transitions in one night.  A
    # cycle with |measured| below the deadband renders NEITHER self-charging
    # NOR flap -- floating at the top is the healthy steady state of a full
    # pack.  25 W sits above the metering noise floor (tens of watts) and an
    # order below the genuine CT-following self-charge class (-520..-560 W);
    # the ceiling is 100 because beyond it the deadband would begin to eat
    # the legitimate float class.
    self_charge_deadband_w: float = 25.0
    # §3.2 (Wave 0, W0-2a): the authorization-gap grace the coherence
    # baseline survives.  An authorization gap (intent renewal lapse,
    # telemetry_stale dip) shorter than this, with delivery continuing at the
    # commanded level, is the SAME episode: the pre-command baseline does not
    # move and is never re-anchored onto the watts the pod was already
    # delivering.  12 s is the night-writer detector's handback-grace
    # precedent (bounds mirrored), covering the observed ~4-8 s watchdog
    # hand-back with margin.  Beyond the grace, or once the measured power
    # has returned to the idle band, the baseline re-anchors as before.
    coherence_gap_grace_s: float = 12.0
    # The commissioned EXPECTED autonomy envelope (the live-write example's
    # documented value; live on observe-only deployments, which compose this
    # default verbatim when no policy block is configured).
    expected_autonomy_band_w: tuple[int, int] = (-2600, 1000)
    unresponsive_attempts: int = 3
    unexpected_autonomy_min_interval_s: float = 60.0

    def __post_init__(self) -> None:
        for name in ("actuation_coherence_cycles", "unresponsive_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.actuation_coherence_min_movement_w) is not int or (
            self.actuation_coherence_min_movement_w < 1
        ):
            raise ValueError("actuation_coherence_min_movement_w must be a positive integer")
        deadband = self.self_charge_deadband_w
        if (
            isinstance(deadband, bool)
            or not isinstance(deadband, int | float)
            or not math.isfinite(float(deadband))
            or not 0.0 < float(deadband) <= 100.0
        ):
            raise ValueError("self_charge_deadband_w must be in (0, 100] watts")
        grace = self.coherence_gap_grace_s
        if (
            isinstance(grace, bool)
            or not isinstance(grace, int | float)
            or not math.isfinite(float(grace))
            or not 1.0 <= float(grace) <= 300.0
        ):
            raise ValueError("coherence_gap_grace_s must be between 1 and 300 seconds")
        interval = self.unexpected_autonomy_min_interval_s
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int | float)
            or not math.isfinite(float(interval))
            or interval <= 0
        ):
            raise ValueError("unexpected_autonomy_min_interval_s must be positive and finite")
        low, high = self.expected_autonomy_band_w
        if not low < high or low > 0 or high < 0:
            raise ValueError(
                "expected_autonomy_band_w must be a strictly ascending pair spanning the "
                "pods' negative self-charge to small positive float region"
            )


@dataclass(frozen=True, slots=True)
class UnitHealthView:
    """One unit's derived recovery state, served on snapshot and health."""

    unit_id: str
    state: HealthState
    reasons: tuple[str, ...]
    remediation_hint: str | None


@dataclass(frozen=True, slots=True)
class CycleFindings:
    """What one observed cycle asked the runtime to do next."""

    coherence_trigger: bool = False
    unexpected_autonomy: bool = False


@dataclass(slots=True)
class _UnitRecord:
    """Everything the derived view is recomputed from (no latching)."""

    unit_id: str
    state: HealthState = HealthState.HEALTHY
    # Coherence watchdog bookkeeping (P1 vi).
    streak: int = 0
    episode_open: bool = False
    incoherent_active: bool = False
    echo_classification: str | None = None
    baseline_watts: float | None = None
    idle_measured_watts: float | None = None
    authorized_watts: int = 0
    authorized_direction: str | None = None
    # DESIGN_BATTERY_HEALTH_WATCH §3.2 (Wave 0, W0-2a): the episode a
    # preserved baseline belongs to, and when its authorization gap opened.
    # ``episode_authorized_watts`` is the last authorized figure the episode
    # ran under (``authorized_watts`` reads 0 while the gap stands, so the
    # still-band at gap time needs its own carrier); it is 0 exactly when no
    # baseline is preserved.
    episode_authorized_watts: int = 0
    authorization_gap_open_mono: float | None = None
    # Responsiveness streaks (R4 class discrimination).
    read_failure_streak: int = 0
    connect_failure_streak: int = 0
    # Evidence-capture throttle (the unexpected-autonomy recorder).
    last_autonomy_event_mono: float | None = None
    # The latest cycle facts the derived state is computed from.
    lifecycle: UnitLifecycle = UnitLifecycle.BOOT
    inhibit_latched: bool = False
    inhibit_reason: str | None = None
    claimed: bool = False
    measured_watts: float | None = None
    cell_spread_v: float | None = None
    # DESIGN_POD_PARKING section 3: the parked inputs, fed from the lease
    # state by the supervision pass.  ``park_expired`` and
    # ``park_write_unverified`` name their own reasons beside ``parked``.
    parked: bool = False
    park_expired: bool = False
    park_write_unverified: bool = False


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class AuditSink(Protocol):
    async def append(self, event: Any) -> None: ...


class EventPublisher(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


def _lifecycle(raw: Any) -> UnitLifecycle:
    if isinstance(raw, UnitLifecycle):
        return raw
    return UnitLifecycle(_enum_text(raw))


class RecoveryMonitor:
    """Per-unit detection over the supervised fleet cycle; passive everywhere."""

    def __init__(
        self,
        *,
        unit_ids: frozenset[str],
        settings: RecoverySettings,
        clock: Clock,
        audit: AuditSink,
        bus: EventPublisher,
        process_instance_id: str,
        process_origin_mono: float,
        configuration_version: int,
    ) -> None:
        if not unit_ids or any(
            not isinstance(unit_id, str) or not unit_id.strip() for unit_id in unit_ids
        ):
            raise ValueError("unit_ids must contain normalized identifiers")
        if not isinstance(process_instance_id, str) or not process_instance_id.strip():
            raise ValueError("process_instance_id must be non-empty")
        if type(configuration_version) is not int or configuration_version < 0:
            raise ValueError("configuration_version must be a non-negative integer")
        self._settings = settings
        self._clock = clock
        self._audit = audit
        self._bus = bus
        self._process_instance_id = process_instance_id
        self._process_origin_mono = float(process_origin_mono)
        self._configuration_version = configuration_version
        self._records: dict[str, _UnitRecord] = {
            unit_id: _UnitRecord(unit_id=unit_id) for unit_id in sorted(unit_ids)
        }

    # --- supervision inputs -----------------------------------------------------

    def record_read_outcome(self, unit_id: str, outcome: str) -> None:
        """Fold one cycle's bus/read outcome into the responsiveness streaks.

        ``CONNECT_FAILED`` is the gateway/TCP class (a single occurrence is
        unambiguous); ``READ_FAILED`` is the pod-silent-on-the-bus class and
        needs the configured consecutive attempts; any success clears both.
        """
        record = self._records.get(unit_id)
        if record is None:
            raise LookupError(f"no unit with id {unit_id!r}")
        if outcome == CONNECT_FAILED:
            record.connect_failure_streak += 1
            record.read_failure_streak = 0
        elif outcome == READ_FAILED:
            record.read_failure_streak += 1
            record.connect_failure_streak = 0
        elif outcome == READ_OK:
            record.read_failure_streak = 0
            record.connect_failure_streak = 0
        else:
            raise ValueError(f"unknown read outcome {outcome!r}")

    async def observe_cycle(
        self,
        unit_id: str,
        *,
        authorized_watts: int,
        authorized_direction: str | None,
        claimed: bool,
        lifecycle: Any,
        inhibit_latched: bool,
        inhibit_reason: str | None,
        observation: Any,
        now_mono: float,
        parked: bool = False,
        park_expired: bool = False,
        park_write_unverified: bool = False,
    ) -> CycleFindings:
        """Record one unit's cycle facts and derive the recovery view.

        The caller is the supervision loop, once per fleet cycle per unit,
        after the heartbeats and polls: ``authorized_watts`` is the authority
        the heartbeat just consumed (peeked before the write), the
        observation is the poll's fresh decode, and ``claimed`` says whether
        a live intent names the unit.  The ``park_*`` inputs are fed from the
        lease ledger (DESIGN_POD_PARKING section 3).  Returns what the
        runtime should do next (perform the objective echo read-back on a
        coherence trigger).
        """
        record = self._records.get(unit_id)
        if record is None:
            raise LookupError(f"no unit with id {unit_id!r}")
        measured = _optional_float(getattr(observation, "battery_watts", None))
        spread = _optional_float(getattr(observation, "cell_imbalance_v", None))
        record.lifecycle = _lifecycle(lifecycle)
        record.inhibit_latched = bool(inhibit_latched)
        record.inhibit_reason = inhibit_reason if isinstance(inhibit_reason, str) else None
        record.claimed = bool(claimed)
        record.measured_watts = measured
        record.cell_spread_v = spread
        record.parked = bool(parked)
        record.park_expired = bool(park_expired)
        record.park_write_unverified = bool(park_write_unverified)

        trigger = self._track_coherence(
            record, authorized_watts, authorized_direction, measured, now_mono
        )
        if trigger:
            # Exactly one detection fact and one detection event per episode
            # (throttled; re-arms only on a coherent cycle or a control-state
            # change).  The echo read-back follows through the owning actor.
            await self._emit_actuation_incoherent(record)
        autonomy = await self._record_unexpected_autonomy(record, observation, now_mono)

        await self._publish_state_change(record)
        return CycleFindings(coherence_trigger=trigger, unexpected_autonomy=autonomy)

    async def _publish_state_change(self, record: _UnitRecord) -> None:
        """Publish ``unit.health_changed`` iff the derived state moved.

        Shared by the cycle pass and the echo pass: the transition to
        ``actuation_incoherent`` fires at ECHO time (Wave 0 W0-2b -- the
        state opens on the discriminator's verdict, not the streak), so both
        passes must publish transitions through one door.
        """
        state, reasons, _hint = self._derive(record)
        if state is not record.state:
            previous, record.state = record.state, state
            await self._publish(
                {
                    "type": "unit.health_changed",
                    "payload": {
                        "unit_id": record.unit_id,
                        "from": previous.value,
                        "to": state.value,
                        "reasons": list(reasons),
                    },
                }
            )

    async def record_incoherence_echo(
        self,
        unit_id: str,
        *,
        classification: str,
        served_active_w: int | None,
        served_reactive_var: int | None,
    ) -> None:
        """Record the trigger-time objective echo read-back and audit it.

        Called by supervision exactly once per coherence trigger, after it
        performed the bounded fresh read of the served objective through the
        owning actor (P1 iii -- never per heartbeat; the budget is one read
        per episode).  Appends the ``objective_echo`` audit fact carrying the
        classification and the read value, and publishes the discriminated
        follow-up to the detection event.

        DESIGN_BATTERY_HEALTH_WATCH §3.2 (Wave 0, W0-2b): this is also where
        the episode's health STATE is decided.  The state may OPEN -- become
        ``actuation_incoherent`` -- only when the discriminator classified
        the episode ``echo_matches_write`` or ``objective_not_served``; an
        ``echo_unreadable``, unclassified, or ``external_writer`` episode
        records its evidence and downgrades (the external-writer class
        belongs to the standing latch path, and an unreadable echo is not
        pod-side proof).
        """
        record = self._records.get(unit_id)
        if record is None:
            raise LookupError(f"no unit with id {unit_id!r}")
        record.echo_classification = classification
        with contextlib.suppress(Exception):
            await self._audit.append(
                self._event(
                    event_type="objective_echo",
                    unit_id=unit_id,
                    reason_codes=(classification,),
                    result="classified",
                    record=record,
                    facts={
                        "classification": classification,
                        "served_active_w": served_active_w,
                        "served_reactive_var": served_reactive_var,
                        "authorized_watts": record.authorized_watts,
                        "authorized_direction": record.authorized_direction,
                    },
                )
            )
        await self._publish(
            {
                "type": "actuation.incoherent",
                "payload": self._incoherent_payload(record)
                | {
                    "echo_classification": classification,
                    "served_active_w": served_active_w,
                    "served_reactive_var": served_reactive_var,
                },
            }
        )
        if record.episode_open and classification in {
            ECHO_MATCHES_WRITE,
            ECHO_OBJECTIVE_NOT_SERVED,
        }:
            record.incoherent_active = True
            await self._publish_state_change(record)

    # --- read views ----------------------------------------------------------------

    async def unit_health_states(self) -> dict[str, UnitHealthView]:
        """The derived per-unit recovery view (the facade's projection)."""
        views: dict[str, UnitHealthView] = {}
        for unit_id, record in self._records.items():
            state, reasons, hint = self._derive(record)
            views[unit_id] = UnitHealthView(
                unit_id=unit_id, state=state, reasons=reasons, remediation_hint=hint
            )
        return views

    # --- internals -------------------------------------------------------------------

    def _track_coherence(
        self,
        record: _UnitRecord,
        authorized_watts: int,
        direction: str | None,
        measured: float | None,
        now_mono: float,
    ) -> bool:
        """Advance the actuation-coherence watchdog for one cycle.

        The judgment bands: with ``required = _MOVEMENT_FRACTION * authorized``
        and the commissioned absolute floor, a cycle is COHERENT when EITHER
        the measured movement from the episode baseline OR the delivered
        power in the commanded direction reaches ``max(required, floor)``
        (confident actuation -- the episode closes and re-arms), it counts
        toward the streak only when BOTH stay below ``min(required, floor)``
        (confident stillness on both axes -- the wedge signature), and
        anything else is INCONCLUSIVE.  The dead zone keeps tiny setpoints
        out of court: a healthy 100 W command moves less than the 150 W
        floor, so it is never declared incoherent OR coherent and no streak
        accumulates; a fully silent pod (movement ~0) falls below the
        proportional band and still alarms.

        DESIGN_BATTERY_HEALTH_WATCH §3.2 (Wave 0, W0-2b): incoherence is
        judged on DELIVERY, not movement alone.  Steady delivery at 87-96%
        of command (this fleet's known spread) passes the delivery test and
        is coherent -- it IS delivery; both live 2026-08-24 incoherent
        detections were the movement-only test reading exactly that delivery
        as "no movement" after a gap re-anchored the baseline underneath it.
        Movement from the PRE-COMMAND baseline remains the other axis: a pod
        that never moved and never delivered is the silent-loss wedge this
        watchdog exists for.

        The baseline survives short authorization gaps (W0-2a): an
        authorization ending re-anchors only once the measured power has
        returned to the idle band (within the still-band of the episode
        baseline) or the gap exceeded ``coherence_gap_grace_s`` -- see
        ``_close_authorization_gap``.

        The trigger this method returns records evidence and orders the
        objective-echo read; the health STATE opens only on the
        discriminator's verdict (``record_incoherence_echo``) -- an
        ``echo_unreadable``, unclassified, or ``external_writer`` episode
        records evidence and downgrades (the external-writer class belongs
        to the standing latch path).
        """
        if authorized_watts is None or authorized_watts <= 0:
            self._close_authorization_gap(record, measured, now_mono)
            return False
        record.authorized_watts = int(authorized_watts)
        record.authorized_direction = direction
        record.authorization_gap_open_mono = None
        if record.baseline_watts is None:
            # The pre-command watts the authorization episode started from.
            record.baseline_watts = (
                record.idle_measured_watts if record.idle_measured_watts is not None else measured
            )
        if measured is None or record.baseline_watts is None:
            return False  # no usable measurement: neither evidence nor alarm
        # The episode's live authorization figure (the gap-time still-band's
        # carrier -- ``authorized_watts`` reads 0 while a gap stands).
        record.episode_authorized_watts = record.authorized_watts
        movement = abs(measured - record.baseline_watts)
        required = _MOVEMENT_FRACTION * record.authorized_watts
        floor = float(self._settings.actuation_coherence_min_movement_w)
        # Delivered power IN THE COMMANDED DIRECTION (the ``_signed_authorized``
        # convention: charge signs negative), so a pod serving its command
        # reads as delivery whatever its distance from the pre-command
        # baseline.
        sign = -1.0 if record.authorized_direction == "charge" else 1.0
        delivery = sign * measured
        if movement >= max(required, floor) or delivery >= max(required, floor):
            record.streak = 0
            record.episode_open = False
            record.incoherent_active = False
            record.echo_classification = None
            return False
        if movement < min(required, floor) and delivery < min(required, floor):
            record.streak += 1
        if not record.episode_open and record.streak >= self._settings.actuation_coherence_cycles:
            # Evidence and the echo read only: the state opens (or downgrades)
            # in ``record_incoherence_echo`` once the discriminator speaks.
            record.episode_open = True
            return True
        return False

    def _close_authorization_gap(
        self,
        record: _UnitRecord,
        measured: float | None,
        now_mono: float,
    ) -> None:
        """Fold one unauthorized cycle into the coherence episode (W0-2a).

        DESIGN_BATTERY_HEALTH_WATCH §3.2: the baseline re-anchors only when
        the measured power has returned to the idle band OR the
        authorization gap exceeded ``coherence_gap_grace_s``.  A shorter gap,
        with delivery continuing at the commanded level, is the SAME
        episode: the pre-command baseline does not move, and the watts the
        pod is ALREADY delivering are NOT adopted as the idle level -- that
        adoption is exactly the 2026-08-24 false-positive mechanism (an
        intent-renewal lapse re-anchored the baseline mid-flight, and steady
        87-96%-of-command delivery then read as "no movement" for four
        cycles, with a physical-restart hint attached).
        """
        record.authorized_watts = 0
        record.authorized_direction = None
        if record.baseline_watts is None:
            # No preserved episode: a control-state change ends any episode
            # silently -- an uncommanded pod drifting back to its baseline is
            # autonomy, not a defect.
            record.streak = 0
            record.episode_open = False
            record.incoherent_active = False
            record.echo_classification = None
            record.episode_authorized_watts = 0
            record.authorization_gap_open_mono = None
            if measured is not None:
                record.idle_measured_watts = measured
            return
        if record.authorization_gap_open_mono is None:
            record.authorization_gap_open_mono = float(now_mono)
        # "Returned to the idle band" is the watchdog's own confident-still
        # band around the episode baseline: a pod back within that band of
        # its pre-command level is genuinely idle again, whatever the clock
        # says.
        still_band = min(
            _MOVEMENT_FRACTION * record.episode_authorized_watts,
            float(self._settings.actuation_coherence_min_movement_w),
        )
        returned_to_idle = (
            measured is not None and abs(measured - record.baseline_watts) < still_band
        )
        gap_exceeded_grace = (
            float(now_mono) - record.authorization_gap_open_mono
        ) > float(self._settings.coherence_gap_grace_s)
        if returned_to_idle or gap_exceeded_grace:
            # The episode is genuinely over: re-anchor from the level the
            # battery now holds (the standing pre-Wave-0 semantics, now
            # bounded by the grace and the idle-band test).
            record.streak = 0
            record.episode_open = False
            record.incoherent_active = False
            record.echo_classification = None
            record.baseline_watts = None
            record.episode_authorized_watts = 0
            record.authorization_gap_open_mono = None
            if measured is not None:
                record.idle_measured_watts = measured
        # Else: PRESERVE -- the same episode across the short gap.  The
        # baseline, the streak, and the episode state all stand; the next
        # authorized cycle judges against the SAME pre-command baseline.

    async def _record_unexpected_autonomy(
        self, record: _UnitRecord, observation: Any, now_mono: float
    ) -> bool:
        """Timestamp one out-of-band uncommanded power observation.

        Measured battery power outside the commissioned autonomy band while no
        intent claims the unit is exactly mid's standing unexplained ±1.2 kHz
        oscillation class: evidence for the next diagnosis, throttled to at
        most one audit fact per unit per configured interval.  Never a block,
        never an alarm tier, and never a health-state change on its own.
        """
        measured = record.measured_watts
        if measured is None or record.claimed:
            return False
        low, high = self._settings.expected_autonomy_band_w
        if low <= measured <= high:
            return False
        last = record.last_autonomy_event_mono
        if (
            last is not None
            and (now_mono - last) < self._settings.unexpected_autonomy_min_interval_s
        ):
            return False
        record.last_autonomy_event_mono = now_mono
        facts: dict[str, Any] = {
            "unit_id": record.unit_id,
            "measured_watts": measured,
            "soc_pct": _optional_float(getattr(observation, "authoritative_soc_pct", None)),
            "debug_mode_w": getattr(observation, "debug_mode_w", None),
            "ctrl_mode_w": getattr(observation, "ctrl_mode_w", None),
            "work_mode_w": getattr(observation, "work_mode_w", None),
            "run_mode_w": getattr(observation, "run_mode_w", None),
        }
        with contextlib.suppress(Exception):
            await self._audit.append(
                self._event(
                    event_type="unexpected_autonomy",
                    unit_id=record.unit_id,
                    reason_codes=("outside_expected_autonomy_band",),
                    result="observed",
                    record=record,
                    facts=facts | {"band_w": [low, high]},
                )
            )
        # The bus carries the full figures (quiet tier: pure evidence the
        # console may feature-detect; it is NOT an alarm vocabulary entry).
        await self._publish({"type": "unit.unexpected_autonomy", "payload": dict(facts)})
        return True

    # --- derived classification ---------------------------------------------------

    def _derive(self, record: _UnitRecord) -> tuple[HealthState, tuple[str, ...], str | None]:
        settings = self._settings
        if record.connect_failure_streak >= 1:
            # The TCP path itself is down: the gateway class.  The pod behind
            # it may be perfectly fine, so no pod-restart guidance applies.
            return HealthState.UNREACHABLE, ("gateway_unreachable",), None
        if record.read_failure_streak >= settings.unresponsive_attempts:
            # Connects fine, reads time out: the whole-pod firmware-wedge
            # signature (research class 2).  R1 transport resync has already
            # retried by construction -- remote recovery is exhausted.
            return HealthState.NOT_RESPONDING, ("reads_timing_out",), PHYSICAL_RESTART_HINT
        if record.inhibit_latched and record.inhibit_reason == "external_writer":
            return HealthState.FOREIGN_WRITER, ("external_writer_latched",), None
        if record.inhibit_latched or record.lifecycle is UnitLifecycle.INHIBITED:
            reasons = ("inhibited",) + (
                () if record.inhibit_reason is None else (record.inhibit_reason,)
            )
            return HealthState.INHIBITED, reasons, None
        if record.incoherent_active:
            reasons = ("authorized_not_actuating",) + (
                () if record.echo_classification is None else (record.echo_classification,)
            )
            return HealthState.ACTUATION_INCOHERENT, reasons, self._incoherence_hint(record)
        healing: list[str] = []
        if (
            not record.inhibit_latched
            and record.inhibit_reason is not None
            and record.lifecycle in {UnitLifecycle.OBSERVE_ONLY, UnitLifecycle.INHIBITED}
        ):
            healing.append("requalifying_after_inhibit")
        if record.cell_spread_v is not None and record.cell_spread_v > _CELL_BALANCING_SPREAD_V:
            healing.append("cell_balancing")
        measured = record.measured_watts
        # DESIGN_BATTERY_HEALTH_WATCH §3.1 (Wave 0, W0-1): the self-charge
        # float deadband.  The old exact-zero test flapped 158 transitions a
        # night at 96-99% SoC, where the pods float ACROSS zero; a cycle with
        # |measured| below the deadband renders NEITHER self-charging NOR
        # flap -- floating at the top is the steady state of a full pack.
        # Beyond the deadband the classification is unchanged: an in-band
        # float (e.g. +99 W CT-following) still renders self_healing, so the
        # deadband kills the zero-crossing flap, not the float visibility.
        if (
            measured is not None
            and abs(measured) >= float(settings.self_charge_deadband_w)
            and not record.claimed
        ):
            low, high = settings.expected_autonomy_band_w
            if low <= measured <= high:
                healing.append("autonomous_self_charge")
        if record.parked:
            # The composable park (DESIGN_POD_PARKING section 3): the healing
            # list is computed FIRST, then ("parked", *healing) -- a parked
            # pod balancing its cells keeps the balancing visibility.  The
            # terminal write_unverified posture names the operator resume;
            # expiry's hint names the operator act.
            reasons = ("parked", *healing)
            if record.park_write_unverified:
                reasons = ("park_write_unverified", *reasons)
                return HealthState.PARKED, reasons, PARK_WRITE_UNVERIFIED_HINT
            if record.park_expired:
                return HealthState.PARKED, reasons, PARK_EXPIRY_HINT
            return HealthState.PARKED, reasons, None
        if healing:
            return HealthState.SELF_HEALING, tuple(healing), None
        return HealthState.HEALTHY, (), None

    @staticmethod
    def _incoherence_hint(record: _UnitRecord) -> str | None:
        """The honest terminal guidance for a proven wedge (R5 rail)."""
        if record.echo_classification == ECHO_MATCHES_WRITE:
            # The transport and our write path are proven fine: the defect is
            # inside the pod and nothing remote remains.
            return PHYSICAL_RESTART_HINT
        if record.echo_classification == ECHO_OBJECTIVE_NOT_SERVED:
            return MODE_CHECK_HINT
        return None

    # --- audit and bus ----------------------------------------------------------------

    def _incoherent_payload(self, record: _UnitRecord) -> dict[str, Any]:
        movement = (
            None
            if record.measured_watts is None or record.baseline_watts is None
            else abs(record.measured_watts - record.baseline_watts)
        )
        return {
            "unit_id": record.unit_id,
            "cycles": record.streak,
            "authorized_watts": record.authorized_watts,
            "authorized_direction": record.authorized_direction,
            "measured_watts": record.measured_watts,
            "baseline_watts": record.baseline_watts,
            "movement_watts": movement,
        }

    async def _emit_actuation_incoherent(self, record: _UnitRecord) -> None:
        """Append the one-per-episode detection audit fact and bus event."""
        with contextlib.suppress(Exception):
            await self._audit.append(
                self._event(
                    event_type="actuation_incoherent",
                    unit_id=record.unit_id,
                    reason_codes=("authorized_not_actuating",),
                    result="detected",
                    record=record,
                    facts=self._incoherent_payload(record),
                )
            )
        await self._publish(
            {"type": "actuation.incoherent", "payload": self._incoherent_payload(record)}
        )

    def _signed_authorized(self, record: _UnitRecord) -> int:
        watts = int(record.authorized_watts or 0)
        return -watts if record.authorized_direction == "charge" else watts

    def _event(
        self,
        *,
        event_type: str,
        unit_id: str,
        reason_codes: tuple[str, ...],
        result: str,
        record: _UnitRecord,
        facts: Mapping[str, Any],
    ) -> AuditEvent:
        now_mono = float(self._clock.monotonic())
        wall = self._clock.wall_now()
        if not isinstance(wall, datetime) or wall.tzinfo is None:
            raise ValueError("clock wall time must be timezone-aware")
        return AuditEvent(
            event_id=f"recovery-{uuid.uuid4().hex}",
            occurred_at=wall.astimezone(UTC),
            monotonic_offset_s=now_mono - self._process_origin_mono,
            process_instance_id=self._process_instance_id,
            event_type=event_type,
            unit_id=unit_id,
            principal=_RECOVERY_PRINCIPAL,
            correlation_id=f"recovery:{event_type}:{unit_id}",
            policy_version=_RECOVERY_POLICY_VERSION,
            configuration_version=self._configuration_version,
            observation_sequences={},
            reason_codes=reason_codes,
            requested_active_w=0,
            authorized_active_w=self._signed_authorized(record),
            request_fingerprint=_fingerprint({"event_type": event_type, **dict(facts)}),
            response_fingerprint=_fingerprint({"result": result}),
            result=result,
            lifecycle=record.lifecycle,
        )

    async def _publish(self, body: Mapping[str, Any]) -> int:
        """Publish without ever letting observability gate control."""
        with contextlib.suppress(Exception):
            return await self._bus.publish(body)
        return 0
