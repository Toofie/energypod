"""T-CAL-TRIGGER/ELIGIBILITY/TRAVERSE/FROZEN-WORD/TAPER/MEASUREMENT/ECONOMICS
/PROJECTION + the C1-C16 amendment legs.

DESIGN_CALIBRATION_CYCLING (CONTRACT v1.1) §3/§4/§5/§6/§8/§11, tested against
``energypod.application.calibration``:

- the trigger: days-since from hourly rollup minima, the C1 shape (a
  never-deep pod is due on its HORIZON AGE, flagged, never ineligible by
  absence), the evidence-short deferral and its CLEARING, gap exclusion;
- eligibility: the lhs-class exclusion, mid-class throughput, the rhs-class
  interlock (deferred / unlocked by a pass / kept deferred by a fail), the
  health-watch-absent class, selection by greatest days-since with
  deterministic ties, the C6 one-shot overriding SELECTION only and consumed
  exactly once;
- the traverse: the deadline-rate math, the stop-set ordering (the floor
  pre-submission invariant), non-renewal stops only, the energy bound's
  co-computation, C9's integration discipline, the deadline miss, preemption,
  the C3 skip and own-claim exclusion, the full skip-if vocabulary, the C2
  restart reconstruction, and advise submitting nothing on any tick;
- the frozen word end-to-end (the measurement-first failure) and C4's
  late-step companion;
- the close: the taper signature, the hold, the attribution split with C5's
  semantics, graduation's four members with C10's quality gate, the durable
  anchored fact, and the stand-down;
- the economics (C7's both branches), the projection's additive keys, and
  the architecture pin: no write method exists anywhere on the path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from energypod.application import calibration as cal

ZONE = ZoneInfo("Australia/Brisbane")
UNIT_IDS = ("lhs", "mid", "rhs")
PLAN_AT = datetime(2026, 8, 25, 14, 0, 0, tzinfo=ZONE)
START_MONO = 10_000.0
POLICY = SimpleNamespace(
    max_telemetry_age_s=60.0,
    max_soc_disagreement_pct=5.0,
    maximum_soc_jump_pct=5.0,
    maximum_soc_pct=95.0,
    minimum_soc_pct=10.0,
    max_unit_discharge_w=2500,
)


# --- deterministic fakes ---------------------------------------------------------


@dataclass
class FakeClock:
    now: float = START_MONO
    wall: datetime = PLAN_AT

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

    def jump_to(self, wall: datetime) -> None:
        self.wall = wall
        self.now += max(0.0, (wall - PLAN_AT).total_seconds())


@dataclass
class FakeObservations:
    latest: dict[str, Any] = field(default_factory=dict)

    async def all_latest(self) -> dict[str, Any]:
        return dict(self.latest)


def observation(
    *,
    soc: float = 100.0,
    system: float | None = 100.0,
    watts: float = 0.0,
    spread: float = 30.0,
    ccl: float = 3000.0,
    lifecycle: str = "armed_idle",
    debug: int = 0,
    captured: float | None = None,
    system_quality: str = "good",
) -> SimpleNamespace:
    return SimpleNamespace(
        authoritative_soc_pct=soc,
        system_soc_pct=system,
        battery_watts=watts,
        cell_spread_mv=spread,
        dynamic_charge_limit_w=ccl,
        dynamic_discharge_limit_w=3000.0,
        lifecycle=lifecycle,
        debug_mode_w=debug,
        captured_at_mono=captured if captured is not None else START_MONO,
        quality={"system_soc_pct": system_quality, "bms_soc_pct": "good"},
    )


@dataclass
class FakeIntent:
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
    intents: FakeIntents | None = None
    clock: FakeClock | None = None
    refuse: bool = False

    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> dict[str, Any]:
        if self.refuse:
            raise RuntimeError("the facade refused")
        self.submissions.append(
            {
                "unit_ids": sorted(unit_ids),
                "direction": getattr(direction, "value", direction),
                "watts": watts,
                "ttl_s": ttl_s,
            }
        )
        intent_id = f"cal-{len(self.submissions)}"
        if self.intents is not None:
            now = 0.0 if self.clock is None else self.clock.now
            self.intents.entries.append(
                FakeIntent(intent_id, "optimizer", tuple(sorted(unit_ids)), accepted_at_mono=now)
            )
        return {"intent_id": intent_id, "status": "accepted"}


def rollup(
    unit_id: str,
    hour_start: datetime,
    *,
    soc_min: float,
    watts_mean: float = 0.0,
    quality: str = "good",
) -> SimpleNamespace:
    return SimpleNamespace(
        unit_id=unit_id,
        hour_start=hour_start.astimezone(UTC),
        metrics={
            "bms_soc_pct": (soc_min, soc_min + 2.0, soc_min + 1.0),
            "battery_watts": (watts_mean - 10.0, watts_mean + 10.0, watts_mean),
        },
        worst_quality=quality,
    )


def hour(day: date, hour_of_day: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour_of_day, tzinfo=ZONE)


@dataclass
class FakeHistory:
    rollups: tuple[Any, ...] = ()
    sample_rows: tuple[Any, ...] = ()

    def rollup_hours(
        self, unit_ids: Any, from_at: datetime, to_at: datetime
    ) -> tuple[Any, ...]:
        return tuple(
            row
            for row in self.rollups
            if row.unit_id in set(unit_ids) and from_at <= row.hour_start <= to_at
        )

    def samples(self, unit_ids: Any, from_at: datetime, to_at: datetime) -> tuple[Any, ...]:
        return tuple(
            row
            for row in self.sample_rows
            if row.unit_id in set(unit_ids) and from_at <= row.sampled_at <= to_at
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


def seeded_row(
    event_type: str,
    unit_id: str | None,
    payload: dict[str, Any],
    *,
    occurred_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=event_type,
        unit_id=unit_id,
        payload=payload,
        occurred_at=occurred_at or PLAN_AT.astimezone(UTC),
    )


def make_settings(**overrides: Any) -> cal.CalibrationSettings:
    values: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "mode": "act",
        "window_local": time(15, 0),
        "traverse_end_local": time(22, 30),
        "plan_local": time(14, 0),
        "trigger": cal.CalibrationTriggerSettings(
            trigger_floor_pct=30.0,
            trigger_after_days=60,
            eligibility_window_days=14,
            cycles_daily_min_days=5,
            min_daily_throughput_wh=1000.0,
            throughput_min_days=3,
        ),
        "traverse": cal.CalibrationTraverseSettings(
            floor_pct=10.0,
            discharge_w=800,
            min_discharge_w=200,
            intent_ttl_s=4.0,
            assumed_delivery_frac=0.8,
            # The walking legs advance 120 s per tick, so the test bound
            # widens; the C9 gap leg constructs its own adviser at the
            # contract's 10 s default.
            integration_max_gap_s=300.0,
            energy_margin_wh=20.0,
            metering_allowance_wh=5.0,
            # C8's SUM rule at these margins: (10-5)/100 x 500 = 25 >= 25.
            assumed_capacity_wh={"lhs": 500, "mid": 500, "rhs": 500},
        ),
        "top_anchor": cal.CalibrationTopAnchorSettings(
            taper_deadline_local=time(12, 0),
            taper_soc_pct=99.0,
            taper_limit_w=0,
            taper_sustain_s=2,
            hold_min_s=1800,
            hold_float_w=100,
            poor_surplus_kwh=3.0,
        ),
        "measurement": cal.CalibrationMeasurementSettings(
            reanchor_delta_pct=2.0,
            floor_epsilon_pct=2.0,
        ),
        "unit_ids": UNIT_IDS,
    }
    values.update(overrides)
    return cal.CalibrationSettings(**values)


def make_adviser(
    *,
    clock: FakeClock | None = None,
    observations: FakeObservations | None = None,
    intents: FakeIntents | None = None,
    submit: FakeSubmit | None = None,
    history: FakeHistory | None = None,
    audit: FakeAudit | None = None,
    bus: FakeBus | None = None,
    health_state: str = "healthy",
    parked: frozenset[str] = frozenset(),
    stopped: frozenset[str] = frozenset(),
    watch_stages: tuple[str, ...] = ("census", "probe"),
    settings: cal.CalibrationSettings | None = None,
    tariff: dict[str, float] | None = None,
) -> tuple[cal.CalibrationAdviser, dict[str, Any]]:
    clock = clock or FakeClock()
    observations = observations or FakeObservations()
    intents = intents or FakeIntents()
    submit = submit or FakeSubmit()
    history = history or FakeHistory()
    audit = audit or FakeAudit()
    bus = bus or FakeBus()
    submit.intents = intents
    submit.clock = clock

    async def health_states() -> dict[str, Any]:
        return {unit: SimpleNamespace(state=health_state) for unit in UNIT_IDS}

    adviser = cal.CalibrationAdviser(
        settings=settings or make_settings(),
        policy=POLICY,
        clock=clock,
        observations=observations,
        intents=intents,
        submit=submit,
        history=history,
        audit=audit,
        bus=bus,
        health_states=health_states,
        health_watch_stages=lambda: watch_stages,
        parked_units=lambda: parked,
        latched_stop_units=lambda: stopped,
        tariff=tariff,
        night_window_end_local=time(6, 0),
    )
    handles = {
        "clock": clock,
        "observations": observations,
        "intents": intents,
        "submit": submit,
        "history": history,
        "audit": audit,
        "bus": bus,
    }
    return adviser, handles


def events(audit: FakeAudit, event_type: str) -> list[Any]:
    return [row for row in audit.appended if row.event_type == event_type]


def payload_of(row: Any) -> dict[str, Any]:
    return dict(row.payload)


# --- the historian seed: the fleet the contract itself describes -----------------
#
# lhs cycles daily (below 25% on >= 5 of the last 14 dates -> excluded); mid
# never went deep but flows 3.3 kWh/day (throughput evidence -> eligible,
# the designed first target); rhs never went deep and flows nothing.


def fleet_rollups(today: date) -> tuple[Any, ...]:
    rows: list[Any] = []
    horizon_start = today - timedelta(days=70)
    day = horizon_start
    while day <= today:
        hours = (6, 12, 18)
        lhs_deep = (today - day).days < 14 and (today - day).days % 3 != 0
        for hour_of_day in hours:
            rows.append(
                rollup(
                    "lhs",
                    hour(day, hour_of_day),
                    soc_min=12.0 if lhs_deep else 40.0,
                    watts_mean=300.0,
                )
            )
            rows.append(rollup("mid", hour(day, hour_of_day), soc_min=100.0, watts_mean=450.0))
            rows.append(rollup("rhs", hour(day, hour_of_day), soc_min=99.0, watts_mean=0.0))
        day += timedelta(days=1)
    return tuple(rows)


@pytest.mark.asyncio
async def test_trigger_horizon_bounded_never_deep_pod_is_due() -> None:
    """T-CAL-TRIGGER: the C1 shape — mid never went below 30% since the
    historian's first rollup, so it is DUE on its HORIZON AGE with
    ``horizon_bounded`` flagged; ``None`` exists nowhere in the arithmetic."""
    today = date(2026, 8, 25)
    adviser, handles = make_adviser(history=FakeHistory(rollups=fleet_rollups(today)))
    await adviser.tick()
    row = events(handles["audit"], "calibration_trigger_evaluated")
    assert len(row) == 1
    units = {entry["unit_id"]: entry for entry in payload_of(row[0])["units"]}
    mid = units["mid"]
    assert mid["class"] == cal.CLASS_ELIGIBLE
    assert mid["due"] is True
    assert mid["horizon_bounded"] is True
    assert mid["last_deep_date"] is None
    assert mid["days_since_deep"] == mid["horizon_days"] == 70
    assert payload_of(row[0])["selected"] == "mid"
    # The rhs-class interlock: throughput absent, no probe row -> deferred.
    assert units["rhs"]["class"] == cal.CLASS_DEFERRED_PROBE
    # lhs's own regime provides the bottom anchor (>= 5 sub-25% dates).
    assert units["lhs"]["class"] == cal.CLASS_EXCLUDED_CYCLES
    assert units["lhs"]["sub_floor_dates_14d"] >= 5  # >= cycles_daily_min_days


@pytest.mark.asyncio
async def test_trigger_deep_date_resets_the_clock() -> None:
    """T-CAL-TRIGGER: a date's rollup minimum below X resets the clock; above
    does not; the evidence-short deferral defers instead of minting a
    verdict, and clears into a horizon-bounded due."""
    today = date(2026, 8, 25)
    recent = tuple(
        rollup("mid", hour(today - timedelta(days=offset), 12), soc_min=20.0)
        for offset in range(5)
    )
    vector = cal.trigger_vector(
        {today - timedelta(days=offset): cal.DayFigures(soc_min_pct=20.0) for offset in range(5)},
        today=today,
        trigger_floor_pct=30.0,
        after_days=3,
    )
    assert vector.evidence_short is False
    assert vector.days_since_deep == 0
    assert vector.horizon_bounded is False
    # The never-deep + young-horizon deferral: no verdict from a short window.
    young = cal.trigger_vector(
        {today - timedelta(days=2): cal.DayFigures(soc_min_pct=100.0)},
        today=today,
        trigger_floor_pct=30.0,
        after_days=60,
    )
    assert young.evidence_short is True and young.due is False
    assert recent  # the fleet seed keeps the same spelling the scan reads


@pytest.mark.asyncio
async def test_trigger_excludes_degraded_dates_never_interpolates() -> None:
    """T-CAL-TRIGGER: a date whose rollups are missing or degraded is
    EXCLUDED from the scan — a bad-quality rollup never feeds the minimum."""
    rollups = (
        rollup("mid", hour(date(2026, 6, 1), 12), soc_min=8.0),
        rollup("mid", hour(date(2026, 6, 2), 12), soc_min=8.0, quality="bad"),
    )
    daily = cal.rollup_daily(rollups, unit_id="mid", zone=ZONE)
    assert daily[date(2026, 6, 1)].soc_min_pct == 8.0
    assert date(2026, 6, 2) not in daily


@pytest.mark.asyncio
async def test_rhs_interlock_unlocked_by_passing_probe_and_absent_watch_defers() -> None:
    """T-CAL-ELIGIBILITY: the rhs-class interlock — deferred with no passing
    probe in the window, UNLOCKED by a ``pass`` row (C11), kept deferred by a
    ``fail_*`` verdict; a health-watch-less site defers
    ``no_control_evidence``, never a guess."""
    today = date(2026, 8, 25)
    seed = fleet_rollups(today)
    probe_pass = seeded_row(
        "health_probe_completed",
        "rhs",
        {"verdict": "pass", "night": (today - timedelta(days=2)).isoformat(), "as_of": "x"},
    )
    adviser, handles = make_adviser(
        history=FakeHistory(rollups=seed), audit=FakeAudit(seeded=[probe_pass])
    )
    await adviser.tick()
    units = {
        entry["unit_id"]: entry
        for entry in payload_of(
            events(handles["audit"], "calibration_trigger_evaluated")[0]
        )["units"]
    }
    assert units["rhs"]["class"] == cal.CLASS_ELIGIBLE
    assert units["rhs"]["probe_verdict"] == "pass"

    probe_fail = seeded_row(
        "health_probe_completed", "rhs", {"verdict": "fail_partial", "night": today.isoformat()}
    )
    adviser2, handles2 = make_adviser(
        history=FakeHistory(rollups=seed), audit=FakeAudit(seeded=[probe_fail])
    )
    await adviser2.tick()
    units2 = {
        entry["unit_id"]: entry
        for entry in payload_of(
            events(handles2["audit"], "calibration_trigger_evaluated")[0]
        )["units"]
    }
    assert units2["rhs"]["class"] == cal.CLASS_DEFERRED_PROBE

    adviser3, handles3 = make_adviser(
        history=FakeHistory(rollups=seed), watch_stages=("census",)
    )
    await adviser3.tick()
    units3 = {
        entry["unit_id"]: entry
        for entry in payload_of(
            events(handles3["audit"], "calibration_trigger_evaluated")[0]
        )["units"]
    }
    assert units3["rhs"]["class"] == cal.CLASS_NO_CONTROL_EVIDENCE


@pytest.mark.asyncio
async def test_selection_greatest_days_since_deep_deterministic_ties() -> None:
    """T-CAL-ELIGIBILITY: the target is the GREATEST days-since-deep
    (horizon-bounded figures competing on their honest lower bounds), ties
    break by unit id, and one target per civil night is derived from rows."""
    today = date(2026, 8, 25)
    rows: list[Any] = []
    for offset in range(65):
        day = today - timedelta(days=offset)
        for hour_of_day in (6, 18):
            rows.append(rollup("lhs", hour(day, hour_of_day), soc_min=100.0, watts_mean=600.0))
            rows.append(rollup("mid", hour(day, hour_of_day), soc_min=100.0, watts_mean=600.0))
    # mid went deep 10 days ago; lhs 40 days ago -> lhs wins on the number.
    rows.append(rollup("mid", hour(today - timedelta(days=10), 6), soc_min=22.0, watts_mean=600.0))
    rows.append(rollup("lhs", hour(today - timedelta(days=40), 6), soc_min=22.0, watts_mean=600.0))
    adviser, handles = make_adviser(history=FakeHistory(rollups=tuple(rows)))
    await adviser.tick()
    assert (
        payload_of(events(handles["audit"], "calibration_trigger_evaluated")[0])["selected"]
        == "lhs"
    )


@pytest.mark.asyncio
async def test_request_measurement_selects_once_and_waives_due_only() -> None:
    """T-CAL-TRIGGER: the C6 one-shot — consumed at plan_local with the
    ``due_waived`` record; a RESTART (the same config revision re-read) never
    consumes the same request twice; a probe-deferred named unit still
    defers (the waiver is selection authority only)."""
    today = date(2026, 8, 25)
    seed = fleet_rollups(today)
    settings = make_settings(request_measurement=("mid", "2026-08-25 operator request"))
    adviser, handles = make_adviser(history=FakeHistory(rollups=seed), settings=settings)
    await adviser.tick()
    row = payload_of(events(handles["audit"], "calibration_trigger_evaluated")[0])
    assert row["selected"] == "mid"
    assert row["due_waived"] == "request_measurement"
    assert row["request_measurement"]["consumed"] is True
    # The restart: a fresh adviser over the SAME durable rows (the config
    # revision still carries the request) does not consume it again.
    adviser2, handles2 = make_adviser(
        history=FakeHistory(rollups=seed),
        audit=FakeAudit(seeded=list(handles["audit"].appended)),
        settings=settings,
    )
    await adviser2.tick()
    row2 = payload_of(events(handles2["audit"], "calibration_trigger_evaluated")[0])
    assert row2["request_measurement"]["consumed_prior"] is True
    assert row2["due_waived"] is None
    # The deferred named unit still defers: every class gate still applies.
    settings_rhs = make_settings(request_measurement=("rhs", "note"))
    adviser3, handles3 = make_adviser(history=FakeHistory(rollups=seed), settings=settings_rhs)
    await adviser3.tick()
    row3 = payload_of(events(handles3["audit"], "calibration_trigger_evaluated")[0])
    assert row3["selected"] != "rhs"  # the named deferred unit still defers
    assert row3["due_waived"] is None  # the waiver never applied


# --- the traverse ---------------------------------------------------------------


def traverse_handles(**observation_kwargs: Any) -> tuple[cal.CalibrationAdviser, dict[str, Any]]:
    """A live leg on mid: the plan named it, the window is open, the first
    tick submitted.  The observation starts at 100% with 800 W flowing."""
    today = date(2026, 8, 25)
    clock = FakeClock(wall=datetime(2026, 8, 25, 15, 0, 30, tzinfo=ZONE))
    obs = FakeObservations(latest={unit: observation() for unit in UNIT_IDS})
    obs.latest["mid"] = observation(**observation_kwargs)
    adviser, handles = make_adviser(
        clock=clock,
        observations=obs,
        history=FakeHistory(rollups=fleet_rollups(today)),
    )
    return adviser, handles


@pytest.mark.asyncio
async def test_traverse_opens_submits_and_stops_at_floor_presubmission() -> None:
    """T-CAL-TRAVERSE: the floor member fires PRE-SUBMISSION — the tick that
    touches the floor submits nothing, and the kernel never sees an
    at-or-below-floor intent (the named invariant); stops are non-renewal
    only (no stop triple, no idle intent, no zero-watt submission)."""
    adviser, handles = traverse_handles()
    await adviser.tick()  # 15:00:30 — the plan named mid; the window is open
    submits = handles["submit"].submissions
    assert adviser.state_payload()["phase"] == "traversing"
    assert submits, "the traverse opens with a discharge submission"
    assert all(entry["direction"] == "discharge" for entry in submits)
    assert all(entry["watts"] >= 200 for entry in submits)
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    # Walk the pod down to the floor: 30 s per tick at 800 W on 500 Wh.
    soc = 100.0
    count_before = len(submits)
    while soc > 12.0:
        soc -= 800.0 * 30.0 / (36.0 * 500.0)
        clock.advance(30.0)
        obs.latest["mid"] = observation(soc=soc, watts=800.0, captured=clock.now, system=soc)
        await adviser.tick()
    floor_tick_submits = len(submits)
    assert floor_tick_submits > count_before
    # The tick AT the floor submits NOTHING: the stop is pre-submission.
    clock.advance(30.0)
    obs.latest["mid"] = observation(soc=10.0, watts=0.0, captured=clock.now, system=12.0)
    await adviser.tick()
    assert len(submits) == floor_tick_submits
    completed = events(handles["audit"], "calibration_cycle_completed")
    assert len(completed) == 1
    body = payload_of(completed[0])
    assert body["verdict"] == cal.VERDICT_FLOOR_REACHED
    assert body["stop_members"]["floor_reached"] is True
    assert body["trace_class"] == cal.TRACE_MONOTONE
    assert body["pinned_sentence"] == cal.PINNED_SENTENCE
    assert handles["submit"].submissions  # the architectural pin: it DID act


@pytest.mark.asyncio
async def test_traverse_deadline_rate_self_corrects_and_clamps() -> None:
    """T-CAL-TRAVERSE: the §4.2 math — required recomputed from MEASURED SoC,
    clamped to [min, cap], at-risk rendered when required exceeds the cap."""
    rate, at_risk = cal.deadline_rate_w(
        soc_pct=100.0,
        floor_pct=10.0,
        capacity_wh=5000.0,
        remaining_s=7.5 * 3600,
        cap_w=800,
        min_w=200,
    )
    assert rate == 600 and at_risk is False
    behind, at_risk2 = cal.deadline_rate_w(
        soc_pct=100.0,
        floor_pct=10.0,
        capacity_wh=5000.0,
        remaining_s=3600.0,
        cap_w=800,
        min_w=200,
    )
    assert behind == 800 and at_risk2 is True
    ahead, _ = cal.deadline_rate_w(
        soc_pct=12.0,
        floor_pct=10.0,
        capacity_wh=5000.0,
        remaining_s=7 * 3600,
        cap_w=800,
        min_w=200,
    )
    assert ahead == 200  # the sensing-clear floor clamp


@pytest.mark.asyncio
async def test_frozen_word_traverse_stops_on_the_energy_bound() -> None:
    """T-CAL-FROZEN-WORD: a pod whose SoC word never moves while watts flow —
    the floor member never fires, the energy bound stops the traverse, the
    alert fires, the trace class is ``frozen``, NO graduation, stand-down
    until acknowledgement."""
    adviser, handles = traverse_handles()
    await adviser.tick()
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    while adviser.state_payload().get("traverse") is not None:
        clock.advance(30.0)
        obs.latest["mid"] = observation(soc=100.0, watts=800.0, captured=clock.now, system=100.0)
        await adviser.tick()
    completed = events(handles["audit"], "calibration_cycle_completed")
    body = payload_of(completed[0])
    assert body["verdict"] == cal.VERDICT_FLOOR_MISS_ENERGY
    assert body["trace_class"] == cal.TRACE_FROZEN
    assert body["tier"] == cal.TIER_ALERT
    # The energy bound is the co-computed figure from the START SoC.
    assert body["energy_bound_wh"] == pytest.approx(450.0 + 20.0, abs=0.5)
    assert body["energy_wh"] >= body["energy_bound_wh"]
    cycle_events = [e for e in handles["bus"].events if e["type"] == cal.CALIBRATION_EVENT_TYPE]
    assert cycle_events[-1]["payload"]["tier"] == cal.TIER_ALERT


@pytest.mark.asyncio
async def test_late_step_twin_satisfies_graduation_c() -> None:
    """T-CAL-FROZEN-WORD (C4's companion leg): a word that steps BELOW the
    floor only after the energy-bound stop, inside the close, within
    ``floor_epsilon_pct`` — criterion (c) SATISFIED, graduation proceeds on
    the other members."""
    adviser, handles = traverse_handles()
    await adviser.tick()
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    while adviser.state_payload().get("traverse") is not None:
        clock.advance(30.0)
        obs.latest["mid"] = observation(soc=100.0, watts=800.0, captured=clock.now, system=100.0)
        await adviser.tick()
    completed = events(handles["audit"], "calibration_cycle_completed")
    assert payload_of(completed[0])["verdict"] == cal.VERDICT_FLOOR_MISS_ENERGY
    # The close: the word STEPS to 11.0 (<= floor 10 + epsilon 2) and the
    # system word moves with it (a material delta change, quality GOOD).
    clock.jump_to(datetime(2026, 8, 26, 10, 0, 0, tzinfo=ZONE))
    obs.latest["mid"] = observation(soc=11.0, system=8.5, watts=0.0, captured=clock.now)
    await adviser.tick()
    anchored, failed = cal.graduation(
        trace=cal.TRACE_STEPPED,
        delta_change_pct=3.0,
        delta_quality_ok=True,
        verdict=cal.VERDICT_FLOOR_MISS_ENERGY,
        late_step_pct=11.0,
        floor_pct=10.0,
        floor_epsilon_pct=2.0,
        taper_observed=False,
        attribution=cal.TOP_ANCHOR_MISSED_SOLAR,
    )
    assert anchored is True and failed is None


@pytest.mark.asyncio
async def test_integration_gap_ends_the_leg_never_interpolates() -> None:
    """T-CAL-TRAVERSE (C9): a sample gap beyond ``integration_max_gap_s``
    ends the leg ``aborted:telemetry_lost`` — the bound is never evaluated
    across a gap."""
    today = date(2026, 8, 25)
    clock = FakeClock(wall=datetime(2026, 8, 25, 15, 0, 30, tzinfo=ZONE))
    obs = FakeObservations(latest={unit: observation() for unit in UNIT_IDS})
    obs.latest["mid"] = observation(soc=100.0, watts=800.0)
    settings = make_settings(
        traverse=cal.CalibrationTraverseSettings(
            floor_pct=10.0,
            discharge_w=800,
            min_discharge_w=200,
            intent_ttl_s=4.0,
            assumed_delivery_frac=0.8,
            integration_max_gap_s=10.0,
            energy_margin_wh=20.0,
            metering_allowance_wh=5.0,
            assumed_capacity_wh={"lhs": 500, "mid": 500, "rhs": 500},
        )
    )
    adviser, handles = make_adviser(
        clock=clock,
        observations=obs,
        history=FakeHistory(rollups=fleet_rollups(today)),
        settings=settings,
    )
    await adviser.tick()
    assert handles["submit"].submissions
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    clock.advance(60.0)
    # The next sample's capture jumps 30 s past the last one (> the 10 s
    # bound): a staleness-class event by construction.
    obs.latest["mid"] = observation(soc=95.0, watts=800.0, captured=clock.now + 30.0)
    await adviser.tick()
    completed = events(handles["audit"], "calibration_cycle_completed")
    assert payload_of(completed[0])["verdict"] == "aborted:telemetry_lost"
    assert payload_of(completed[0])["tier"] == cal.TIER_NOTICE


@pytest.mark.asyncio
async def test_deadline_miss_closes_honestly_at_the_depth_reached() -> None:
    """T-CAL-TRAVERSE: the deadline arrives with the floor unmet and the
    bound unmet -> ``floor_miss_deadline``, alert tier, the record says a
    partial traverse anchors nothing rather than pretending otherwise."""
    adviser, handles = traverse_handles()
    await adviser.tick()
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    last_capture = clock.now
    clock.jump_to(datetime(2026, 8, 25, 22, 31, 0, tzinfo=ZONE))
    obs.latest["mid"] = observation(soc=80.0, watts=0.0, captured=last_capture + 30.0)
    await adviser.tick()
    completed = events(handles["audit"], "calibration_cycle_completed")
    body = payload_of(completed[0])
    assert body["verdict"] == cal.VERDICT_FLOOR_MISS_DEADLINE
    assert body["tier"] == cal.TIER_ALERT


@pytest.mark.asyncio
async def test_manual_claim_preempts_inconclusive_no_rerun() -> None:
    """T-CAL-TRAVERSE: a MANUAL claim arriving mid-traverse preempts it
    instantly -> ``inconclusive_preempted`` (notice), no depth credit, no
    re-run that night."""
    adviser, handles = traverse_handles()
    await adviser.tick()
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    intents: FakeIntents = handles["intents"]
    clock.advance(60.0)
    obs.latest["mid"] = observation(soc=95.0, watts=800.0, captured=clock.now)
    intents.entries.append(
        FakeIntent("manual-1", "manual", ("mid",), accepted_at_mono=clock.now, duration_s=600.0)
    )
    await adviser.tick()
    completed = events(handles["audit"], "calibration_cycle_completed")
    assert payload_of(completed[0])["verdict"] == cal.VERDICT_PREEMPTED
    # No re-run: the night is retired and nothing more is submitted.
    clock.advance(60.0)
    obs.latest["mid"] = observation(soc=95.0, watts=0.0, captured=clock.now)
    intents.entries = [entry for entry in intents.entries if entry.id != "manual-1"]
    await adviser.tick()
    assert len(handles["submit"].submissions) == 1


@pytest.mark.asyncio
async def test_c3_not_own_optimizer_claim_skips_and_own_intent_never_does() -> None:
    """T-CAL-TRAVERSE (C3): a live not-own OPTIMIZER claim (the excess
    adviser at the 15:00 open) excludes the unit at submission — the anchor
    waits, never contests; the adviser's own ``cal-`` intent is never a
    foreign claim."""
    today = date(2026, 8, 25)
    clock = FakeClock(wall=datetime(2026, 8, 25, 15, 0, 30, tzinfo=ZONE))
    obs = FakeObservations(latest={unit: observation() for unit in UNIT_IDS})
    obs.latest["mid"] = observation(soc=100.0, watts=0.0)
    adviser, handles = make_adviser(
        clock=clock,
        observations=obs,
        history=FakeHistory(rollups=fleet_rollups(today)),
    )
    handles["intents"].entries.append(
        FakeIntent("excess-1", "optimizer", ("mid",), accepted_at_mono=clock.now, duration_s=600.0)
    )
    await adviser.tick()
    completed = events(handles["audit"], "calibration_cycle_completed")
    body = payload_of(completed[0])
    assert body["verdict"] == "skipped:optimizer_claim"
    assert handles["submit"].submissions == []
    # The own-intent half: a live submission IS an optimizer intent on the
    # unit, and the next tick must not treat it as a foreign claim.
    handles["intents"].entries = [e for e in handles["intents"].entries if e.id != "excess-1"]
    clock.advance(60.0)
    obs.latest["mid"] = observation(soc=100.0, watts=0.0, captured=clock.now)
    await adviser.tick()  # the night was retired by the skip: still nothing
    assert handles["submit"].submissions == []
    # A fresh night with the own claim live from the first tick:
    clock.jump_to(datetime(2026, 8, 26, 15, 0, 30, tzinfo=ZONE))
    handles["intents"].entries.append(
        FakeIntent("cal-own", "optimizer", ("mid",), accepted_at_mono=clock.now, duration_s=600.0)
    )
    obs.latest["mid"] = observation(soc=100.0, watts=0.0, captured=clock.now)
    await adviser.tick()
    assert handles["submit"].submissions, "the adviser's own cal- intent is never a foreign claim"


@pytest.mark.asyncio
async def test_skip_if_vocabulary_renders_verbatim() -> None:
    """T-CAL-TRAVERSE: the full skip-if set each rendering its skip
    verbatim — parked, latched stop, vendor mode, telemetry staleness,
    disarmed, a SCHEDULE claim, the probe-deferral re-check."""
    today = date(2026, 8, 25)

    async def skip_for(
        *,
        parked: frozenset[str] = frozenset(),
        stopped: frozenset[str] = frozenset(),
        observation_kwargs: dict[str, Any] | None = None,
        intents: list[Any] | None = None,
        plan_probe_deferred: bool = False,
    ) -> str:
        clock = FakeClock(wall=datetime(2026, 8, 25, 15, 0, 30, tzinfo=ZONE))
        kwargs: dict[str, Any] = {"soc": 100.0}
        kwargs.update(observation_kwargs or {})
        obs = FakeObservations(latest={unit: observation() for unit in UNIT_IDS})
        obs.latest["mid"] = observation(**kwargs)
        seed = fleet_rollups(today)
        if plan_probe_deferred:
            seed = tuple(
                row
                for row in seed
                if row.unit_id != "mid"
                or row.metrics["battery_watts"][2] < 1000.0 / 3
            )
            # mid flows nothing: the rhs-class deferral applies to it.
        adviser, handles = make_adviser(
            clock=clock,
            observations=obs,
            history=FakeHistory(rollups=seed),
            parked=parked,
            stopped=stopped,
        )
        for entry in intents or []:
            handles["intents"].entries.append(entry)
        await adviser.tick()
        completed = events(handles["audit"], "calibration_cycle_completed")
        assert completed, "a skip renders as a completed row, never silence"
        return str(payload_of(completed[0])["verdict"])

    assert await skip_for(parked=frozenset({"mid"})) == "skipped:unit_parked"
    assert await skip_for(stopped=frozenset({"mid"})) == "skipped:latched_stop"
    assert await skip_for(observation_kwargs={"debug": 5}) == "skipped:vendor_mode"
    assert (
        await skip_for(observation_kwargs={"captured": START_MONO - 120.0})
        == "skipped:telemetry_stale"
    )
    assert await skip_for(observation_kwargs={"lifecycle": "disarmed"}) == "skipped:unit_disarmed"
    assert (
        await skip_for(
            intents=[
                FakeIntent(
                    "sched-1", "schedule", ("mid",), accepted_at_mono=START_MONO, duration_s=600.0
                )
            ]
        )
        == "skipped:schedule_claim"
    )


@pytest.mark.asyncio
async def test_advise_mode_submits_nothing_on_any_tick() -> None:
    """T-CAL-TRAVERSE: ``advise`` submits no intent on any tick under any
    input (§7's named test) — the plan renders with ``submits: never``."""
    today = date(2026, 8, 25)
    clock = FakeClock(wall=datetime(2026, 8, 25, 15, 0, 30, tzinfo=ZONE))
    obs = FakeObservations(latest={unit: observation() for unit in UNIT_IDS})
    adviser, handles = make_adviser(
        clock=clock,
        observations=obs,
        history=FakeHistory(rollups=fleet_rollups(today)),
        settings=make_settings(mode="advise"),
    )
    for _ in range(5):
        await adviser.tick()
        clock.advance(60.0)
    assert handles["submit"].submissions == []
    state = adviser.state_payload()
    assert state["mode"] == "advise"
    assert state["submits"] == "never"
    assert state["target"] == "mid"  # the plan named it; the posture idles it


@pytest.mark.asyncio
async def test_no_window_no_traverse() -> None:
    """T-CAL-TRAVERSE: the program cannot run outside its window — before
    15:00 and at/after 22:30 nothing opens, whatever the plan says."""
    today = date(2026, 8, 25)
    clock = FakeClock(wall=datetime(2026, 8, 25, 14, 30, 0, tzinfo=ZONE))
    obs = FakeObservations(latest={unit: observation() for unit in UNIT_IDS})
    adviser, handles = make_adviser(
        clock=clock, observations=obs, history=FakeHistory(rollups=fleet_rollups(today))
    )
    await adviser.tick()
    assert handles["submit"].submissions == []
    assert adviser.state_payload()["phase"] == "planned"
    clock.jump_to(datetime(2026, 8, 25, 22, 45, 0, tzinfo=ZONE))
    obs.latest["mid"] = observation(soc=100.0, captured=clock.now)
    await adviser.tick()
    assert handles["submit"].submissions == []


# --- the C2 restart --------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_retires_the_night_never_resumes() -> None:
    """T-CAL-TRAVERSE (C2): a mid-traverse restart writes
    ``inconclusive_interrupted`` from the open row at boot, retires the
    night, raises the morning alert (alert tier iff the pod was left deeper
    than floor + reanchor_delta_pct), and NO same-night resume exists under
    any input."""
    today = date(2026, 8, 25)
    opened = seeded_row(
        "calibration_traverse_opened",
        "mid",
        {
            "night": today.isoformat(),
            "kind": "measurement",
            "start_soc_bms_pct": 100.0,
            "energy_bound_wh": 110.0,
        },
    )
    deep_night = (
        rollup("mid", hour(today, 20), soc_min=9.0),
        rollup("mid", hour(today, 21), soc_min=9.0),
    )
    clock = FakeClock(wall=datetime(2026, 8, 25, 23, 30, 0, tzinfo=ZONE))
    obs = FakeObservations(latest={unit: observation(soc=9.0) for unit in UNIT_IDS})
    adviser, handles = make_adviser(
        clock=clock,
        observations=obs,
        history=FakeHistory(rollups=deep_night),
        audit=FakeAudit(seeded=[opened]),
    )
    await adviser.reconstruct_at_boot()
    completed = events(handles["audit"], "calibration_cycle_completed")
    assert len(completed) == 1
    body = payload_of(completed[0])
    assert body["verdict"] == cal.VERDICT_INTERRUPTED
    assert body["reconstructed"] is True
    # 9.0 < floor 10 + reanchor_delta 2 -> the alert tier (the worry line).
    assert body["tier"] == cal.TIER_ALERT
    assert body["left_deeper_than_graceful"] is True
    # No same-night resume: the next ticks submit nothing (the night retired).
    for _ in range(3):
        await adviser.tick()
        clock.advance(30.0)
    assert handles["submit"].submissions == []
    # The shallow-interruption twin renders the notice tier.
    shallow = seeded_row(
        "calibration_traverse_opened",
        "mid",
        {"night": today.isoformat(), "kind": "measurement", "start_soc_bms_pct": 60.0},
    )
    shallow_night = (rollup("mid", hour(today, 20), soc_min=55.0),)
    adviser2, handles2 = make_adviser(
        clock=FakeClock(wall=datetime(2026, 8, 25, 23, 30, 0, tzinfo=ZONE)),
        history=FakeHistory(rollups=shallow_night),
        audit=FakeAudit(seeded=[shallow]),
    )
    await adviser2.reconstruct_at_boot()
    body2 = payload_of(events(handles2["audit"], "calibration_cycle_completed")[0])
    assert body2["tier"] == cal.TIER_NOTICE


# --- the close, the measurement, the graduation ----------------------------------


@pytest.mark.asyncio
async def test_taper_signature_hold_and_graduation_write_the_anchored_fact() -> None:
    """T-CAL-TAPER/MEASUREMENT: the signature (99% + CCL 0 sustained), the
    hold window observed, graduation's four members, and the durable
    ``calibration_anchored`` fact written exactly once."""
    adviser, handles = traverse_handles()
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    await adviser.tick()
    soc = 100.0
    while adviser.state_payload().get("traverse") is not None:
        soc -= 800.0 * 30.0 / (36.0 * 500.0)
        clock.advance(30.0)
        obs.latest["mid"] = observation(soc=soc, watts=800.0, captured=clock.now, system=soc)
        await adviser.tick()
    # The morning: the taper signature lands and holds.
    clock.jump_to(datetime(2026, 8, 26, 9, 30, 0, tzinfo=ZONE))
    obs.latest["mid"] = observation(soc=99.5, system=96.0, ccl=0.0, watts=20.0, captured=clock.now)
    await adviser.tick()  # the signature begins
    clock.advance(5.0)  # sustain 2 s
    obs.latest["mid"] = observation(soc=99.5, system=96.0, ccl=0.0, watts=20.0, captured=clock.now)
    await adviser.tick()  # the taper is observed; the hold opens
    clock.advance(1800.0)  # the full 30-minute hold window
    obs.latest["mid"] = observation(soc=99.5, system=96.0, ccl=0.0, watts=20.0, captured=clock.now)
    await adviser.tick()  # the hold completes; the close finishes
    audit: FakeAudit = handles["audit"]
    taper = payload_of(events(audit, "calibration_taper_observed")[0])
    assert taper["taper_observed_at"] is not None
    assert taper["hold"]["completed"] is True
    assert taper["graduation"] == cal.GRADUATION_ANCHORED
    anchored = events(audit, "calibration_anchored")
    assert len(anchored) == 1
    assert anchored[0].unit_id == "mid"
    # The routine-rotation receipt rides the next plan (mid's next cycle
    # is kind ROUTINE): roll to the next day's plan tick and re-read.
    clock.jump_to(datetime(2026, 8, 26, 14, 0, 30, tzinfo=ZONE))
    await adviser.tick()
    state = adviser.state_payload()
    mid_row = {row["unit_id"]: row for row in state["units"]}.get("mid")
    assert mid_row is not None and mid_row["anchored"] is True
    assert mid_row["kind"] == cal.KIND_ROUTINE


@pytest.mark.asyncio
async def test_hold_interruption_is_honest_never_protected() -> None:
    """T-CAL-TAPER: ``hold_interrupted`` honesty — the house draws the pod
    down inside the window, the hold records the partial, and no write
    protects it (the anchor was had at the taper)."""
    adviser, handles = traverse_handles()
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    await adviser.tick()
    soc = 100.0
    while adviser.state_payload().get("traverse") is not None:
        soc -= 800.0 * 30.0 / (36.0 * 500.0)
        clock.advance(30.0)
        obs.latest["mid"] = observation(soc=soc, watts=800.0, captured=clock.now, system=soc)
        await adviser.tick()
    clock.jump_to(datetime(2026, 8, 26, 9, 30, 0, tzinfo=ZONE))
    obs.latest["mid"] = observation(soc=99.5, system=96.0, ccl=0.0, watts=20.0, captured=clock.now)
    await adviser.tick()
    clock.advance(5.0)
    await adviser.tick()  # taper observed; hold opens on the NEXT inside tick
    clock.advance(60.0)
    obs.latest["mid"] = observation(soc=99.5, system=96.0, ccl=0.0, watts=20.0, captured=clock.now)
    await adviser.tick()  # inside: the hold starts
    clock.advance(30.0)
    obs.latest["mid"] = observation(
        soc=97.0, system=94.0, ccl=300.0, watts=900.0, captured=clock.now
    )
    await adviser.tick()  # house draw: interrupted
    audit: FakeAudit = handles["audit"]
    taper = payload_of(events(audit, "calibration_taper_observed")[0])
    assert taper["hold"]["interrupted"] is not None
    assert taper["hold"]["interrupted"]["fraction"] < 1.0


@pytest.mark.asyncio
async def test_attribution_split_c5_semantics() -> None:
    """T-CAL-TAPER: the attribution split — poor recorded surplus renders
    ``top_anchor_missed_solar`` (the sky's account, SATISFIES §6.3(d)); good
    surplus with the pod below full renders ``taper_never_observed`` (the
    pod's account, FAILS it)."""
    def surplus_rows(moment: datetime, watts: float) -> tuple[Any, ...]:
        rows: list[Any] = []
        for offset in (0, 180, 360, 540):
            for unit in UNIT_IDS:
                unit_watts = watts if unit == "lhs" else 0.0
                rows.append(
                    SimpleNamespace(
                        unit_id=unit,
                        sampled_at=moment + timedelta(seconds=offset),
                        grid_power_w=unit_watts,
                        battery_watts=-unit_watts,
                        dynamic_charge_limit_w=3000.0,
                    )
                )
        return tuple(rows)

    morning = datetime(2026, 8, 26, 1, 0, 0, tzinfo=UTC)
    poor = surplus_rows(morning, 800.0)  # one covered blip: << 3 kWh
    rich = surplus_rows(morning, 20000.0)  # a big export morning: >= 3 kWh
    for rows, expected, satisfies in (
        (poor, cal.TOP_ANCHOR_MISSED_SOLAR, True),
        (rich, cal.TAPER_NEVER_OBSERVED, False),
    ):
        adviser, handles = traverse_handles()
        handles["history"].sample_rows = rows
        clock: FakeClock = handles["clock"]
        obs: FakeObservations = handles["observations"]
        await adviser.tick()
        soc = 100.0
        while adviser.state_payload().get("traverse") is not None:
            soc -= 800.0 * 30.0 / (36.0 * 500.0)
            clock.advance(30.0)
            obs.latest["mid"] = observation(soc=soc, watts=800.0, captured=clock.now, system=soc)
            await adviser.tick()
        clock.jump_to(datetime(2026, 8, 26, 12, 1, 0, tzinfo=ZONE))
        obs.latest["mid"] = observation(
            soc=60.0, system=57.0, ccl=3000.0, watts=0.0, captured=clock.now
        )
        await adviser.tick()
        taper = payload_of(events(handles["audit"], "calibration_taper_observed")[0])
        assert taper["attribution"] == expected
        anchored, failed = cal.graduation(
            trace=cal.TRACE_MONOTONE,
            delta_change_pct=3.0,
            delta_quality_ok=True,
            verdict=cal.VERDICT_FLOOR_REACHED,
            late_step_pct=None,
            floor_pct=10.0,
            floor_epsilon_pct=2.0,
            taper_observed=False,
            attribution=taper["attribution"],
        )
        assert anchored is satisfies


@pytest.mark.asyncio
async def test_quality_gate_stale_system_word_never_satisfies_b() -> None:
    """T-CAL-MEASUREMENT (C10): a delta change computed against a STALE or
    non-GOOD system-word endpoint can NEVER satisfy criterion (b) — the
    staleness-artifact leg is the named regression vector."""
    ok, failed = cal.graduation(
        trace=cal.TRACE_MONOTONE,
        delta_change_pct=5.0,
        delta_quality_ok=True,
        verdict=cal.VERDICT_FLOOR_REACHED,
        late_step_pct=None,
        floor_pct=10.0,
        floor_epsilon_pct=2.0,
        taper_observed=True,
        attribution=None,
    )
    assert ok and failed is None
    gated, failed_member = cal.graduation(
        trace=cal.TRACE_MONOTONE,
        delta_change_pct=5.0,
        delta_quality_ok=False,  # a stale system-word endpoint
        verdict=cal.VERDICT_FLOOR_REACHED,
        late_step_pct=None,
        floor_pct=10.0,
        floor_epsilon_pct=2.0,
        taper_observed=True,
        attribution=None,
    )
    assert gated is False and failed_member == "b"


@pytest.mark.asyncio
async def test_standdown_survives_restart_and_acknowledgement_lifts() -> None:
    """T-CAL-MEASUREMENT: a failed first cycle stands the pod down; the
    stand-down is durable-row-derived (a restart neither grants nor lifts
    it); the acknowledge-inhibit path lifts it."""
    adviser, handles = traverse_handles()
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    await adviser.tick()
    # Frozen word + no delta movement: the signature fails, the stand-down.
    payload = adviser.state_payload()
    while payload.get("traverse") is not None or payload.get("close"):
        clock.advance(30.0)
        obs.latest["mid"] = observation(soc=100.0, watts=800.0, captured=clock.now, system=100.0)
        if payload.get("traverse") is None:
            clock.jump_to(datetime(2026, 8, 26, 12, 1, 0, tzinfo=ZONE))
            obs.latest["mid"] = observation(soc=100.0, system=100.0, captured=clock.now)
        await adviser.tick()
        payload = adviser.state_payload()
    audit: FakeAudit = handles["audit"]
    taper = payload_of(events(audit, "calibration_taper_observed")[0])
    assert taper["graduation"] == cal.GRADUATION_NOT_OBSERVED
    assert events(audit, "calibration_anchored") == []
    # The restart: a fresh adviser re-derives the stand-down from the rows.
    adviser2, handles2 = make_adviser(
        history=FakeHistory(rollups=fleet_rollups(date(2026, 8, 27))),
        audit=FakeAudit(seeded=list(audit.appended)),
    )
    await adviser2.refresh_durable_facts()
    handles2["clock"].jump_to(datetime(2026, 8, 27, 14, 0, 30, tzinfo=ZONE))
    await adviser2.tick()  # the plan re-derives the stand-down from the rows
    state = adviser2.state_payload()
    mid_row = {row["unit_id"]: row for row in state["units"]}.get("mid")
    assert mid_row is not None and mid_row["standdown"] is True
    # A stood-down pod never opens a leg.
    handles2["clock"].jump_to(datetime(2026, 8, 27, 15, 1, 0, tzinfo=ZONE))
    handles2["observations"].latest["mid"] = observation(soc=100.0)
    await adviser2.tick()
    await adviser2.tick()
    assert handles2["submit"].submissions == []
    # The acknowledgement lifts it (the next plan re-derives eligible).
    await adviser2.acknowledge_standdown("mid")
    await adviser2.refresh_durable_facts()
    assert adviser2.state_payload()["phase"] in {"idle", "planned"}


# --- the economics and the projection ---------------------------------------------


def test_economics_both_branches_c7() -> None:
    """T-CAL-ECONOMICS: the tariff arithmetic — about +99 c house-absorbed, about
    -26 c fully exported against the ~35 c refill; the numbers on the
    surface are the numbers in the record (C7's correction is the named
    vector)."""
    figures = cal.cycle_economics_cents(
        discharge_kwh=4.35,
        refill_wh=4250.0,
        charge_efficiency=0.9,
        import_cents_per_kwh=30.77,
        offpeak_cents_per_kwh=7.27,
        export_cents_per_kwh=2.0,
    )
    assert figures["displaced_import_cents"] == pytest.approx(133.8, abs=0.2)
    assert figures["refill_cost_cents"] == pytest.approx(34.3, abs=0.3)
    assert figures["net_house_absorbed_cents"] == pytest.approx(99.5, abs=0.5)
    assert figures["exported_receipt_cents"] == pytest.approx(8.7, abs=0.1)
    assert figures["net_fully_exported_cents"] == pytest.approx(-25.6, abs=0.5)
    assert figures["net_fully_exported_cents"] < 0  # a real minus, carried


@pytest.mark.asyncio
async def test_cycle_row_carries_the_economics_and_the_record_fields() -> None:
    """T-CAL-MEASUREMENT/ECONOMICS: the full §6.1 record's fields — both SoC
    words, the delta pair, energy, rates — and the economics on the row when
    a tariff truth is composed."""
    adviser, handles = traverse_handles()
    handles["submit"]  # keep the handle reference alive for clarity
    tariff = {
        "import_cents_per_kwh": 30.77,
        "offpeak_cents_per_kwh": 7.27,
        "export_cents_per_kwh": 2.0,
    }
    # Re-compose with the tariff (the traverse handles predate it).
    adviser, handles = traverse_handles()
    adviser._tariff = tariff
    clock: FakeClock = handles["clock"]
    obs: FakeObservations = handles["observations"]
    await adviser.tick()
    while adviser.state_payload().get("traverse") is not None:
        clock.advance(30.0)
        obs.latest["mid"] = observation(soc=100.0, watts=800.0, captured=clock.now, system=100.0)
        await adviser.tick()
    body = payload_of(events(handles["audit"], "calibration_cycle_completed")[0])
    assert body["start"]["bms_soc_pct"] == 100.0
    assert body["start"]["system_soc_pct"] == 100.0
    assert body["energy_wh"] > 100.0
    assert "economics_cents" in body
    assert body["cell_spread_mv_before"] == 30.0


@pytest.mark.asyncio
async def test_projection_shape_and_additive_keys() -> None:
    """T-CAL-PROJECTION: the §8 shape — the mode, the window, the per-unit
    vector with ``horizon_bounded``, the last cycle, the one-shot view; the
    advise ``submits: never``; tiers per §9."""
    adviser, handles = traverse_handles()
    await adviser.tick()
    state = adviser.state_payload()
    assert set(state) >= {
        "mode",
        "window",
        "phase",
        "as_of",
        "units",
        "last_cycle",
        "request_measurement",
    }
    assert state["window"] == {"opens_local": "15:00", "ends_local": "22:30"}
    units = {row["unit_id"]: row for row in state["units"]}
    assert units["mid"]["horizon_bounded"] is True
    traverse = state["traverse"]
    assert traverse is not None
    assert traverse["stop_route"] == cal.STOP_ROUTE_SENTENCE
    assert traverse["pinned_sentence"] == cal.PINNED_SENTENCE
    assert cal.cycle_tier(cal.VERDICT_FLOOR_MISS_ENERGY) == cal.TIER_ALERT
    assert cal.cycle_tier(cal.VERDICT_PREEMPTED) == cal.TIER_NOTICE
    assert cal.close_tier(cal.GRADUATION_NOT_OBSERVED, None) == cal.TIER_ALERT
    assert cal.close_tier(cal.GRADUATION_ANCHORED, cal.TOP_ANCHOR_MISSED_SOLAR) == cal.TIER_NOTICE


# --- the architecture pin ----------------------------------------------------------


def test_no_write_method_exists_anywhere_on_the_path() -> None:
    """T-CAL-ARCHITECTURE: the adviser composes the facade intent twin and no
    transport of its own; no new write method exists anywhere on the path
    (the 0x8000 register is unreachable from this program by construction —
    the module's only dispatch reach is the ``submit`` port, and no
    actor/park/transport handle may compose)."""
    source = Path(cal.__file__).read_text(encoding="utf-8")
    for forbidden in ("write_debug_mode", "apply_debug_mode", "script_debug_mode"):
        assert forbidden not in source, f"the calibration program must never reach {forbidden!r}"
    code = cal.CalibrationAdviser.__init__.__code__
    constructor_ports = code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    assert "submit" in constructor_ports
    for absent in ("actors", "park_control", "transport", "disarm", "arm"):
        assert absent not in constructor_ports, f"no {absent} reach may compose"
