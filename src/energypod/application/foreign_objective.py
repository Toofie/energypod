"""Night-writer detector: foreign-objective observation while we command nothing.

The 2026-08-23 census left the overnight window unverified (a 7.3 h audit gap)
and PRODUCT_NEXT §2 S2 queued this detector: zero-extra-frames periodic
sampling of the served PQ objective (PCS detail block ``0x1060+17/+18`` --
the arm preflight's own window, already inside the shipped read plan) while
the controller commands nothing, honest classification of what (if anything)
commands the batteries overnight, and a per-unit session record the night
window can finally be characterized from.

The honest center of the design (API_CONTRACTS "Night-writer detector"): the
served-objective signature alone CANNOT distinguish an external night charge
from the pod's own self-charge.  The night writers historically CHARGE --
negative objectives sitting INSIDE the commissioned autonomy band, right next
to the pods' own ~-520..-700 W daytime CT-following and ~-2.27 kW deep
self-charge -- and the site's own scheduled writer holds -2500 W on every
battery from 00:00 to 06:00.  So the detector records EVERY nonzero sample as
timestamped evidence and drives the alert tier from pattern rules on top:

- quiet: ``pod_autonomy_objective_observed`` (in-band float/self-charge),
  ``handback_grace`` (the residue of our own lapsed objective), and
  ``expected_nightly_charge`` (the site's own fleet-synchronized scheduled
  writer, commissioned as expected);
- alert: ``foreign_objective_observed`` with a reason -- a reactive component,
  an out-of-band objective, a sustained in-band objective while the PCS
  reports Remote-PQ mode, or a sustained beyond-class charge with no PV
  evidence -- ONE audit fact and ONE bus event per (episode, reason).

It is PASSIVE by construction: audit facts, bus events, and in-memory session
records only.  No write path, no latch, no block, no refusal originates here,
and the arm-time sole-writer preflight with its ``external_writer`` latch
remains the enforcement point, untouched.  Every failure inside the monitor
(a failing audit store, a failing bus, an unreadable observation) is fully
suppressed: a failed sample is a gap, never an alarm.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from energypod.domain.audit import AuditEvent
from energypod.domain.observations import UnitLifecycle

# The pinned classification vocabulary (API_CONTRACTS "Night-writer detector").
CLASS_POD_AUTONOMY = "pod_autonomy_objective_observed"
CLASS_FOREIGN = "foreign_objective_observed"
CLASS_HANDBACK = "handback_grace"
CLASS_EXPECTED_NIGHTLY = "expected_nightly_charge"

REASON_OUTSIDE_BAND = "outside_autonomy_band"
REASON_REACTIVE = "reactive_objective_observed"
REASON_REMOTE_MODE = "sustained_remote_mode_objective"
REASON_SUSTAINED_CHARGE = "sustained_charge_without_pv_evidence"

# The lifecycle states in which a unit is uncommanded by us and therefore
# sampleable.  ``boot`` has no steady telemetry yet, ``active`` means WE are
# the writer on the wire, and ``stopping``/``disconnected`` are transitional;
# an ``inhibited`` unit is by definition uncommanded and its overnight
# evidence matters most (the latched ``external_writer`` case).
_SAMPLEABLE_LIFECYCLES = frozenset(
    {
        UnitLifecycle.OBSERVE_ONLY.value,
        UnitLifecycle.DISARMED.value,
        UnitLifecycle.ARMED_IDLE.value,
        UnitLifecycle.INHIBITED.value,
    }
)

# The rolling session record's bounds: seven days of evidence per unit, at
# most 4096 samples each (the live cadence records roughly one sample per
# ~96-108 s cold-ring rotation, so the cap is what binds only at far faster
# cadences).  A restart resets the record -- the durable audit trail keeps
# the alerts.
_RETENTION_S = 7.0 * 24.0 * 3600.0
_MAX_SAMPLES_PER_UNIT = 4096

# The expected nightly charge's magnitude class: 0.8x..1.2x of the
# commissioned figure -- the scheduled -2500 W x N-unit charge plus whatever
# small regulation drift the served words carry.
_EXPECTED_TOLERANCE = 0.2

_PRINCIPAL = "energypod:foreign-objective"
_POLICY_VERSION = "foreign_objective"


def empty_objective_entry(unit_id: str) -> dict[str, Any]:
    """The per-unit characterization shape with nothing recorded yet.

    One implementation shared by the monitor's window payload and the
    facade's uncomposed degradation path, so the route's shape never depends
    on whether a detector is wired behind it.
    """
    return {
        "unit_id": unit_id,
        "first_seen_at": None,
        "last_seen_at": None,
        "sample_count": 0,
        "charge_sample_count": 0,
        "discharge_sample_count": 0,
        "min_active_w": None,
        "typical_active_w": None,
        "max_active_w": None,
        "classification_counts": {
            CLASS_POD_AUTONOMY: 0,
            CLASS_EXPECTED_NIGHTLY: 0,
            CLASS_HANDBACK: 0,
            CLASS_FOREIGN: 0,
        },
        "foreign_episode_count": 0,
        "foreign_active": False,
        "foreign_reason": None,
        "last_objective_observed": None,
    }


def _fingerprint(facts: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(facts), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _lifecycle_text(raw: Any) -> str:
    value = getattr(raw, "value", raw)
    return value if isinstance(value, str) else str(raw)


def _optional_word(raw: Any) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _optional_float(raw: Any) -> float | None:
    if isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw):
        return float(raw)
    return None


@dataclass(frozen=True, slots=True)
class ForeignObjectiveSettings:
    """The detector's commissioned knobs (defaulted ``policy`` keys).

    Detection only: no control path consumes any of these.  An absent policy
    block composes the pinned defaults so observe-only deployments detect
    with the same eyes.
    """

    sample_interval_s: float = 30.0
    sustained_samples: int = 3
    self_charge_class_w: int = 1000
    handback_grace_s: float = 12.0
    # The site's own scheduled night writer, commissioned as the EXPECTED
    # nightly charge (None = the strict posture: nothing is expected, every
    # sustained beyond-class charge without PV evidence escalates).
    expected_charge_w: int | None = None
    # How many units must hold the synchronized charge for the recognition:
    # the scheduler charges the batteries it chooses (observed live at
    # commissioning: lhs+mid together while a full rhs floated), so the
    # signature is a synchronized GROUP -- never a lone pod.
    expected_min_units: int = 2
    # The commissioned EXPECTED autonomy envelope (the live-write example's
    # documented value): -2600 deep self-charge to the +1000 recalibrated
    # evening CT-following edge (config rev 5, 2026-08-23).  This default is
    # LIVE on observe-only deployments (an absent policy block composes it
    # verbatim), so it must carry the commissioned value, never a stale one.
    expected_autonomy_band_w: tuple[int, int] = (-2600, 1000)

    def __post_init__(self) -> None:
        interval = self.sample_interval_s
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int | float)
            or not math.isfinite(float(interval))
            or not 1.0 <= float(interval) <= 3600.0
        ):
            raise ValueError("sample_interval_s must be between 1 and 3600 seconds")
        if type(self.sustained_samples) is not int or not 1 <= self.sustained_samples <= 100:
            raise ValueError("sustained_samples must be between 1 and 100")
        if type(self.self_charge_class_w) is not int or not 1 <= self.self_charge_class_w <= 50000:
            raise ValueError("self_charge_class_w must be between 1 and 50000 watts")
        grace = self.handback_grace_s
        if (
            isinstance(grace, bool)
            or not isinstance(grace, int | float)
            or not math.isfinite(float(grace))
            or not 1.0 <= float(grace) <= 300.0
        ):
            raise ValueError("handback_grace_s must be between 1 and 300 seconds")
        if self.expected_charge_w is not None and (
            type(self.expected_charge_w) is not int or not 1 <= self.expected_charge_w <= 50000
        ):
            raise ValueError("expected_charge_w must be between 1 and 50000 watts")
        if type(self.expected_min_units) is not int or not 2 <= self.expected_min_units <= 50:
            raise ValueError("expected_min_units must be between 2 and 50 units")
        low, high = self.expected_autonomy_band_w
        if not low < high or low > 0 or high < 0:
            raise ValueError(
                "expected_autonomy_band_w must be a strictly ascending pair spanning the "
                "pods' negative self-charge to small positive float region"
            )

    def sync_horizon_s(self) -> float:
        """How recent every OTHER unit's latest sample must be to corroborate
        the fleet-synchronized expected charge."""
        return max(5.0 * float(self.sample_interval_s), 300.0)

    def expected_class_band_w(self) -> tuple[int, int] | None:
        """The charge-magnitude window that recognizes the expected writer:
        0.8x..1.2x of the commissioned figure (negative side), intersected
        with the commissioned band."""
        expected = self.expected_charge_w
        if expected is None:
            return None
        low, high = self.expected_autonomy_band_w
        class_low = max(low, math.floor(-(1.0 + _EXPECTED_TOLERANCE) * expected))
        class_high = min(high, math.ceil(-(1.0 - _EXPECTED_TOLERANCE) * expected))
        if class_low > class_high:
            return None
        return (class_low, class_high)


@dataclass(frozen=True, slots=True)
class ObjectiveSample:
    """One recorded nonzero observation of the served objective."""

    observed_at_wall: datetime
    observed_at_mono: float
    active_w: int
    reactive_var: int
    classification: str
    reason: str | None
    lifecycle: str
    claimed: bool
    run_mode_w: int | None
    ctrl_mode_w: int | None
    work_mode_w: int | None
    debug_mode_w: int | None
    grid_power_w: float | None

    def payload(self) -> dict[str, Any]:
        """The FULL evidence record (the read endpoint's shape)."""
        return {
            "observed_at": self.observed_at_wall.isoformat(),
            "active_w": self.active_w,
            "reactive_var": self.reactive_var,
            "classification": self.classification,
            "reason": self.reason,
            "lifecycle": self.lifecycle,
            "claimed": self.claimed,
            "run_mode_w": self.run_mode_w,
            "ctrl_mode_w": self.ctrl_mode_w,
            "work_mode_w": self.work_mode_w,
            "debug_mode_w": self.debug_mode_w,
            "grid_power_w": self.grid_power_w,
        }

    def summary(self) -> dict[str, Any]:
        """The COMPACT five-key summary the snapshot and health views carry."""
        return {
            "observed_at": self.observed_at_wall.isoformat(),
            "active_w": self.active_w,
            "reactive_var": self.reactive_var,
            "classification": self.classification,
            "reason": self.reason,
        }


@dataclass(slots=True)
class _UnitRecord:
    """The per-unit session record and classifier state (no persistence)."""

    unit_id: str
    samples: deque[ObjectiveSample] = field(default_factory=deque)
    last_sample_mono: float | None = None
    last_capture_mono: float | None = None
    last_commanded_mono: float = -math.inf
    remote_streak: int = 0
    charge_streak: int = 0
    episode_open: bool = False
    episode_reason: str | None = None
    last_closed_reason: str | None = None


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def wall_now(self) -> datetime: ...


class AuditSink(Protocol):
    async def append(self, event: Any) -> None: ...


class EventPublisher(Protocol):
    async def publish(self, body: Mapping[str, Any]) -> int: ...


class ForeignObjectiveMonitor:
    """Per-unit foreign-objective detection over the supervised fleet cycle.

    Driven once per fleet cycle per unit by the supervision loop (after the
    polls, beside the recovery pass): the monitor reads each unit's FRESH
    observation -- the served-objective words and their capture clock -- plus
    the live claim state and the peeked authority, and samples when eligible.
    Every audit/bus failure inside is suppressed; the read surfaces degrade
    to explicit nulls instead of raising.
    """

    def __init__(
        self,
        *,
        unit_ids: frozenset[str],
        settings: ForeignObjectiveSettings,
        clock: Clock,
        audit: AuditSink,
        bus: EventPublisher,
        process_instance_id: str,
        process_origin_mono: float,
        configuration_version: int,
    ) -> None:
        if not unit_ids or any(
            not isinstance(unit_id, str) or not unit_id.strip() or unit_id != unit_id.strip()
            for unit_id in unit_ids
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
        # The session store keyed by unit (the read surfaces consume this
        # defensively: a corrupted entry reads as empty, never raises).
        self._samples: dict[str, deque[ObjectiveSample]] = {
            unit_id: record.samples for unit_id, record in self._records.items()
        }

    # --- supervision input -------------------------------------------------------

    async def observe_cycle(
        self,
        unit_id: str,
        *,
        lifecycle: Any,
        claimed: bool,
        authorized_watts: int,
        observation: Any,
        now_mono: float,
    ) -> None:
        """Fold one unit's cycle facts in and sample when eligible.

        The caller is the supervision loop, once per fleet cycle per unit:
        ``observation`` is the poll's fresh decode or ``None`` when the poll
        failed (a gap, never an alarm), ``claimed`` says whether a live
        intent names the unit, and ``authorized_watts`` is the authority the
        heartbeat just consumed.  Never raises for a failing observation --
        there is then simply nothing fresh to sample.
        """
        record = self._records.get(unit_id)
        if record is None:
            raise LookupError(f"no unit with id {unit_id!r}")
        lifecycle_text = _lifecycle_text(lifecycle)
        watts = authorized_watts if isinstance(authorized_watts, int) else 0
        if bool(claimed) or watts > 0 or lifecycle_text == UnitLifecycle.ACTIVE.value:
            # The handback-grace anchor: the last moment WE commanded this
            # unit.  Our own lapsed objective's residue (the watchdog clears
            # it in 4-8 s) must never classify foreign.
            record.last_commanded_mono = float(now_mono)
        if bool(claimed) or lifecycle_text not in _SAMPLEABLE_LIFECYCLES:
            return
        active_w = getattr(observation, "served_active_objective_w", None) if observation else None
        reactive_var = (
            getattr(observation, "served_reactive_objective_var", None) if observation else None
        )
        captured = (
            _optional_float(getattr(observation, "objective_captured_at_mono", None))
            if observation
            else None
        )
        active = _optional_word(active_w)
        reactive = _optional_word(reactive_var)
        if active is None or reactive is None or captured is None:
            return  # the block was never served: nothing fresh to sample
        if record.last_capture_mono is not None and captured <= record.last_capture_mono:
            return  # cached ride-along between cold-ring rotations
        record.last_capture_mono = captured
        if active == 0 and reactive == 0:
            # "Zero = nothing": no sample records, the streaks reset, and any
            # open foreign episode closes on the evidence that the objective
            # cleared.
            record.remote_streak = 0
            record.charge_streak = 0
            self._close_episode(record)
            return
        interval = float(self._settings.sample_interval_s)
        if record.last_sample_mono is not None and (now_mono - record.last_sample_mono) < interval:
            return
        record.last_sample_mono = float(now_mono)

        wall = getattr(observation, "wall_timestamp", None)
        if not isinstance(wall, datetime):
            wall = self._clock.wall_now()
        grid_power_w = _optional_float(getattr(observation, "grid_power_w", None))
        pv_evidence = grid_power_w is not None and grid_power_w > 0.0
        sample = ObjectiveSample(
            observed_at_wall=wall,
            observed_at_mono=float(now_mono),
            active_w=active,
            reactive_var=reactive,
            classification="",
            reason=None,
            lifecycle=lifecycle_text,
            claimed=False,
            run_mode_w=_optional_word(getattr(observation, "run_mode_w", None)),
            ctrl_mode_w=_optional_word(getattr(observation, "ctrl_mode_w", None)),
            work_mode_w=_optional_word(getattr(observation, "work_mode_w", None)),
            debug_mode_w=_optional_word(getattr(observation, "debug_mode_w", None)),
            grid_power_w=grid_power_w,
        )

        if (now_mono - record.last_commanded_mono) < float(self._settings.handback_grace_s):
            # Our own claim or authority ended inside the grace window: the
            # residue's provenance is ours, whatever its words.
            classification, reason = CLASS_HANDBACK, None
            record.remote_streak = 0
            record.charge_streak = 0
        elif reactive != 0:
            classification, reason = CLASS_FOREIGN, REASON_REACTIVE
            self._advance_streaks(record, sample, pv_evidence)
        else:
            low, high = self._settings.expected_autonomy_band_w
            if not low <= active <= high:
                classification, reason = CLASS_FOREIGN, REASON_OUTSIDE_BAND
                self._advance_streaks(record, sample, pv_evidence)
            elif self._is_expected_nightly(record, sample):
                classification, reason = CLASS_EXPECTED_NIGHTLY, None
                record.remote_streak = 0
                record.charge_streak = 0
            else:
                self._advance_streaks(record, sample, pv_evidence)
                if record.remote_streak >= self._settings.sustained_samples:
                    classification, reason = CLASS_FOREIGN, REASON_REMOTE_MODE
                elif record.charge_streak >= self._settings.sustained_samples:
                    classification, reason = CLASS_FOREIGN, REASON_SUSTAINED_CHARGE
                else:
                    classification, reason = CLASS_POD_AUTONOMY, None

        record.samples.append(
            ObjectiveSample(
                observed_at_wall=sample.observed_at_wall,
                observed_at_mono=sample.observed_at_mono,
                active_w=sample.active_w,
                reactive_var=sample.reactive_var,
                classification=classification,
                reason=reason,
                lifecycle=sample.lifecycle,
                claimed=sample.claimed,
                run_mode_w=sample.run_mode_w,
                ctrl_mode_w=sample.ctrl_mode_w,
                work_mode_w=sample.work_mode_w,
                debug_mode_w=sample.debug_mode_w,
                grid_power_w=sample.grid_power_w,
            )
        )
        self._prune(record)
        await self._settle_episode(record, classification, reason, pv_evidence)

    # --- read surfaces -----------------------------------------------------------

    def unit_last_observed(self, unit_id: str) -> dict[str, Any] | None:
        """The newest recorded sample's FULL evidence record, or None."""
        try:
            samples = self._samples.get(unit_id)
            if not samples:
                return None
            return samples[-1].payload()
        except Exception:
            return None

    def window_payload(self, *, last_hours: int) -> dict[str, Any]:
        """The night window's characterization over the requested hours.

        The window is WALL-time based (the operator asks in clock hours, and
        a restart resets the monotonic clock while the wall keeps counting);
        every failure degrades to explicit empties, never a raise.
        """
        window_s = float(last_hours) * 3600.0
        now_wall: datetime | None
        as_of: str | None
        try:
            now_wall = self._clock.wall_now()
            as_of = now_wall.isoformat()
            cutoff = now_wall - timedelta(seconds=window_s)
        except Exception:
            now_wall = None
            cutoff = datetime.min.replace(tzinfo=UTC)
            as_of = None
        spelling = f"{last_hours}h"
        try:
            units = [self._unit_entry(unit_id, cutoff) for unit_id in self._samples]
        except Exception:
            units = []
        return {"as_of": as_of, "last": spelling, "window_s": int(window_s), "units": units}

    # --- internals ------------------------------------------------------------------

    def _unit_entry(self, unit_id: str, cutoff: datetime) -> dict[str, Any]:
        samples = self._samples.get(unit_id)
        record = self._records.get(unit_id)
        in_window = [sample for sample in (samples or ()) if sample.observed_at_wall >= cutoff]
        if not in_window:
            return self._empty_entry(unit_id)
        actives = sorted(sample.active_w for sample in in_window)
        classifications = {
            CLASS_POD_AUTONOMY: 0,
            CLASS_EXPECTED_NIGHTLY: 0,
            CLASS_HANDBACK: 0,
            CLASS_FOREIGN: 0,
        }
        for sample in in_window:
            classifications[sample.classification] = (
                classifications.get(sample.classification, 0) + 1
            )
        episode_count = 0
        previous: ObjectiveSample | None = None
        for sample in samples or ():
            if sample.observed_at_wall < cutoff:
                previous = sample
                continue
            if sample.classification == CLASS_FOREIGN and not (
                previous is not None
                and previous.classification == CLASS_FOREIGN
                and previous.reason == sample.reason
            ):
                episode_count += 1
            previous = sample
        foreign_reason = None
        if record is not None:
            foreign_reason = record.episode_reason or record.last_closed_reason
        return {
            "unit_id": unit_id,
            "first_seen_at": in_window[0].observed_at_wall.isoformat(),
            "last_seen_at": in_window[-1].observed_at_wall.isoformat(),
            "sample_count": len(in_window),
            "charge_sample_count": sum(1 for sample in in_window if sample.active_w < 0),
            "discharge_sample_count": sum(1 for sample in in_window if sample.active_w > 0),
            "min_active_w": actives[0],
            "typical_active_w": actives[(len(actives) - 1) // 2],
            "max_active_w": actives[-1],
            "classification_counts": classifications,
            "foreign_episode_count": episode_count,
            "foreign_active": bool(record is not None and record.episode_open),
            "foreign_reason": foreign_reason,
            "last_objective_observed": in_window[-1].payload(),
        }

    @staticmethod
    def _empty_entry(unit_id: str) -> dict[str, Any]:
        return empty_objective_entry(unit_id)

    def _advance_streaks(
        self, record: _UnitRecord, sample: ObjectiveSample, pv_evidence: bool
    ) -> None:
        """Advance the pattern rules' consecutive-sample streaks."""
        if sample.active_w != 0 and sample.run_mode_w == 1:
            record.remote_streak += 1
        else:
            record.remote_streak = 0
        low, _high = self._settings.expected_autonomy_band_w
        if (
            sample.active_w <= -self._settings.self_charge_class_w
            and sample.active_w >= low
            and not pv_evidence
        ):
            record.charge_streak += 1
        else:
            record.charge_streak = 0

    def _is_expected_nightly(self, record: _UnitRecord, sample: ObjectiveSample) -> bool:
        """Whether this charge sample is the site's own scheduled writer.

        The commissioned figure, the magnitude class, and a SYNCHRONIZED
        GROUP: at least ``expected_min_units`` units (this one included) hold
        a charge inside the same expected class, each corroborated by its
        most recent recorded sample inside the sync horizon.  The pods' own
        self-charge is per-pod and PV-correlated; the scheduler starts
        identical charges on multiple batteries at the same moment -- and it
        charges the batteries it chooses, not necessarily every one, so a
        synchronized pair (or larger group) is the signature and a lone pod
        never qualifies.
        """
        band = self._settings.expected_class_band_w()
        if band is None:
            return False
        class_low, class_high = band
        if not class_low <= sample.active_w <= class_high:
            return False
        horizon = self._settings.sync_horizon_s()
        corroborating = 1  # this sample itself
        for other_id, other_samples in self._samples.items():
            if corroborating >= self._settings.expected_min_units:
                break
            if other_id == record.unit_id or not other_samples:
                continue
            latest = other_samples[-1]
            if sample.observed_at_mono - latest.observed_at_mono > horizon:
                continue
            if not class_low <= latest.active_w <= class_high:
                continue
            corroborating += 1
        return corroborating >= self._settings.expected_min_units

    def _prune(self, record: _UnitRecord) -> None:
        newest = record.samples[-1].observed_at_mono if record.samples else None
        while record.samples:
            oldest = record.samples[0]
            expired = newest is not None and oldest.observed_at_mono < newest - _RETENTION_S
            if not expired and len(record.samples) <= _MAX_SAMPLES_PER_UNIT:
                break
            record.samples.popleft()

    def _close_episode(self, record: _UnitRecord) -> None:
        if record.episode_open:
            record.last_closed_reason = record.episode_reason
        record.episode_open = False
        record.episode_reason = None

    async def _settle_episode(
        self,
        record: _UnitRecord,
        classification: str,
        reason: str | None,
        pv_evidence: bool,
    ) -> None:
        """One alert per (episode, reason); quiet evidence closes silently."""
        if classification != CLASS_FOREIGN:
            self._close_episode(record)
            return
        if record.episode_open and record.episode_reason == reason:
            return  # the episode persists: evidence only, no repeat alert
        reopened = record.episode_open
        record.episode_open = True
        record.episode_reason = reason
        sample = record.samples[-1]
        facts = {"unit_id": record.unit_id} | sample.payload() | {"pv_evidence": pv_evidence}
        del reopened  # ordering in the audit trail distinguishes a re-alert
        with contextlib.suppress(Exception):
            await self._audit.append(
                AuditEvent(
                    event_id=f"foreign-objective-{uuid.uuid4().hex}",
                    occurred_at=sample.observed_at_wall.astimezone(UTC),
                    monotonic_offset_s=sample.observed_at_mono - self._process_origin_mono,
                    process_instance_id=self._process_instance_id,
                    event_type=CLASS_FOREIGN,
                    unit_id=record.unit_id,
                    principal=_PRINCIPAL,
                    correlation_id=f"foreign_objective:{record.unit_id}",
                    policy_version=_POLICY_VERSION,
                    configuration_version=self._configuration_version,
                    observation_sequences={},
                    reason_codes=(reason or CLASS_FOREIGN,),
                    requested_active_w=0,
                    authorized_active_w=0,
                    request_fingerprint=_fingerprint(facts),
                    response_fingerprint=_fingerprint({"result": "observed"}),
                    result="observed",
                    lifecycle=UnitLifecycle(sample.lifecycle),
                )
            )
        with contextlib.suppress(Exception):
            await self._bus.publish({"type": "foreign_objective.observed", "payload": dict(facts)})
