"""Contract tests for the night TRUE STANDBY posture (``park_standby``).

The operator's directive 2026-08-26, per-phase independence confirmed
("Only the heavy one"): during the night window, a battery whose OWN LOAD
word exceeds 1,000 W goes into TRUE STANDBY — it answers neither charge nor
discharge — via an ACTIVE PARK (vendor Standby 0x8000←1 under a
ParkController lease), never passive exclusion (tried 2026-08-24, reversed:
exclusion lets pods autonomy-discharge into the load).  Below ~800 W it
resumes normal full-rate operation.  The fail-closed evidence arm is
preserved byte-for-byte: missing/bad/stale words still trickle-hold at
``hold_rate_w``, and evidence going bad DURING standby KEEPS the park.

Mechanical doctrine pinned here (plan §2/§4): the engage choreography rides
the adviser's remove→submit gap — ``_remove_held()`` → disarm twin →
``park(...)`` → ``_submit()`` WITHOUT the parked unit, submission LAST.
Act budget ≤1 engage AND ≤1 release per tick.  Every exit path releases
(window close, participation loss); an E-stop does NOT (resume refuses
stop-latched anyway — parked answers nothing, consistent with stop).
Expiry stays alarm-only: boot-time leases whose authorizer is
``energypod:night-adviser`` and reason ``night_demand_standby`` are adopted;
operator and foreign leases are never touched, never fought.

The red phase fails cleanly while the posture is absent: the settings block
has no ``demand_response``/``max_lease_s`` keys and the adviser takes no
park ports yet, so every test dies at arrange with the honest TypeError
naming the missing contract.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from energypod.application.parking import (
    PARK_CONFLICT_REFUSED,
    PARK_LEASE_CAP_REACHED,
    PARK_READBACK_UNVERIFIED,
    PARK_WRITE_FAILED,
    ParkingRefusal,
)
from tests.unit.test_health_watch_recovery import FakeArm, FakeDisarm, FakeParkControl
from tests.unit.test_night_charge import (
    NOW,
    QUALITY_FIELDS,
    ZONE,
    FakeClock,
    FakeIntents,
    FakeObservations,
    FakeSubmit,
    claimed_intent,
    make_fleet,
    make_policy,
    make_settings,
    targets,
)


@pytest.fixture(scope="module")
def api() -> Any:
    importlib.import_module("energypod.domain")
    return importlib.import_module("energypod.domain")


@pytest.fixture(scope="module")
def night() -> Any:
    try:
        module = importlib.import_module("energypod.application.night_charge")
    except ImportError as error:  # pragma: no cover - module exists today
        pytest.fail(f"the night-charge strategy contract is not implemented: {error}")
        raise  # pragma: no cover - pytest.fail never returns
    return module


# --- the six additive reason codes (23→29); literals everywhere else -------------

NIGHT_STANDBY_PARKED = "night_standby_parked"
NIGHT_STANDBY_PARK_REFUSED = "night_standby_park_refused"
NIGHT_STANDBY_RELEASE_FAILED = "night_standby_release_failed"
NIGHT_STANDBY_RELEASE_UNVERIFIED = "night_standby_release_unverified"
NIGHT_STANDBY_REARM_FAILED = "night_standby_rearm_failed"
NIGHT_STANDBY_ADOPTED = "night_standby_adopted"

PARK_REASON = "night_demand_standby"
ADVISER_SUBJECT = "energypod:night-adviser"


# --- deterministic fakes ----------------------------------------------------------


@dataclass
class TracedIntents(FakeIntents):
    """The night intents fake plus one shared act-ordering trace."""

    trace: list[str] = field(default_factory=list)

    async def remove(self, intent_id: str) -> None:
        self.trace.append(f"remove:{intent_id}")
        await super().remove(intent_id)


@dataclass
class TracedSubmit(FakeSubmit):
    trace: list[str] = field(default_factory=list)

    async def __call__(
        self,
        *,
        unit_ids: Any,
        direction: Any,
        watts: Any,
        ttl_s: Any,
        watts_by_unit: Any = None,
    ) -> dict[str, Any]:
        self.trace.append("submit")
        return await super().__call__(
            unit_ids=unit_ids,
            direction=direction,
            watts=watts,
            ttl_s=ttl_s,
            watts_by_unit=watts_by_unit,
        )


@dataclass
class TracedParkControl(FakeParkControl):
    trace: list[str] = field(default_factory=list)

    async def park(
        self,
        unit_id: str,
        *,
        reason: str,
        principal_subject: str,
        request_id: str,
        lease_s: int | None = None,
    ) -> dict[str, Any]:
        self.trace.append(f"park:{unit_id}")
        return await super().park(
            unit_id,
            reason=reason,
            principal_subject=principal_subject,
            request_id=request_id,
            lease_s=lease_s,
        )

    async def resume(
        self, unit_id: str, *, principal_subject: str, request_id: str
    ) -> dict[str, Any]:
        self.trace.append(f"resume:{unit_id}")
        return await super().resume(
            unit_id, principal_subject=principal_subject, request_id=request_id
        )


@dataclass
class TracedDisarm(FakeDisarm):
    trace: list[str] = field(default_factory=list)

    async def __call__(self, *, unit_id: str) -> dict[str, str]:
        self.trace.append(f"disarm:{unit_id}")
        return await super().__call__(unit_id=unit_id)


@dataclass
class TracedArm(FakeArm):
    trace: list[str] = field(default_factory=list)

    async def __call__(self, *, unit_id: str) -> dict[str, str]:
        self.trace.append(f"rearm:{unit_id}")
        return await super().__call__(unit_id=unit_id)


@dataclass
class FakeStandbyLeases:
    """The composer's suppressing lease-view closure: {unit: lease payload}."""

    leases: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: int = 0

    def __call__(self) -> dict[str, dict[str, Any]]:
        self.calls += 1
        return {unit: dict(view) for unit, view in self.leases.items()}


def standby_lease(
    *,
    authorizer: str = ADVISER_SUBJECT,
    reason: str = PARK_REASON,
    expires_at: str = "2026-08-27T05:30:00+10:00",
    max_total_s: int = 25_200,
    epoch: int = 1,
) -> dict[str, Any]:
    """A ParkLease.to_payload()-shaped view row (domain/parking.py:163)."""
    return {
        "parked_at": "2026-08-27T01:00:00+10:00",
        "expires_at": expires_at,
        "max_total_s": max_total_s,
        "reason": reason,
        "authorizer": authorizer,
        "epoch": epoch,
    }


def make_standby_settings(night: Any, **overrides: Any) -> Any:
    """The commissioned posture: per_phase independence, park_standby, and a
    lease cap sized past the longest window span +120 s (the config gate's
    arithmetic, mirrored here so the engage lease math has headroom)."""
    values: dict[str, Any] = {
        "demand_response": "park_standby",
        "demand_scope": "per_phase",
        "max_lease_s": 25_200,
    }
    values.update(overrides)
    return make_settings(night, **values)


def make_standby_rig(
    night: Any,
    api: Any,
    observations: dict[str, Any],
    *,
    settings: Any | None = None,
    standby_leases: FakeStandbyLeases | None = None,
    participation: Any = None,
    park_refusals: list[ParkingRefusal | None] | None = None,
    resume_refusals: list[ParkingRefusal | None] | None = None,
    arm_armed: bool = True,
    clock: FakeClock | None = None,
) -> SimpleNamespace:
    """A fully ported adviser: every act lands on one shared ordered trace.

    The trace is the choreography witness — the remove→disarm→park→submit
    gap (and resume→rearm on release) is readable as one narrative.
    """
    trace: list[str] = []
    park = TracedParkControl(trace=trace)
    if park_refusals is not None:
        park.park_refusals = list(park_refusals)
    if resume_refusals is not None:
        park.resume_refusals = list(resume_refusals)
    disarm = TracedDisarm(trace=trace)
    arm = TracedArm(trace=trace, armed=arm_armed)
    submit = TracedSubmit(trace=trace)
    intents = TracedIntents(trace=trace)
    used_clock = clock or FakeClock()
    # The adviser is constructed DIRECTLY (never through the v1 helper): the
    # park ports ARE this posture's contract, so they ride the constructor as
    # pinned optional kwargs — the same additive-optional-port pattern every
    # V2 port already uses (parked_units, trust_state, audit, ...).
    adviser = night.NightChargeAdviser(
        settings=settings or make_standby_settings(night),
        policy=make_policy(api),
        clock=used_clock,
        observations=FakeObservations(latest=observations),
        intents=intents,
        submit=submit,
        participation=participation,
        park_control=park,
        disarm=disarm,
        arm=arm,
        standby_leases=standby_leases,
    )
    return SimpleNamespace(
        adviser=adviser,
        fleet=observations,
        intents=intents,
        submit=submit,
        clock=used_clock,
        park=park,
        disarm=disarm,
        arm=arm,
        trace=trace,
        leases=standby_leases,
    )


def set_load(rig: SimpleNamespace, unit_id: str, load_w: float) -> None:
    """Rewrite one unit's MEASURED LOAD word (frozen domain objects)."""
    object.__setattr__(rig.fleet[unit_id], "load_power_w", load_w)


def set_word_quality(api: Any, rig: SimpleNamespace, unit_id: str, word: Any) -> None:
    """Demote one unit's LOAD word's quality (bad evidence mid-standby)."""
    quality = {name: api.DataQuality.GOOD for name in QUALITY_FIELDS}
    quality["load_power_w"] = word
    object.__setattr__(rig.fleet[unit_id], "quality", quality)


def set_word_stale(rig: SimpleNamespace, unit_id: str) -> None:
    """Age one unit's LOAD word past demand_telemetry_max_age_s (3.0)."""
    object.__setattr__(rig.fleet[unit_id], "captured_at_mono", NOW - 5.0)


def drop_observation(rig: SimpleNamespace, unit_id: str) -> None:
    """Remove one unit's observation entirely (MISSING evidence)."""
    rig.fleet.pop(unit_id)


def row(decision: Any, unit_id: str) -> Any:
    """One unit's per-tick plan row."""
    return next(plan for plan in decision.unit_plans if plan.unit_id == unit_id)


# --- the engage choreography -------------------------------------------------------


async def test_engage_one_heavy_unit_parks_it_inside_the_remove_submit_gap(
    night: Any, api: Any
) -> None:
    """THE directive's core act: one heavy OWN word parks THAT battery alone.

    The choreography rides the remove→submit gap exactly as pinned — old
    intent OUT (the pod still executes its last objective; the watchdog has
    not fired), stop-direction act, Standby INSIDE the watchdog window, new
    submission WITHOUT the parked unit.  Submission happens LAST.
    """
    rig = make_standby_rig(
        night, api, make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0})
    )

    decision = await rig.adviser.tick()

    # Ordering IS the safety argument: the conflict guard sees no live
    # intent when park lands, and the fleet submission carries the result.
    assert rig.trace == ["disarm:lhs", "park:lhs", "submit"]
    call = rig.park.park_calls[0]
    assert call["unit_id"] == "lhs"
    assert call["reason"] == PARK_REASON
    assert call["principal_subject"] == ADVISER_SUBJECT
    # The lease spans the window plus 120 s of margin, capped by max_lease_s:
    # 01:00 wall → 06:00 end = 18_000 s remaining → 18_000 + 120 = 18_120.
    assert call["request_id"] == "night-standby:lhs:2026-08-27"
    assert call["lease_s"] == 18_120
    # The parked unit is OUT of the submission; the clean sibling paces on.
    assert rig.submit.submissions[0]["unit_ids"] == ["mid"]
    assert rig.submit.submissions[0]["watts_by_unit"] == {"mid": 2_500}
    assert NIGHT_STANDBY_PARKED in decision.reason_codes


async def test_the_parked_row_is_excluded_and_fleet_vocabulary_unchanged(
    night: Any, api: Any
) -> None:
    """Steady-state rendering: the parked battery's row names the truth —
    ``standing_by_parked`` at zero — while the FLEET vocabulary stays v1
    (zero new fleet phases; transitions ride reason_codes alone)."""
    rig = make_standby_rig(
        night, api, make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0})
    )
    await rig.adviser.tick()

    steady = await rig.adviser.tick()

    lhs = row(steady, "lhs")
    assert lhs.phase == "standing_by_parked"
    assert lhs.target_w == 0
    assert lhs.reason == NIGHT_STANDBY_PARKED
    # The fleet phase keeps its existing word: mid is pacing, so the frame
    # is pacing — the new unit phase never leaks into the fleet projection.
    assert steady.phase == "pacing"
    assert steady.active_unit_ids == ("mid",)
    # Parked means silent: no further writes ride steady ticks.
    assert len(rig.park.park_calls) == 1
    assert rig.disarm.calls == ["lhs"]
    assert rig.submit.submissions[-1]["watts_by_unit"] == {"mid": 2_500}


async def test_a_park_conflict_refusal_never_leaves_the_unit_uncovered(
    night: Any, api: Any
) -> None:
    """Ladder rung 1: a refused park re-includes the unit at ``hold_rate_w``
    THIS TICK — a demand spike must never meet an uncovered battery, even
    when the park itself lost the race."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        park_refusals=[ParkingRefusal(PARK_CONFLICT_REFUSED, "an objective is live")],
    )

    decision = await rig.adviser.tick()

    assert NIGHT_STANDBY_PARK_REFUSED in decision.reason_codes
    assert rig.park.park_calls, "the engage was attempted"
    assert rig.arm.calls == [], "a refused park never re-arms anything"
    assert rig.submit.submissions[0]["watts_by_unit"] == {
        "lhs": 100,
        "mid": 2_500,
    }, "the fallback is the positive hold-rate cover, this same tick"

    # One refusal does not latch: the next tick tries again.
    await rig.adviser.tick()
    assert len(rig.disarm.calls) == 2
    assert any(call["unit_id"] == "lhs" for call in rig.park.park_calls[1:])


async def test_a_write_failure_gets_exactly_one_reissue(night: Any, api: Any) -> None:
    """Ladder rung 2: a failed WRITE gets ONE scripted re-issue next tick —
    never a churn loop against a wedged register."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        park_refusals=[ParkingRefusal(PARK_WRITE_FAILED, "register busy")],
    )

    first = await rig.adviser.tick()
    assert NIGHT_STANDBY_PARK_REFUSED in first.reason_codes
    assert rig.submit.submissions[0]["watts_by_unit"]["lhs"] == 100, "covered while retrying"

    second = await rig.adviser.tick()
    assert len(rig.park.park_calls) == 2, "exactly one re-issue"
    assert NIGHT_STANDBY_PARKED in second.reason_codes
    assert "lhs" not in rig.submit.submissions[-1]["watts_by_unit"], "the re-issue landed"


async def test_the_standby_hysteresis_band_never_flaps(night: Any, api: Any) -> None:
    """The same no-flap guarantee as the hold, lived inside the park: above
    800 the pod STAYS parked through the band, below it releases once, and
    an oscillation back under the threshold never re-parks (strictly >)."""
    rig = make_standby_rig(
        night, api, make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0})
    )
    await rig.adviser.tick()

    # INTO the band (800 ≤ 900 < 1000): the park persists, silently.
    set_load(rig, "lhs", 900.0)
    banded = await rig.adviser.tick()
    assert row(banded, "lhs").phase == "standing_by_parked"
    assert len(rig.park.park_calls) == 1 and rig.park.resume_calls == []

    set_load(rig, "lhs", 850.0)
    still = await rig.adviser.tick()
    assert row(still, "lhs").phase == "standing_by_parked"
    assert rig.park.resume_calls == [], "the band keeps the park exactly as it was"

    # Strictly below the exit bound: one release.
    set_load(rig, "lhs", 600.0)
    released = await rig.adviser.tick()
    assert "demand_below_exit" in released.reason_codes
    assert [call["unit_id"] for call in rig.park.resume_calls] == ["lhs"]

    # An oscillation back into the band does NOT re-engage: engage is
    # strictly `> threshold`, and 850 is not.
    set_load(rig, "lhs", 850.0)
    await rig.adviser.tick()
    assert len(rig.park.park_calls) == 1
    assert rig.submit.submissions[-1]["watts_by_unit"].get("lhs") == 2_500

    set_load(rig, "lhs", 950.0)
    again = await rig.adviser.tick()
    assert len(rig.park.park_calls) == 1, "no flap around the threshold"
    assert targets(again)["lhs"] == 2_500


@pytest.mark.parametrize(("word",), [("bad",), ("stale",), ("missing",)])
async def test_bad_evidence_during_standby_keeps_the_park(night: Any, api: Any, word: str) -> None:
    """The safe direction is NOT symmetric: evidence going bad DURING
    standby KEEPS the park (a parked pod answers nothing — releasing onto
    unseen demand is the one unrecoverable mistake)."""
    rig = make_standby_rig(
        night, api, make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0})
    )
    engaged = await rig.adviser.tick()
    assert NIGHT_STANDBY_PARKED in engaged.reason_codes, "arrange: parked on the good word"

    if word == "bad":
        set_word_quality(api, rig, "lhs", api.DataQuality.BAD)
    elif word == "stale":
        set_word_stale(rig, "lhs")
    else:
        drop_observation(rig, "lhs")
    during = await rig.adviser.tick()

    assert rig.park.resume_calls == [], "bad evidence NEVER releases the park"
    assert len(rig.park.park_calls) == 1, "and never re-parks either"
    assert "lhs" not in rig.submit.submissions[-1]["watts_by_unit"], "still excluded"
    if word != "missing":
        # With the observation still PRESENT the row names the truth; with
        # it gone the walk may honestly sit the unit out instead.
        assert row(during, "lhs").phase == "standing_by_parked"

    await rig.adviser.tick()
    assert rig.park.resume_calls == [], "the park outlasts the evidence loss"


async def test_evidence_failure_still_trickle_holds_units_that_never_parked(
    night: Any, api: Any
) -> None:
    """Under the NEW posture the OLD fail-closed polarity is byte-for-byte:
    a non-good word on a never-parked phase holds it at ``hold_rate_w`` as
    ``holding_on_demand`` — the evidence-failure fallback alone, never
    dressed up as a response to demand the adviser cannot see."""
    fleet = make_fleet(
        api,
        {"lhs": 300.0, "mid": 300.0, "rhs": 100.0},
        mid__load_quality=api.DataQuality.BAD,
    )
    rig = make_standby_rig(night, api, fleet)

    decision = await rig.adviser.tick()

    assert decision.demand_evidence == "bad"
    assert decision.phase == "holding_on_demand"
    assert "demand_evidence_bad" in decision.reason_codes
    assert targets(decision)["mid"] == 100
    assert rig.park.park_calls == [], "evidence failure never parks"
    assert rig.submit.submissions[0]["watts_by_unit"]["mid"] == 100


# --- the release path ----------------------------------------------------------------


async def test_release_resumes_and_rearms_full_rate(night: Any, api: Any) -> None:
    """Release is a CHOREOGRAPHY, not an omission: resume lifts the vendor
    standby, then the ONE bounded re-arm restores the kernel's control —
    and the next tick charges the unit at the full capped pace."""
    rig = make_standby_rig(
        night, api, make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0})
    )
    await rig.adviser.tick()

    set_load(rig, "lhs", 600.0)
    await rig.adviser.tick()

    assert "resume:lhs" in rig.trace
    assert rig.trace.index("resume:lhs") < rig.trace.index("rearm:lhs"), "resume, THEN re-arm"
    call = rig.park.resume_calls[0]
    assert call["unit_id"] == "lhs"
    assert call["principal_subject"] == ADVISER_SUBJECT
    assert call["request_id"] == "night-standby:lhs:2026-08-27"
    assert rig.arm.calls == ["lhs"]

    resumed = await rig.adviser.tick()
    assert targets(resumed)["lhs"] == 2_500, "full rate replaces the stand-by"
    assert row(resumed, "lhs").phase == "pacing"


async def test_the_last_participant_leaving_through_the_gap_submits_nothing(
    night: Any, api: Any
) -> None:
    """The whole submission can leave through the gap: when the tick's ONE
    engagement is its LAST participant (the siblings sit out disarmed), the
    selection is empty — and an empty selection is a facade refusal, never a
    submission.  The frame closes withdraw-shaped with the park named; the
    next steady tick renders the parked row as usual."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(
            api,
            {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0},
            mid__lifecycle=api.UnitLifecycle.DISARMED,
            rhs__lifecycle=api.UnitLifecycle.DISARMED,
        ),
    )

    decision = await rig.adviser.tick()

    assert [call["unit_id"] for call in rig.park.park_calls] == ["lhs"]
    assert rig.submit.submissions == [], "an empty selection is never submitted"
    assert decision.active_unit_ids == ()
    assert NIGHT_STANDBY_PARKED in decision.reason_codes

    steady = await rig.adviser.tick()
    assert row(steady, "lhs").phase == "standing_by_parked"
    assert rig.submit.submissions == [], "the close shape holds on later ticks too"


async def test_the_act_budget_spends_one_engage_and_one_release_per_tick(
    night: Any, api: Any
) -> None:
    """≤1 engage AND ≤1 release per tick: two heavy phases engage across two
    ticks (largest W first), and each release is spent in its own tick as
    each word crosses its own exit bound."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 1_200.0, "rhs": 100.0}, socs={"rhs": 80.0}),
    )

    await rig.adviser.tick()
    assert [call["unit_id"] for call in rig.park.park_calls] == ["lhs"], "largest W first"
    assert rig.submit.submissions[0]["watts_by_unit"] == {"mid": 100, "rhs": 2_500}

    await rig.adviser.tick()
    assert [call["unit_id"] for call in rig.park.park_calls] == ["lhs", "mid"]
    assert rig.submit.submissions[-1]["watts_by_unit"] == {"rhs": 2_500}

    # Only mid's word crossed below the exit bound: exactly one release.
    set_load(rig, "mid", 600.0)
    third = await rig.adviser.tick()
    assert [call["unit_id"] for call in rig.park.resume_calls] == ["mid"]
    assert row(third, "lhs").phase == "standing_by_parked", "still heavy, still parked"

    set_load(rig, "lhs", 600.0)
    await rig.adviser.tick()
    assert sorted(call["unit_id"] for call in rig.park.resume_calls) == ["lhs", "mid"]

    fifth = await rig.adviser.tick()
    assert targets(fifth) == {"lhs": 2_500, "mid": 2_500, "rhs": 2_500}
    assert sorted(rig.arm.calls) == ["lhs", "mid"], "each release carried its bounded re-arm"


async def test_a_disable_verdict_on_the_window_boundary_still_releases_every_park(
    night: Any, api: Any
) -> None:
    """The boundary-crossing corner of the exit-path invariant: when the
    disable verdict lands ON the open→close crossing, the roll has already
    moved the park map into the closing stash — the disabled path must read
    BOTH maps or the stash strands the park behind the disabled early-return
    for as long as the night stays disabled (expiry alone is alarm-only)."""
    clock = FakeClock()
    verdict_box: dict[str, str | None] = {"verdict": None}
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        participation=lambda: verdict_box["verdict"],
        clock=clock,
    )
    await rig.adviser.tick()

    clock.wall = datetime(2026, 8, 27, 6, 30, tzinfo=ZONE)
    verdict_box["verdict"] = "disabled_by_runtime"
    await rig.adviser.tick()

    assert [call["unit_id"] for call in rig.park.resume_calls] == ["lhs"]
    assert rig.arm.calls == ["lhs"], "released means RE-ARMED, past the stash too"


async def test_window_close_releases_every_parked_unit(night: Any, api: Any) -> None:
    """A timer is not a principal and expiry is alarm-only, so the WINDOW
    itself is the exit path: closing releases every parked unit and the
    release is not repeated on later outside frames."""
    clock = FakeClock()
    rig = make_standby_rig(
        night, api, make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}), clock=clock
    )
    await rig.adviser.tick()

    clock.wall = datetime(2026, 8, 27, 6, 30, tzinfo=ZONE)
    decision = await rig.adviser.tick()

    assert decision.in_window is False
    assert [call["unit_id"] for call in rig.park.resume_calls] == ["lhs"]
    assert rig.arm.calls == ["lhs"], "released means RE-ARMED, not merely resumed"
    assert rig.submit.submissions[-1] is rig.submit.submissions[0], "outside frames submit nothing"

    outside_again = await rig.adviser.tick()
    assert len(rig.park.resume_calls) == 1, "the release is spent once"
    assert outside_again.reason_codes == ("outside_window",)


async def test_participation_loss_releases_every_parked_unit(night: Any, api: Any) -> None:
    """The disable toggle is an exit path too: a participation verdict rides
    BEFORE the idle frame returns, and it releases — a disabled night never
    leaves pods answering neither charge nor discharge."""
    verdict_box: dict[str, str | None] = {"verdict": None}
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        participation=lambda: verdict_box["verdict"],
    )
    await rig.adviser.tick()

    verdict_box["verdict"] = "disabled_by_runtime"
    decision = await rig.adviser.tick()

    assert decision.phase == "idle"
    assert "disabled_by_runtime" in decision.reason_codes
    assert [call["unit_id"] for call in rig.park.resume_calls] == ["lhs"]
    assert rig.arm.calls == ["lhs"]

    await rig.adviser.tick()
    assert len(rig.park.resume_calls) == 1, "released once, not every tick"


async def test_an_emergency_stop_attempts_no_resume(night: Any, api: Any) -> None:
    """E-stop owns its units: the withdraw path does NOT release a parked
    unit (resume refuses stop-latched anyway; parked answers nothing —
    consistent with stop), and clearing the stop finds the park INTACT, so
    no re-engage churn follows either."""
    rig = make_standby_rig(
        night, api, make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0})
    )
    await rig.adviser.tick()

    rig.intents.entries.append(
        claimed_intent(source="EMERGENCY_STOP", unit_ids=("lhs",), intent_id="stop-1")
    )
    stopped = await rig.adviser.tick()

    assert stopped.action == "withdraw"
    assert "yielding_to_higher_priority" in stopped.reason_codes
    assert rig.park.resume_calls == [], "a stop never resumes anything"
    assert len(rig.park.park_calls) == 1, "and never re-parks either"

    rig.intents.entries.clear()
    after_stop = await rig.adviser.tick()

    assert len(rig.park.park_calls) == 1, "the park survived the stop untouched"
    assert row(after_stop, "lhs").phase == "standing_by_parked"
    assert "lhs" not in rig.submit.submissions[-1]["watts_by_unit"]


# --- adoption and the already-parked corner -------------------------------------------


async def test_boot_adopts_only_our_own_standby_leases(night: Any, api: Any) -> None:
    """Boot reconstruction: leases whose authorizer is the night adviser and
    reason is ``night_demand_standby`` come back into the map (bookkeeping
    only — never a re-park, never a resume); operator and foreign leases
    keep sitting out untouched."""
    leases = FakeStandbyLeases(
        leases={
            "lhs": standby_lease(),
            "mid": standby_lease(authorizer="person:operator", reason="manual_park"),
            "rhs": standby_lease(authorizer="energypod:health-adviser", reason="health_route_a"),
        }
    )
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}, socs={"rhs": 98.0}),
        standby_leases=leases,
    )

    decision = await rig.adviser.tick()

    assert NIGHT_STANDBY_ADOPTED in decision.reason_codes
    adopted_row = row(decision, "lhs")
    assert adopted_row.phase == "standing_by_parked"
    assert adopted_row.reason == NIGHT_STANDBY_PARKED
    assert row(decision, "mid").reason == "unit_parked"
    assert row(decision, "rhs").reason == "unit_parked"
    # Adoption is bookkeeping: zero acts, zero submissions.
    assert rig.park.park_calls == [] and rig.park.resume_calls == []
    assert rig.disarm.calls == [] and rig.arm.calls == []
    assert rig.submit.submissions == []

    steady = await rig.adviser.tick()
    assert NIGHT_STANDBY_ADOPTED not in steady.reason_codes, "the adopt flag is one-shot"
    assert rig.park.park_calls == [], "adoption never becomes an act later either"


async def test_an_operator_resume_is_never_reparked(night: Any, api: Any) -> None:
    """Never fight an operator act: our adopted unit the operator RESUMES
    drops out of the map and falls back to the hold-posture cover at
    ``hold_rate_w`` — never a re-park, however heavy the word stays."""
    leases = FakeStandbyLeases(leases={"lhs": standby_lease()})
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        standby_leases=leases,
    )
    await rig.adviser.tick()  # adopted

    del leases.leases["lhs"]  # the operator's Resume closed our lease
    decision = await rig.adviser.tick()

    assert rig.park.park_calls == [], "a resumed unit is never re-parked"
    assert rig.disarm.calls == [], "and never re-disarmed either"
    assert row(decision, "lhs").phase == "standing_by_on_demand"
    assert targets(decision)["lhs"] == 100, "the protected fallback covers the demand"
    assert NIGHT_STANDBY_PARKED not in decision.reason_codes

    await rig.adviser.tick()
    assert rig.park.park_calls == [], "not on the next tick either"


# --- the renewal sweep and the cap ------------------------------------------------------


async def test_a_lease_in_the_renewal_zone_gets_one_recomputed_park(night: Any, api: Any) -> None:
    """Leases expiring inside 600 s renew IN PLACE — one renewal-shaped park
    with the recomputed window-plus-margin lease, no resume, no cycling."""
    leases = FakeStandbyLeases(leases={"lhs": standby_lease()})
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        standby_leases=leases,
    )
    await rig.adviser.tick()  # adopted; comfortably outside the zone
    assert rig.park.park_calls == []

    leases.leases["lhs"]["expires_at"] = "2026-08-27T01:04:00+10:00"  # 240 s left
    renewed = await rig.adviser.tick()

    assert len(rig.park.park_calls) == 1
    call = rig.park.park_calls[0]
    assert call["unit_id"] == "lhs"
    assert call["reason"] == PARK_REASON
    assert call["lease_s"] == 18_120, "recomputed from the window, not the remainder"
    assert rig.park.resume_calls == [], "renewal never resumes"
    assert row(renewed, "lhs").phase == "standing_by_parked"

    leases.leases["lhs"]["expires_at"] = "2026-08-27T02:30:00+10:00"  # renewed
    await rig.adviser.tick()
    assert len(rig.park.park_calls) == 1, "one renewal per zone entry"


async def test_cap_reached_on_renewal_is_loud_and_never_cycles(night: Any, api: Any) -> None:
    """A cumulative-cap refusal on renewal is LOUD and inert: the unit stays
    parked and excluded, nothing is resumed to dodge the cap, and no
    resume/re-park cycle spins up against the exhausted budget."""
    leases = FakeStandbyLeases(leases={"lhs": standby_lease()})
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        standby_leases=leases,
        park_refusals=[
            ParkingRefusal(PARK_LEASE_CAP_REACHED, "cumulative lease cap reached"),
            ParkingRefusal(PARK_LEASE_CAP_REACHED, "cumulative lease cap reached"),
        ],
    )
    await rig.adviser.tick()  # adopted outside the zone

    leases.leases["lhs"]["expires_at"] = "2026-08-27T01:04:00+10:00"  # renewal zone
    loud = await rig.adviser.tick()

    assert NIGHT_STANDBY_PARK_REFUSED in loud.reason_codes
    assert rig.park.resume_calls == [], "never resume to dodge a cap"
    assert row(loud, "lhs").phase == "standing_by_parked", "still parked, still excluded"
    assert "lhs" not in rig.submit.submissions[-1]["watts_by_unit"]

    await rig.adviser.tick()
    assert rig.park.resume_calls == [], "no cycling on later ticks either"
    assert row(loud, "lhs").phase == "standing_by_parked"


# --- the rest of the ladder --------------------------------------------------------------


async def test_readback_unverified_is_terminal_for_the_window(night: Any, api: Any) -> None:
    """An unverifiable park write MAY have landed: commanding that pod again
    would fight an unknown state.  Terminal for the window — loud, excluded
    (not even hold-rate cover), and never another write."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        park_refusals=[ParkingRefusal(PARK_READBACK_UNVERIFIED, "readback did not match")],
    )

    first = await rig.adviser.tick()

    assert NIGHT_STANDBY_PARK_REFUSED in first.reason_codes
    assert "lhs" not in rig.submit.submissions[0]["watts_by_unit"], "excluded, not covered"
    assert rig.submit.submissions[0]["watts_by_unit"] == {"mid": 2_500}

    for _ in range(2):
        await rig.adviser.tick()
    assert len(rig.disarm.calls) == 1 and len(rig.park.park_calls) == 1, "terminal: no more writes"
    assert rig.arm.calls == []


async def test_three_conflicts_latch_to_trickle_hold_for_the_window(night: Any, api: Any) -> None:
    """≥3 consecutive conflicts latch the unit to the trickle-hold FOR THE
    WINDOW: never churn into a standing refusal, and the latch still rides
    loudly."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        park_refusals=[
            ParkingRefusal(PARK_CONFLICT_REFUSED, "claim"),
            ParkingRefusal(PARK_CONFLICT_REFUSED, "claim"),
            ParkingRefusal(PARK_CONFLICT_REFUSED, "claim"),
        ],
    )

    for _ in range(3):
        decision = await rig.adviser.tick()
        assert NIGHT_STANDBY_PARK_REFUSED in decision.reason_codes
        assert rig.submit.submissions[-1]["watts_by_unit"]["lhs"] == 100, "covered throughout"
    assert len(rig.park.park_calls) == 3, "three attempts, three conflicts"

    latched = await rig.adviser.tick()
    assert len(rig.disarm.calls) == 3, "the latch stops the writes"
    latched_row = row(latched, "lhs")
    assert latched_row.phase == "standing_by_on_demand"
    assert latched_row.target_w == 100, "the trickle-hold latch renders the v1 row"
    assert NIGHT_STANDBY_PARK_REFUSED in latched.reason_codes, "loud while latched"

    await rig.adviser.tick()
    assert len(rig.park.park_calls) == 3, "latched for the window"


async def test_a_refused_rearm_is_loud_and_never_retried(night: Any, api: Any) -> None:
    """Re-arm refusal ≠ release failure: the unit sits DISARMED (it cannot
    discharge — the safe side), the alert code is loud ONCE, and the
    operator arms manually; the adviser never retries the bounded act."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        arm_armed=False,
    )
    await rig.adviser.tick()

    set_load(rig, "lhs", 600.0)
    released = await rig.adviser.tick()

    assert len(rig.park.resume_calls) == 1, "the release itself succeeded"
    assert rig.arm.calls == ["lhs"], "attempted exactly once"
    assert NIGHT_STANDBY_REARM_FAILED in released.reason_codes
    assert "lhs" not in rig.submit.submissions[-1]["watts_by_unit"]

    await rig.adviser.tick()
    assert rig.arm.calls == ["lhs"], "never retried — it is the operator's act now"


async def test_release_write_failure_retries_while_condition_persists(night: Any, api: Any) -> None:
    """Ladder: a failed RESUME retries next tick while the below-threshold
    condition persists (idempotent), loud each failing tick, and the re-arm
    waits for a real resume."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        resume_refusals=[ParkingRefusal(PARK_WRITE_FAILED, "bus timeout")],
    )
    await rig.adviser.tick()

    set_load(rig, "lhs", 600.0)
    failing = await rig.adviser.tick()

    assert NIGHT_STANDBY_RELEASE_FAILED in failing.reason_codes
    assert len(rig.park.resume_calls) == 1
    assert rig.arm.calls == [], "never re-arm an un-resumed pod"
    assert row(failing, "lhs").phase == "standing_by_parked", "still parked while failing"

    await rig.adviser.tick()
    assert len(rig.park.resume_calls) == 2, "the condition persists, so the retry lands"
    assert rig.arm.calls == ["lhs"]

    settled = await rig.adviser.tick()
    assert targets(settled)["lhs"] == 2_500


async def test_release_unverified_is_terminal_for_the_window(night: Any, api: Any) -> None:
    """An unverifiable resume leaves the pod in UNKNOWN standby state: the
    release goes terminal for the window — loud, no re-arm, no further
    writes — until the operator looks."""
    rig = make_standby_rig(
        night,
        api,
        make_fleet(api, {"lhs": 1_500.0, "mid": 300.0, "rhs": 100.0}),
        resume_refusals=[ParkingRefusal(PARK_READBACK_UNVERIFIED, "readback did not match")],
    )
    await rig.adviser.tick()

    set_load(rig, "lhs", 600.0)
    failing = await rig.adviser.tick()

    assert NIGHT_STANDBY_RELEASE_UNVERIFIED in failing.reason_codes
    assert rig.arm.calls == []

    set_load(rig, "lhs", 500.0)
    await rig.adviser.tick()
    assert len(rig.park.resume_calls) == 1, "terminal: no further release attempts"
