"""Contract tests for the self-healing awareness layer (recovery monitor).

``energypod.application.recovery`` is the detection layer accepted from
docs/POD_RECOVERY_RESEARCH.md ladder rung R4 plus the promoted P1 items vi
(actuation-coherence watchdog) and iii (objective echo read-back): the fleet's
batteries self-heal from command-state, communication, and estimation problems
by design, so what the controller owes the operator is DETECTION -- of
self-healing in progress (quiet, informational), of ambiguous anomalous
behavior (evidence capture), and of self-healing FAILURE (the firmware-wedge
class that historically required a physical power cycle).

The monitor is passive by construction: it appends audit facts and publishes
bus events, and it NEVER writes, latches, blocks, or refuses anything.  All
health states are derived reads recomputed from the latest cycle facts; boot
starts every unit ``healthy``-by-observation.

The production module is imported lazily so this red-phase suite collects
cleanly; every missing contract surfaces as an ordinary test failure.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest


@dataclass
class ManualClock:
    now: float = 1000.0
    wall: datetime = field(default_factory=lambda: datetime(2026, 8, 24, 6, 0, 0, tzinfo=UTC))

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


def observation(
    *,
    battery_watts: float | None = 0.0,
    cell_imbalance_v: float | None = 0.020,
    soc_pct: float | None = 99.0,
    debug_mode_w: int | None = 0,
    ctrl_mode_w: int | None = 1,
    work_mode_w: int | None = 6,
    run_mode_w: int | None = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        battery_watts=battery_watts,
        cell_imbalance_v=cell_imbalance_v,
        authoritative_soc_pct=soc_pct,
        debug_mode_w=debug_mode_w,
        ctrl_mode_w=ctrl_mode_w,
        work_mode_w=work_mode_w,
        run_mode_w=run_mode_w,
    )


@pytest.fixture
def api() -> Any:
    try:
        module = importlib.import_module("energypod.application.recovery")
    except ImportError as error:
        pytest.fail(f"the recovery monitor contract is not implemented: {error}", pytrace=False)
    return module


def monitor(
    api: Any,
    *,
    units: tuple[str, ...] = ("mid",),
    coherence_cycles: int = 4,
    min_movement_w: int = 150,
    band: tuple[int, int] = (-2600, 300),
    unresponsive_attempts: int = 3,
    autonomy_interval_s: float = 60.0,
    deadband_w: float = 25.0,
    gap_grace_s: float = 12.0,
    clock: ManualClock | None = None,
    audit: RecordingAudit | None = None,
    bus: RecordingBus | None = None,
) -> Any:
    resolved_clock = clock if clock is not None else ManualClock()
    return api.RecoveryMonitor(
        unit_ids=frozenset(units),
        settings=api.RecoverySettings(
            actuation_coherence_cycles=coherence_cycles,
            actuation_coherence_min_movement_w=min_movement_w,
            expected_autonomy_band_w=band,
            unresponsive_attempts=unresponsive_attempts,
            unexpected_autonomy_min_interval_s=autonomy_interval_s,
            self_charge_deadband_w=deadband_w,
            coherence_gap_grace_s=gap_grace_s,
        ),
        clock=resolved_clock,
        audit=audit if audit is not None else RecordingAudit(),
        bus=bus if bus is not None else RecordingBus(),
        process_instance_id="recovery-test-process",
        process_origin_mono=resolved_clock.now,
        configuration_version=4,
    )


async def idle_cycle(
    mon: Any,
    unit: str = "mid",
    *,
    battery_watts: float | None = 0.0,
    cell_imbalance_v: float | None = 0.020,
    authorized_watts: int = 0,
    authorized_direction: str | None = None,
    claimed: bool = False,
    lifecycle: str = "disarmed",
    inhibit_latched: bool = False,
    inhibit_reason: str | None = None,
) -> Any:
    """Drive one supervised cycle exactly as the runtime fleet loop does."""
    return await mon.observe_cycle(
        unit,
        authorized_watts=authorized_watts,
        authorized_direction=authorized_direction,
        claimed=claimed,
        lifecycle=lifecycle,
        inhibit_latched=inhibit_latched,
        inhibit_reason=inhibit_reason,
        observation=observation(battery_watts=battery_watts, cell_imbalance_v=cell_imbalance_v),
        now_mono=mon._clock.now,
    )


async def state_of(mon: Any, unit: str = "mid") -> Any:
    states = await mon.unit_health_states()
    return states[unit]


# --- the coherence watchdog (P1 vi) --------------------------------------------


async def test_authorized_but_still_cycles_trigger_exactly_once(api: Any) -> None:
    """THE incident signature (2026-08-23 22:11Z silent actuation loss): the
    kernel authorizes real watts, the measured battery power never leaves the
    pre-command baseline.  After the configured streak of consecutive
    still cycles the monitor raises ONE ``actuation_incoherent`` audit fact,
    one ``actuation.incoherent`` bus event, and one control-readiness health
    reason -- and never repeats while the episode persists."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)

    # Pre-command baseline: the pod self-charges uncommanded at -637 W.
    await idle_cycle(mon, battery_watts=-637.0, authorized_watts=0)

    findings = None
    triggered = False
    for cycle in range(1, 6):
        clock.now += 1.5
        findings = await idle_cycle(
            mon,
            battery_watts=-637.0,  # the wedge: measured never moves
            authorized_watts=1000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
        triggered = triggered or findings.coherence_trigger
        if cycle < 4:
            assert findings.coherence_trigger is False, (
                f"cycle {cycle} must not alarm before the configured streak"
            )
    assert triggered, "the configured streak of still cycles must alarm exactly once"

    # One more still cycle: the alarm never repeats inside the episode.
    clock.now += 1.5
    again = await idle_cycle(
        mon,
        battery_watts=-637.0,
        authorized_watts=1000,
        authorized_direction="discharge",
        claimed=True,
        lifecycle="active",
    )
    assert again.coherence_trigger is False

    incoherent = [e for e in audit.appended if e.event_type == "actuation_incoherent"]
    assert len(incoherent) == 1, "exactly one audit fact per incoherent episode"
    (event,) = incoherent
    assert event.unit_id == "mid"
    assert event.authorized_active_w == 1000
    assert "authorized_not_actuating" in event.reason_codes
    assert len(bus.of_type("actuation.incoherent")) == 1

    # WAVE 0 W0-2b (DESIGN_BATTERY_HEALTH_WATCH §3.2): the trigger records
    # evidence and orders the echo read; the health STATE opens only on the
    # discriminator's verdict.  The supervision loop performs the echo read
    # immediately after the trigger (composition's one-bounded-read-per-
    # episode budget) -- this call is that step, and for the ACK-then-ignore
    # wedge the objective word mirrors our write: echo_matches_write.
    await mon.record_incoherence_echo(
        "mid", classification=api.ECHO_MATCHES_WRITE, served_active_w=1000, served_reactive_var=0
    )
    payload = bus.of_type("actuation.incoherent")[0]["payload"]
    assert payload["unit_id"] == "mid"
    assert payload["authorized_watts"] == 1000
    assert payload["measured_watts"] == -637.0

    health = await state_of(mon)
    assert health.state == api.HealthState.ACTUATION_INCOHERENT
    assert "authorized_not_actuating" in health.reasons


async def test_a_coherent_cycle_re_arms_the_alarm(api: Any) -> None:
    """Throttle semantics: re-arm only after a coherent cycle or a control
    state change.  Movement reaching the authorized figure closes the episode
    and returns health to derived-from-observation; a LATER silent streak --
    from a FRESH pre-command baseline, after the authorization lapsed and the
    pod settled -- alarms again.  (A wedge pinned at the level an unchanged
    command already delivered is not this watchdog's case: movement alone
    cannot distinguish it from delivery, and the docstring pins the limit.)"""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)

    await idle_cycle(mon, battery_watts=-637.0)
    for _ in range(4):
        clock.now += 1.5
        await idle_cycle(
            mon,
            battery_watts=-637.0,
            authorized_watts=1000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
    assert len([e for e in audit.appended if e.event_type == "actuation_incoherent"]) == 1

    # The pod starts actuating: measured reaches the commanded figure.
    clock.now += 1.5
    await idle_cycle(
        mon,
        battery_watts=+1120.0,
        authorized_watts=1000,
        authorized_direction="discharge",
        claimed=True,
        lifecycle="active",
    )
    health = await state_of(mon)
    assert health.state is not api.HealthState.ACTUATION_INCOHERENT
    assert health.state == api.HealthState.HEALTHY

    # Steady delivery holds coherent forever: same command, same level.
    for _ in range(4):
        clock.now += 1.5
        await idle_cycle(
            mon,
            battery_watts=+1120.0,
            authorized_watts=1000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
    assert len([e for e in audit.appended if e.event_type == "actuation_incoherent"]) == 1

    # The command lapses; the pod settles at its idle float; a NEW episode
    # from that fresh baseline wedges and alarms exactly once more.
    clock.now += 1.5
    await idle_cycle(mon, battery_watts=-637.0, authorized_watts=0)
    for _ in range(4):
        clock.now += 1.5
        await idle_cycle(
            mon,
            battery_watts=-637.0,
            authorized_watts=1000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
    assert len([e for e in audit.appended if e.event_type == "actuation_incoherent"]) == 2
    assert len(bus.of_type("actuation.incoherent")) == 2


async def test_authorization_ending_re_arms_without_an_alarm(api: Any) -> None:
    """A control state change (the intent expiring, authority dropping to
    zero) closes the episode silently: the uncommanded pod drifting back to
    its self-charge baseline must never be read as a fresh defect."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)

    await idle_cycle(mon, battery_watts=-637.0)
    for _ in range(3):  # one cycle short of the streak
        clock.now += 1.5
        await idle_cycle(
            mon, battery_watts=-637.0, authorized_watts=500, claimed=True, lifecycle="active"
        )
    # The intent expires: the pod returns to autonomy, no authority anywhere.
    clock.now += 1.5
    await idle_cycle(mon, battery_watts=-637.0, authorized_watts=0)
    for _ in range(4):
        clock.now += 1.5
        await idle_cycle(mon, battery_watts=-690.0, authorized_watts=0)
    assert [e for e in audit.appended if e.event_type == "actuation_incoherent"] == []
    health = await state_of(mon)
    assert health.state == api.HealthState.SELF_HEALING  # in-band self-charge


async def test_tiny_setpoints_never_false_trigger(api: Any) -> None:
    """The absolute movement floor keeps tiny setpoints out of court: a 100 W
    command on a healthy pod moves ~100 W -- below the 150 W floor, so the
    cycle is inconclusive and never accumulates toward an alarm; the same
    tiny command on a dead-silent pod still alarms (movement ~0 is below the
    proportional band too)."""
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(api, audit=audit, clock=clock)

    await idle_cycle(mon, battery_watts=-637.0)
    for cycle in range(1, 7):
        clock.now += 1.5
        findings = await idle_cycle(
            mon,
            battery_watts=-537.0,  # the pod delivers its ~100 W command
            authorized_watts=100,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
        assert findings.coherence_trigger is False, f"healthy tiny setpoint alarmed at {cycle}"
    assert [e for e in audit.appended if e.event_type == "actuation_incoherent"] == []
    assert (await state_of(mon)).state == api.HealthState.HEALTHY

    silent = monitor(api, audit=audit, clock=clock)
    await idle_cycle(silent, battery_watts=-637.0)
    triggered = False
    for _ in range(4):
        clock.now += 1.5
        findings = await idle_cycle(
            silent,
            battery_watts=-637.0,  # nothing moves at all
            authorized_watts=100,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
        triggered = triggered or findings.coherence_trigger
    assert triggered, "a fully silent pod must alarm even under a tiny setpoint"


async def test_partial_movement_stays_inconclusive_not_alarmed(api: Any) -> None:
    """Between the proportional band and the absolute floor the monitor
    refuses to conclude: a 2000 W authorization moving 300 W is neither
    confident actuation nor confident stillness, so no alarm fires (the
    watchdog is for the silent-loss wedge class, not partial delivery --
    mid legitimately delivers 75-87% of charge commands under solar
    self-charge offset)."""
    audit = RecordingAudit()
    clock = ManualClock()
    mon = monitor(api, audit=audit, clock=clock)

    await idle_cycle(mon, battery_watts=0.0)
    for _ in range(8):
        clock.now += 1.5
        await idle_cycle(
            mon,
            battery_watts=300.0,
            authorized_watts=2000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
    assert [e for e in audit.appended if e.event_type == "actuation_incoherent"] == []


# --- the objective echo read-back (P1 iii) -------------------------------------


async def test_the_echo_classification_rides_the_episode_evidence(api: Any) -> None:
    """On the coherence trigger the caller performs one bounded fresh read of
    the served objective and hands the classification back; the monitor
    records it, audits it as an ``objective_echo`` fact carrying the read
    value, and includes it on the health reasons and the bus payload.

    WAVE 0 W0-2b, honestly: the scenario's baseline moved from the historic
    -637 W self-charge to a still float.  Under the delivery test a pod
    already self-charging at -637 W under a 250 W CHARGE command is moving
    power in the commanded direction (more than commanded, in fact) and is
    coherent-by-delivery -- movement-only would have called it a wedge; the
    genuine both-fail wedge here is the still pod that never moved and
    never delivered."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)

    await idle_cycle(mon, battery_watts=0.0)
    for _ in range(4):
        clock.now += 1.5
        await idle_cycle(
            mon,
            battery_watts=0.0,
            authorized_watts=250,
            authorized_direction="charge",
            claimed=True,
            lifecycle="active",
        )
    await mon.record_incoherence_echo(
        "mid", classification=api.ECHO_MATCHES_WRITE, served_active_w=-250, served_reactive_var=0
    )

    echoes = [e for e in audit.appended if e.event_type == "objective_echo"]
    assert len(echoes) == 1
    (echo,) = echoes
    assert echo.unit_id == "mid"
    assert echo.reason_codes == (api.ECHO_MATCHES_WRITE,)
    assert echo.authorized_active_w == -250  # charge signs negative on the audit trail

    health = await state_of(mon)
    assert api.ECHO_MATCHES_WRITE in health.reasons
    payload = bus.of_type("actuation.incoherent")[-1]["payload"]
    assert payload["echo_classification"] == api.ECHO_MATCHES_WRITE
    assert payload["served_active_w"] == -250


async def test_the_unreadable_echo_is_honest_evidence(api: Any) -> None:
    """A failing echo read is itself classification: ``echo_unreadable``
    records that the discriminator could not run, never guesses."""
    audit = RecordingAudit()
    mon = monitor(api, audit=audit, clock=ManualClock())
    await idle_cycle(mon, battery_watts=-637.0)
    for _ in range(4):
        await idle_cycle(
            mon,
            battery_watts=-637.0,
            authorized_watts=800,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
    await mon.record_incoherence_echo(
        "mid", classification=api.ECHO_UNREADABLE, served_active_w=None, served_reactive_var=None
    )
    (echo,) = [e for e in audit.appended if e.event_type == "objective_echo"]
    assert echo.reason_codes == (api.ECHO_UNREADABLE,)
    assert (await state_of(mon)).remediation_hint is None, (
        "an unreadable echo is not pod-side proof; no terminal guidance"
    )
    # WAVE 0 W0-2b: the unreadable episode records evidence and downgrades --
    # the health state never OPENS on it (see the T-BHW-WAVE0 section).
    assert (await state_of(mon)).state is not api.HealthState.ACTUATION_INCOHERENT


# --- the unresponsiveness classifier (R4) ---------------------------------------


async def test_read_timeouts_reach_not_responding_with_terminal_guidance(api: Any) -> None:
    """The firmware-wedge signature: the gateway path is fine (no connect
    failures) but reads time out for K consecutive attempts.  The unit
    derives ``not_responding`` and the R5 honest-terminal remediation hint --
    remote recovery is exhausted (R1 resync already retries by construction),
    a physical restart is required."""
    bus = RecordingBus()
    mon = monitor(api, bus=bus, clock=ManualClock())

    for attempt in range(1, 4):
        mon.record_read_outcome("mid", api.READ_FAILED)
        await idle_cycle(mon, battery_watts=None)
        states = await mon.unit_health_states()
        if attempt < 3:
            assert states["mid"].state != api.HealthState.NOT_RESPONDING
    states = await mon.unit_health_states()
    assert states["mid"].state == api.HealthState.NOT_RESPONDING
    assert states["mid"].remediation_hint is not None
    assert "physical restart" in states["mid"].remediation_hint

    # A healthy read clears the streak and the state.
    mon.record_read_outcome("mid", api.READ_OK)
    await idle_cycle(mon, battery_watts=-637.0)
    assert (await state_of(mon)).state == api.HealthState.SELF_HEALING


async def test_connect_failures_derive_unreachable(api: Any) -> None:
    """TCP connect failure is the gateway class, not the pod-wedge class:
    the unit derives ``unreachable`` (and it outranks read timeouts), with
    no physical-restart guidance -- the pod behind the gateway may be fine."""
    mon = monitor(api, clock=ManualClock())
    for _ in range(3):
        mon.record_read_outcome("mid", api.READ_FAILED)
    mon.record_read_outcome("mid", api.CONNECT_FAILED)
    await idle_cycle(mon, battery_watts=None)
    health = await state_of(mon)
    assert health.state == api.HealthState.UNREACHABLE
    assert health.remediation_hint is None


async def test_foreign_writer_and_inhibited_derive_from_the_existing_latch(api: Any) -> None:
    """The existing arm-preflight latch is the source of truth: a latched
    ``external_writer`` inhibit derives ``foreign_writer``; any other inhibit
    derives ``inhibited``.  Nothing new latches here."""
    mon = monitor(api, clock=ManualClock())
    await idle_cycle(
        mon, battery_watts=-637.0, inhibit_latched=True, inhibit_reason="external_writer"
    )
    assert (await state_of(mon)).state == api.HealthState.FOREIGN_WRITER

    await idle_cycle(
        mon, battery_watts=-637.0, lifecycle="inhibited", inhibit_reason="write_failed"
    )
    assert (await state_of(mon)).state == api.HealthState.INHIBITED
    assert "write_failed" in (await state_of(mon)).reasons


async def test_self_healing_is_quiet_and_informational(api: Any) -> None:
    """Every trusted self-recovery pattern derives ``self_healing`` with a
    name: requalification after an inhibit, top-of-charge cell balancing, and
    in-band autonomy self-charge.  Each is evidence-only -- no audit fact, no
    alarm-tier event, and health stays informational."""
    bus = RecordingBus()
    audit = RecordingAudit()
    mon = monitor(api, bus=bus, audit=audit, clock=ManualClock())

    # Requalifying after an inhibit (the actor collects stable samples again).
    await idle_cycle(
        mon, battery_watts=-637.0, lifecycle="observe_only", inhibit_reason="write_failed"
    )
    health = await state_of(mon)
    assert health.state == api.HealthState.SELF_HEALING
    assert "requalifying_after_inhibit" in health.reasons

    # Cell balancing: the operator's 50 mV early-warning line.
    await idle_cycle(mon, battery_watts=0.0, cell_imbalance_v=0.054)
    health = await state_of(mon)
    assert health.state == api.HealthState.SELF_HEALING
    assert "cell_balancing" in health.reasons

    # Autonomy-band self-charge: uncommanded, in the commissioned band.
    await idle_cycle(mon, battery_watts=-2270.0, cell_imbalance_v=0.020)
    health = await state_of(mon)
    assert health.state == api.HealthState.SELF_HEALING
    assert "autonomous_self_charge" in health.reasons

    # Quiet: informational states never emit detection events.
    assert bus.of_type("actuation.incoherent") == []
    assert [e for e in audit.appended if e.event_type == "unexpected_autonomy"] == []


async def test_classification_precedence_is_pinned(api: Any) -> None:
    """unreachable > not_responding > foreign_writer > inhibited >
    actuation_incoherent > self_healing > healthy."""
    mon = monitor(api, clock=ManualClock())
    await idle_cycle(mon, battery_watts=-637.0, lifecycle="observe_only", inhibit_reason="x")
    assert (await state_of(mon)).state == api.HealthState.SELF_HEALING

    # An active wedge while requalifying: the wedge outranks self-healing.
    # (Wave 0 W0-2b: the state opens on the discriminating echo, not the
    # streak -- the echo step is driven exactly as supervision drives it.)
    for _ in range(4):
        await idle_cycle(
            mon,
            battery_watts=-637.0,
            authorized_watts=900,
            claimed=True,
            lifecycle="active",
        )
    await mon.record_incoherence_echo(
        "mid",
        classification=api.ECHO_OBJECTIVE_NOT_SERVED,
        served_active_w=0,
        served_reactive_var=0,
    )
    assert (await state_of(mon)).state == api.HealthState.ACTUATION_INCOHERENT

    # An inhibit outranks the wedge verdict.
    await idle_cycle(
        mon,
        battery_watts=-637.0,
        authorized_watts=900,
        claimed=True,
        lifecycle="inhibited",
        inhibit_reason="write_failed",
    )
    assert (await state_of(mon)).state == api.HealthState.INHIBITED

    # The external-writer latch outranks every other inhibit.
    await idle_cycle(
        mon,
        battery_watts=-637.0,
        authorized_watts=900,
        claimed=True,
        lifecycle="inhibited",
        inhibit_latched=True,
        inhibit_reason="external_writer",
    )
    assert (await state_of(mon)).state == api.HealthState.FOREIGN_WRITER

    # Unresponsiveness outranks the latch, and unreachability outranks it all.
    for _ in range(3):
        mon.record_read_outcome("mid", api.READ_FAILED)
    await idle_cycle(
        mon, battery_watts=None, inhibit_latched=True, inhibit_reason="external_writer"
    )
    assert (await state_of(mon)).state == api.HealthState.NOT_RESPONDING
    mon.record_read_outcome("mid", api.CONNECT_FAILED)
    await idle_cycle(mon, battery_watts=None)
    assert (await state_of(mon)).state == api.HealthState.UNREACHABLE


async def test_transitions_publish_unit_health_changed(api: Any) -> None:
    """Every health-state transition publishes exactly one ``unit.health_changed``
    bus event naming the unit, the from/to states, and the reasons; staying in
    a state publishes nothing."""
    bus = RecordingBus()
    mon = monitor(api, bus=bus, units=("mid", "rhs"), clock=ManualClock())
    await idle_cycle(mon, battery_watts=-637.0)
    # Boot is healthy-by-observation; in-band self-charge is the first change.
    transitions = bus.of_type("unit.health_changed")
    assert len(transitions) == 1
    assert transitions[0]["payload"] == {
        "unit_id": "mid",
        "from": "healthy",
        "to": "self_healing",
        "reasons": ["autonomous_self_charge"],
    }

    # Another cycle in the same state: silence.
    await idle_cycle(mon, battery_watts=-640.0)
    assert len(bus.of_type("unit.health_changed")) == 1

    # The sibling unit never moved: it still published its boot observation.
    states = await mon.unit_health_states()
    assert states["rhs"].state == api.HealthState.HEALTHY


# --- the unexpected-autonomy evidence recorder (mid's ±1.2 kW mystery) ----------


async def test_out_of_band_uncommanded_power_is_timestamped_evidence(api: Any) -> None:
    """Measured battery power outside the commissioned autonomy band while no
    intent claims the unit appends one ``unexpected_autonomy`` audit fact
    carrying the measured watts, SOC, and mode words -- and at most one per
    unit per 60 s.  Pure evidence: no block, no alarm-tier event, health stays
    healthy/self_healing."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)

    # mid's standing unexplained oscillation: +1200 W discharging, unclaimed.
    await idle_cycle(mon, battery_watts=1200.0)
    events = [e for e in audit.appended if e.event_type == "unexpected_autonomy"]
    assert len(events) == 1
    (event,) = events
    assert event.unit_id == "mid"
    assert event.authorized_active_w == 0
    assert "outside_expected_autonomy_band" in event.reason_codes

    # Throttled: the next cycles inside the window record nothing more.
    for _ in range(5):
        clock.now += 1.5
        await idle_cycle(mon, battery_watts=1180.0)
    assert len([e for e in audit.appended if e.event_type == "unexpected_autonomy"]) == 1

    # Beyond the window, still out of band: exactly one more.
    clock.now += 61.0
    await idle_cycle(mon, battery_watts=-3100.0)
    assert len([e for e in audit.appended if e.event_type == "unexpected_autonomy"]) == 2

    # The bus carries the full figures for diagnosis, quiet-tier.
    payload = bus.of_type("unit.unexpected_autonomy")[0]["payload"]
    assert payload == {
        "unit_id": "mid",
        "measured_watts": 1200.0,
        "soc_pct": 99.0,
        "debug_mode_w": 0,
        "ctrl_mode_w": 1,
        "work_mode_w": 6,
        "run_mode_w": 3,
    }

    # And the state stays informational.
    assert (await state_of(mon)).state == api.HealthState.HEALTHY


async def test_expected_autonomy_and_claimed_units_never_record(api: Any) -> None:
    """In-band self-charge records nothing (that is the pod being healthy),
    and a claimed unit's measured power is commanded, not autonomous."""
    audit = RecordingAudit()
    mon = monitor(api, audit=audit, clock=ManualClock())

    await idle_cycle(mon, battery_watts=-2270.0)  # deep in-band self-charge
    await idle_cycle(mon, battery_watts=290.0)  # small positive float, in band
    await idle_cycle(
        mon,
        battery_watts=1200.0,
        claimed=True,
        authorized_watts=1000,
        authorized_direction="discharge",
        lifecycle="active",
    )
    assert [e for e in audit.appended if e.event_type == "unexpected_autonomy"] == []


# --- the honest terminal guidance (R5 rail) -------------------------------------


async def test_pod_side_wedge_carries_the_physical_restart_hint(api: Any) -> None:
    """Echo-matches-our-write while incoherent: the transport and the write
    path are proven fine, so the defect is pod-side and remote recovery is
    exhausted -- the R5 physical-restart guidance with the vendor-app
    checklist reference appears on the health view."""
    mon = monitor(api, clock=ManualClock())
    await idle_cycle(mon, battery_watts=-637.0)
    for _ in range(4):
        await idle_cycle(
            mon, battery_watts=-637.0, authorized_watts=1000, claimed=True, lifecycle="active"
        )
    assert (await state_of(mon)).remediation_hint is None, (
        "before the echo discriminates, no terminal claim is made"
    )
    await mon.record_incoherence_echo(
        "mid", classification=api.ECHO_MATCHES_WRITE, served_active_w=1000, served_reactive_var=0
    )
    hint = (await state_of(mon)).remediation_hint
    assert hint is not None and "physical restart" in hint
    assert "MiniES" in hint  # the vendor-app checklist reference


async def test_objective_not_served_points_at_the_mode_checklist(api: Any) -> None:
    """Echo-zero-while-authorized: the objective is not being served at all,
    the mode/autonomy conflict class -- the hint names the vendor-app mode
    checklist, not a physical restart."""
    mon = monitor(api, clock=ManualClock())
    await idle_cycle(mon, battery_watts=-637.0)
    for _ in range(4):
        await idle_cycle(
            mon, battery_watts=-637.0, authorized_watts=1000, claimed=True, lifecycle="active"
        )
    await mon.record_incoherence_echo(
        "mid",
        classification=api.ECHO_OBJECTIVE_NOT_SERVED,
        served_active_w=0,
        served_reactive_var=0,
    )
    hint = (await state_of(mon)).remediation_hint
    assert hint is not None
    assert "Normal Mode" in hint and "Remote" in hint
    assert "physical restart" not in hint


async def test_a_resurfaced_foreign_writer_needs_no_restart_guidance(api: Any) -> None:
    """Echo-nonzero-but-not-ours: the existing external_writer semantics own
    the remediation (find the other writer); the monitor contributes no
    terminal hint."""
    mon = monitor(api, clock=ManualClock())
    await idle_cycle(mon, battery_watts=-637.0)
    for _ in range(4):
        await idle_cycle(
            mon, battery_watts=-637.0, authorized_watts=1000, claimed=True, lifecycle="active"
        )
    await mon.record_incoherence_echo(
        "mid", classification=api.ECHO_EXTERNAL_WRITER, served_active_w=-900, served_reactive_var=0
    )
    assert (await state_of(mon)).remediation_hint is None


# --- passivity and failure honesty ----------------------------------------------


async def test_detection_failures_never_propagate(api: Any) -> None:
    """Observability must never gate control: a failing audit store or bus is
    suppressed on every detection path, and the derived state still moves."""
    audit, bus = RecordingAudit(), RecordingBus()
    audit.failing = bus.failing = True
    mon = monitor(api, audit=audit, bus=bus, clock=ManualClock())

    await idle_cycle(mon, battery_watts=1200.0)  # evidence recorder, stores down
    await idle_cycle(mon, battery_watts=-637.0)
    for _ in range(4):
        await idle_cycle(
            mon, battery_watts=-637.0, authorized_watts=900, claimed=True, lifecycle="active"
        )
    await mon.record_incoherence_echo(
        "mid", classification=api.ECHO_MATCHES_WRITE, served_active_w=900, served_reactive_var=0
    )
    assert (await state_of(mon)).state == api.HealthState.ACTUATION_INCOHERENT


async def test_settings_and_units_are_validated(api: Any) -> None:
    with pytest.raises(ValueError, match="actuation_coherence_cycles"):
        api.RecoverySettings(actuation_coherence_cycles=0)
    with pytest.raises(ValueError, match="actuation_coherence_min_movement_w"):
        api.RecoverySettings(actuation_coherence_min_movement_w=0)
    with pytest.raises(ValueError, match="unresponsive_attempts"):
        api.RecoverySettings(unresponsive_attempts=0)
    with pytest.raises(ValueError, match="unexpected_autonomy_min_interval_s"):
        api.RecoverySettings(unexpected_autonomy_min_interval_s=0)
    with pytest.raises(ValueError, match="expected_autonomy_band_w"):
        api.RecoverySettings(expected_autonomy_band_w=(300, -2600))
    # DESIGN_BATTERY_HEALTH_WATCH §3.1/§3.2 (Wave 0): the deadband lives in
    # (0, 100] -- at 100 it would begin to eat the legitimate float class --
    # and the gap grace between 1 and 300 s (the handback-grace precedent's
    # bounds, mirrored at the application layer).
    with pytest.raises(ValueError, match="self_charge_deadband_w"):
        api.RecoverySettings(self_charge_deadband_w=0.0)
    with pytest.raises(ValueError, match="self_charge_deadband_w"):
        api.RecoverySettings(self_charge_deadband_w=100.5)
    with pytest.raises(ValueError, match="coherence_gap_grace_s"):
        api.RecoverySettings(coherence_gap_grace_s=0.5)
    with pytest.raises(ValueError, match="coherence_gap_grace_s"):
        api.RecoverySettings(coherence_gap_grace_s=301.0)
    with pytest.raises(ValueError, match="unit_ids"):
        monitor(api, units=())
    unknown = monitor(api)
    with pytest.raises(LookupError):
        await unknown.observe_cycle(
            "ghost",
            authorized_watts=0,
            authorized_direction=None,
            claimed=False,
            lifecycle="disarmed",
            inhibit_latched=False,
            inhibit_reason=None,
            observation=observation(),
            now_mono=1000.0,
        )
    with pytest.raises(LookupError):
        unknown.record_read_outcome("ghost", api.READ_OK)


# --- DESIGN_BATTERY_HEALTH_WATCH §3: Wave 0 (T-BHW-WAVE0) ------------------------
#
# Both fixes below correct live-verified defects in the monitor's own
# evidence, landed BEFORE any stage of that program composes: W0-1 the
# self-charge float deadband (the 158-transition night on rhs), W0-2
# coherence judged on delivery with a gap-surviving baseline (both live
# 2026-08-24 actuation_incoherent detections were false positives), and the
# standing invariant I10 pinning that no automated response anywhere keys
# on actuation_incoherent alone.


async def test_the_float_deadband_renders_the_zero_straddle_steady(api: Any) -> None:
    """W0-1 (§3.1): the 158-transition night replays to ~0 transitions.  rhs
    at 96-99% SoC floats ACROSS zero (observed straddle -16/0/+33 W) and the
    exact-zero discriminator flipped healthy <-> self_healing on every zero
    crossing; inside the deadband the float is the healthy steady state of a
    full pack, on BOTH sides of zero, and the transition wall is gone."""
    bus = RecordingBus()
    clock = ManualClock()
    mon = monitor(api, bus=bus, clock=clock)

    # The straddle's within-deadband bulk: alternating sign across zero, the
    # exact shape that flapped per-sample under the exact-zero test.
    straddle = [-16.0, 0.0, +16.0, 0.0, -8.0, 0.0, +12.0, 0.0] * 20
    for watts in straddle:
        clock.now += 96.0  # the cold ring's ~96-108 s serving period
        await idle_cycle(mon, battery_watts=watts)
        assert (await state_of(mon)).state == api.HealthState.HEALTHY, watts

    assert bus.of_type("unit.health_changed") == [], (
        "the zero-straddling float must render steady health, not a wall of "
        "alternating entries"
    )
    assert bus.of_type("unit.unexpected_autonomy") == [], "a float is in-band, never evidence"


async def test_beyond_deadband_float_stays_self_healing_on_both_sides(api: Any) -> None:
    """W0-1 (§3.1): the deadband kills the zero-crossing flap, NOT the float
    visibility -- the ±33 W observed-straddle extremes on BOTH sides of zero
    and §3.1's own +99 W CT-following example still render self_healing."""
    mon = monitor(api, clock=ManualClock())
    for watts in (-33.0, +33.0, -99.0, +99.0):
        await idle_cycle(mon, battery_watts=watts)
        health = await state_of(mon)
        assert health.state == api.HealthState.SELF_HEALING, watts
        assert "autonomous_self_charge" in health.reasons

    # The band edge is the deadband itself: below it steady healthy, at it
    # the float becomes visible again (>= semantics, both signs).
    await idle_cycle(mon, battery_watts=+24.9)
    assert (await state_of(mon)).state == api.HealthState.HEALTHY
    await idle_cycle(mon, battery_watts=-25.0)
    assert (await state_of(mon)).state == api.HealthState.SELF_HEALING


async def test_steady_partial_delivery_never_trips_the_watchdog(api: Any) -> None:
    """W0-2 (§3.2): the two live false positives as named regression
    vectors -- mid 2026-08-24 14:11Z (~87% of command) and lhs 15:34Z (~96%):
    an authorization gap (the telemetry_stale dip ending the intent) while
    the pod held steady partial delivery must NEVER read as incoherence.
    The delivery test passes (steady 87-96% of command IS delivery), the
    baseline survives the short gap instead of re-anchoring onto the watts
    the pod was already delivering, and no physical-restart hint appears."""
    for fraction, incident in ((0.87, "mid 14:11Z"), (0.96, "lhs 15:34Z")):
        audit, bus = RecordingAudit(), RecordingBus()
        clock = ManualClock()
        mon = monitor(api, audit=audit, bus=bus, clock=clock, band=(-2600, 1000))

        await idle_cycle(mon, battery_watts=0.0)  # the pre-command float
        delivery = 1000.0 * fraction
        for _ in range(4):  # steady partial delivery under the command
            clock.now += 1.5
            await idle_cycle(
                mon,
                battery_watts=delivery,
                authorized_watts=1000,
                authorized_direction="discharge",
                claimed=True,
                lifecycle="active",
            )
        # THE GAP: the intent lapses for ~3 s (two fleet cycles) while the
        # pod keeps delivering; renewal then restores the same command.
        for _ in range(2):
            clock.now += 1.5
            await idle_cycle(mon, battery_watts=delivery, authorized_watts=0)
        for _ in range(8):
            clock.now += 1.5
            await idle_cycle(
                mon,
                battery_watts=delivery,
                authorized_watts=1000,
                authorized_direction="discharge",
                claimed=True,
                lifecycle="active",
            )

        assert [e for e in audit.appended if e.event_type == "actuation_incoherent"] == [], incident
        assert bus.of_type("actuation.incoherent") == [], incident
        health = await state_of(mon)
        assert health.state == api.HealthState.HEALTHY, incident
        assert health.remediation_hint is None, incident


async def test_a_short_authorization_gap_does_not_re_anchor_the_baseline(api: Any) -> None:
    """W0-2a (§3.2): a gap inside ``coherence_gap_grace_s`` (default 12 s)
    with delivery continuing is the SAME episode -- the pre-command baseline
    survives.  Observable through the evidence: when the pod then goes
    genuinely DEAD (delivers nothing, back at its pre-command float), the
    alarm still names the ORIGINAL baseline and the dead pod still alarms
    (movement ~0 AND delivery ~0 against the survived baseline)."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)

    await idle_cycle(mon, battery_watts=0.0)  # baseline 0 W: the pre-command float
    for _ in range(3):  # steady delivery at 87% of the command
        clock.now += 1.5
        await idle_cycle(
            mon,
            battery_watts=870.0,
            authorized_watts=1000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
    for _ in range(2):  # the 3 s gap, delivery continuing throughout
        clock.now += 1.5
        await idle_cycle(mon, battery_watts=870.0, authorized_watts=0)
    # The command resumes and the pod dies: nothing delivered, no movement.
    triggered = False
    for _ in range(4):
        clock.now += 1.5
        findings = await idle_cycle(
            mon,
            battery_watts=0.0,
            authorized_watts=1000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
        triggered = triggered or bool(findings.coherence_trigger)
    assert triggered, "a dead pod still alarms (W0-2's negative case)"

    await mon.record_incoherence_echo(
        "mid", classification=api.ECHO_MATCHES_WRITE, served_active_w=1000, served_reactive_var=0
    )
    (event,) = [e for e in audit.appended if e.event_type == "actuation_incoherent"]
    assert event.unit_id == "mid"
    # The figures ride the bus payload (the audit row keeps fingerprints);
    # the FIRST incoherent event is the trigger-time one.
    trigger_payload = bus.of_type("actuation.incoherent")[0]["payload"]
    assert trigger_payload["baseline_watts"] == 0.0, (
        "the baseline survived the gap -- the pre-command float, never the "
        "in-flight delivery watts"
    )
    assert (await state_of(mon)).state == api.HealthState.ACTUATION_INCOHERENT


async def test_a_gap_beyond_the_grace_re_anchors_the_baseline(api: Any) -> None:
    """W0-2a (§3.2): past ``coherence_gap_grace_s`` the standing semantics
    resume -- the baseline re-anchors onto the level the battery then holds.
    The contrast with the short-gap case is the pin: after a beyond-grace
    re-anchor at the delivery level, the same subsequent dead-at-float drop
    reads as MOVEMENT (the pod released its hold), never as stillness from
    the original baseline, and no incoherence is declared."""
    audit, bus = RecordingAudit(), RecordingBus()
    clock = ManualClock()
    mon = monitor(api, audit=audit, bus=bus, clock=clock)

    await idle_cycle(mon, battery_watts=0.0)
    for _ in range(3):
        clock.now += 1.5
        await idle_cycle(
            mon,
            battery_watts=870.0,
            authorized_watts=1000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
    # The gap runs past the 12 s grace with delivery continuing: the
    # baseline re-anchors at 870 W (the standing pre-Wave-0 behavior, now
    # bounded by the grace).
    for gap_s in (1.5, 3.0, 14.0):
        clock.now += gap_s
        await idle_cycle(mon, battery_watts=870.0, authorized_watts=0)
    # The command resumes and the pod dies at its float.
    for _ in range(6):
        clock.now += 1.5
        await idle_cycle(
            mon,
            battery_watts=0.0,
            authorized_watts=1000,
            authorized_direction="discharge",
            claimed=True,
            lifecycle="active",
        )
    assert [e for e in audit.appended if e.event_type == "actuation_incoherent"] == []
    assert bus.of_type("actuation.incoherent") == []
    assert (await state_of(mon)).state == api.HealthState.HEALTHY


async def test_only_discriminated_episodes_open_the_state(api: Any) -> None:
    """W0-2b (§3.2): the episode may OPEN the health state only once the
    objective-echo discriminator classified it ``echo_matches_write`` or
    ``objective_not_served``; ``echo_unreadable`` and ``external_writer``
    episodes record their evidence and downgrade (the external-writer class
    belongs to the standing latch path), and so does an episode whose echo
    never arrives (unclassified)."""
    for classification, served in (
        (api.ECHO_UNREADABLE, (None, None)),
        (api.ECHO_EXTERNAL_WRITER, (-900, 0)),
    ):
        audit, bus = RecordingAudit(), RecordingBus()
        clock = ManualClock()
        mon = monitor(api, audit=audit, bus=bus, clock=clock)

        await idle_cycle(mon, battery_watts=-637.0)
        for _ in range(4):
            clock.now += 1.5
            await idle_cycle(
                mon,
                battery_watts=-637.0,
                authorized_watts=800,
                authorized_direction="discharge",
                claimed=True,
                lifecycle="active",
            )
        await mon.record_incoherence_echo(
            "mid",
            classification=classification,
            served_active_w=served[0],
            served_reactive_var=served[1],
        )

        # Evidence recorded, state downgraded: never actuation_incoherent.
        health = await state_of(mon)
        assert health.state is not api.HealthState.ACTUATION_INCOHERENT, classification
        assert health.state == api.HealthState.HEALTHY, classification
        assert health.remediation_hint is None, classification
        assert [e for e in audit.appended if e.event_type == "actuation_incoherent"], (
            "the streak evidence is still recorded -- downgraded is not deleted"
        )
        assert [e for e in audit.appended if e.event_type == "objective_echo"]

    # The unclassified episode: the trigger fires (evidence + the echo read
    # ordered) but the read never lands -- the state must not open either.
    audit = RecordingAudit()
    mon = monitor(api, audit=audit, clock=ManualClock())
    await idle_cycle(mon, battery_watts=-637.0)
    for _ in range(4):
        await idle_cycle(
            mon,
            battery_watts=-637.0,
            authorized_watts=800,
            claimed=True,
            lifecycle="active",
        )
    assert [e for e in audit.appended if e.event_type == "actuation_incoherent"]
    assert (await state_of(mon)).state is not api.HealthState.ACTUATION_INCOHERENT


def test_no_automated_response_keys_on_actuation_incoherent(api: Any) -> None:
    """T-BHW-WAVE0 / DESIGN_BATTERY_HEALTH_WATCH invariant I10 (§3.2, §8,
    §17): no automated response ANYWHERE keys on ``actuation_incoherent``
    alone -- Stage R's eligibility is census-flag AND probe-failure, and the
    health ladder is context, never a trigger.

    The pin is structural: today only the detector itself and PASSIVE
    surfaces read the vocabulary (projections, advisories, audit-evidence
    collection, and the one bounded objective-echo READ the trigger orders);
    any module that starts reading it breaks this allowlist until the pin is
    consciously revised by a contract that names the response and why it
    does not stand on the signal Wave 0 exists to make trustworthy."""
    from pathlib import Path

    package_root = Path(api.__file__).resolve().parents[1]  # src/energypod
    vocabulary = (
        "ACTUATION_INCOHERENT",
        "actuation_incoherent",
        "actuation.incoherent",
        "coherence_trigger",
    )
    # Every current consumer, with WHY it is passive:
    allowed = {
        # The detector itself: audit facts, bus events, the derived view.
        Path("application") / "recovery.py",
        # The facade's read surfaces: the unit-detail recovery advisory, the
        # control-readiness reason strings, the health projection.
        Path("application") / "service.py",
        # The resume checklist's faults_while_parked: a bounded read of PAST
        # audit facts over the park window (DESIGN_POD_PARKING section 4),
        # never a response to the live state.
        Path("application") / "parking.py",
        # The supervision pass's trigger consumer: it performs the ONE
        # bounded objective-echo READ per episode (P1 iii) -- detection
        # evidence, not a control act.
        Path("runtime") / "composition.py",
        # The policy block's Wave-0 justification COMMENTS name the incident
        # class (§15 item 6); a comment is documentation, never a consumer.
        Path("runtime") / "config.py",
        # The nightly health watch's census/probe skip-if set reads the
        # ladder word as CONTEXT -- an actuation_incoherent unit is skipped
        # (excluded at the census, never probed), exactly §4's "the health
        # ladder is context, never a trigger" doctrine.  No health-watch
        # code path responds to the state with any act at all.
        Path("application") / "health_watch.py",
        # The calibration cycling program's section 3.3 skip-if set reads
        # the same ladder word as CONTEXT (DESIGN_CALIBRATION_CYCLING
        # section 3.3): an actuation_incoherent unit is deferred from the
        # traverse, never cycled and never responded to -- the program's
        # only act is the ordinary discharge intent, gated on its own class
        # gates.
        Path("application") / "calibration.py",
    }
    mentioning: set[Path] = set()
    for path in sorted(package_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if any(word in text for word in vocabulary):
            mentioning.add(path.relative_to(package_root))
    assert mentioning <= allowed, (
        f"modules beyond the passive-consumer allowlist read the "
        f"actuation_incoherent vocabulary: {sorted(mentioning - allowed)} -- "
        f"I10 requires a conscious pin revision, not a silent consumer"
    )
