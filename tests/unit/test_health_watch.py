"""T-BHW-CENSUS + T-BHW-PROBE + the shared program frame (the C/P wave).

DESIGN_BATTERY_HEALTH_WATCH (CONTRACT v1.1) §4/§5/§6/§8/§10/§11, tested
against ``energypod.application.health_watch``:

- the frame: the once-per-civil-night semantics derived from durable rows,
  the no-new-ACT deadline (A1), the quiet-window verification, and the
  interrupted-program reconstruction (A4's C/P shape — an interrupted
  census/probe leaves the alert naming the unit state);
- the census: the five stuck-signature predicates with their hold/fail
  edges, S3's either-corroborator, S4's sibling comparison, S5's mode
  exclusion, ``degraded_evidence`` on historian gaps, persistence
  promotion, ZERO writes, and the row/event/projection shapes;
- the probe: the skip-if set each rendering its skip, the pass math, the
  FULL §6.3 verdict matrix (A3), spike patterns never passing, preemption
  (A12: MANUAL/AGENT/e-stop preempt, a schedule claim does not),
  return-to-baseline with its A8 demand-confound downgrade, the quiet-load
  gate on the grid-IMPORT words including the stale-evidence skip, the
  explicit cancel, sequential ordering with the inter-unit gap, and no
  probe outside the window;
- the structural pin, restated for the Stage-R era: a probe verdict ALONE
  still triggers nothing (only the §7.1 conjunction composes a cycle, and
  that lives in test_health_watch_recovery); the module's write reach is
  exactly the injected composer ports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from energypod.application import health_watch as hw
from energypod.domain.observations import UnitLifecycle

ZONE = ZoneInfo("Australia/Brisbane")
UNIT_IDS = ("lhs", "mid", "rhs")
NIGHT = datetime(2026, 8, 25, 23, 5, 0, tzinfo=ZONE)
START_MONO = 100.0
POLICY = SimpleNamespace(max_telemetry_age_s=5.0, max_soc_disagreement_pct=5.0)


# --- deterministic fakes ---------------------------------------------------------


@dataclass
class FakeClock:
    now: float = START_MONO
    wall: datetime = NIGHT

    def wall_now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall = self.wall + timedelta(seconds=seconds)


@dataclass
class FakeObservations:
    latest: dict[str, Any] = field(default_factory=dict)
    # While set, every read re-stamps the fleet's observations fresh: the
    # fakes hold STATIC namespaces, but the probe's freshness gates judge
    # against the advancing injected clock.
    auto_stamp_mono: Any = None

    async def all_latest(self) -> dict[str, Any]:
        latest = dict(self.latest)
        if self.auto_stamp_mono is not None:
            now = float(self.auto_stamp_mono())
            for observation in latest.values():
                observation.captured_at_mono = now
        return latest


@dataclass
class _FakeIntent:
    id: str
    source: str
    unit_ids: tuple[str, ...]
    accepted_at_mono: float
    duration_s: float = 10.0

    @property
    def selected_unit_ids(self) -> frozenset[str]:
        return frozenset(self.unit_ids)

    @property
    def expires_at_mono(self) -> float:
        return self.accepted_at_mono + self.duration_s


@dataclass
class FakeIntents:
    entries: list[Any] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return tuple(item for item in self.entries if item.expires_at_mono > now_mono)

    async def remove(self, intent_id: str) -> None:
        self.removed.append(intent_id)
        self.entries = [item for item in self.entries if item.id != intent_id]


@dataclass
class FakeSubmit:
    submissions: list[dict[str, Any]] = field(default_factory=list)
    # The twin of the facade's own store: a submitted probe intent becomes a
    # live OPTIMIZER claim the controller's claim checks and cancels see.
    intents: FakeIntents | None = None
    clock: FakeClock | None = None

    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> dict[str, Any]:
        self.submissions.append(
            {
                "unit_ids": sorted(unit_ids),
                "direction": getattr(direction, "value", direction),
                "watts": watts,
                "ttl_s": ttl_s,
            }
        )
        intent_id = f"health-{len(self.submissions)}"
        if self.intents is not None:
            now = 0.0 if self.clock is None else self.clock.now
            self.intents.entries.append(
                _FakeIntent(intent_id, "optimizer", tuple(sorted(unit_ids)), accepted_at_mono=now)
            )
        return {"intent_id": intent_id, "status": "accepted"}


@dataclass
class FakeHistory:
    rows: tuple[Any, ...] = ()

    def samples(
        self, unit_ids: Any, from_at: datetime, to_at: datetime
    ) -> tuple[Any, ...]:
        return tuple(
            row
            for row in self.rows
            if from_at <= getattr(row, "sampled_at", from_at) <= to_at
        )


@dataclass
class FakeAudit:
    appended: list[Any] = field(default_factory=list)
    seeded: list[Any] = field(default_factory=list)

    async def append(self, event: Any) -> None:
        self.appended.append(event)

    async def recent(
        self, *, limit: int, after_sequence: int | None = None
    ) -> tuple[Any, ...]:
        return tuple(self.seeded + self.appended)


@dataclass
class FakeBus:
    events: list[dict[str, Any]] = field(default_factory=list)

    async def publish(self, body: Any) -> int:
        self.events.append(dict(body))
        return len(self.events)


@dataclass
class FakeEcho:
    classification: str = hw.ECHO_UNREADABLE

    async def read_objective_echo(self) -> tuple[str, tuple[int | None, int | None]]:
        return (self.classification, (300, 0))


def make_settings(**overrides: Any) -> hw.HealthWatchSettings:
    values: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "window_local": time(23, 0),
        "deadline_local": time(23, 45),
        "stages": ("census", "probe"),
        "stuck": hw.StuckSettings(),
        "probe": hw.ProbeSettings(
            settle_s=10,
            sustain_s=20,
            baseline_return_s=10,
            inter_unit_gap_s=0,
        ),
        "recovery_mode": "advise",
        "unit_ids": UNIT_IDS,
        "sample_interval_s": 30.0,
        "intent_ttl_s": 4.0,
    }
    values.update(overrides)
    return hw.HealthWatchSettings(**values)


def make_controller(
    *,
    clock: FakeClock | None = None,
    observations: FakeObservations | None = None,
    intents: FakeIntents | None = None,
    submit: FakeSubmit | None = None,
    history: FakeHistory | None = None,
    audit: FakeAudit | None = None,
    bus: FakeBus | None = None,
    echo: str = hw.ECHO_UNREADABLE,
    health_state: str = "healthy",
    parked: frozenset[str] = frozenset(),
    stopped: frozenset[str] = frozenset(),
    settings: hw.HealthWatchSettings | None = None,
    auto_stamp: bool = True,
) -> tuple[hw.HealthWatchController, dict[str, Any]]:
    clock = clock or FakeClock()
    observations = observations or FakeObservations()
    if auto_stamp:
        observations.auto_stamp_mono = clock.monotonic
    intents = intents or FakeIntents()
    submit = submit or FakeSubmit()
    history = history or FakeHistory()
    audit = audit or FakeAudit()
    bus = bus or FakeBus()
    echo_handle = FakeEcho(echo)
    submit.intents = intents
    submit.clock = clock

    async def health_states() -> dict[str, Any]:
        return {unit: SimpleNamespace(state=health_state) for unit in UNIT_IDS}

    controller = hw.HealthWatchController(
        settings=settings or make_settings(),
        policy=POLICY,
        clock=clock,
        observations=observations,
        intents=intents,
        submit=submit,
        history=history,
        audit=audit,
        bus=bus,
        actors={unit: echo_handle for unit in UNIT_IDS},
        health_states=health_states,
        parked_units=lambda: parked,
        latched_stop_units=lambda: stopped,
    )
    handles = {
        "clock": clock,
        "observations": observations,
        "intents": intents,
        "submit": submit,
        "history": history,
        "audit": audit,
        "bus": bus,
        "echo": echo_handle,
    }
    return controller, handles


def make_observation(
    *,
    battery_watts: float = 0.0,
    grid_power_w: float = -100.0,
    load_power_w: float = 100.0,
    soc_pct: float = 80.0,
    debug_mode_w: int = 0,
    lifecycle: Any = None,
    captured_at_mono: float = START_MONO,
) -> Any:
    return SimpleNamespace(
        battery_watts=battery_watts,
        grid_power_w=grid_power_w,
        load_power_w=load_power_w,
        authoritative_soc_pct=soc_pct,
        debug_mode_w=debug_mode_w,
        lifecycle=lifecycle or UnitLifecycle.ARMED_IDLE,
        captured_at_mono=captured_at_mono,
    )


def fleet_observations(**per_unit: Any) -> dict[str, Any]:
    """A quiet, healthy, armed fleet unless a unit overrides a field."""
    defaults = {
        "battery_watts": 0.0,
        "grid_power_w": -100.0,
        "load_power_w": 120.0,
        "soc_pct": 80.0,
        "debug_mode_w": 0,
    }
    latest: dict[str, Any] = {}
    for unit in UNIT_IDS:
        values = dict(defaults)
        for key, per in per_unit.items():
            unit_name, _, field_name = key.partition("__")
            if unit_name == unit:
                values[field_name] = per
        latest[unit] = make_observation(**values)
    return latest


def historian_row(
    unit: str,
    at: datetime,
    *,
    soc: float = 80.0,
    watts: float = 0.0,
    grid: float = -100.0,
    load: float = 120.0,
    mode: int = 0,
) -> Any:
    return SimpleNamespace(
        unit_id=unit,
        sampled_at=at,
        bms_soc_pct=soc,
        system_soc_pct=soc,
        battery_watts=watts,
        grid_power_w=grid,
        load_power_w=load,
        debug_mode_w=mode,
    )


def evidence_rows(
    *,
    hours: int = 6,
    step_s: int = 30,
    end: datetime = NIGHT,
    socs: dict[str, float] | None = None,
    watts: dict[str, float] | None = None,
    loads: dict[str, float] | None = None,
    modes: dict[str, int] | None = None,
) -> tuple[Any, ...]:
    """A full-coverage evidence window for the fleet (default: healthy)."""
    socs = socs or {"lhs": 80.0, "mid": 80.0, "rhs": 80.0}
    watts = watts or {"lhs": 0.0, "mid": 0.0, "rhs": 0.0}
    loads = loads or {"lhs": 120.0, "mid": 120.0, "rhs": 120.0}
    modes = modes or {"lhs": 0, "mid": 0, "rhs": 0}
    rows = []
    for offset in range(0, hours * 3600, step_s):
        at = (end - timedelta(seconds=hours * 3600) + timedelta(seconds=offset)).astimezone(UTC)
        for unit in UNIT_IDS:
            rows.append(
                historian_row(
                    unit,
                    at,
                    soc=socs.get(unit, 80.0),
                    watts=watts.get(unit, 0.0),
                    load=loads.get(unit, 120.0),
                    mode=modes.get(unit, 0),
                )
            )
    return tuple(rows)


def bind_state(controller: hw.HealthWatchController, handles: dict[str, Any]) -> None:
    """Bind the run-program loop's phase view (B023-safe per iteration)."""
    handles["controller"] = controller

    def state() -> str:
        return controller.state_payload()["phase"]

    handles["state"] = state


async def run_program(handles: dict[str, Any], *, seconds: float = 240.0) -> None:
    """Drive the controller one tick per second until the program is done."""
    clock: FakeClock = handles["clock"]
    deadline = clock.now + seconds
    controller_view = handles["controller"]
    while clock.now < deadline:
        await controller_view.tick()
        if handles["state"]() == "done":
            return
        clock.advance(1.0)
    raise AssertionError("the program did not reach done inside the budget")


# --- the pure verdict arithmetic (§6.3) ---------------------------------------------


def test_measured_class_thresholds() -> None:
    """0.5 x 300 = 150 W on >= 0.8 of samples is the delivery band."""
    samples = [261.0] * 20
    assert hw.measured_class(samples, probe_w=300, pass_fraction=0.5,
                             pass_sample_frac=0.8, still_w=50) == ("met", 20)
    # 15/20 = 0.75 < 0.8: not met, and the mean abs is far above still.
    samples = [261.0] * 15 + [0.0] * 5
    assert hw.measured_class(samples, probe_w=300, pass_fraction=0.5,
                             pass_sample_frac=0.8, still_w=50) == ("partial", 15)
    # The fleet-bias band passes with margin (87% and 96% of command).
    for share in (0.87, 0.96, 1.15):
        samples = [300.0 * share] * 20
        assert hw.measured_class(samples, probe_w=300, pass_fraction=0.5,
                                 pass_sample_frac=0.8, still_w=50)[0] == "met"


def test_measured_class_spike_pattern_never_passes() -> None:
    """90% at zero, 10% at 2x command: qualifying share 0.1, mean 60 W —
    below the band and above the still band: partial, never met."""
    samples = [0.0] * 18 + [600.0] * 2
    measured, qualifying = hw.measured_class(
        samples, probe_w=300, pass_fraction=0.5, pass_sample_frac=0.8, still_w=50
    )
    assert (measured, qualifying) == ("partial", 2)


def test_measured_class_still_is_sustained() -> None:
    """A spectator (< 50 W) is still; one metering blip does not un-still it."""
    assert hw.measured_class([20.0] * 20, probe_w=300, pass_fraction=0.5,
                             pass_sample_frac=0.8, still_w=50) == ("still", 0)
    assert hw.measured_class([20.0] * 19 + [90.0], probe_w=300, pass_fraction=0.5,
                             pass_sample_frac=0.8, still_w=50) == ("still", 0)


def test_the_full_verdict_matrix_a3() -> None:
    """Every measured x echo x baseline cell routes to its pinned verdict."""
    matrix = [
        # (measured, echo, baseline, confounded, expected)
        ("met", hw.ECHO_MATCHES_WRITE, True, False, hw.PROBE_PASS),
        ("met", hw.ECHO_MATCHES_WRITE, False, False, hw.PROBE_FAIL_BASELINE),
        ("met", hw.ECHO_OBJECTIVE_NOT_SERVED, True, False, hw.PROBE_INCONCLUSIVE_ECHO),
        ("met", hw.ECHO_EXTERNAL_WRITER, True, False, hw.PROBE_INCONCLUSIVE_ECHO),
        ("met", hw.ECHO_UNREADABLE, True, False, hw.PROBE_INCONCLUSIVE_ECHO),
        ("partial", hw.ECHO_MATCHES_WRITE, True, False, hw.PROBE_FAIL_PARTIAL),
        ("partial", hw.ECHO_OBJECTIVE_NOT_SERVED, True, False, hw.PROBE_FAIL_PARTIAL),
        ("still", hw.ECHO_MATCHES_WRITE, True, False, hw.PROBE_FAIL_NO_RESPONSE),
        ("still", hw.ECHO_OBJECTIVE_NOT_SERVED, True, False, hw.PROBE_INCONCLUSIVE_ECHO),
        ("still", hw.ECHO_EXTERNAL_WRITER, True, False, hw.PROBE_INCONCLUSIVE_ECHO),
        ("still", hw.ECHO_UNREADABLE, True, False, hw.PROBE_INCONCLUSIVE_ABORTED),
        ("partial", hw.ECHO_UNREADABLE, True, False, hw.PROBE_INCONCLUSIVE_ABORTED),
    ]
    for measured, echo, baseline, confounded, expected in matrix:
        verdict = hw.probe_verdict(
            measured=measured,
            echo=echo,
            baseline_returned=baseline,
            demand_move_confounded=confounded,
        )
        assert verdict == expected, (measured, echo, baseline, expected)


def test_the_echoed_and_dead_row_is_the_r_eligible_class() -> None:
    """§6.3 row 5 EXACTLY: stillness + echo_matches_write + baseline returned
    is fail_no_response — the write path proven fine, the actuation dead."""
    assert (
        hw.probe_verdict(
            measured="still",
            echo=hw.ECHO_MATCHES_WRITE,
            baseline_returned=True,
            demand_move_confounded=False,
        )
        == hw.PROBE_FAIL_NO_RESPONSE
    )


def test_the_still_and_not_served_row_is_advisory_only() -> None:
    """§6.3 row 6: stillness + objective_not_served is inconclusive_echo_
    mismatch — Stage R is inapplicable to the not-served presentation."""
    assert (
        hw.probe_verdict(
            measured="still",
            echo=hw.ECHO_OBJECTIVE_NOT_SERVED,
            baseline_returned=True,
            demand_move_confounded=False,
        )
        == hw.PROBE_INCONCLUSIVE_ECHO
    )


def test_the_a8_demand_move_downgrades_only_the_baseline_judged_rows() -> None:
    """A mid-leg demand move confounds the BASELINE judgment: the met/still
    rows under a matching echo downgrade; the echo-mismatch rows do not."""
    assert (
        hw.probe_verdict(
            measured="met",
            echo=hw.ECHO_MATCHES_WRITE,
            baseline_returned=True,
            demand_move_confounded=True,
        )
        == hw.PROBE_INCONCLUSIVE_CONFOUNDED
    )
    assert (
        hw.probe_verdict(
            measured="still",
            echo=hw.ECHO_MATCHES_WRITE,
            baseline_returned=True,
            demand_move_confounded=True,
        )
        == hw.PROBE_INCONCLUSIVE_CONFOUNDED
    )
    assert (
        hw.probe_verdict(
            measured="still",
            echo=hw.ECHO_OBJECTIVE_NOT_SERVED,
            baseline_returned=True,
            demand_move_confounded=True,
        )
        == hw.PROBE_INCONCLUSIVE_ECHO
    )


def test_the_tiers_follow_section_11() -> None:
    for verdict in (
        hw.PROBE_FAIL_NO_RESPONSE,
        hw.PROBE_FAIL_PARTIAL,
        hw.PROBE_FAIL_BASELINE,
    ):
        assert hw.probe_tier(verdict) == hw.TIER_ALERT
    for verdict in (hw.PROBE_PASS, hw.PROBE_INCONCLUSIVE_ECHO, "skipped:unit_disarmed"):
        assert hw.probe_tier(verdict) == hw.TIER_NOTICE
    assert hw.census_tier(1, 2) == hw.TIER_NOTICE
    assert hw.census_tier(2, 2) == hw.TIER_ALERT
    assert hw.census_tier(3, 2) == hw.TIER_ALERT


# --- Stage C: the census (§5) --------------------------------------------------------


async def test_the_exhibiting_signature_flags_stuck_suspected() -> None:
    """rhs's own fingerprint: SoC pinned 97, still, dead load CT, siblings
    flowing — all five predicates hold and the first night is a notice."""
    rows = evidence_rows(
        socs={"lhs": 80.0, "mid": 80.0, "rhs": 97.0},
        watts={"lhs": 600.0, "mid": 600.0, "rhs": 20.0},
        loads={"lhs": 150.0, "mid": 150.0, "rhs": 16.0},
    )
    controller, handles = make_controller(
        history=FakeHistory(rows=rows),
        # Census-only staging for the zero-writes pin: the program's whole
        # surface is the read-only evaluation.
        settings=make_settings(stages=("census",)),
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations(rhs__soc_pct=97.0)
    await run_program(handles, seconds=30)
    payload = controller.state_payload()
    census = {unit["unit_id"]: unit["census"] for unit in payload["units"]}
    assert census["rhs"]["verdict"] == hw.CENSUS_STUCK
    assert census["rhs"]["nights"] == 1
    assert census["rhs"]["tier"] == hw.TIER_NOTICE
    assert census["rhs"]["predicates"] == {
        "full": True,
        "still": True,
        "house_needed": True,
        "no_ct_view": True,
        "modes_normal": True,
    }
    assert census["lhs"]["verdict"] == hw.CENSUS_NOMINAL
    # ZERO writes: the census never submits anything, ever.
    assert handles["submit"].submissions == []
    assert payload["units"][0]["recovery"] == {"mode": "uncommissioned"}
    # The row carries the full predicate vector + the window provenance.
    rows_written = [
        e for e in handles["audit"].appended if e.event_type == "health_census_recorded"
    ]
    assert len(rows_written) == 3
    rhs_row = next(e for e in rows_written if e.unit_id == "rhs")
    assert rhs_row.payload["verdict"] == hw.CENSUS_STUCK
    assert rhs_row.payload["predicates"]["no_ct_view"] is True
    assert rhs_row.payload["evidence_window_h"] == 6
    assert rhs_row.payload["night"] == "2026-08-25"
    # The event rides the bus at the notice tier.
    events = [e for e in handles["bus"].events if e["type"] == "health.census"]
    assert any(e["payload"]["unit_id"] == "rhs" for e in events)


async def test_each_predicates_fail_edge_renders_nominal() -> None:
    """One predicate failing is enough: the stuck flag needs ALL five."""
    base = dict(
        socs={"lhs": 80.0, "mid": 80.0, "rhs": 97.0},
        watts={"lhs": 600.0, "mid": 600.0, "rhs": 20.0},
        loads={"lhs": 150.0, "mid": 150.0, "rhs": 16.0},
    )
    variants = {
        "S1 soc": dict(socs={"lhs": 80.0, "mid": 80.0, "rhs": 90.0}),
        "S2 still": dict(watts={"lhs": 600.0, "mid": 600.0, "rhs": 400.0}),
        "S3 house needed": dict(
            watts={"lhs": 0.0, "mid": 0.0, "rhs": 20.0},
            loads={"lhs": 150.0, "mid": 150.0, "rhs": 16.0},
        ),
        "S4 ct view": dict(loads={"lhs": 150.0, "mid": 150.0, "rhs": 140.0}),
        "S5 modes": dict(modes={"lhs": 0, "mid": 0, "rhs": 3}),
    }
    for name, override in variants.items():
        rows = evidence_rows(**{**base, **override})
        controller, handles = make_controller(
            history=FakeHistory(rows=rows), settings=make_settings(stages=("census",))
        )
        bind_state(controller, handles)
        handles["observations"].latest = fleet_observations()
        await run_program(handles, seconds=30)
        payload = controller.state_payload()
        census = {unit["unit_id"]: unit["census"] for unit in payload["units"]}
        assert census["rhs"]["verdict"] == hw.CENSUS_NOMINAL, name


async def test_s3s_either_corroborator_suffices() -> None:
    """Import beyond standby OR any sibling actively flowing — each alone
    corroborates that the house needed them."""
    stuck_kwargs = dict(
        socs={"lhs": 80.0, "mid": 80.0, "rhs": 97.0},
        watts={"lhs": 600.0, "mid": 0.0, "rhs": 20.0},
        loads={"lhs": 150.0, "mid": 150.0, "rhs": 16.0},
    )
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows(**stuck_kwargs)),
        settings=make_settings(stages=("census",)),
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    await run_program(handles, seconds=30)
    payload = controller.state_payload()
    census = {unit["unit_id"]: unit["census"] for unit in payload["units"]}
    assert census["rhs"]["verdict"] == hw.CENSUS_STUCK


async def test_historian_gaps_render_degraded_evidence() -> None:
    """A window with too few rows renders NO verdict, never a wrong one."""
    rows = evidence_rows()[:60]  # twenty minutes of a six-hour window
    controller, handles = make_controller(history=FakeHistory(rows=rows))
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    await run_program(handles, seconds=600)
    payload = controller.state_payload()
    census = {unit["unit_id"]: unit["census"] for unit in payload["units"]}
    assert all(unit["verdict"] == hw.CENSUS_DEGRADED for unit in census.values())
    # Degraded units are never probed: their probe verdict says so.
    probe = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    assert probe["lhs"]["verdict"] == "skipped:census_degraded_evidence"


@pytest.mark.parametrize(
    ("state", "parked", "expected"),
    [
        ("healthy", frozenset({"rhs"}), "excluded:parked"),
        ("unreachable", frozenset(), "excluded:unreachable"),
        ("not_responding", frozenset(), "excluded:not_responding"),
        ("foreign_writer", frozenset(), "excluded:foreign_writer"),
        ("inhibited", frozenset(), "excluded:inhibited"),
    ],
)
async def test_excluded_classes_are_named_and_owned_elsewhere(
    state: str, parked: frozenset[str], expected: str
) -> None:
    """S5's exclusion set: the census never re-classifies another surface's
    state — it names the class and renders no verdict of its own."""
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()),
        health_state=state,
        parked=parked,
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    await run_program(handles, seconds=600)
    payload = controller.state_payload()
    census = {unit["unit_id"]: unit["census"] for unit in payload["units"]}
    assert census["rhs"]["verdict"] == expected


async def test_a_vendor_mode_word_excludes_the_unit() -> None:
    controller, handles = make_controller(history=FakeHistory(rows=evidence_rows()))
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations(rhs__debug_mode_w=4)
    await run_program(handles, seconds=600)
    payload = controller.state_payload()
    census = {unit["unit_id"]: unit["census"] for unit in payload["units"]}
    assert census["rhs"]["verdict"] == "excluded:vendor_mode"


async def test_persistence_promotes_the_flag_to_alert() -> None:
    """Two consecutive stuck nights: notice -> alert (§5's promotion)."""
    seeded = []
    for night in ("2026-08-23", "2026-08-24"):
        seeded.append(
            SimpleNamespace(
                event_type="health_census_recorded",
                unit_id="rhs",
                payload={"night": night, "verdict": hw.CENSUS_STUCK},
            )
        )
    rows = evidence_rows(
        socs={"lhs": 80.0, "mid": 80.0, "rhs": 97.0},
        watts={"lhs": 600.0, "mid": 600.0, "rhs": 20.0},
        loads={"lhs": 150.0, "mid": 150.0, "rhs": 16.0},
    )
    audit = FakeAudit(seeded=seeded)
    controller, handles = make_controller(
        history=FakeHistory(rows=rows), audit=audit, settings=make_settings(stages=("census",))
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    await run_program(handles, seconds=30)
    payload = controller.state_payload()
    census = {unit["unit_id"]: unit["census"] for unit in payload["units"]}
    assert census["rhs"]["verdict"] == hw.CENSUS_STUCK
    assert census["rhs"]["nights"] == 3
    assert census["rhs"]["tier"] == hw.TIER_ALERT


async def test_a_nominal_night_between_stucks_caps_the_streak() -> None:
    """Stuck 08-24, nominal 08-23, stuck tonight: TWO consecutive stuck
    nights (tonight is the second), never a longer streak."""
    seeded = [
        SimpleNamespace(
            event_type="health_census_recorded",
            unit_id="rhs",
            payload={"night": "2026-08-24", "verdict": hw.CENSUS_STUCK},
        ),
        SimpleNamespace(
            event_type="health_census_recorded",
            unit_id="rhs",
            payload={"night": "2026-08-23", "verdict": hw.CENSUS_NOMINAL},
        ),
    ]
    rows = evidence_rows(
        socs={"lhs": 80.0, "mid": 80.0, "rhs": 97.0},
        watts={"lhs": 600.0, "mid": 600.0, "rhs": 20.0},
        loads={"lhs": 150.0, "mid": 150.0, "rhs": 16.0},
    )
    audit = FakeAudit(seeded=seeded)
    controller, handles = make_controller(
        history=FakeHistory(rows=rows), audit=audit, settings=make_settings(stages=("census",))
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    await run_program(handles, seconds=30)
    payload = controller.state_payload()
    census = {unit["unit_id"]: unit["census"] for unit in payload["units"]}
    assert census["rhs"]["nights"] == 2
    assert census["rhs"]["tier"] == hw.TIER_ALERT


# --- Stage P: the probe (§6) -----------------------------------------------------------


async def run_probe_night(
    *,
    delivery: dict[str, list[float]] | None = None,
    echo: str = hw.ECHO_MATCHES_WRITE,
    returns_to_baseline: bool = True,
    demand_at_cancel: float | None = None,
    overrides: dict[str, Any] | None = None,
    settings: hw.HealthWatchSettings | None = None,
    history_rows: tuple[Any, ...] | None = None,
) -> tuple[hw.HealthWatchController, dict[str, Any]]:
    """Drive one full night's program with a scripted probe response.

    The fake fleet's telemetry follows the leg machine's own observable
    state: while no probe intent is live the pods are idle (the baseline and
    return windows); while one is live (settle/sustain) the scripted
    delivery is served.  ``returns_to_baseline=False`` holds the measured
    watts high through the return window; ``demand_at_cancel`` moves the
    fleet grid import once the cancel lands (A8's confound).
    """
    controller, handles = make_controller(
        echo=echo,
        settings=settings,
        history=FakeHistory(rows=history_rows if history_rows is not None else evidence_rows()),
    )
    observations: FakeObservations = handles["observations"]
    clock: FakeClock = handles["clock"]
    overrides = overrides or {}
    delivery = delivery or {}

    def refresh() -> None:
        leg = getattr(controller, "_leg", None)
        step = getattr(leg, "step", None)
        live_intent = bool(handles["intents"].entries)
        latest = fleet_observations(**overrides)
        for unit, sequence in delivery.items():
            if live_intent and sequence:
                watts = sequence[0]
            elif step == "return" and not returns_to_baseline:
                # The not-returned leg: the measured watts stay high through
                # the return window (a held 400 W against a ~0 baseline).
                watts = 400.0
            else:
                watts = 0.0
            latest[unit] = SimpleNamespace(**{**latest[unit].__dict__, "battery_watts": watts})
        if demand_at_cancel is not None and step in {"echo", "return"}:
            # A8's mid-leg demand move: the fleet import rises by the time
            # the cancel's quiet re-check reads it.
            for unit, observation in latest.items():
                latest[unit] = SimpleNamespace(
                    **{**observation.__dict__, "grid_power_w": -demand_at_cancel}
                )
        observations.latest = latest

    for _ in range(500):
        if controller.state_payload()["phase"] == "done":
            break
        refresh()
        await controller.tick()
        clock.advance(1.0)
    return controller, handles


async def test_a_delivering_pod_passes() -> None:
    controller, handles = await run_probe_night(
        delivery={"lhs": [261.0], "mid": [261.0], "rhs": [261.0]},
        echo=hw.ECHO_MATCHES_WRITE,
    )
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] == hw.PROBE_PASS, (unit, probes[unit])
        assert probes[unit]["core_samples"] > 0
        assert probes[unit]["qualifying_samples"] == probes[unit]["core_samples"]
        assert probes[unit]["echo"] == hw.ECHO_MATCHES_WRITE
        assert probes[unit]["probe_w"] == 300
    # The submission is ONE discharge intent at probe_w, renewed per cycle.
    assert handles["submit"].submissions
    assert all(
        s["direction"] == "discharge" and s["watts"] == 300 and s["unit_ids"] == [s["unit_ids"][0]]
        for s in handles["submit"].submissions
    )
    # The cancel is deliberate: the probe's own intents were removed through
    # the intent path (never mere non-renewal).
    assert handles["intents"].removed


async def test_the_spectator_fails_no_response() -> None:
    """Still + echo_matches_write + returned: the echoed-and-dead row."""
    controller, handles = await run_probe_night(
        delivery={"lhs": [20.0], "mid": [20.0], "rhs": [20.0]},
        echo=hw.ECHO_MATCHES_WRITE,
    )
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] == hw.PROBE_FAIL_NO_RESPONSE
    # Alert tier on the fail class (§11).
    events = [e for e in handles["bus"].events if e["type"] == "health.probe"]
    assert events and all(e["payload"]["tier"] == hw.TIER_ALERT for e in events)
    # The row carries the figures: commanded/measured, qualifying count,
    # echo classification, baseline figures, verdict class (§10).
    row = next(
        e
        for e in handles["audit"].appended
        if e.event_type == "health_probe_completed" and e.unit_id == "lhs"
    )
    assert row.payload["verdict"] == hw.PROBE_FAIL_NO_RESPONSE
    assert row.payload["echo"] == hw.ECHO_MATCHES_WRITE
    assert row.payload["qualifying_samples"] == 0
    assert row.payload["returned_to_baseline"] is True
    assert row.payload["export_note"] == hw.EXPORT_HONESTY_NOTE


async def test_the_degraded_pod_fails_partial() -> None:
    controller, _ = await run_probe_night(
        delivery={"lhs": [90.0], "mid": [90.0], "rhs": [90.0]},
        echo=hw.ECHO_MATCHES_WRITE,
    )
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] == hw.PROBE_FAIL_PARTIAL


async def test_a_not_returned_baseline_is_its_own_failure() -> None:
    controller, _ = await run_probe_night(
        delivery={"lhs": [261.0], "mid": [261.0], "rhs": [261.0]},
        echo=hw.ECHO_MATCHES_WRITE,
        returns_to_baseline=False,
    )
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] == hw.PROBE_FAIL_BASELINE


async def test_still_and_not_served_is_inconclusive_advisory() -> None:
    controller, _ = await run_probe_night(
        delivery={"lhs": [20.0], "mid": [20.0], "rhs": [20.0]},
        echo=hw.ECHO_OBJECTIVE_NOT_SERVED,
    )
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] == hw.PROBE_INCONCLUSIVE_ECHO


async def test_an_unreadable_echo_aborts() -> None:
    controller, _ = await run_probe_night(
        delivery={"lhs": [20.0], "mid": [20.0], "rhs": [20.0]},
        echo=hw.ECHO_UNREADABLE,
    )
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] == hw.PROBE_INCONCLUSIVE_ABORTED


async def test_a_mid_leg_demand_move_confounds_the_baseline() -> None:
    controller, _ = await run_probe_night(
        delivery={"lhs": [261.0], "mid": [261.0], "rhs": [261.0]},
        echo=hw.ECHO_MATCHES_WRITE,
        demand_at_cancel=600.0,
    )
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] == hw.PROBE_INCONCLUSIVE_CONFOUNDED


async def test_the_spiked_pod_never_passes_end_to_end() -> None:
    """90% at zero, 10% at 2x: the share rule refuses it end to end."""
    spike = [0.0] * 9 + [600.0]
    controller, _ = await run_probe_night(
        delivery={"lhs": spike, "mid": spike, "rhs": spike},
        echo=hw.ECHO_MATCHES_WRITE,
    )
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] != hw.PROBE_PASS


@pytest.mark.parametrize(
    ("overrides", "parked", "stopped", "state", "expected"),
    [
        ({"rhs__debug_mode_w": 1}, frozenset(), frozenset(), "healthy", "excluded:parked"),
        ({"rhs__debug_mode_w": 3}, frozenset(), frozenset(), "healthy", "excluded:vendor_mode"),
        ({}, frozenset({"rhs"}), frozenset(), "healthy", "excluded:parked"),
        ({}, frozenset(), frozenset({"rhs"}), "healthy", "excluded:latched_stop"),
        ({}, frozenset(), frozenset(), "unreachable", "excluded:unreachable"),
        ({}, frozenset(), frozenset(), "not_responding", "excluded:not_responding"),
        ({}, frozenset(), frozenset(), "foreign_writer", "excluded:foreign_writer"),
        ({}, frozenset(), frozenset(), "inhibited", "excluded:inhibited"),
        ({"rhs__lifecycle": UnitLifecycle.DISARMED}, frozenset(), frozenset(), "healthy",
         "skipped:unit_disarmed"),
    ],
)
async def test_the_skip_if_set_renders_each_skip(
    overrides: dict[str, Any],
    parked: frozenset[str],
    stopped: frozenset[str],
    state: str,
    expected: str,
) -> None:
    """Every skip is a recorded verdict with its reason — never silence,
    never a failure (§4).  The states another surface owns exclude the unit
    at the CENSUS (S5's exclusion set: named, owned elsewhere); the arm
    requirement is the PROBE's own skip (the program never arms)."""
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()),
        health_state=state,
        parked=parked,
        stopped=stopped,
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations(
        rhs__debug_mode_w=overrides.get("rhs__debug_mode_w", 0),
        rhs__lifecycle=overrides.get("rhs__lifecycle", UnitLifecycle.ARMED_IDLE),
    )
    await run_program(handles, seconds=600)
    payload = controller.state_payload()
    unit = next(u for u in payload["units"] if u["unit_id"] == "rhs")
    if expected.startswith("excluded:"):
        assert unit["census"]["verdict"] == expected
        assert unit["probe"]["verdict"] == "skipped:census_excluded"
    else:
        assert unit["census"]["verdict"] == hw.CENSUS_NOMINAL
        assert unit["probe"]["verdict"] == expected
    row = next(
        e
        for e in handles["audit"].appended
        if e.event_type == "health_probe_completed" and e.unit_id == "rhs"
    )
    assert row.payload["verdict"] == unit["probe"]["verdict"]


async def test_a_live_foreign_intent_skips_the_probe() -> None:
    controller, handles = make_controller(history=FakeHistory(rows=evidence_rows()))
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    long_claim = dict(duration_s=10_000.0)
    # A live manual claim anywhere also defers the whole night at window open
    # (the quiet-window check) — so the claim arrives at the probe phase
    # instead: the census runs quiet, the probe skips.
    await controller.tick()  # window evaluation (quiet: nothing seeded yet)
    await controller.tick()  # census
    handles["intents"].entries = [
        _FakeIntent("manual-1", "manual", ("rhs",), accepted_at_mono=START_MONO, **long_claim)
    ]
    for _ in range(400):
        if controller.state_payload()["phase"] == "done":
            break
        await controller.tick()
        handles["clock"].advance(1.0)
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    assert probes["rhs"]["verdict"] == "skipped:under_intent"
    # The unclaimed siblings still ran (an honest skip, never a fleet fail).
    assert probes["lhs"]["verdict"] is not None


async def test_stale_telemetry_skips_the_probe() -> None:
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()), auto_stamp=False
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    # Fresh at the census, frozen afterward: rhs is probed last, and its
    # captured word has aged past the commissioned bound by then — the
    # PROBE's own staleness skip, not the census's exclusion.
    handles["observations"].latest = fleet_observations()
    await run_program(handles, seconds=600)
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    assert probes["rhs"]["verdict"] == "skipped:telemetry_stale"


async def test_the_quiet_load_gate_reads_the_grid_import_words() -> None:
    """Fleet-mean grid IMPORT above the gate skips the probe (A8): the
    control-grade PCS word, never the load-CT mean."""
    controller, handles = make_controller(history=FakeHistory(rows=evidence_rows()))
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations(
        **{f"{unit}__grid_power_w": -1200.0 for unit in UNIT_IDS}
    )
    await run_program(handles, seconds=600)
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    assert probes["lhs"]["verdict"] == "skipped:quiet_load_gate"


async def test_a_missing_grid_word_skips_on_stale_evidence() -> None:
    """Any unit's grid word missing: the gate is unjudgeable, the night's
    probes skip with quiet_evidence_stale — never run on bad evidence."""
    controller, handles = make_controller(history=FakeHistory(rows=evidence_rows()))
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    latest = fleet_observations()
    observation = latest["mid"]
    latest["mid"] = SimpleNamespace(
        **{**observation.__dict__, "grid_power_w": None}
    )
    handles["observations"].latest = latest
    await run_program(handles, seconds=240)
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    assert probes["lhs"]["verdict"] == "skipped:quiet_evidence_stale"


async def test_a_manual_claim_mid_probe_preempts_never_fails() -> None:
    controller, handles = make_controller(history=FakeHistory(rows=evidence_rows()))
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    await controller.tick()
    await controller.tick()
    preempted = False
    for _ in range(200):
        state = controller.state_payload()
        if state["phase"] == "done":
            break
        if len(handles["submit"].submissions) >= 3 and not preempted:
            preempted = True
            handles["intents"].entries.append(
                _FakeIntent(
                    "manual-9", "manual", ("lhs",), accepted_at_mono=START_MONO, duration_s=10_000.0
                )
            )
        # A delivering pod so the preempted verdict is not a still-verdict.
        handles["observations"].latest = fleet_observations(
            **{f"{u}__battery_watts": 261.0 for u in UNIT_IDS}
        )
        await controller.tick()
        handles["clock"].advance(1.0)
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    assert probes["lhs"]["verdict"] == hw.PROBE_INCONCLUSIVE_PREEMPTED


async def test_a_schedule_claim_does_not_preempt() -> None:
    """A12: ``optimizer`` outranks ``schedule`` — a schedule claim mid-probe
    does NOT preempt; the leg runs to its verdict."""
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()), echo=hw.ECHO_MATCHES_WRITE
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    await controller.tick()
    await controller.tick()
    claimed = False
    for _ in range(300):
        state = controller.state_payload()
        if state["phase"] == "done":
            break
        if len(handles["submit"].submissions) >= 3 and not claimed:
            claimed = True
            handles["intents"].entries.append(
                _FakeIntent(
                    "schedule-1", "schedule", ("lhs",),
                    accepted_at_mono=START_MONO, duration_s=10_000.0,
                )
            )
        handles["observations"].latest = fleet_observations(
            **{f"{u}__battery_watts": 261.0 for u in UNIT_IDS}
        )
        await controller.tick()
        handles["clock"].advance(1.0)
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    assert probes["lhs"]["verdict"] == hw.PROBE_PASS


async def test_probes_run_sequentially_with_the_inter_unit_gap() -> None:
    """lhs -> mid -> rhs, strictly one at a time (§6.1)."""
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()),
        echo=hw.ECHO_MATCHES_WRITE,
        settings=make_settings(
            probe=hw.ProbeSettings(
                settle_s=2, sustain_s=4, baseline_return_s=2, inter_unit_gap_s=5
            )
        ),
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    active_units: list[tuple[float, set[str]]] = []
    handles["observations"].latest = fleet_observations()
    await controller.tick()
    await controller.tick()
    for _ in range(400):
        if controller.state_payload()["phase"] == "done":
            break
        units = {tuple(s["unit_ids"]) for s in handles["submit"].submissions}
        active_units.append((handles["clock"].now, units))
        handles["observations"].latest = fleet_observations(
            **{f"{u}__battery_watts": 261.0 for u in UNIT_IDS}
        )
        await controller.tick()
        handles["clock"].advance(1.0)
    # Every submission named exactly ONE unit.
    assert all(len(s["unit_ids"]) == 1 for s in handles["submit"].submissions)
    # And the ORDER was sorted: lhs before mid before rhs.
    order = [s["unit_ids"][0] for s in handles["submit"].submissions]
    first_seen: dict[str, int] = {}
    for index, unit in enumerate(order):
        first_seen.setdefault(unit, index)
    assert first_seen["lhs"] < first_seen["mid"] < first_seen["rhs"]
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    for unit in UNIT_IDS:
        assert probes[unit]["verdict"] == hw.PROBE_PASS


async def test_no_probe_starts_after_the_deadline() -> None:
    """I2: the window's remaining probes after the deadline are skipped,
    never started late."""
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()),
        settings=make_settings(
            # Default leg bounds: ~45 s per unit, so lhs finishes inside the
            # 30-second window and mid/rhs fall past the no-new-ACT line.
            deadline_local=time(23, 5, 30),
        ),
    )
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    for _ in range(600):
        if controller.state_payload()["phase"] == "done":
            break
        handles["observations"].latest = fleet_observations(
            **{f"{u}__battery_watts": 261.0 for u in UNIT_IDS}
        )
        await controller.tick()
        handles["clock"].advance(1.0)
    payload = controller.state_payload()
    probes = [unit["probe"]["verdict"] for unit in payload["units"]]
    assert any(v == "skipped:deadline_passed" for v in probes)


# --- the frame (§4): once-per-night, quiet window, restart reconstruction -------------


async def test_before_the_window_the_program_awaits() -> None:
    clock = FakeClock(wall=datetime(2026, 8, 25, 21, 0, 0, tzinfo=ZONE))
    controller, handles = make_controller(clock=clock, history=FakeHistory(rows=evidence_rows()))
    await controller.tick()
    payload = controller.state_payload()
    assert payload["phase"] == "await_window"
    assert handles["submit"].submissions == []
    assert handles["audit"].appended == []


async def test_a_live_claim_at_window_open_defers_the_night() -> None:
    """§4's quiet-window verification: window_not_quiet defers to the next
    night — never fights, never wedges itself in."""
    controller, handles = make_controller(history=FakeHistory(rows=evidence_rows()))
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()
    handles["intents"].entries = [
        _FakeIntent("schedule-1", "schedule", ("lhs",), accepted_at_mono=START_MONO)
    ]
    await controller.tick()
    payload = controller.state_payload()
    assert payload["phase"] == "done"
    assert payload["reason"] == hw.REASON_WINDOW_NOT_QUIET
    program_events = [e for e in handles["bus"].events if e["type"] == "health.program"]
    assert program_events and program_events[0]["payload"]["reason"] == hw.REASON_WINDOW_NOT_QUIET
    # The deferral is DURABLE: a restart the same night does not re-run.
    seeded = [
        SimpleNamespace(
            event_type="health_program_recorded",
            unit_id=None,
            payload={"night": "2026-08-25", "reason": hw.REASON_WINDOW_NOT_QUIET},
        )
    ]
    controller2, handles2 = make_controller(
        history=FakeHistory(rows=evidence_rows()), audit=FakeAudit(seeded=seeded)
    )
    await controller2.tick()
    assert controller2.state_payload()["phase"] == "done"
    assert handles2["submit"].submissions == []


async def test_a_night_with_rows_never_re_runs() -> None:
    """I8/A4: 'an attempt' is ANY health-watch row for that unit that civil
    night — one crash retires the night's program unambiguously."""
    seeded = [
        SimpleNamespace(
            event_type="health_census_recorded",
            unit_id="lhs",
            payload={"night": "2026-08-25", "verdict": hw.CENSUS_NOMINAL},
        )
    ]
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()), audit=FakeAudit(seeded=seeded)
    )
    await controller.tick()
    payload = controller.state_payload()
    assert payload["phase"] == "done"
    assert handles["submit"].submissions == []
    # No census rows were re-written for the retired night.
    assert not [e for e in handles["audit"].appended if e.event_type == "health_census_recorded"]


async def test_an_interrupted_program_alerts_naming_the_unit_state() -> None:
    """A4's C/P shape: rows for the night WITHOUT a complete program raise
    the alert naming the state each unit was actually left in."""
    seeded = [
        SimpleNamespace(
            event_type="health_census_recorded",
            unit_id="lhs",
            payload={"night": "2026-08-25", "verdict": hw.CENSUS_NOMINAL},
        ),
        SimpleNamespace(
            event_type="health_probe_completed",
            unit_id="lhs",
            payload={"night": "2026-08-25", "verdict": hw.PROBE_PASS},
        ),
        # mid and rhs have no probe rows: the program is incomplete.
    ]
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()), audit=FakeAudit(seeded=seeded)
    )
    handles["observations"].latest = fleet_observations()
    await controller.tick()
    payload = controller.state_payload()
    assert payload["phase"] == "done"
    assert payload["reason"] == hw.REASON_INTERRUPTED
    events = [e for e in handles["bus"].events if e["type"] == "health.program"]
    assert events and events[0]["payload"]["tier"] == hw.TIER_ALERT
    # The alert names the state of the units the interrupted program had
    # reached (lhs carries the night's rows); the unreached units' night is
    # simply retired with it.
    states = events[0]["payload"]["units"]
    assert set(states) == {"lhs"}
    assert states["lhs"]["lifecycle"] == "armed_idle"
    assert states["lhs"]["parked"] is False
    assert states["lhs"]["debug_mode_w"] == 0
    assert handles["submit"].submissions == []


async def test_a_completed_program_does_not_raise_the_interrupted_alert() -> None:
    """All units carry last-stage rows: the night stands done, no false
    'interrupted' alert on a restart after completion."""
    seeded = []
    for unit in UNIT_IDS:
        seeded.append(
            SimpleNamespace(
                event_type="health_census_recorded",
                unit_id=unit,
                payload={"night": "2026-08-25", "verdict": hw.CENSUS_NOMINAL},
            )
        )
        seeded.append(
            SimpleNamespace(
                event_type="health_probe_completed",
                unit_id=unit,
                payload={"night": "2026-08-25", "verdict": hw.PROBE_PASS},
            )
        )
    controller, handles = make_controller(
        history=FakeHistory(rows=evidence_rows()), audit=FakeAudit(seeded=seeded)
    )
    await controller.tick()
    payload = controller.state_payload()
    assert payload["phase"] == "done"
    assert payload["reason"] is None
    assert not [e for e in handles["bus"].events if e["type"] == "health.program"]


# --- the projection (§10) ---------------------------------------------------------------


async def test_the_projection_renders_the_honest_shapes() -> None:
    controller, handles = make_controller(history=FakeHistory(rows=evidence_rows()))
    await controller.tick()
    payload = controller.state_payload()
    assert payload["stages"] == ["census", "probe"]
    assert payload["window"] == {"opens_local": "23:00", "deadline_local": "23:45"}
    assert payload["phase"] in {"await_window", "census", "probe", "record", "done"}
    assert payload["night"] == "2026-08-25"
    for unit in payload["units"]:
        # Uncommissioned stages render their honesty, never absence-that-
        # looks-like-health.
        assert unit["recovery"] == {"mode": "uncommissioned"}
        assert set(unit["census"]) == {"verdict", "nights", "predicates", "tier"}
        assert set(unit["probe"]) == {
            "verdict",
            "probe_w",
            "qualifying_samples",
            "core_samples",
            "echo",
        }


def test_a_failed_health_watch_refusal_is_well_formed() -> None:
    refusal = hw.HealthWatchRefusal(hw.HEALTH_WATCH_NOT_COMMISSIONED, "not composed")
    assert refusal.code == hw.HEALTH_WATCH_NOT_COMMISSIONED
    with pytest.raises(ValueError):
        hw.HealthWatchRefusal(" bad ", "x")


# --- the structural pin: no write response exists ----------------------------------------


def test_no_code_path_responds_to_a_probe_verdict_with_any_write() -> None:
    """The I10-adjacent rule, restated for the Stage-R era: a probe verdict
    ALONE still triggers nothing — only the §7.1 conjunction (census flag AND
    ``fail_no_response``) composes a cycle, and the module's write reach is
    exactly the injected composer ports.  The source holds no transport and
    no actor-lifecycle method; the ONE park and ONE resume call ride the
    injected ``_park_control`` port (the ParkController's own guarded
    primitive), the disarm/re-arm ride the injected lifecycle ports."""
    from pathlib import Path

    source = Path(hw.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "write_debug_mode",
        "request_debug_mode_change(",
        "request_bounded_zero(",
        "emergency_stop(",
    ):
        assert forbidden not in source, forbidden
    # The recovery composer's mode writes reach ONLY the injected port.
    assert source.count(".park(") == 1 and "control.park(" in source
    assert source.count(".resume(") == 1 and "control.resume(" in source
    # And the lifecycle reach is the two injected ports, never an actor.
    assert ".disarm(" not in source and ".arm(" not in source


def test_the_application_module_imports_no_adapters() -> None:
    """The historian port pattern (T-BHW-ARCHITECTURE): the application
    module imports no adapters (providers or transport) anywhere."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(hw.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(not alias.name.startswith("energypod.adapters") for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("energypod.adapters")


async def test_a_guard_refusal_of_the_dispatch_is_a_skip_never_a_failure() -> None:
    """§2: every standing guard judges the probe through the ordinary
    dispatch path; a refusal is a SKIP with that guard's reason."""
    controller, handles = make_controller(history=FakeHistory(rows=evidence_rows()))
    handles["controller"] = controller
    handles["state"] = lambda: controller.state_payload()["phase"]
    handles["observations"].latest = fleet_observations()

    refused = True

    async def refusing_submit(**kwargs: Any) -> dict[str, Any]:
        if refused:
            raise ValueError("unit not dispatchable")
        return {"intent_id": "health-1", "status": "accepted"}

    handles["submit"].__class__ = type(
        "RefusingSubmit", (FakeSubmit,), {"__call__": staticmethod(refusing_submit)}
    )
    for _ in range(600):
        if controller.state_payload()["phase"] == "done":
            break
        await controller.tick()
        handles["clock"].advance(1.0)
    payload = controller.state_payload()
    probes = {unit["unit_id"]: unit["probe"] for unit in payload["units"]}
    assert probes["lhs"]["verdict"] == "skipped:dispatch_refused"
    events = [e for e in handles["bus"].events if e["type"] == "health.probe"]
    assert events and all(e["payload"]["tier"] == hw.TIER_NOTICE for e in events)
