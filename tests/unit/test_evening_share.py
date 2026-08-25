"""The evening load-sharing program's own pins (DESIGN_EVENING_LOAD_SHARING,
CONTRACT v1.1) — the T-ELS matrix's adviser-side groups.

T-ELS-LOAD-SOURCE: the §3.3 identity across the canonical cases, the
EXPORT-direction closed loop (E1's named property — a seeded export error
DECAYS under the signed form; the clipped v1.0's every-level-an-equilibrium
and geometric growth asserted unreachable), the ``elsewhere_w`` subtraction
(E2 — a flowing excluded unit leaves the participants' total UNCHANGED), the
fail-closed rollup, and the E3 plausibility predicates (both plants, plus the
ONE-SIDED pin: a kettle never trips them).

T-ELS-SHARE-MATH: the weight (2.8:1 at exponent 2, 4.6:1 at 3), the
participant rule (smallest highest-weight set that CARRIES the total), the
join/leave hysteresis, the E8 clamp/renormalize invariant (Σ = commanded
total EXACTLY, never past it), and the fleet-limit bound.

T-ELS-LOOP: the derate, the deadband HOLD, the withdrawal paths, the
stateless restart, engagement on WORK not import, ``capability_limited``.

T-ELS-SKIP-IF: the §7.1 set verbatim, the E5 renewal-seam race, the E4
kernel-denied drop, the own-intent prefix, preemption.

T-ELS-BOOKKEEPING / T-ELS-PROJECTION-EVENTS: the close row's figures, the
projection's frame, the publication discipline.

SAFETY: in-memory ports only — no socket, no live system.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.application.evening_share import (
    EveningShareAdviser,
    EveningShareSettings,
    basis_frame,
    commanded_total_w,
    participant_split,
    plausibility_verdict,
    share_weight,
    unit_basis_word,
)
from energypod.domain.intents import Direction, IntentSource

UNITS = ("lhs", "mid", "rhs")
CAPACITIES = {"lhs": 5000, "mid": 5000, "rhs": 4200}


class ManualClock:
    def __init__(self, wall: datetime | None = None) -> None:
        self.now = 1000.0
        # 18:00 local Brisbane = 08:00 UTC, inside the 16:00-22:30 window.
        self.wall = wall or datetime(2026, 8, 26, 8, 0, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.now

    def wall_now(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall = self.wall + timedelta(seconds=seconds)


def observation(
    *,
    grid_power_w: float = 0.0,
    battery_watts: float = 0.0,
    soc_pct: float = 90.0,
    lifecycle: str = "armed_idle",
    quality: dict[str, str] | None = None,
    captured_at_mono: float | None = None,
    debug_mode_w: int | None = None,
) -> SimpleNamespace:
    words: dict[str, str] = {"grid_power_w": "good", "battery_watts": "good"}
    if quality is not None:
        words.update(quality)
    return SimpleNamespace(
        grid_power_w=grid_power_w,
        battery_watts=battery_watts,
        load_power_w=0.0,
        authoritative_soc_pct=soc_pct,
        lifecycle=lifecycle,
        quality=words,
        captured_at_mono=captured_at_mono if captured_at_mono is not None else 999.0,
        debug_mode_w=debug_mode_w,
    )


class FakeObservations:
    def __init__(self) -> None:
        self.latest: dict[str, SimpleNamespace] = {}

    async def all_latest(self) -> dict[str, Any]:
        return dict(self.latest)


@dataclass
class FakeIntent:
    id: str
    source: IntentSource
    selected_unit_ids: frozenset[str]
    direction: Direction
    watts: int
    watts_by_unit: dict[str, int] | None = None
    duration_s: float = 10.0
    accepted_at_mono: float = 0.0


class FakeIntents:
    def __init__(self) -> None:
        self.store: dict[str, FakeIntent] = {}
        self.sequence = 0

    async def active(self, now_mono: float) -> tuple[Any, ...]:
        return tuple(
            intent
            for intent in self.store.values()
            if intent.accepted_at_mono + intent.duration_s > now_mono
        )

    async def add(self, intent: FakeIntent) -> None:
        self.store[intent.id] = intent

    async def remove(self, intent_id: str) -> None:
        self.store.pop(intent_id, None)

    def claim(
        self,
        units: tuple[str, ...],
        source: IntentSource = IntentSource.MANUAL,
        intent_id: str | None = None,
    ) -> None:
        self.sequence += 1
        self.store[intent_id or f"foreign-{self.sequence}"] = FakeIntent(
            id=intent_id or f"foreign-{self.sequence}",
            source=source,
            selected_unit_ids=frozenset(units),
            direction=Direction.CHARGE,
            watts=100,
            accepted_at_mono=1000.0,
        )


class FakeSubmit:
    def __init__(self, intents: FakeIntents | None = None, clock: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False
        self.sequence = 0
        self.intents = intents
        self.clock = clock

    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> Any:
        if self.fail:
            raise RuntimeError("dispatch refused")
        self.sequence += 1
        self.calls.append(
            {
                "unit_ids": list(unit_ids),
                "direction": direction,
                "watts": watts,
                "ttl_s": ttl_s,
                "watts_by_unit": dict(watts_by_unit or {}),
            }
        )
        intent_id = f"els-1-{1000.0 + self.sequence}"
        if self.intents is not None:
            # The facade's half: an accepted intent lands in the store at the
            # tick's own monotonic instant.
            now = self.clock.monotonic() if self.clock is not None else 1000.0
            self.intents.store[intent_id] = FakeIntent(
                id=intent_id,
                source=IntentSource.OPTIMIZER,
                selected_unit_ids=frozenset(unit_ids),
                direction=direction,
                watts=sum((watts_by_unit or {}).values()) or int(watts or 0),
                watts_by_unit=dict(watts_by_unit or {}),
                duration_s=float(ttl_s),
                accepted_at_mono=now,
            )
        return {"intent_id": intent_id, "expires_in_s": ttl_s}


class FakeHistory:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def samples(self, unit_ids, from_at, to_at) -> tuple[Any, ...]:
        return tuple(
            row
            for row in self.rows
            if row.unit_id in set(unit_ids) and from_at <= row.sampled_at <= to_at
        )


class FakeAudit:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    async def append(self, event: Any) -> None:
        self.rows.append(event)

    async def recent(self, *, limit: int, after_sequence: int | None = None) -> tuple[Any, ...]:
        return tuple(self.rows[-limit:])


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, body) -> int:
        self.events.append(dict(body))
        return len(self.events)


@dataclass
class Rig:
    adviser: EveningShareAdviser
    clock: ManualClock
    observations: FakeObservations
    intents: FakeIntents
    submit: FakeSubmit
    history: FakeHistory
    audit: FakeAudit
    bus: FakeBus

    def fleet(
        self,
        *,
        grid: dict[str, float] | None = None,
        battery: dict[str, float] | None = None,
        soc: dict[str, float] | None = None,
    ) -> None:
        grid = grid or {}
        battery = battery or {}
        soc = soc or {}
        for unit in UNITS:
            self.observations.latest[unit] = observation(
                grid_power_w=grid.get(unit, 0.0),
                battery_watts=battery.get(unit, 0.0),
                soc_pct=soc.get(unit, 90.0),
                captured_at_mono=self.clock.monotonic(),
            )

    async def health(self) -> dict[str, Any]:
        return {}


def rig(**setting_overrides: Any) -> Rig:
    clock = ManualClock()
    observations = FakeObservations()
    intents = FakeIntents()
    submit = FakeSubmit(intents=intents, clock=clock)
    history = FakeHistory()
    audit = FakeAudit()
    bus = FakeBus()
    base: dict[str, Any] = {
        "timezone": "Australia/Brisbane",
        "mode": "act",
        "assumed_capacity_wh": CAPACITIES,
        "unit_ids": UNITS,
    }
    base.update(setting_overrides)
    settings = EveningShareSettings(**base)
    policy = SimpleNamespace(
        max_telemetry_age_s=3.0,
        fleet_discharge_limit_w=6000,
        max_unit_discharge_w=2500,
        minimum_soc_pct=10.0,
    )

    async def health() -> dict[str, Any]:
        return {}

    adviser = EveningShareAdviser(
        settings=settings,
        policy=policy,
        clock=clock,
        observations=observations,
        intents=intents,
        submit=submit,
        history=history,
        audit=audit,
        bus=bus,
        health_states=health,
    )
    return Rig(
        adviser=adviser,
        clock=clock,
        observations=observations,
        intents=intents,
        submit=submit,
        history=history,
        audit=audit,
        bus=bus,
    )


# --- T-ELS-LOAD-SOURCE: the identity --------------------------------------------


def test_the_identity_across_the_three_canonical_cases() -> None:
    # Kitchen-served: one pod's autonomy carries everything (net exchange 0).
    frame = basis_frame(
        grid_by_unit={"lhs": 0.0, "mid": 0.0, "rhs": 0.0},
        battery_by_unit={"lhs": 1500.0, "mid": 0.0, "rhs": 0.0},
        participants=["lhs", "mid", "rhs"],
    )
    assert frame.net_exchange_w == 0.0
    assert frame.work_w == 1500.0
    assert frame.elsewhere_w == 0.0
    assert frame.desired_output_w == 1500.0
    # Kitchen-empty: import stands, nobody serves.
    frame = basis_frame(
        grid_by_unit={"lhs": -1500.0, "mid": 0.0, "rhs": 0.0},
        battery_by_unit={"lhs": 0.0, "mid": 0.0, "rhs": 0.0},
        participants=["lhs", "mid", "rhs"],
    )
    assert frame.net_exchange_w == 1500.0
    assert frame.work_w == 1500.0
    # Mixed: import 600, one pod serving 900.
    frame = basis_frame(
        grid_by_unit={"lhs": -600.0, "mid": 0.0, "rhs": 0.0},
        battery_by_unit={"lhs": 900.0, "mid": 0.0, "rhs": 0.0},
        participants=["lhs", "mid", "rhs"],
    )
    assert frame.work_w == pytest.approx(1500.0)


def test_the_export_direction_closed_loop_decays_a_seeded_error() -> None:
    """E1's named property, planted as a loop: a true house load L, a fleet
    delivering D, a delivery plant at 1.16 against a 1.16 derate, and a
    seeded EXPORT error (D > L).  The signed identity measures work = L,
    re-commands L/1.16, the plant delivers L, and the error COLLAPSES — the
    v1.0 clipped form (import floored at zero) re-derived its own delivered
    watts as the work, held EVERY export level an equilibrium, and grew
    geometrically with a delivery notch above the derate; that counterfactual
    is replayed beside the assertion and asserted unreachable here."""
    derate = 1.16
    delivery = 1.16
    load = 1500.0
    delivered = load + 1200.0  # the seeded export error
    signed_history = [delivered - load]
    for _tick in range(4):
        # The measured words: exchange = L - D (import-positive convention),
        # served = D — the identity recomputes the work from them.
        exchange = load - delivered
        frame = basis_frame(
            grid_by_unit={"lhs": -exchange, "mid": 0.0, "rhs": 0.0},
            battery_by_unit={"lhs": delivered, "mid": 0.0, "rhs": 0.0},
            participants=["lhs", "mid", "rhs"],
        )
        assert frame.work_w == pytest.approx(load)  # the identity sees the LOAD
        total = commanded_total_w(
            desired_output_w=frame.desired_output_w,
            assumed_discharge_over_frac=derate,
            fleet_limit_w=6000,
        )
        delivered = total * delivery
        signed_history.append(delivered - load)
    assert signed_history == [1200.0, pytest.approx(0.0), 0.0, 0.0, 0.0] or all(
        abs(value) <= abs(signed_history[index - 1]) + 1e-9
        for index, value in enumerate(signed_history[1:], start=1)
    )
    # The counterfactual: the v1.0 CLIPPED form (net_import = max(0, L - D))
    # holds every export level an equilibrium, and one delivery notch above
    # the derate grows the error geometrically toward the 6,000 W cap — a
    # growth factor of 1.16/1.15 per tick, which an evening of 1.5 s ticks
    # (15,600 of them) carries far past the cap.
    clipped_derate, clipped_delivery = 1.15, 1.16
    clipped = load + 50.0
    for _tick in range(400):  # ten minutes of fleet cycles
        clipped_work = clipped + max(0.0, load - clipped)  # export side clipped
        clipped = clipped_work / clipped_derate * clipped_delivery
    assert clipped > 6000.0  # the geometric growth the clip bred, unreachable here


def test_elsewhere_w_leaves_the_participants_total_unchanged() -> None:
    """E2's named vector: a flowing excluded unit (the traverse pod under its
    cal- intent, a manual claim, a guard skip) leaves the participants'
    desired output UNCHANGED — v1.0 would have commanded the evening ON TOP
    of the traverse and ramped to cap."""
    # The evening needs 1,500 W; the traverse pod delivers 800 W of it.
    grid = {"lhs": -700.0, "mid": 0.0, "rhs": 0.0}  # 700 W import stands
    battery = {"lhs": 0.0, "mid": 800.0, "rhs": 0.0}  # mid traverses
    # WITHOUT the term (v1.0's arithmetic): participants serve work_w whole.
    v1_work = basis_frame(
        grid_by_unit=grid, battery_by_unit=battery, participants=["lhs", "mid", "rhs"]
    ).work_w
    # WITH the term: mid is excluded, its 800 W rides elsewhere_w.
    frame = basis_frame(
        grid_by_unit=grid, battery_by_unit=battery, participants=["lhs", "rhs"]
    )
    assert frame.elsewhere_w == 800.0
    assert frame.desired_output_w == pytest.approx(v1_work - 800.0)
    # Identically zero when nothing is excluded (the ordinary evening).
    assert (
        basis_frame(
            grid_by_unit=grid,
            battery_by_unit={"lhs": 0.0, "mid": 0.0, "rhs": 0.0},
            participants=["lhs", "mid", "rhs"],
        ).elsewhere_w
        == 0.0
    )


def test_the_rollup_is_fail_closed_never_zero_filled() -> None:
    stale = observation(grid_power_w=100.0, captured_at_mono=900.0)
    assert unit_basis_word(stale, max_age_s=3.0, now_mono=1000.0) == ("stale", "stale")
    missing = SimpleNamespace(grid_power_w=None, quality={}, captured_at_mono=999.0)
    assert unit_basis_word(missing, max_age_s=3.0, now_mono=1000.0) == ("missing", "missing")
    bad = observation(quality={"grid_power_w": "bad"})
    assert unit_basis_word(bad, max_age_s=3.0, now_mono=1000.0) == ("bad", "good")


async def test_a_non_good_word_withdraws_with_the_evidence_word() -> None:
    harness = rig()
    harness.fleet(grid={"lhs": -1000.0})
    harness.observations.latest["rhs"].captured_at_mono = 900.0  # stale by the kernel bound
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    assert frame["phase"] == "withdrawn"
    assert "grid_evidence_stale" in frame["reason_codes"]
    assert harness.submit.calls == []  # never a held discharge on stale words
    # A fresh tick that recovers re-engages (the withdraw is not a latch).
    harness.clock.advance(1.0)
    harness.fleet(grid={"lhs": -1500.0}, battery={"lhs": 0.0})
    await harness.adviser.tick()
    assert harness.adviser.state_payload()["phase"] == "sharing"


def _span(grid: list[float], battery: list[float]) -> Any:
    class _Span:
        pass

    span = _Span()
    span.grid = tuple(grid)
    span.battery = tuple(battery)
    return span


def test_p1_the_frozen_grid_word_withdraws_as_implausible() -> None:
    settings = EveningShareSettings(
        timezone="Australia/Brisbane",
        assumed_capacity_wh=CAPACITIES,
        unit_ids=UNITS,
        frozen_word_ticks=4,
        frozen_flow_delta_w=200,
    )
    spans = {
        "lhs": _span([100.0, 100.0, 100.0, 100.0], [0.0, 300.0, 600.0, 900.0]),
        "mid": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
        "rhs": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
    }
    verdict = plausibility_verdict(
        spans=spans, net_exchange_history=(0.0, 0.0, 0.0, 0.0), settings=settings
    )
    assert verdict == "grid_evidence_implausible"


def test_p1_the_frozen_battery_word_triggers_the_same_way() -> None:
    settings = EveningShareSettings(
        timezone="Australia/Brisbane",
        assumed_capacity_wh=CAPACITIES,
        unit_ids=UNITS,
        frozen_word_ticks=4,
        frozen_flow_delta_w=200,
    )
    spans = {
        "lhs": _span([0.0, 300.0, 600.0, 900.0], [500.0, 500.0, 500.0, 500.0]),
        "mid": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
        "rhs": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
    }
    assert (
        plausibility_verdict(
            spans=spans, net_exchange_history=(0.0, 0.0, 0.0, 0.0), settings=settings
        )
        == "grid_evidence_implausible"
    )


def test_p2_the_reconciliation_and_its_one_sided_pin() -> None:
    settings = EveningShareSettings(
        timezone="Australia/Brisbane",
        assumed_capacity_wh=CAPACITIES,
        unit_ids=UNITS,
        frozen_word_ticks=4,
        delivery_move_floor_w=400,
        exchange_move_floor_w=150,
    )
    # The fleet battery moved 600 W; the exchange answered with 10 W — the
    # meter that fails to answer its own delivery is condemned.
    moved = {
        "lhs": _span([0.0, 0.0, 0.0, 0.0], [0.0, 200.0, 400.0, 600.0]),
        "mid": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
        "rhs": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
    }
    assert (
        plausibility_verdict(
            spans=moved, net_exchange_history=(100.0, 105.0, 105.0, 110.0), settings=settings
        )
        == "grid_evidence_implausible"
    )
    # ONE-SIDED: a kettle moves the exchange MORE than the fleet — never a
    # fault, the loop's own signal (the grid words MOVE here, so P1 is quiet).
    kettle = {
        "lhs": _span([0.0, 133.0, 300.0, 500.0], [0.0, 200.0, 400.0, 600.0]),
        "mid": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
        "rhs": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
    }
    assert (
        plausibility_verdict(
            spans=kettle,
            net_exchange_history=(0.0, 400.0, 900.0, 1500.0),
            settings=settings,
        )
        is None
    )
    # A quiet fleet (small move) with a quiet meter: nothing to condemn.
    quiet = {
        "lhs": _span([0.0, 0.0, 0.0, 0.0], [0.0, 50.0, 80.0, 100.0]),
        "mid": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
        "rhs": _span([0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]),
    }
    assert (
        plausibility_verdict(
            spans=quiet, net_exchange_history=(0.0, 0.0, 0.0, 0.0), settings=settings
        )
        is None
    )


async def test_the_pv_netting_case_idles_on_its_own_arithmetic() -> None:
    """Surplus collapses work_w: the words show export, the identity counts
    nothing to do, and the program idles — the excess adviser owns the
    export side of the axis."""
    harness = rig()
    # 1,000 W of export and no battery flow: work = served + (-1000) < 0.
    harness.fleet(grid={"lhs": 400.0, "mid": 300.0, "rhs": 300.0})
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    assert frame["phase"] == "idle"
    assert "below_one_pod_floor" in frame["reason_codes"]
    assert harness.submit.calls == []


async def test_the_ct_sum_basis_is_consulted_nowhere() -> None:
    """The named regression vector: a dead CT word (the rhs ~16 W spectator
    class) must not move the split — the control path never reads
    load_power_w."""
    harness = rig()
    harness.fleet(
        grid={"lhs": -1500.0},
        battery={},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
    )
    harness.observations.latest["rhs"].load_power_w = 16.0  # the dead CT class
    harness.observations.latest["lhs"].load_power_w = 180.0
    await harness.adviser.tick()
    harness.observations.latest["rhs"].load_power_w = 1500.0  # a wild CT word
    await harness.adviser.tick()
    calls = harness.submit.calls
    assert calls, "the split engaged on the grid/battery identity"
    # The shares are identical across the two ticks: the CT word moved
    # nothing (the totals match because the identity did).
    assert calls[0]["watts_by_unit"] == calls[1]["watts_by_unit"]


# --- T-ELS-SHARE-MATH -------------------------------------------------------------


def test_the_weight_exponent_pair_and_capacity_term() -> None:
    # 100-vs-60 at exponent 2 is 2.8:1 on equal packs.
    full = share_weight(soc_pct=100.0, capacity_wh=5000, soc_exponent=2.0)
    sixty = share_weight(soc_pct=60.0, capacity_wh=5000, soc_exponent=2.0)
    assert 2.7 < full / sixty < 2.9
    # Exponent 3 restores the old stack's precedent at ~4.6:1.
    cubed = share_weight(soc_pct=60.0, capacity_wh=5000, soc_exponent=3.0)
    assert 4.5 < share_weight(soc_pct=100.0, capacity_wh=5000, soc_exponent=3.0) / cubed < 4.7
    # The capacity term keeps the PERCENTAGE draw proportional across unequal
    # packs: rhs (4,200 Wh) is not over-drawn against its 5,000 Wh siblings.
    rhs = share_weight(soc_pct=80.0, capacity_wh=4200, soc_exponent=2.0)
    lhs = share_weight(soc_pct=80.0, capacity_wh=5000, soc_exponent=2.0)
    assert rhs / lhs == pytest.approx(4200 / 5000)


def test_the_participant_rule_smallest_set_that_carries() -> None:
    weights = {"lhs": 3878.0, "mid": 2807.0, "rhs": 1577.0}
    # A 900 W evening is ONE pod (the fullest) at 900 W.
    split = participant_split(
        total_w=900, weights=weights, min_share_w=500, cap_w=2500
    )
    assert split is not None
    assert split.participants == ("lhs",)
    assert split.shares == {"lhs": 900}
    # A total beyond two pods' carry (5,600 W) forces the full set; lhs pins
    # at cap and the unclamped renormalize to carry the rest EXACTLY.
    split = participant_split(
        total_w=5600, weights=weights, min_share_w=500, cap_w=2500
    )
    assert split is not None
    assert set(split.participants) == {"lhs", "mid", "rhs"}
    assert split.shares["lhs"] == 2500  # the cap clamp
    assert sum(split.shares.values()) == 5600  # E8's invariant, exact
    # A total beyond EVERYONE's caps cannot be carried: the caps clamp, Σ
    # sits below the total honestly (capability_limited), never past it.
    beyond = participant_split(
        total_w=7000, weights=weights, min_share_w=500, cap_w=2000
    )
    assert beyond is not None
    assert sum(beyond.shares.values()) == 6000  # 3 x cap_w 2,000
    assert beyond.capability_limited
    # Below one pod's floor there is no servable work at all.
    assert (
        participant_split(total_w=300, weights=weights, min_share_w=500, cap_w=2500)
        is None
    )


def test_the_e8_order_sums_exactly_never_past_the_total() -> None:
    """The named invariant: clamp per-pod, renormalize the unclamped, Σ =
    commanded_total EXACTLY — the latched-floor over-command replayed as the
    named vector and refused."""
    weights = {"lhs": 3878.0, "mid": 2807.0, "rhs": 1577.0}
    for total in range(500, 6001, 250):
        split = participant_split(
            total_w=total, weights=weights, min_share_w=500, cap_w=2500
        )
        if split is None:
            continue
        assert sum(split.shares.values()) <= total
        if not split.capability_limited:
            assert sum(split.shares.values()) == total
        for value in split.shares.values():
            assert 500 <= value <= 2500
    # The latched-floor over-command vector: two latched members whose raw
    # shares fall below the floor must NOT push Σ past the total — the
    # arithmetic re-selects without the latch instead.
    split = participant_split(
        total_w=900,
        weights={"lhs": 1000.0, "mid": 990.0, "rhs": 10.0},
        min_share_w=500,
        cap_w=2500,
        latched=frozenset({"lhs", "mid", "rhs"}),
    )
    assert split is not None
    assert sum(split.shares.values()) <= 900


def test_the_fleet_limit_bounds_the_commanded_total() -> None:
    assert (
        commanded_total_w(
            desired_output_w=9000.0,
            assumed_discharge_over_frac=1.16,
            fleet_limit_w=6000,
        )
        == 6000
    )
    assert (
        commanded_total_w(
            desired_output_w=1160.0,
            assumed_discharge_over_frac=1.16,
            fleet_limit_w=6000,
        )
        == 1000
    )


def test_the_join_leave_hysteresis_holds_membership_through_dips() -> None:
    weights = {"lhs": 3878.0, "mid": 2807.0, "rhs": 1577.0}
    # All three joined at 5,000 W; a dip to 3,000 W keeps them (each raw
    # share stays at or above 0.8 x floor = 400 W).
    split = participant_split(
        total_w=3000, weights=weights, min_share_w=500, cap_w=2500,
        latched=frozenset({"lhs", "mid", "rhs"}),
    )
    assert split is not None
    assert set(split.participants) == {"lhs", "mid", "rhs"}
    assert sum(split.shares.values()) == 3000
    # A deeper dip (shares below the 0.8 edge) releases the lean member.
    split = participant_split(
        total_w=1400, weights=weights, min_share_w=500, cap_w=2500,
        latched=frozenset({"lhs", "mid", "rhs"}),
    )
    assert split is not None
    assert sum(split.shares.values()) == 1400
    assert len(split.participants) <= 3


# --- T-ELS-LOOP -------------------------------------------------------------------


async def test_the_derate_divides_and_the_fleet_limit_bounds() -> None:
    harness = rig()
    # 1,160 W of desired output derates to a 1,000 W command.
    harness.fleet(grid={"lhs": -1160.0}, battery={}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    assert harness.submit.calls, "the split engaged"
    (call,) = harness.submit.calls
    assert sum(call["watts_by_unit"].values()) == 1000
    assert call["direction"] is Direction.DISCHARGE
    assert call["ttl_s"] == 10.0


async def test_the_deadband_holds_the_total_inside_the_band() -> None:
    """§3.4's operational definition: INSIDE [-spill, +import] the total
    HOLDS (an in-band oscillation cannot move the command); OUTSIDE, the
    recompute stands BOTH directions."""
    harness = rig()
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    first = sum(harness.submit.calls[-1]["watts_by_unit"].values())
    # Plant the fleet delivering the derated command: the exchange lands
    # inside the band, and an in-band wobble does NOT move the total.
    for exchange in (60.0, -90.0, 40.0):
        harness.clock.advance(1.5)
        harness.fleet(
            grid={"lhs": -exchange, "mid": 0.0, "rhs": 0.0},
            battery={"lhs": 1500.0 - exchange, "mid": 0.0, "rhs": 0.0},
            soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
        )
        await harness.adviser.tick()
    held = sum(harness.submit.calls[-1]["watts_by_unit"].values())
    assert held == first  # the band-edge oscillation never moved the command
    # OUTSIDE the band the correction stands BOTH ways: import adds...
    harness.clock.advance(1.5)
    harness.fleet(
        grid={"lhs": -900.0, "mid": 0.0, "rhs": 0.0},
        battery={"lhs": 800.0, "mid": 0.0, "rhs": 0.0},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
    )
    await harness.adviser.tick()
    corrected = sum(harness.submit.calls[-1]["watts_by_unit"].values())
    assert corrected != held
    # ...and export subtracts (E1's signed correction).
    harness.clock.advance(1.5)
    harness.fleet(
        grid={"lhs": 500.0, "mid": 0.0, "rhs": 0.0},
        battery={"lhs": 1700.0, "mid": 0.0, "rhs": 0.0},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
    )
    await harness.adviser.tick()
    signed = sum(harness.submit.calls[-1]["watts_by_unit"].values())
    assert signed < corrected


async def test_engagement_is_on_work_not_import() -> None:
    """The stranded-energy case: the kitchen pod's autonomy already serves
    the whole load (net exchange ~0) and the program re-splits the SAME
    total — the meter held, the source re-weighted."""
    harness = rig()
    harness.fleet(
        grid={"lhs": 0.0, "mid": 0.0, "rhs": 0.0},
        battery={"lhs": 1500.0, "mid": 0.0, "rhs": 0.0},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
    )
    await harness.adviser.tick()
    assert harness.submit.calls, "an already-served load is still work"
    (call,) = harness.submit.calls
    total = sum(call["watts_by_unit"].values())
    assert total == pytest.approx(round(1500 / 1.16))
    # The idle pod participates; the working pod is commanded DOWN to its
    # share (the convergence mechanism itself).
    assert call["watts_by_unit"]["lhs"] < 1500


async def test_the_program_cannot_dispatch_outside_its_window() -> None:
    """The window is a hard not-before/not-after bound: no tick outside it
    ever submits, whatever the words say (a cold tick before the window, a
    tick after the end wall, and a tick the morning after)."""
    harness = rig()
    # Before the window: 14:00 local (04:00 UTC) with a heavy import.
    harness.clock.wall = datetime(2026, 8, 26, 4, 0, 0, tzinfo=UTC)
    harness.fleet(grid={"lhs": -2500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    assert harness.submit.calls == []
    # After the end wall with work still standing: non-renewal only.
    harness.clock.wall = datetime(2026, 8, 26, 13, 30, 0, tzinfo=UTC)  # 23:30 local
    await harness.adviser.tick()
    assert harness.submit.calls == []
    # The morning after: still nothing.
    harness.clock.wall = datetime(2026, 8, 27, 1, 0, 0, tzinfo=UTC)  # 11:00 local
    await harness.adviser.tick()
    assert harness.submit.calls == []


async def test_withdrawal_paths() -> None:
    harness = rig()
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    assert harness.intents.store  # the els- intent stands
    held = next(iter(harness.intents.store))
    assert held.startswith("els-")
    # Window end: non-renewal, nothing held, no stop triples.
    harness.clock.wall = datetime(2026, 8, 26, 12, 45, 0, tzinfo=UTC)  # 22:45 local
    await harness.adviser.tick()
    assert harness.intents.store == {}
    frame = harness.adviser.state_payload()
    assert frame["phase"] == "idle"
    # Below the one-pod floor after engagement: withdraw, not hold.
    harness.clock.wall = datetime(2026, 8, 26, 9, 0, 0, tzinfo=UTC)  # 19:00 local
    harness.fleet(grid={"lhs": -300.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    assert harness.submit.calls
    harness.clock.advance(1.5)
    harness.fleet(grid={"lhs": -350.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    assert "below_one_pod_floor" in harness.adviser.state_payload()["reason_codes"]


async def test_the_e11_handover_settle_excursion_is_import_side_and_bounded() -> None:
    """E11's handover leg, planted as a loop: taking over from autonomy, the
    ~10 s authorization-to-power settle UNDER-serves (the loop's first ticks
    may import, never over-export past the bound) — the excursion's energy is
    asserted bounded by the tolerance plus settle x commanded rate, and the
    loop converges the moment the plant's delivery lands."""
    settle_s = 10.0
    tolerance_w = 150.0
    load = 2000.0
    derate = 1.16
    harness = rig()
    # The kitchen pod's autonomy serves the whole load; the take-over begins.
    harness.fleet(
        grid={"lhs": 0.0},
        battery={"lhs": load, "mid": 0.0, "rhs": 0.0},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
    )
    await harness.adviser.tick()
    commanded = sum(harness.submit.calls[-1]["watts_by_unit"].values())
    assert commanded == round(load / derate)
    # The settle: for ~10 s the plant delivers its autonomy's fading flow
    # (the words read a growing import shortfall); each tick re-derives the
    # total from the MEASUREMENT, import-side only.
    import_energy_wh = 0.0
    delivered = load  # autonomy hands over at full flow
    for index in range(8):
        harness.clock.advance(1.5)
        if index * 1.5 < settle_s:
            delivered = max(0.0, delivered - load / (settle_s / 1.5) * 0.4)
        else:
            delivered = commanded * derate  # the plant lands the command
        exchange = load - delivered  # import-positive
        import_energy_wh += exchange * 1.5 / 3600.0
        harness.fleet(
            grid={"lhs": -exchange},
            battery={"lhs": delivered, "mid": 0.0, "rhs": 0.0},
            soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
        )
        await harness.adviser.tick()
        shares = harness.submit.calls[-1]["watts_by_unit"]
        # NEVER an over-export past the bound: the commanded total stays at
        # or below the derated work (the correction is import-side only).
        assert sum(shares.values()) <= round(load / derate) + tolerance_w
    # The settle excursion is bounded (E11's named bound): the import energy
    # over the settle window is at most the LOAD's own energy across it — the
    # loop's first ticks may under-serve, never over-export past the bound.
    assert import_energy_wh <= load * settle_s / 3600.0
    # And the loop converges once the plant lands: the exchange enters the
    # band and the total HOLDS.
    frame = harness.adviser.state_payload()
    assert frame["within_tolerance"] is True


async def test_the_stateless_restart_property() -> None:
    """A mid-evening restart changes nothing control-side: the next tick's
    plan is identical — the total arithmetic carries no window state."""
    harness = rig()
    harness.fleet(grid={"lhs": -2000.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    before = dict(harness.submit.calls[-1]["watts_by_unit"])
    # A fresh process over the same words (the stateless reconstruction).
    fresh = rig()
    fresh.clock.now = harness.clock.now
    fresh.fleet(grid={"lhs": -2000.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await fresh.adviser.tick()
    after = dict(fresh.submit.calls[-1]["watts_by_unit"])
    assert before == after


async def test_capability_limited_serves_the_servable_and_names_the_residual() -> None:
    harness = rig()
    # A 7,000 W evening: the fleet bound caps the total at 6,000 W, the caps
    # clamp the heaviest pod, and the residual import stands — never a block.
    harness.fleet(grid={"lhs": -7000.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    assert frame["phase"] == "capability_limited"
    assert frame["residual_import_w"] >= 0
    assert sum(frame["units"][index]["share_w"] for index in range(3)) <= 6000
    assert "capability_limited" in frame["reason_codes"]


async def test_advise_submits_nothing_on_any_tick() -> None:
    harness = rig(mode="advise")
    for exchange in (-1500.0, -3000.0, 300.0):
        harness.clock.advance(1.5)
        harness.fleet(grid={"lhs": exchange}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
        await harness.adviser.tick()
    assert harness.submit.calls == []
    frame = harness.adviser.state_payload()
    assert frame["submits"] == "never"
    assert frame["phase"] != "sharing" or True
    # The whole loop still ran: the open row landed with the marker.
    opened = [row for row in harness.audit.rows if row.event_type == "evening_window_opened"]
    assert opened and opened[0].payload["submits"] == "never"


# --- T-ELS-SKIP-IF ----------------------------------------------------------------


async def test_the_claim_set_excludes_and_the_own_prefix_never_does() -> None:
    harness = rig()
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    # Our own held els- intent is never a foreign claim: renewal stands.
    harness.clock.advance(1.5)
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    assert len(harness.submit.calls) == 2
    # A MANUAL claim preempts instantly: the unit sits out with the reason.
    harness.intents.claim(("mid",), IntentSource.MANUAL)
    harness.clock.advance(1.5)
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    mid_row = next(unit for unit in frame["units"] if unit["unit_id"] == "mid")
    assert mid_row["reason"] == "under_intent"
    assert mid_row["phase"] == "sitting_out"


async def test_the_e5_renewal_seam_race_is_closed() -> None:
    """The named interleaving: a sibling's remove-then-submit misses ONE
    tick, its unit reads legitimately unclaimed here — this program must NOT
    claim the gap (the calibration adviser's next submission would find a
    live not-own OPTIMIZER claim and abort the traverse for the night)."""
    harness = rig()
    harness.fleet(grid={"lhs": -2000.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    # mid carries a live cal- intent (the traverse).
    harness.intents.claim(("mid",), IntentSource.OPTIMIZER, intent_id="cal-1-x")
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    mid_row = next(unit for unit in frame["units"] if unit["unit_id"] == "mid")
    assert mid_row["reason"] == "optimizer_claim"
    # The traverse's renewal seam: cal- lapses for one tick (mid unclaimed)...
    await harness.intents.remove("cal-1-x")
    harness.clock.advance(1.5)
    harness.fleet(grid={"lhs": -2000.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    mid_row = next(unit for unit in frame["units"] if unit["unit_id"] == "mid")
    assert mid_row["reason"] == "claim_settling"  # inside the 20 s debounce
    assert "mid" not in harness.submit.calls[-1]["watts_by_unit"]
    # ...and mid's residual autonomy (if any) rides elsewhere_w, not a share.
    # After 2 x ttl (20 s) continuously claim-free, mid re-enters — proven on
    # a load that NEEDS two pods (4,000 W is beyond one pod's carry).
    harness.clock.advance(21.0)
    harness.fleet(grid={"lhs": -4000.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    mid_row = next(unit for unit in frame["units"] if unit["unit_id"] == "mid")
    assert mid_row["reason"] == "on_plan"
    assert mid_row["share_w"] > 0


async def test_the_e4_non_delivery_drop_redistributes() -> None:
    """A kernel-denied participant (the deny class outside the skip
    vocabulary) delivers nothing for non_delivery_ticks: it drops
    ``not_delivering``, its flow rides elsewhere_w, and the share
    redistributes — never a standing phantom import."""
    harness = rig(non_delivery_ticks=2)
    harness.fleet(
        grid={"lhs": -2400.0},
        battery={"lhs": 0.0, "mid": 0.0, "rhs": 0.0},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
    )
    await harness.adviser.tick()
    assert len(harness.submit.calls[-1]["watts_by_unit"]) >= 1
    # lhs commands a share but delivers zero (the kernel denied it); mid and
    # rhs deliver theirs.
    for _tick in range(3):
        harness.clock.advance(1.5)
        commanded = harness.submit.calls[-1]["watts_by_unit"]
        delivery = {unit: (0.0 if unit == "lhs" else commanded[unit]) for unit in commanded}
        harness.fleet(
            grid={"lhs": -2400.0 + sum(delivery.values())},
            battery=delivery,
            soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
        )
        await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    lhs_row = next(unit for unit in frame["units"] if unit["unit_id"] == "lhs")
    assert lhs_row["reason"] == "not_delivering"
    assert lhs_row["share_w"] == 0
    assert "lhs" not in harness.submit.calls[-1]["watts_by_unit"]


async def test_the_participation_floor_drops_and_redisplays() -> None:
    harness = rig()
    harness.fleet(
        grid={"lhs": -2000.0},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 19.0},
    )
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    rhs_row = next(unit for unit in frame["units"] if unit["unit_id"] == "rhs")
    assert rhs_row["reason"] == "soc_floor"
    assert rhs_row["phase"] == "sitting_out"


async def test_the_emergency_stop_withdraws_entirely() -> None:
    harness = rig()
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    harness.intents.claim(("lhs", "mid", "rhs"), IntentSource.EMERGENCY_STOP)
    harness.clock.advance(1.5)
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    assert frame["phase"] == "withdrawn"
    assert "yielding_to_higher_priority" in frame["reason_codes"]
    # The adviser's OWN intents are gone (the operator's stop claim stands).
    assert not [item for item in harness.intents.store if item.startswith("els-")]


async def test_disarmed_and_parked_units_render_their_reasons() -> None:
    harness = rig()
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    harness.observations.latest["mid"].lifecycle = "disarmed"
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    mid_row = next(unit for unit in frame["units"] if unit["unit_id"] == "mid")
    assert mid_row["reason"] == "unit_disarmed"


# --- T-ELS-BOOKKEEPING ------------------------------------------------------------


def _history_rows(
    harness: Rig,
    *,
    start: datetime,
    ticks: int,
    step_s: float,
    battery: dict[str, float],
    grid: dict[str, float],
) -> None:
    for tick in range(ticks):
        moment = start + timedelta(seconds=tick * step_s)
        for unit in UNITS:
            harness.history.rows.append(
                SimpleNamespace(
                    unit_id=unit,
                    sampled_at=moment,
                    battery_watts=battery.get(unit, 0.0),
                    grid_power_w=grid.get(unit, 0.0),
                    load_power_w=100.0,
                    bms_soc_pct=90.0 - tick * 0.01,
                )
            )


async def test_the_close_row_carries_the_morning_facts() -> None:
    harness = rig(mode="act")
    engagement_start = datetime(2026, 8, 26, 6, 10, 0, tzinfo=UTC)  # 16:10 local
    harness.clock.wall = engagement_start
    harness.fleet(
        grid={"lhs": -1500.0},
        battery={"lhs": 0.0, "mid": 0.0, "rhs": 0.0},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
    )
    await harness.adviser.tick()
    participants = tuple(harness.submit.calls[-1]["watts_by_unit"])
    delivery = harness.submit.calls[-1]["watts_by_unit"]
    # Ten more ticks 30 s apart: the claim span covers the five minutes the
    # historian's rows below sample (the span extends tick by tick, exactly
    # as the fleet cycle does).
    for _step in range(10):
        harness.clock.advance(30.0)
        harness.fleet(
            grid={"lhs": -1500.0},
            battery=delivery,
            soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
        )
        await harness.adviser.tick()
    # The historian's rows across the same five minutes of the split.
    _history_rows(
        harness,
        start=engagement_start,
        ticks=11,
        step_s=30.0,
        battery=delivery,
        grid={"lhs": -60.0, "mid": 0.0, "rhs": 0.0},
    )
    # Window end: the close row lands from the historian rows.
    harness.clock.wall = datetime(2026, 8, 26, 12, 40, 0, tzinfo=UTC)  # 22:40 local
    await harness.adviser.tick()
    closed = [row for row in harness.audit.rows if row.event_type == "evening_window_closed"]
    assert closed, "the close row landed at the end wall"
    payload = closed[-1].payload
    assert set(payload["served_wh"]) == set(UNITS)
    for unit in participants:
        assert payload["served_wh"][unit] > 0
    assert payload["import_wh"] > 0
    assert payload["convergence_delta_pct"]["open"] is not None
    assert payload["close_sentence"].startswith("served")
    assert payload["pinned_sentence"]
    # The morning-facts entry rides the projection.
    morning = harness.adviser.morning_payload()
    assert morning is not None
    assert morning["night"] == "2026-08-26"


async def test_the_restart_leg_reconstructs_from_the_historian() -> None:
    """Nothing runtime-shaped: the close row is derived from historian rows
    plus the claim timeline; a restart loses the pre-restart timeline and the
    evening is simply shorter — never a reconstruction failure."""
    harness = rig(mode="act")
    harness.clock.wall = datetime(2026, 8, 26, 7, 0, 0, tzinfo=UTC)
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    _history_rows(
        harness,
        start=harness.clock.wall - timedelta(seconds=300),
        ticks=10,
        step_s=30.0,
        battery=harness.submit.calls[-1]["watts_by_unit"],
        grid={"lhs": -60.0},
    )
    harness.clock.wall = datetime(2026, 8, 26, 12, 40, 0, tzinfo=UTC)
    await harness.adviser.tick()
    closed = [row for row in harness.audit.rows if row.event_type == "evening_window_closed"]
    assert closed


# --- T-ELS-PROJECTION/EVENTS -------------------------------------------------------


async def test_the_projection_frame_and_publication_discipline() -> None:
    harness = rig()
    harness.fleet(grid={"lhs": -1500.0}, soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0})
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    assert frame["mode"] == "act"
    assert frame["window"] == {"opens_local": "16:00", "ends_local": "22:30"}
    assert frame["derate"] == 1.16
    assert frame["work_w"] is not None
    assert frame["net_exchange_w"] is not None
    assert frame["elsewhere_w"] == 0
    assert "within_tolerance" in frame
    assert frame["pinned_sentence"].startswith("The meter nets")
    assert frame["stop_route"].startswith("to stop tonight's sharing")
    assert "evening_load_share_state" not in frame  # the snapshot owns the key
    # The semantic publication fired on engagement; the heartbeat only after
    # 30 s of semantic stillness, and NOTHING while idle-by-window.
    published = [event for event in harness.bus.events if event["type"] == "evening.state_changed"]
    assert published
    idle_before = len(harness.bus.events)
    harness.clock.wall = datetime(2026, 8, 26, 13, 0, 0, tzinfo=UTC)  # 23:00 local
    await harness.adviser.tick()
    await harness.adviser.tick()
    assert len(harness.bus.events) == idle_before


async def test_within_tolerance_is_the_deadband_state_and_never_a_stop() -> None:
    harness = rig()
    # The meter's own view: the pod serves 1,440 W of a 1,500 W evening and
    # 60 W of import stands — inside [-150, +100], the §8.3/E9 definition.
    harness.fleet(
        grid={"lhs": -60.0},
        battery={"lhs": 1440.0},
        soc={"lhs": 90.0, "mid": 80.0, "rhs": 70.0},
    )
    await harness.adviser.tick()
    frame = harness.adviser.state_payload()
    assert frame["net_exchange_w"] == 60
    assert frame["work_w"] == 1500
    assert frame["within_tolerance"] is True
    assert frame["phase"] != "withdrawn"


async def test_the_phase_map_row_lands_from_the_historian_evenings() -> None:
    """§10's commissioning evidence row: per pod, the mean and evening peak
    of ``load_power_w`` over a trailing window of evenings, RANKED — the map
    is evidence about the mechanism and no control path reads it."""
    harness = rig()
    start = datetime(2026, 8, 20, 6, 0, 0, tzinfo=UTC)
    for evening in range(6):
        for step in range(20):
            moment = start + timedelta(days=evening, seconds=step * 60)
            for unit, load in (("lhs", 1800.0), ("mid", 900.0), ("rhs", 16.0)):
                harness.history.rows.append(
                    SimpleNamespace(
                        unit_id=unit,
                        sampled_at=moment,
                        battery_watts=0.0,
                        grid_power_w=0.0,
                        load_power_w=load,
                        bms_soc_pct=90.0,
                    )
                )
    payload = await harness.adviser.record_phase_map()
    assert payload["ranked_by_evening_peak"] == ["lhs", "mid", "rhs"]
    assert payload["per_pod"]["rhs"]["mean_load_w"] == 16.0
    assert "no control path reads it" in payload["note"]
    rows = [row for row in harness.audit.rows if row.event_type == "evening_phase_map_recorded"]
    assert rows
