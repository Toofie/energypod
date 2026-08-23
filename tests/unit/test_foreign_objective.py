"""Contract tests for the night-writer detector (foreign-objective observation).

``energypod.application.foreign_objective`` is the census's queued between-cycles
foreign-objective detector (2026-08-23 census; PRODUCT_NEXT §2 S2; API_CONTRACTS
"Night-writer detector"): zero-extra-frames periodic sampling of the served PQ
objective while the controller commands nothing, honest classification of what
(if anything) commands the batteries overnight, and a session record the night
window can finally be characterized from.

The doctrine under test, in one sentence: the served-objective signature alone
CANNOT distinguish an external night charge from the pod's own self-charge (the
night writers historically CHARGE -- negative objectives sitting INSIDE the
commissioned autonomy band), so the detector records EVERY nonzero sample as
timestamped evidence and drives the alert tier from pattern rules on top,
never crying wolf on the pods' normal autonomy.

The monitor is passive by construction: audit facts, bus events, and in-memory
session records only -- no write path, no latch, no block, no refusal, and the
arm-time ``external_writer`` latch remains the enforcement point.

The production module is imported lazily so this red-phase suite collects
cleanly; every missing contract surfaces as an ordinary test failure.
"""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

# The pinned contract vocabulary (API_CONTRACTS "Night-writer detector").
CLASS_POD_AUTONOMY = "pod_autonomy_objective_observed"
CLASS_FOREIGN = "foreign_objective_observed"
CLASS_HANDBACK = "handback_grace"
CLASS_EXPECTED_NIGHTLY = "expected_nightly_charge"
REASON_OUTSIDE_BAND = "outside_autonomy_band"
REASON_REACTIVE = "reactive_objective_observed"
REASON_REMOTE_MODE = "sustained_remote_mode_objective"
REASON_SUSTAINED_CHARGE = "sustained_charge_without_pv_evidence"


@dataclass
class ManualClock:
    now: float = 1000.0
    wall: datetime = field(default_factory=lambda: datetime(2026, 8, 26, 14, 0, 0, tzinfo=UTC))

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall


class RecordingAudit:
    def __init__(self) -> None:
        self.appended: list[Any] = []
        self.failing = False

    async def append(self, event: Any) -> None:
        if self.failing:
            raise OSError("audit store unavailable")
        self.appended.append(event)

    def of_type(self, event_type: str) -> list[Any]:
        return [event for event in self.appended if event.event_type == event_type]


class RecordingBus:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.failing = False

    async def publish(self, body: dict[str, Any]) -> int:
        if self.failing:
            raise OSError("event bus unavailable")
        self.published.append(dict(body))
        return len(self.published)

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [body for body in self.published if body.get("type") == event_type]


def make_observation(
    *,
    active_w: int | None = 0,
    reactive_var: int | None = 0,
    captured_at_mono: float | None = None,
    grid_power_w: float | None = None,
    run_mode_w: int | None = None,
    ctrl_mode_w: int | None = 1,
    work_mode_w: int | None = 6,
    debug_mode_w: int | None = 0,
    sequence: int = 1,
    wall: datetime | None = None,
) -> SimpleNamespace:
    resolved_wall = wall if wall is not None else datetime(2026, 8, 26, 14, 0, 0, tzinfo=UTC)
    return SimpleNamespace(
        served_active_objective_w=active_w,
        served_reactive_objective_var=reactive_var,
        objective_captured_at_mono=captured_at_mono,
        grid_power_w=grid_power_w,
        run_mode_w=run_mode_w,
        ctrl_mode_w=ctrl_mode_w,
        work_mode_w=work_mode_w,
        debug_mode_w=debug_mode_w,
        sequence=sequence,
        wall_timestamp=resolved_wall,
    )


@pytest.fixture
def api() -> Any:
    try:
        module = importlib.import_module("energypod.application.foreign_objective")
    except ImportError as error:
        pytest.fail(
            f"the night-writer detector contract is not implemented: {error}", pytrace=False
        )
    return module


def monitor(
    api: Any,
    *,
    units: tuple[str, ...] = ("mid",),
    interval_s: float = 30.0,
    sustained_samples: int = 3,
    self_charge_class_w: int = 1000,
    handback_grace_s: float = 12.0,
    expected_charge_w: int | None = None,
    band: tuple[int, int] = (-2600, 300),
    clock: ManualClock | None = None,
    audit: RecordingAudit | None = None,
    bus: RecordingBus | None = None,
) -> Any:
    resolved_clock = clock if clock is not None else ManualClock()
    return api.ForeignObjectiveMonitor(
        unit_ids=frozenset(units),
        settings=api.ForeignObjectiveSettings(
            sample_interval_s=interval_s,
            sustained_samples=sustained_samples,
            self_charge_class_w=self_charge_class_w,
            handback_grace_s=handback_grace_s,
            expected_charge_w=expected_charge_w,
            expected_autonomy_band_w=band,
        ),
        clock=resolved_clock,
        audit=audit if audit is not None else RecordingAudit(),
        bus=bus if bus is not None else RecordingBus(),
        process_instance_id="objective-test-process",
        process_origin_mono=resolved_clock.now,
        configuration_version=5,
    )


async def cycle(
    mon: Any,
    unit: str = "mid",
    *,
    active_w: int | None = 0,
    reactive_var: int | None = 0,
    captured_at_mono: float | None = None,
    grid_power_w: float | None = None,
    run_mode_w: int | None = None,
    ctrl_mode_w: int | None = 1,
    work_mode_w: int | None = 6,
    debug_mode_w: int | None = 0,
    lifecycle: str = "disarmed",
    claimed: bool = False,
    authorized_watts: int = 0,
    observation: Any = None,
    poll_failed: bool = False,
    now_mono: float | None = None,
) -> None:
    """Drive one supervised cycle exactly as the runtime fleet loop does.

    ``poll_failed`` models a cycle whose poll never landed (no observation at
    all); a non-None ``observation`` replaces the synthesized decode verbatim.
    """
    resolved = (
        None
        if poll_failed
        else (
            observation
            if observation is not None
            else make_observation(
                active_w=active_w,
                reactive_var=reactive_var,
                captured_at_mono=captured_at_mono,
                grid_power_w=grid_power_w,
                run_mode_w=run_mode_w,
                ctrl_mode_w=ctrl_mode_w,
                work_mode_w=work_mode_w,
                debug_mode_w=debug_mode_w,
            )
        )
    )
    clock = mon._clock
    clock.now = clock.now if now_mono is None else now_mono
    if resolved is not None and getattr(resolved, "wall_timestamp", None) is not None:
        # The synthesized decode carries the monitor's own wall clock so the
        # session record's timestamps follow the scripted timeline.
        resolved.wall_timestamp = clock.wall
    await mon.observe_cycle(
        unit,
        lifecycle=lifecycle,
        claimed=claimed,
        authorized_watts=authorized_watts,
        observation=resolved,
        now_mono=clock.now,
    )


def session_of(mon: Any, unit: str = "mid") -> Any:
    payload = mon.window_payload(last_hours=24)
    for entry in payload["units"]:
        if entry["unit_id"] == unit:
            return entry
    raise AssertionError(f"no session entry for {unit!r}: {payload!r}")


# --- settings: the pinned defaults and their bounds ------------------------------


def test_settings_defaults_are_the_pinned_contract_values(api: Any) -> None:
    settings = api.ForeignObjectiveSettings()
    assert settings.sample_interval_s == 30.0
    assert settings.sustained_samples == 3
    assert settings.self_charge_class_w == 1000
    assert settings.handback_grace_s == 12.0
    assert settings.expected_charge_w is None, "strict until the writer is commissioned"
    assert settings.expected_min_units == 2, "a synchronized pair, never a lone pod"
    # The commissioned rev-5 envelope (the live-write example's documented
    # value): this default is LIVE on observe-only deployments, which compose
    # it verbatim when no policy block is configured.
    assert settings.expected_autonomy_band_w == (-2600, 1000)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("sample_interval_s", 0.5),
        ("sample_interval_s", 3601.0),
        ("sample_interval_s", math.inf),
        ("sustained_samples", 0),
        ("sustained_samples", 101),
        ("self_charge_class_w", 0),
        ("self_charge_class_w", 50001),
        ("handback_grace_s", 0.0),
        ("handback_grace_s", 301.0),
        ("expected_charge_w", 0),
        ("expected_charge_w", 50001),
        ("expected_min_units", 1),
        ("expected_min_units", 51),
    ],
)
def test_settings_reject_out_of_bounds_knobs(api: Any, field_name: str, value: Any) -> None:
    with pytest.raises(ValueError, match=field_name):
        api.ForeignObjectiveSettings(**{field_name: value})


@pytest.mark.parametrize("band", [(300, -2600), (100, 200), (1, 300), (-2600, -1)])
def test_settings_reject_bands_that_do_not_bracket_the_self_charge_region(
    api: Any, band: tuple[int, int]
) -> None:
    with pytest.raises(ValueError, match="expected_autonomy_band_w"):
        api.ForeignObjectiveSettings(expected_autonomy_band_w=band)


def test_monitor_rejects_unknown_and_malformed_unit_ids(api: Any) -> None:
    with pytest.raises(ValueError, match="unit_ids"):
        monitor(api, units=())
    with pytest.raises(ValueError, match="unit_ids"):
        monitor(api, units=(" mid",))


# --- sampling eligibility ----------------------------------------------------------


async def test_a_zero_objective_records_nothing(api: Any) -> None:
    """``zero = nothing``: a served (0, 0) objective never becomes a sample,
    never alerts, and leaves the session record empty."""
    audit, bus = RecordingAudit(), RecordingBus()
    mon = monitor(api, audit=audit, bus=bus)
    await cycle(mon, active_w=0, reactive_var=0, captured_at_mono=1000.0)

    assert session_of(mon)["sample_count"] == 0
    assert mon.unit_last_observed("mid") is None
    assert audit.appended == []
    assert bus.published == []


async def test_the_interval_floor_governs_when_fresh_words_record(api: Any) -> None:
    """A fresh serving records only after the configured minimum spacing: the
    first sample lands, an earlier-than-floor serving does not, the next one
    after the floor does."""
    clock = ManualClock()
    mon = monitor(api, interval_s=30.0, clock=clock)
    await cycle(mon, active_w=-600, captured_at_mono=1000.0, now_mono=1000.0)
    clock.now = 1010.0
    await cycle(mon, active_w=-620, captured_at_mono=1010.0, now_mono=1010.0)
    clock.now = 1031.0
    await cycle(mon, active_w=-640, captured_at_mono=1031.0, now_mono=1031.0)

    payload = mon.window_payload(last_hours=24)
    entry = session_of(mon)
    assert entry["sample_count"] == 2, payload
    assert mon.unit_last_observed("mid")["active_w"] == -640


async def test_stale_cached_words_never_record_a_second_time(api: Any) -> None:
    """The live plan serves the detail block once per cold-ring rotation and
    the merged decode carries the CACHED words between rotations: only a
    capture time that ADVANCED is a fresh sample."""
    clock = ManualClock()
    mon = monitor(api, interval_s=1.0, clock=clock)
    await cycle(mon, active_w=-600, captured_at_mono=1000.0, now_mono=1000.0)
    for now in (1002.0, 1004.0, 1006.0):
        clock.now = now
        await cycle(mon, active_w=-600, captured_at_mono=1000.0, now_mono=now)

    assert session_of(mon)["sample_count"] == 1


async def test_absent_words_are_a_gap_never_an_alarm(api: Any) -> None:
    """A poll that did not serve the block leaves the words null: no sample,
    no classification, no alert -- the honest gap."""
    audit, bus = RecordingAudit(), RecordingBus()
    mon = monitor(api, audit=audit, bus=bus)
    await cycle(mon, active_w=None, reactive_var=None, captured_at_mono=None)

    assert session_of(mon)["sample_count"] == 0
    assert audit.appended == [] and bus.published == []


async def test_a_failed_poll_contributes_nothing_and_keeps_the_episode(api: Any) -> None:
    """A cycle whose poll failed passes no observation: no sample (a gap), and
    an OPEN foreign episode is not silently closed by the missing evidence."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0, now_mono=1000.0)
    assert audit.of_type(CLASS_FOREIGN), "the out-of-band objective must alert"
    clock.now = 1040.0
    await cycle(mon, poll_failed=True, now_mono=1040.0)

    assert session_of(mon)["foreign_active"] is True, "a failed poll closes nothing"
    assert len(audit.of_type(CLASS_FOREIGN)) == 1


@pytest.mark.parametrize("lifecycle", ["observe_only", "disarmed", "armed_idle", "inhibited"])
async def test_uncommanded_lifecycles_sample(api: Any, lifecycle: str) -> None:
    mon = monitor(api)
    await cycle(mon, active_w=-600, captured_at_mono=1000.0, lifecycle=lifecycle)

    assert session_of(mon)["sample_count"] == 1


@pytest.mark.parametrize("lifecycle", ["boot", "active", "stopping", "disconnected"])
async def test_commanded_or_transitional_lifecycles_never_sample(api: Any, lifecycle: str) -> None:
    mon = monitor(api)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0, lifecycle=lifecycle)

    assert session_of(mon)["sample_count"] == 0
    assert mon.unit_last_observed("mid") is None


async def test_a_unit_claimed_by_a_live_intent_is_never_sampled(api: Any) -> None:
    """While our own intent claims the unit we are the writer on the wire:
    no sample, no classification, whatever the words hold."""
    audit, bus = RecordingAudit(), RecordingBus()
    mon = monitor(api, audit=audit, bus=bus)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0, claimed=True, authorized_watts=1000)

    assert session_of(mon)["sample_count"] == 0
    assert audit.appended == [] and bus.published == []


# --- the handback grace (our own lapsed objective is never foreign) ---------------


async def test_our_own_lapsed_objective_records_handback_grace_quiet(api: Any) -> None:
    """THE self-observation case: our intent lapses, the watchdog clears the
    objective in 4-8 s, and a sample inside the grace window classifies
    ``handback_grace`` -- recorded evidence, never foreign, even beyond band."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock, handback_grace_s=12.0)
    # Our command runs: the unit is claimed and authorized.
    await cycle(mon, claimed=True, authorized_watts=2000, lifecycle="active", now_mono=1000.0)
    # The intent lapses; the residue (+2000, out of band) is still on the wire.
    clock.now = 1006.0
    await cycle(mon, active_w=2000, captured_at_mono=1006.0, now_mono=1006.0)

    entry = session_of(mon)
    assert entry["sample_count"] == 1
    assert entry["last_objective_observed"]["classification"] == CLASS_HANDBACK
    assert audit.appended == [] and bus.published == [], "grace never alerts"


async def test_after_the_grace_the_same_residue_classifies_foreign(api: Any) -> None:
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock, handback_grace_s=12.0)
    await cycle(mon, claimed=True, authorized_watts=2000, now_mono=1000.0)
    clock.now = 1030.0
    await cycle(mon, active_w=2000, captured_at_mono=1030.0, now_mono=1030.0)

    assert len(audit.of_type(CLASS_FOREIGN)) == 1


async def test_grace_samples_reset_the_pattern_streaks(api: Any) -> None:
    """A grace sample must not accumulate toward a pattern escalation: the
    provenance is ours, so the streaks restart from zero."""
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(api, audit=audit, sustained_samples=2, clock=clock)
    await cycle(mon, claimed=True, authorized_watts=1500, now_mono=1000.0)
    # Grace sample one with a remote-mode word set (our own write does that).
    clock.now = 1002.0
    await cycle(
        mon,
        active_w=-1200,
        captured_at_mono=1002.0,
        run_mode_w=1,
        grid_power_w=-300.0,
        now_mono=1002.0,
    )
    # Well after the grace: two more qualifying samples are needed, not one.
    clock.now = 1040.0
    await cycle(
        mon,
        active_w=-1200,
        captured_at_mono=1040.0,
        run_mode_w=1,
        grid_power_w=-300.0,
        now_mono=1040.0,
    )
    assert audit.of_type(CLASS_FOREIGN) == []
    clock.now = 1080.0
    await cycle(
        mon,
        active_w=-1200,
        captured_at_mono=1080.0,
        run_mode_w=1,
        grid_power_w=-300.0,
        now_mono=1080.0,
    )
    assert len(audit.of_type(CLASS_FOREIGN)) == 1


# --- the signature tier ------------------------------------------------------------


async def test_in_band_self_charge_is_quiet_evidence(api: Any) -> None:
    """The pods' own daytime self-charge (~-520..-700 W) sits inside the band:
    recorded as ``pod_autonomy_objective_observed`` with no audit and no bus --
    the detector never cries wolf on normal autonomy."""
    audit, bus = RecordingAudit(), RecordingBus()
    mon = monitor(api, audit=audit, bus=bus)
    await cycle(mon, active_w=-637, captured_at_mono=1000.0)

    entry = session_of(mon)
    assert entry["last_objective_observed"]["classification"] == CLASS_POD_AUTONOMY
    assert entry["last_objective_observed"]["reason"] is None
    assert audit.appended == [] and bus.published == []


async def test_in_band_positive_float_is_quiet_evidence(api: Any) -> None:
    mon = monitor(api)
    await cycle(mon, active_w=250, captured_at_mono=1000.0)

    assert session_of(mon)["last_objective_observed"]["classification"] == CLASS_POD_AUTONOMY


async def test_out_of_band_charge_alerts_on_the_first_sample(api: Any) -> None:
    """Beyond the commissioned band the signature is enough (the same doctrine
    as the arm preflight): one audit fact plus one bus event, immediately."""
    audit, bus = RecordingAudit(), RecordingBus()
    mon = monitor(api, audit=audit, bus=bus)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0)

    (event,) = audit.of_type(CLASS_FOREIGN)
    assert event.unit_id == "mid"
    assert REASON_OUTSIDE_BAND in event.reason_codes
    (published,) = bus.of_type("foreign_objective.observed")
    assert published["payload"]["reason"] == REASON_OUTSIDE_BAND
    assert published["payload"]["active_w"] == -3000


async def test_out_of_band_discharge_alerts_on_the_first_sample(api: Any) -> None:
    audit = RecordingAudit()
    mon = monitor(api, audit=audit)
    await cycle(mon, active_w=2000, captured_at_mono=1000.0)

    assert len(audit.of_type(CLASS_FOREIGN)) == 1


async def test_a_reactive_component_is_foreign_whatever_the_active_word(api: Any) -> None:
    """The pods' own signature carries Q == 0 (ADD-1): any reactive objective
    is not pod autonomy, with or without an active component.  Both shapes
    classify foreign; the second is the SAME reason inside one open episode,
    so the alert fires once and the evidence carries both."""
    audit = RecordingAudit()
    mon = monitor(api, audit=audit)
    await cycle(mon, active_w=0, reactive_var=100, captured_at_mono=1000.0)
    clock = mon._clock
    clock.now = 1040.0
    await cycle(mon, active_w=-600, reactive_var=-50, captured_at_mono=1040.0)

    (event,) = audit.of_type(CLASS_FOREIGN)
    assert event.reason_codes == (REASON_REACTIVE,)
    last = mon.unit_last_observed("mid")
    assert last["classification"] == CLASS_FOREIGN
    assert last["reason"] == REASON_REACTIVE
    assert last["active_w"] == -600 and last["reactive_var"] == -50


# --- alert episode semantics --------------------------------------------------------


async def test_a_sustained_foreign_objective_alerts_exactly_once_per_episode(api: Any) -> None:
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)
    for index, now in enumerate((1000.0, 1040.0, 1080.0, 1120.0)):
        await cycle(mon, active_w=-3000, captured_at_mono=now, now_mono=now)
        assert index >= 0

    assert len(audit.of_type(CLASS_FOREIGN)) == 1, "evidence every sample, one alert"
    assert len(bus.of_type("foreign_objective.observed")) == 1
    assert session_of(mon)["sample_count"] == 4


async def test_a_quiet_sample_closes_the_episode_and_a_reopen_re_alerts(api: Any) -> None:
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0, now_mono=1000.0)
    clock.now = 1040.0
    await cycle(mon, active_w=0, captured_at_mono=1040.0, now_mono=1040.0)
    assert session_of(mon)["foreign_active"] is False, "the zero closed the episode"
    clock.now = 1080.0
    await cycle(mon, active_w=-3000, captured_at_mono=1080.0, now_mono=1080.0)

    assert len(audit.of_type(CLASS_FOREIGN)) == 2, "the reopened episode alerts again"


async def test_a_reason_change_inside_an_episode_re_alerts(api: Any) -> None:
    """A materially different signature (the charge writer flips to a reactive
    objective) is a new alert even though the episode never went quiet."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0, now_mono=1000.0)
    clock.now = 1040.0
    await cycle(mon, active_w=0, reactive_var=200, captured_at_mono=1040.0, now_mono=1040.0)

    events = audit.of_type(CLASS_FOREIGN)
    assert len(events) == 2
    assert events[0].reason_codes == (REASON_OUTSIDE_BAND,)
    assert events[1].reason_codes == (REASON_REACTIVE,)
    assert len(bus.of_type("foreign_objective.observed")) == 2


async def test_an_open_episode_closes_on_a_grace_sample(api: Any) -> None:
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(api, audit=audit, clock=clock, handback_grace_s=60.0)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0, now_mono=1000.0)
    assert session_of(mon)["foreign_active"] is True
    # We take the unit over (claimed), then our own command lapses into grace.
    clock.now = 1010.0
    await cycle(mon, claimed=True, authorized_watts=500, now_mono=1010.0)
    clock.now = 1045.0
    await cycle(mon, active_w=-3000, captured_at_mono=1045.0, now_mono=1045.0)

    assert session_of(mon)["foreign_active"] is False
    assert len(audit.of_type(CLASS_FOREIGN)) == 1, "no second alert inside grace"


# --- the pattern tier ----------------------------------------------------------------


async def test_sustained_in_band_charge_without_pv_evidence_escalates(api: Any) -> None:
    """THE night-writer case: an external -2400 W charge sits INSIDE the band
    (the pods' deep self-charge shares the sign and the magnitude class), so
    only the sustained pattern -- beyond the observed self-charge class, with
    the site importing (no PV evidence) -- escalates, on the Nth sample."""
    audit, bus = RecordingAudit(), RecordingBus()
    mon = monitor(api, audit=audit, bus=bus, sustained_samples=3, self_charge_class_w=1000)
    for now in (1000.0, 1040.0, 1080.0):
        await cycle(
            mon,
            active_w=-2400,
            captured_at_mono=now,
            grid_power_w=-1500.0,
            now_mono=now,
        )
        if now < 1080.0:
            assert audit.of_type(CLASS_FOREIGN) == [], f"no alert before the streak ({now})"

    (event,) = audit.of_type(CLASS_FOREIGN)
    assert REASON_SUSTAINED_CHARGE in event.reason_codes
    (published,) = bus.of_type("foreign_objective.observed")
    assert published["payload"]["pv_evidence"] is False


async def test_pv_evidence_keeps_a_deep_charge_quiet(api: Any) -> None:
    """The once-observed -2.27 kW deep self-charge happened BY DAY with the
    site exporting: PV evidence on a sample breaks the streak, so the pod's
    own deepest charging never escalates."""
    audit = RecordingAudit()
    mon = monitor(api, audit=audit, sustained_samples=3, self_charge_class_w=1000)
    await cycle(mon, active_w=-2270, captured_at_mono=1000.0, grid_power_w=900.0, now_mono=1000.0)
    for now in (1040.0, 1080.0, 1120.0):
        await cycle(mon, active_w=-2270, captured_at_mono=now, grid_power_w=900.0, now_mono=now)

    assert audit.of_type(CLASS_FOREIGN) == []
    assert session_of(mon)["sample_count"] == 4, "the evidence still records"


async def test_charge_below_the_self_charge_class_never_escalates(api: Any) -> None:
    audit = RecordingAudit()
    mon = monitor(api, audit=audit, sustained_samples=2, self_charge_class_w=1000)
    for now in (1000.0, 1040.0, 1080.0):
        await cycle(mon, active_w=-800, captured_at_mono=now, grid_power_w=-100.0, now_mono=now)

    assert audit.of_type(CLASS_FOREIGN) == []


async def test_a_non_qualifying_sample_resets_the_charge_streak(api: Any) -> None:
    clock = ManualClock()
    audit = RecordingAudit()
    mon = monitor(api, audit=audit, clock=clock, sustained_samples=3, self_charge_class_w=1000)
    await cycle(mon, active_w=-2400, captured_at_mono=1000.0, grid_power_w=-100.0, now_mono=1000.0)
    clock.now = 1040.0
    await cycle(mon, active_w=-500, captured_at_mono=1040.0, grid_power_w=-100.0, now_mono=1040.0)
    clock.now = 1080.0
    await cycle(mon, active_w=-2400, captured_at_mono=1080.0, grid_power_w=-100.0, now_mono=1080.0)
    clock.now = 1120.0
    await cycle(mon, active_w=-2400, captured_at_mono=1120.0, grid_power_w=-100.0, now_mono=1120.0)

    assert audit.of_type(CLASS_FOREIGN) == [], "only two qualifying samples after a reset"
    clock.now = 1160.0
    await cycle(mon, active_w=-2400, captured_at_mono=1160.0, grid_power_w=-100.0, now_mono=1160.0)
    assert len(audit.of_type(CLASS_FOREIGN)) == 1


async def test_sustained_remote_mode_objective_escalates_in_band(api: Any) -> None:
    """The run-mode discriminator: the PCS's own word says "Remote PQ Power"
    (the written-objective state) while no intent of ours claims the unit --
    sustained, that is a foreign writer whatever the sign or the magnitude."""
    audit, bus = RecordingAudit(), RecordingBus()
    mon = monitor(api, audit=audit, bus=bus, sustained_samples=3)
    for now in (1000.0, 1040.0, 1080.0):
        await cycle(
            mon,
            active_w=-2400,
            captured_at_mono=now,
            run_mode_w=1,
            grid_power_w=-100.0,
            now_mono=now,
        )

    (event,) = audit.of_type(CLASS_FOREIGN)
    assert REASON_REMOTE_MODE in event.reason_codes


async def test_matching_load_autonomy_never_escalates_by_the_remote_rule(api: Any) -> None:
    """Live evidence 2026-08-23: lhs held +695..914 W uncommanded after dark
    in run mode 0 ("Matching Load") -- the pod's own load-following.  Run mode
    0 (or absent) keeps an in-band objective quiet however long it holds."""
    audit = RecordingAudit()
    mon = monitor(api, audit=audit, sustained_samples=3, band=(-2600, 1000))
    for now in (1000.0, 1040.0, 1080.0, 1120.0):
        await cycle(
            mon,
            active_w=870,
            captured_at_mono=now,
            run_mode_w=0,
            grid_power_w=-200.0,
            now_mono=now,
        )

    assert audit.of_type(CLASS_FOREIGN) == []
    assert session_of(mon)["sample_count"] == 4


async def test_a_remote_mode_break_resets_the_remote_streak(api: Any) -> None:
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(api, audit=audit, sustained_samples=3)
    await cycle(mon, active_w=-600, captured_at_mono=1000.0, run_mode_w=1, now_mono=1000.0)
    clock.now = 1040.0
    await cycle(mon, active_w=-600, captured_at_mono=1040.0, run_mode_w=0, now_mono=1040.0)
    clock.now = 1080.0
    await cycle(mon, active_w=-600, captured_at_mono=1080.0, run_mode_w=1, now_mono=1080.0)

    assert audit.of_type(CLASS_FOREIGN) == [], "streak of one after the break"


# --- the expected nightly charge (the site's own scheduled writer) ------------------


async def test_the_synchronized_nightly_charge_is_expected_and_quiet(api: Any) -> None:
    """THE known writer: the site's Docker solution charges ALL THREE
    batteries at -2500 W from 00:00 to 06:00.  A FLEET-SYNCHRONIZED in-band
    charge inside the commissioned magnitude class classifies quiet as
    ``expected_nightly_charge`` -- characterized evidence, never the alert
    tier -- however long it holds and whatever the run-mode word says (the
    scheduler's own writes put the PCS into Remote PQ mode like any
    writer's)."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(
        api,
        units=("lhs", "mid", "rhs"),
        audit=audit,
        bus=bus,
        clock=clock,
        interval_s=1.0,
        sustained_samples=3,
        expected_charge_w=2500,
    )
    for minute in range(8):
        now = 1000.0 + minute
        clock.now = now
        for unit in ("lhs", "mid", "rhs"):
            await cycle(
                mon,
                unit,
                active_w=-2500,
                captured_at_mono=now,
                grid_power_w=-1500.0,
                run_mode_w=1,
                now_mono=now,
            )

    for unit in ("lhs", "mid", "rhs"):
        entry = session_of(mon, unit)
        assert entry["last_objective_observed"]["classification"] == CLASS_EXPECTED_NIGHTLY
        # The very first sample of the first-sampled unit cannot corroborate
        # a fleet pattern (nobody else has sampled yet) and stays plain
        # autonomy evidence; from the first synchronized round on, every
        # sample is the expected writer.
        assert entry["classification_counts"][CLASS_EXPECTED_NIGHTLY] >= 7, unit
        assert entry["classification_counts"][CLASS_FOREIGN] == 0, unit
        assert entry["foreign_active"] is False, unit
    assert audit.appended == [] and bus.published == []


async def test_an_expected_class_charge_without_fleet_sync_still_escalates(api: Any) -> None:
    """Synchronization is the discriminator: the same -2400 W charge on ONE
    battery is the wrong signature for the known writer (the scheduler starts
    all three at once), so the sustained-charge rule escalates normally."""
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(
        api,
        units=("lhs", "mid", "rhs"),
        audit=audit,
        clock=clock,
        interval_s=1.0,
        sustained_samples=3,
        expected_charge_w=2500,
    )
    for now in (1000.0, 1001.0, 1002.0):
        clock.now = now
        await cycle(
            mon,
            "mid",
            active_w=-2400,
            captured_at_mono=now,
            grid_power_w=-1500.0,
            run_mode_w=None,
            now_mono=now,
        )

    (event,) = audit.of_type(CLASS_FOREIGN)
    assert event.unit_id == "mid"
    assert REASON_SUSTAINED_CHARGE in event.reason_codes


async def test_an_off_magnitude_charge_is_not_the_expected_writer(api: Any) -> None:
    """A synchronized charge OUTSIDE the 0.8x..1.2x magnitude class of the
    commissioned figure is not the site's scheduler: it escalates."""
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(
        api,
        units=("lhs", "mid", "rhs"),
        audit=audit,
        clock=clock,
        interval_s=1.0,
        sustained_samples=3,
        expected_charge_w=2500,
    )
    for now in (1000.0, 1001.0, 1002.0):
        clock.now = now
        for unit in ("lhs", "mid", "rhs"):
            await cycle(
                mon,
                unit,
                active_w=-1500,
                captured_at_mono=now,
                grid_power_w=-1500.0,
                run_mode_w=None,
                now_mono=now,
            )

    assert len(audit.of_type(CLASS_FOREIGN)) == 3, "every unit's off-magnitude charge"


async def test_without_a_commissioned_expectation_every_sustained_charge_escalates(
    api: Any,
) -> None:
    """The strict default: until the operator commissions the expected writer
    (``foreign_objective_expected_charge_w``), a synchronized nightly charge
    is simply a sustained beyond-class charge without PV evidence."""
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(
        api,
        units=("lhs", "mid", "rhs"),
        audit=audit,
        clock=clock,
        interval_s=1.0,
        sustained_samples=3,
        expected_charge_w=None,
    )
    for now in (1000.0, 1001.0, 1002.0):
        clock.now = now
        for unit in ("lhs", "mid", "rhs"):
            await cycle(
                mon,
                unit,
                active_w=-2500,
                captured_at_mono=now,
                grid_power_w=-1500.0,
                run_mode_w=None,
                now_mono=now,
            )

    assert len(audit.of_type(CLASS_FOREIGN)) == 3


async def test_the_sync_horizon_expires_and_the_expectation_lapses(api: Any) -> None:
    """When fewer than the commissioned minimum of units corroborate inside
    the sync horizon, the expectation lapses and the sustained-charge rule
    takes over: a PAIR still corroborates (the known writer's footprint), a
    LONE charge escalates (interval 1 s -> horizon 300 s)."""
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(
        api,
        units=("lhs", "mid", "rhs"),
        audit=audit,
        clock=clock,
        interval_s=1.0,
        sustained_samples=3,
        expected_charge_w=2500,
    )
    # The fleet holds the expected charge together.
    for now in (1000.0, 1001.0, 1002.0):
        clock.now = now
        for unit in ("lhs", "mid", "rhs"):
            await cycle(mon, unit, active_w=-2500, captured_at_mono=now, grid_power_w=-100.0)
    assert audit.appended == []
    # lhs's writer stops (its words go zero -- nothing records); mid/rhs keep
    # charging as the synchronized pair: still the known writer's footprint.
    for now in (1400.0, 1401.0, 1402.0, 1403.0):
        clock.now = now
        await cycle(mon, "lhs", active_w=0, captured_at_mono=now)
        for unit in ("mid", "rhs"):
            await cycle(mon, unit, active_w=-2500, captured_at_mono=now, grid_power_w=-100.0)
    assert audit.appended == [], "a synchronized pair is the expected writer"

    # rhs's writer stops too: mid is now alone and its last corroboration
    # (rhs's 1404.0 sample) ages past the horizon -- the expectation lapses
    # and the sustained-charge rule escalates on mid's next streak.
    for index, now in enumerate((1800.0, 1801.0, 1802.0, 1803.0)):
        clock.now = now
        await cycle(mon, "rhs", active_w=0, captured_at_mono=now)
        await cycle(mon, "mid", active_w=-2500, captured_at_mono=now, grid_power_w=-100.0)
        if index < 2:
            assert audit.appended == [], f"inside the horizon ({now})"

    escalated = {event.unit_id for event in audit.of_type(CLASS_FOREIGN)}
    assert escalated == {"mid"}, "the lone uncorroborated charge escalated"


async def test_a_synchronized_pair_is_expected_while_a_third_floats(api: Any) -> None:
    """THE commissioning-night shape, observed live 2026-08-24 00:00 AEST:
    the scheduler charges lhs and mid together at -2500 W while a full rhs
    floats with zero words.  The synchronized PAIR is the known writer's
    signature -- quiet expected evidence on both charging units, no matter
    that a third configured unit never joins."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(
        api,
        units=("lhs", "mid", "rhs"),
        audit=audit,
        bus=bus,
        clock=clock,
        interval_s=1.0,
        sustained_samples=3,
        expected_charge_w=2500,
    )
    for minute in range(10):
        now = 1000.0 + minute
        clock.now = now
        for unit in ("lhs", "mid"):
            await cycle(mon, unit, active_w=-2500, captured_at_mono=now, grid_power_w=-1500.0)
        await cycle(mon, "rhs", active_w=0, captured_at_mono=now)

    for unit in ("lhs", "mid"):
        entry = session_of(mon, unit)
        counts = entry["classification_counts"]
        # The very first sample of the first-sampled unit cannot corroborate
        # a group yet; every sample after that is the expected writer.
        assert counts[CLASS_EXPECTED_NIGHTLY] >= 9, (unit, entry)
        assert counts[CLASS_FOREIGN] == 0, (unit, entry)
        assert entry["foreign_active"] is False, (unit, entry)
    assert session_of(mon, "rhs")["sample_count"] == 0
    assert audit.appended == [] and bus.published == []


# --- the session record and its read surfaces ---------------------------------------


async def test_the_session_record_accumulates_the_characterization(api: Any) -> None:
    """The night window's characterization: first/last seen, sample counts,
    the signed min/typ/max (typical = the LOWER median), and the sign split."""
    clock = ManualClock()
    mon = monitor(api, clock=clock, interval_s=1.0)
    wall = datetime(2026, 8, 26, 22, 30, 0, tzinfo=UTC)
    for index, active in enumerate((-600, -2400, -2400, -2300, 0, -500)):
        clock.wall = wall.replace(minute=30 + index)
        clock.now = 1000.0 + index
        await cycle(
            mon,
            active_w=active,
            captured_at_mono=clock.now,
            grid_power_w=-100.0,
            now_mono=clock.now,
        )

    entry = session_of(mon)
    assert entry["sample_count"] == 5, "the zero sample records nothing"
    assert entry["charge_sample_count"] == 5
    assert entry["discharge_sample_count"] == 0
    assert entry["min_active_w"] == -2400
    assert entry["max_active_w"] == -500
    assert entry["typical_active_w"] == -2300, "lower median of (-600,-2400,-2400,-2300,-500)"
    assert entry["first_seen_at"].startswith("2026-08-26T22:30")
    assert entry["last_seen_at"].startswith("2026-08-26T22:35")
    # The three consecutive beyond-class charges (-2400, -2400, -2300) opened
    # one foreign episode; the closing -500 quiet sample closed it again.
    assert entry["foreign_episode_count"] == 1
    assert entry["foreign_active"] is False
    assert entry["classification_counts"] == {
        CLASS_POD_AUTONOMY: 4,
        CLASS_EXPECTED_NIGHTLY: 0,
        CLASS_HANDBACK: 0,
        CLASS_FOREIGN: 1,
    }
    assert entry["last_objective_observed"]["active_w"] == -500


async def test_the_window_filter_recharacterizes_only_the_requested_hours(api: Any) -> None:
    clock = ManualClock()
    mon = monitor(api, clock=clock, interval_s=1.0)
    start = datetime(2026, 8, 26, 20, 0, 0, tzinfo=UTC)
    for index in range(6):
        clock.wall = start.replace(minute=index * 10)
        clock.now = 1000.0 + index
        await cycle(mon, active_w=-100 * (index + 1), captured_at_mono=clock.now)
    # Three hours later in wall time, two more samples land.
    clock.wall = start.replace(hour=23)
    for offset, active in enumerate((-900, -950)):
        clock.now = 2000.0 + offset
        await cycle(mon, active_w=active, captured_at_mono=clock.now)

    full = session_of(mon)
    assert full["sample_count"] == 8
    recent = mon.window_payload(last_hours=1)["units"][0]
    assert recent["sample_count"] == 2
    assert recent["min_active_w"] == -950
    assert recent["first_seen_at"].startswith("2026-08-26T23:00")


async def test_the_retention_cap_drops_the_oldest_samples(api: Any) -> None:
    clock = ManualClock()
    mon = monitor(api, clock=clock, interval_s=1.0)
    for index in range(4100):
        clock.now = 1000.0 + index
        await cycle(mon, active_w=-index, captured_at_mono=clock.now)

    entry = session_of(mon)
    assert entry["sample_count"] == 4096, "the per-unit retention cap"
    assert entry["min_active_w"] == -4099, "the oldest samples were dropped"
    assert mon.unit_last_observed("mid")["active_w"] == -4099


async def test_every_recorded_sample_carries_the_evidence_fields(api: Any) -> None:
    """Evidence first: each nonzero sample is timestamped with the words, our
    lifecycle/claim state, the mode words, and the CT grid figure."""
    clock = ManualClock()
    mon = monitor(api, clock=clock)
    await cycle(
        mon,
        active_w=-640,
        reactive_var=0,
        captured_at_mono=1000.0,
        run_mode_w=0,
        ctrl_mode_w=1,
        work_mode_w=6,
        debug_mode_w=0,
        grid_power_w=-320.0,
        now_mono=1000.0,
    )

    last = mon.unit_last_observed("mid")
    assert set(last) == {
        "observed_at",
        "active_w",
        "reactive_var",
        "classification",
        "reason",
        "lifecycle",
        "claimed",
        "run_mode_w",
        "ctrl_mode_w",
        "work_mode_w",
        "debug_mode_w",
        "grid_power_w",
    }
    assert last["classification"] == CLASS_POD_AUTONOMY
    assert last["lifecycle"] == "disarmed"
    assert last["claimed"] is False
    assert last["grid_power_w"] == -320.0


def test_the_window_payload_shape_is_exactly_the_pinned_one(api: Any) -> None:
    mon = monitor(api, units=("lhs", "mid", "rhs"))
    payload = mon.window_payload(last_hours=24)

    assert set(payload) == {"as_of", "last", "window_s", "units"}
    assert payload["last"] == "24h"
    assert payload["window_s"] == 86400
    assert [entry["unit_id"] for entry in payload["units"]] == ["lhs", "mid", "rhs"]
    empty = payload["units"][0]
    assert set(empty) == {
        "unit_id",
        "first_seen_at",
        "last_seen_at",
        "sample_count",
        "charge_sample_count",
        "discharge_sample_count",
        "min_active_w",
        "typical_active_w",
        "max_active_w",
        "classification_counts",
        "foreign_episode_count",
        "foreign_active",
        "foreign_reason",
        "last_objective_observed",
    }
    assert empty["first_seen_at"] is None
    assert empty["foreign_active"] is False
    assert empty["classification_counts"] == {
        CLASS_POD_AUTONOMY: 0,
        CLASS_EXPECTED_NIGHTLY: 0,
        CLASS_HANDBACK: 0,
        CLASS_FOREIGN: 0,
    }
    assert empty["last_objective_observed"] is None


def test_unit_last_observed_is_null_for_an_unknown_unit(api: Any) -> None:
    mon = monitor(api, units=("mid",))
    assert mon.unit_last_observed("ghost") is None


def test_the_monitor_projects_defensively_when_its_internals_fail(api: Any) -> None:
    """The read surfaces degrade to explicit nulls instead of raising: the
    facade projects through them, and a broken session store must never fail
    a snapshot or a route."""
    mon = monitor(api)
    samples = mon._samples
    samples["mid"] = None  # type: ignore[assignment]  # a corrupted record
    assert mon.unit_last_observed("mid") is None
    entry = mon.window_payload(last_hours=24)["units"][0]
    assert entry["sample_count"] == 0
    assert entry["last_objective_observed"] is None


# --- suppression: observability never gates anything ---------------------------------


async def test_a_failing_audit_store_never_breaks_sampling_or_the_bus(api: Any) -> None:
    audit, bus = RecordingAudit(), RecordingBus()
    audit.failing = True
    mon = monitor(api, audit=audit, bus=bus)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0)

    assert session_of(mon)["sample_count"] == 1
    assert len(bus.of_type("foreign_objective.observed")) == 1


async def test_a_failing_bus_never_breaks_sampling_or_the_audit(api: Any) -> None:
    audit, bus = RecordingAudit(), RecordingBus()
    bus.failing = True
    mon = monitor(api, audit=audit, bus=bus)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0)

    assert session_of(mon)["sample_count"] == 1
    assert len(audit.of_type(CLASS_FOREIGN)) == 1


async def test_failing_stores_still_leave_the_evidence_recorded(api: Any) -> None:
    audit, bus = RecordingAudit(), RecordingBus()
    audit.failing = True
    bus.failing = True
    mon = monitor(api, audit=audit, bus=bus)
    await cycle(mon, active_w=-3000, captured_at_mono=1000.0)

    entry = session_of(mon)
    assert entry["sample_count"] == 1
    assert entry["foreign_active"] is True
