"""S1 contract tests for deterministic intent arbitration and stop latching."""

from __future__ import annotations

import importlib
import itertools
from types import SimpleNamespace
from typing import Any

import pytest

NOW = 100.0
STOP_ACKNOWLEDGE_SCOPE = "stop-acknowledge"


class FakeIntentRepository:
    def __init__(self, *intents: Any) -> None:
        self._intents = {intent.id: intent for intent in intents}
        self.removed_ids: list[str] = []

    def active(self) -> tuple[Any, ...]:
        return tuple(self._intents.values())

    def add(self, intent: Any) -> None:
        self._intents[intent.id] = intent

    def remove(self, intent_id: str) -> None:
        if intent_id not in self._intents:
            raise KeyError(intent_id)
        self.removed_ids.append(intent_id)
        del self._intents[intent_id]


@pytest.fixture(scope="module")
def api() -> SimpleNamespace:
    domain = importlib.import_module("energypod.domain")
    application = importlib.import_module("energypod.application")
    arbiter = importlib.import_module("energypod.application.arbiter")
    required_domain = ("Direction", "IntentSource", "PowerIntent")
    missing = [name for name in required_domain if not hasattr(domain, name)]
    if not hasattr(application, "IntentArbiter"):
        missing.append("IntentArbiter")
    if not hasattr(arbiter, "CycleArbitration"):
        missing.append("CycleArbitration")
    assert not missing, f"public arbiter contract is not implemented: {', '.join(missing)}"
    return SimpleNamespace(
        **{name: getattr(domain, name) for name in required_domain},
        IntentArbiter=application.IntentArbiter,
        CycleArbitration=arbiter.CycleArbitration,
    )


def make_intent(
    api: SimpleNamespace,
    *,
    intent_id: str,
    source: Any,
    accepted_at_mono: float = 90.0,
    acceptance_revision: int = 1,
    duration_s: float = 20.0,
    direction: Any | None = None,
    watts: int = 1_000,
    actor_identity: str = "owner-a",
    selected_unit_ids: frozenset[str] = frozenset({"mid"}),
) -> Any:
    selected_direction = direction or api.Direction.DISCHARGE
    if source is api.IntentSource.EMERGENCY_STOP:
        selected_direction = api.Direction.IDLE
        watts = 0
    return api.PowerIntent(
        id=intent_id,
        source=source,
        selected_unit_ids=selected_unit_ids,
        direction=selected_direction,
        watts=watts,
        duration_s=duration_s,
        accepted_at_mono=accepted_at_mono,
        acceptance_revision=acceptance_revision,
        actor_identity=actor_identity,
    )


def test_empty_input_selects_no_intent(api: SimpleNamespace) -> None:
    assert api.IntentArbiter().select([], NOW) is None


@pytest.mark.parametrize("now", [110.0, 110.001])
def test_intent_is_expired_at_and_after_its_deadline(api: SimpleNamespace, now: float) -> None:
    intent = make_intent(
        api,
        intent_id="expires",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=100.0,
        duration_s=10.0,
    )
    assert api.IntentArbiter().select([intent], now) is None


def test_intent_is_live_immediately_before_its_deadline(api: SimpleNamespace) -> None:
    intent = make_intent(
        api,
        intent_id="live",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=100.0,
        duration_s=10.0,
    )
    assert api.IntentArbiter().select([intent], 109.999).id == "live"


def test_future_dated_intent_is_not_yet_eligible(api: SimpleNamespace) -> None:
    future = make_intent(
        api,
        intent_id="future",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=100.001,
    )
    assert api.IntentArbiter().select([future], NOW) is None


def test_priority_order_is_emergency_manual_agent_optimizer_schedule(
    api: SimpleNamespace,
) -> None:
    intents = [
        make_intent(api, intent_id="schedule", source=api.IntentSource.SCHEDULE),
        make_intent(api, intent_id="optimizer", source=api.IntentSource.OPTIMIZER),
        make_intent(api, intent_id="agent", source=api.IntentSource.AGENT),
        make_intent(api, intent_id="manual", source=api.IntentSource.MANUAL),
        make_intent(api, intent_id="stop", source=api.IntentSource.EMERGENCY_STOP),
    ]
    arbiter = api.IntentArbiter()

    assert arbiter.select(intents[:-1], NOW).id == "manual"
    assert arbiter.select(intents, NOW).id == "stop"


@pytest.mark.parametrize(
    ("winner_source", "loser_source"),
    [
        ("MANUAL", "AGENT"),
        ("AGENT", "OPTIMIZER"),
        ("OPTIMIZER", "SCHEDULE"),
    ],
)
def test_each_adjacent_priority_dominates_regardless_of_acceptance_revision(
    api: SimpleNamespace, winner_source: str, loser_source: str
) -> None:
    higher = make_intent(
        api,
        intent_id="higher",
        source=getattr(api.IntentSource, winner_source),
        accepted_at_mono=80.0,
        acceptance_revision=1,
        duration_s=30.0,
    )
    newer_lower = make_intent(
        api,
        intent_id="newer-lower",
        source=getattr(api.IntentSource, loser_source),
        accepted_at_mono=99.0,
        acceptance_revision=999,
    )
    assert api.IntentArbiter().select([newer_lower, higher], NOW).id == "higher"


def test_highest_server_acceptance_revision_wins_within_equal_priority(
    api: SimpleNamespace,
) -> None:
    later_but_lower_revision = make_intent(
        api,
        intent_id="later-lower-revision",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=99.0,
        acceptance_revision=41,
    )
    earlier_but_higher_revision = make_intent(
        api,
        intent_id="earlier-higher-revision",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=90.0,
        acceptance_revision=42,
    )
    winner = api.IntentArbiter().select(
        [later_but_lower_revision, earlier_but_higher_revision], NOW
    )
    assert winner.id == "earlier-higher-revision"


def test_lexicographically_smallest_id_is_stable_final_tie_break(
    api: SimpleNamespace,
) -> None:
    alpha = make_intent(
        api,
        intent_id="intent-a",
        source=api.IntentSource.AGENT,
        accepted_at_mono=99.0,
        acceptance_revision=7,
    )
    zulu = make_intent(
        api,
        intent_id="intent-z",
        source=api.IntentSource.AGENT,
        accepted_at_mono=98.0,
        acceptance_revision=7,
    )
    assert api.IntentArbiter().select([zulu, alpha], NOW).id == "intent-a"


def test_selection_is_permutation_invariant(api: SimpleNamespace) -> None:
    intents = [
        make_intent(api, intent_id="schedule", source=api.IntentSource.SCHEDULE),
        make_intent(api, intent_id="optimizer", source=api.IntentSource.OPTIMIZER),
        make_intent(api, intent_id="agent", source=api.IntentSource.AGENT),
        make_intent(api, intent_id="manual", source=api.IntentSource.MANUAL),
    ]

    winners = {
        api.IntentArbiter().select(permutation, NOW).id
        for permutation in itertools.permutations(intents)
    }

    assert winners == {"manual"}


def test_expiry_reveals_next_priority_without_mutating_input(api: SimpleNamespace) -> None:
    manual = make_intent(
        api,
        intent_id="manual",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=90.0,
        duration_s=5.0,
    )
    schedule = make_intent(
        api,
        intent_id="schedule",
        source=api.IntentSource.SCHEDULE,
        accepted_at_mono=90.0,
        duration_s=20.0,
    )
    intents = [manual, schedule]
    before = list(intents)

    winner = api.IntentArbiter().select(intents, NOW)

    assert winner.id == "schedule"
    assert intents == before


def test_winner_preserves_direction_magnitude_scope_and_owner(api: SimpleNamespace) -> None:
    intent = make_intent(
        api,
        intent_id="scoped",
        source=api.IntentSource.AGENT,
        direction=api.Direction.CHARGE,
        watts=777,
        actor_identity="automation-7",
        selected_unit_ids=frozenset({"lhs", "rhs"}),
    )
    winner = api.IntentArbiter().select([intent], NOW)

    assert winner is intent
    assert winner.direction is api.Direction.CHARGE
    assert winner.watts == 777
    assert winner.selected_unit_ids == frozenset({"lhs", "rhs"})
    assert winner.actor_identity == "automation-7"


def test_idle_manual_intent_still_dominates_lower_priority_nonzero_intent(
    api: SimpleNamespace,
) -> None:
    idle = make_intent(
        api,
        intent_id="manual-idle",
        source=api.IntentSource.MANUAL,
        direction=api.Direction.IDLE,
        watts=0,
    )
    discharge = make_intent(
        api,
        intent_id="agent-discharge",
        source=api.IntentSource.AGENT,
    )
    assert api.IntentArbiter().select([discharge, idle], NOW).id == "manual-idle"


def test_emergency_stop_latches_after_first_selection_and_ignores_ttl(
    api: SimpleNamespace,
) -> None:
    arbiter = api.IntentArbiter()
    stop = make_intent(
        api,
        intent_id="stop-1",
        source=api.IntentSource.EMERGENCY_STOP,
        accepted_at_mono=99.0,
        duration_s=1.5,
        actor_identity="safety-owner",
    )
    manual = make_intent(
        api,
        intent_id="manual",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=109.0,
    )

    assert arbiter.select([stop], NOW).id == "stop-1"
    assert arbiter.select([manual], 110.0).id == "stop-1"


def test_expired_unseen_emergency_stop_does_not_latch(api: SimpleNamespace) -> None:
    stop = make_intent(
        api,
        intent_id="already-expired",
        source=api.IntentSource.EMERGENCY_STOP,
        accepted_at_mono=90.0,
        duration_s=5.0,
    )
    assert api.IntentArbiter().select([stop], NOW) is None


def test_emergency_stop_acknowledgement_requires_operator_scope(
    api: SimpleNamespace,
) -> None:
    stop = make_intent(
        api,
        intent_id="stop-protected",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-a",
    )
    repository = FakeIntentRepository(stop)
    arbiter = api.IntentArbiter(intent_repository=repository)
    arbiter.select(repository.active(), NOW)

    with pytest.raises(PermissionError):
        arbiter.acknowledge_emergency_stop(
            intent_id="stop-protected",
            actor_identity="operator-without-scope",
            operator_scopes=frozenset(),
        )

    assert repository.removed_ids == []
    assert arbiter.select(repository.active(), NOW + 1).id == "stop-protected"


def test_acknowledgement_requires_the_exact_latched_stop_id(api: SimpleNamespace) -> None:
    stop = make_intent(
        api,
        intent_id="stop-current",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-a",
    )
    repository = FakeIntentRepository(stop)
    arbiter = api.IntentArbiter(intent_repository=repository)
    arbiter.select(repository.active(), NOW)

    with pytest.raises(KeyError):
        arbiter.acknowledge_emergency_stop(
            intent_id="different-stop",
            actor_identity="authorized-operator",
            operator_scopes=frozenset({STOP_ACKNOWLEDGE_SCOPE}),
        )

    assert repository.removed_ids == []
    assert arbiter.select(repository.active(), NOW + 1).id == "stop-current"


def test_authorized_operator_acknowledgement_removes_intent_and_releases_latch(
    api: SimpleNamespace,
) -> None:
    stop = make_intent(
        api,
        intent_id="stop-acknowledged",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-a",
    )
    manual = make_intent(
        api,
        intent_id="manual-after-acknowledgement",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=99.0,
    )
    repository = FakeIntentRepository(stop, manual)
    arbiter = api.IntentArbiter(intent_repository=repository)
    arbiter.select(repository.active(), NOW)

    arbiter.acknowledge_emergency_stop(
        intent_id="stop-acknowledged",
        actor_identity="different-authorized-operator",
        operator_scopes=frozenset({STOP_ACKNOWLEDGE_SCOPE}),
    )

    assert repository.removed_ids == ["stop-acknowledged"]
    assert [intent.id for intent in repository.active()] == ["manual-after-acknowledgement"]
    assert arbiter.select(repository.active(), NOW + 1).id == "manual-after-acknowledgement"


def test_new_stop_relatches_after_a_previous_stop_was_acknowledged(
    api: SimpleNamespace,
) -> None:
    first = make_intent(
        api,
        intent_id="first-stop",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-a",
    )
    second = make_intent(
        api,
        intent_id="second-stop",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-b",
        accepted_at_mono=101.0,
    )
    repository = FakeIntentRepository(first)
    arbiter = api.IntentArbiter(intent_repository=repository)
    arbiter.select(repository.active(), NOW)
    arbiter.acknowledge_emergency_stop(
        intent_id="first-stop",
        actor_identity="authorized-operator",
        operator_scopes=frozenset({STOP_ACKNOWLEDGE_SCOPE}),
    )
    repository.add(second)

    assert arbiter.select(repository.active(), 101.0).id == "second-stop"
    assert arbiter.select([], 200.0).id == "second-stop"


def test_repeated_selection_is_deterministic_without_an_emergency_latch(
    api: SimpleNamespace,
) -> None:
    arbiter = api.IntentArbiter()
    intents = [
        make_intent(api, intent_id="a", source=api.IntentSource.OPTIMIZER),
        make_intent(api, intent_id="b", source=api.IntentSource.AGENT),
    ]
    assert arbiter.select(intents, NOW) == arbiter.select(intents, NOW)


def test_emergency_stop_domain_contract_rejects_nonzero_or_nonidle_payload(
    api: SimpleNamespace,
) -> None:
    with pytest.raises(ValueError, match="emergency stop"):
        api.PowerIntent(
            id="invalid-stop",
            source=api.IntentSource.EMERGENCY_STOP,
            selected_unit_ids=frozenset({"mid"}),
            direction=api.Direction.DISCHARGE,
            watts=1,
            duration_s=10.0,
            accepted_at_mono=NOW,
            acceptance_revision=1,
            actor_identity="owner-a",
        )


@pytest.mark.parametrize("duration_s", [0.0, -1.0])
def test_nonpositive_ttl_is_rejected(api: SimpleNamespace, duration_s: float) -> None:
    with pytest.raises(ValueError, match="duration"):
        make_intent(
            api,
            intent_id="invalid-ttl",
            source=api.IntentSource.MANUAL,
            duration_s=duration_s,
        )


# --- concurrent per-unit arbitration (2026-08-24 operator requirement) ---------
#
# "I instructed MID to charge at 2,000 watts and RHS to discharge at 1,000
# watts. Only one operation functions at a time. I require both to function
# concurrently whenever a battery request is made."  The arbiter now selects a
# PER-UNIT winner set: for each unit, the highest-priority live intent claiming
# it wins that unit; an intent's effective scope is its selection minus units
# claimed by higher-priority intents; an intent whose entire scope is claimed
# away is simply not represented this cycle.  Priority order, the equal-priority
# revision/id tie rules, expiry, and emergency-stop domination are unchanged --
# applied per unit instead of to the whole fleet.


def test_disjoint_intents_are_both_represented_concurrently(api: SimpleNamespace) -> None:
    """The operator's exact scenario shape: MID charge + RHS discharge in one
    cycle, both winners, neither superseding the other."""
    mid_charge = make_intent(
        api,
        intent_id="mid-charge",
        source=api.IntentSource.MANUAL,
        direction=api.Direction.CHARGE,
        watts=2_000,
        selected_unit_ids=frozenset({"mid"}),
    )
    rhs_discharge = make_intent(
        api,
        intent_id="rhs-discharge",
        source=api.IntentSource.MANUAL,
        direction=api.Direction.DISCHARGE,
        watts=1_000,
        selected_unit_ids=frozenset({"rhs"}),
    )

    selection = api.IntentArbiter().arbitrate([rhs_discharge, mid_charge], NOW)

    assert selection.emergency is None
    assert dict(selection.winners) == {"mid": mid_charge, "rhs": rhs_discharge}
    assert selection.units == frozenset({"mid", "rhs"})
    assert dict(selection.scopes) == {
        "mid-charge": frozenset({"mid"}),
        "rhs-discharge": frozenset({"rhs"}),
    }
    assert set(selection.ranked) == {mid_charge, rhs_discharge}


def test_higher_priority_intent_erodes_a_lower_priority_scope_per_unit(
    api: SimpleNamespace,
) -> None:
    """A manual intent claiming lhs+mid erodes an agent intent claiming mid+rhs
    to just rhs: the shared unit goes to the higher priority, the agent's OTHER
    unit still runs."""
    manual = make_intent(
        api,
        intent_id="manual-two",
        source=api.IntentSource.MANUAL,
        selected_unit_ids=frozenset({"lhs", "mid"}),
    )
    agent = make_intent(
        api,
        intent_id="agent-two",
        source=api.IntentSource.AGENT,
        selected_unit_ids=frozenset({"mid", "rhs"}),
    )

    selection = api.IntentArbiter().arbitrate([agent, manual], NOW)

    assert dict(selection.winners) == {"lhs": manual, "mid": manual, "rhs": agent}
    assert dict(selection.scopes) == {
        "manual-two": frozenset({"lhs", "mid"}),
        "agent-two": frozenset({"rhs"}),
    }
    assert selection.ranked == (manual, agent)


@pytest.mark.parametrize(
    ("winner_source", "loser_source"),
    [
        ("MANUAL", "AGENT"),
        ("AGENT", "OPTIMIZER"),
        ("OPTIMIZER", "SCHEDULE"),
    ],
)
def test_each_adjacent_priority_dominates_per_unit_regardless_of_revision(
    api: SimpleNamespace, loser_source: str, winner_source: str
) -> None:
    """The existing fleet-wide priority invariants, now per unit: an older,
    lower-revision higher-priority intent still claims its unit against a newer
    lower-priority one."""
    higher = make_intent(
        api,
        intent_id="higher",
        source=getattr(api.IntentSource, winner_source),
        accepted_at_mono=80.0,
        acceptance_revision=1,
        duration_s=30.0,
        selected_unit_ids=frozenset({"mid"}),
    )
    newer_lower = make_intent(
        api,
        intent_id="newer-lower",
        source=getattr(api.IntentSource, loser_source),
        accepted_at_mono=99.0,
        acceptance_revision=999,
        selected_unit_ids=frozenset({"mid"}),
    )
    selection = api.IntentArbiter().arbitrate([newer_lower, higher], NOW)
    assert dict(selection.winners) == {"mid": higher}
    assert dict(selection.scopes) == {"higher": frozenset({"mid"})}
    assert selection.ranked == (higher,)


def test_equal_priority_overlap_resolves_per_unit_by_revision_then_id(
    api: SimpleNamespace,
) -> None:
    """Two manual intents both claiming lhs: the newest acceptance revision
    takes lhs; the older intent keeps its other unit and stays represented."""
    older = make_intent(
        api,
        intent_id="older",
        source=api.IntentSource.MANUAL,
        acceptance_revision=41,
        selected_unit_ids=frozenset({"lhs", "rhs"}),
    )
    newer = make_intent(
        api,
        intent_id="newer",
        source=api.IntentSource.MANUAL,
        acceptance_revision=42,
        selected_unit_ids=frozenset({"lhs", "mid"}),
    )

    selection = api.IntentArbiter().arbitrate([older, newer], NOW)

    assert dict(selection.winners) == {"lhs": newer, "mid": newer, "rhs": older}
    assert dict(selection.scopes) == {
        "newer": frozenset({"lhs", "mid"}),
        "older": frozenset({"rhs"}),
    }


def test_equal_revision_overlap_falls_back_to_stable_id_per_unit(
    api: SimpleNamespace,
) -> None:
    alpha = make_intent(
        api,
        intent_id="intent-a",
        source=api.IntentSource.MANUAL,
        acceptance_revision=7,
        selected_unit_ids=frozenset({"mid", "rhs"}),
    )
    zulu = make_intent(
        api,
        intent_id="intent-z",
        source=api.IntentSource.MANUAL,
        acceptance_revision=7,
        selected_unit_ids=frozenset({"mid", "lhs"}),
    )

    selection = api.IntentArbiter().arbitrate([zulu, alpha], NOW)

    assert dict(selection.winners) == {"lhs": zulu, "mid": alpha, "rhs": alpha}
    assert dict(selection.scopes) == {
        "intent-a": frozenset({"mid", "rhs"}),
        "intent-z": frozenset({"lhs"}),
    }


def test_intent_whose_entire_scope_is_claimed_is_not_represented(
    api: SimpleNamespace,
) -> None:
    """Scope erosion to nothing means the intent simply does not appear in the
    cycle -- it is not an error, and it returns the moment its claimer lapses."""
    manual = make_intent(
        api,
        intent_id="manual-wide",
        source=api.IntentSource.MANUAL,
        selected_unit_ids=frozenset({"lhs", "mid"}),
    )
    agent = make_intent(
        api,
        intent_id="agent-claimed",
        source=api.IntentSource.AGENT,
        selected_unit_ids=frozenset({"mid"}),
    )

    selection = api.IntentArbiter().arbitrate([agent, manual], NOW)

    assert selection.ranked == (manual,)
    assert dict(selection.scopes) == {"manual-wide": frozenset({"lhs", "mid"})}
    assert dict(selection.winners) == {"lhs": manual, "mid": manual}

    # The claimer's expiry releases the eroded intent on the very next cycle.
    arbiter = api.IntentArbiter()
    short_manual = make_intent(
        api,
        intent_id="manual-short",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=95.0,
        duration_s=10.0,
        selected_unit_ids=frozenset({"lhs", "mid"}),
    )
    agent_persistent = make_intent(
        api,
        intent_id="agent-persistent",
        source=api.IntentSource.AGENT,
        duration_s=600.0,
        selected_unit_ids=frozenset({"mid"}),
    )
    during = arbiter.arbitrate([short_manual, agent_persistent], NOW)
    assert during.ranked == (short_manual,)
    after = arbiter.arbitrate([short_manual, agent_persistent], 106.0)
    assert after.ranked == (agent_persistent,)
    assert dict(after.winners) == {"mid": agent_persistent}


def test_emergency_stop_dominates_every_unit_and_is_the_whole_cycle(
    api: SimpleNamespace,
) -> None:
    """A live stop is still the whole cycle: no other intent is represented,
    the stop claims exactly its own units (the kernel then fences the fleet),
    and the stop latches."""
    stop = make_intent(
        api,
        intent_id="stop-1",
        source=api.IntentSource.EMERGENCY_STOP,
        selected_unit_ids=frozenset({"mid", "rhs"}),
    )
    manual = make_intent(
        api,
        intent_id="manual-other",
        source=api.IntentSource.MANUAL,
        selected_unit_ids=frozenset({"lhs", "mid"}),
    )
    arbiter = api.IntentArbiter()

    selection = arbiter.arbitrate([manual, stop], NOW)

    assert selection.emergency is stop
    assert selection.ranked == (stop,)
    assert dict(selection.scopes) == {"stop-1": frozenset({"mid", "rhs"})}
    assert dict(selection.winners) == {"mid": stop, "rhs": stop}

    # The latch outlives the stop's own TTL and every later intent, exactly as
    # the single-winner arbiter always did.
    latched = arbiter.arbitrate([manual], 500.0)
    assert latched.emergency is stop
    assert latched.ranked == (stop,)


def test_single_intent_arbitration_matches_the_legacy_single_winner(
    api: SimpleNamespace,
) -> None:
    """The single-intent regression anchor: one live intent produces exactly
    the selection the single-winner arbiter would have run."""
    intent = make_intent(
        api,
        intent_id="solo",
        source=api.IntentSource.MANUAL,
        direction=api.Direction.CHARGE,
        watts=777,
        selected_unit_ids=frozenset({"lhs", "rhs"}),
    )
    arbiter = api.IntentArbiter()

    selection = arbiter.arbitrate([intent], NOW)

    assert selection.single is intent
    assert selection.emergency is None
    assert dict(selection.winners) == {"lhs": intent, "rhs": intent}
    assert dict(selection.scopes) == {"solo": frozenset({"lhs", "rhs"})}


def test_idle_intent_wins_only_its_own_units(api: SimpleNamespace) -> None:
    """A manual idle intent holds its units to zero while a lower-priority
    active intent still runs its own -- idleness is per unit under concurrency."""
    idle = make_intent(
        api,
        intent_id="manual-idle",
        source=api.IntentSource.MANUAL,
        direction=api.Direction.IDLE,
        watts=0,
        selected_unit_ids=frozenset({"mid"}),
    )
    discharge = make_intent(
        api,
        intent_id="agent-discharge",
        source=api.IntentSource.AGENT,
        selected_unit_ids=frozenset({"mid", "rhs"}),
    )

    selection = api.IntentArbiter().arbitrate([discharge, idle], NOW)

    assert dict(selection.winners) == {"mid": idle, "rhs": discharge}
    assert dict(selection.scopes) == {
        "manual-idle": frozenset({"mid"}),
        "agent-discharge": frozenset({"rhs"}),
    }


def test_no_live_intents_yield_an_empty_selection(api: SimpleNamespace) -> None:
    arbiter = api.IntentArbiter()
    selection = arbiter.arbitrate([], NOW)
    assert selection.ranked == ()
    assert dict(selection.winners) == {}
    assert dict(selection.scopes) == {}
    assert selection.emergency is None
    assert selection.units == frozenset()
    assert selection.single is None

    expired = make_intent(
        api,
        intent_id="expired",
        source=api.IntentSource.MANUAL,
        accepted_at_mono=90.0,
        duration_s=5.0,
    )
    assert arbiter.arbitrate([expired], NOW).ranked == ()


def test_per_unit_arbitration_is_permutation_invariant(api: SimpleNamespace) -> None:
    """The composed selection is a pure function of the intent set: identical
    winners, scopes, and rank order under every input permutation."""
    intents = [
        make_intent(
            api,
            intent_id="schedule",
            source=api.IntentSource.SCHEDULE,
            selected_unit_ids=frozenset({"lhs", "rhs"}),
        ),
        make_intent(
            api,
            intent_id="agent",
            source=api.IntentSource.AGENT,
            selected_unit_ids=frozenset({"mid", "rhs"}),
        ),
        make_intent(
            api,
            intent_id="manual",
            source=api.IntentSource.MANUAL,
            selected_unit_ids=frozenset({"mid"}),
        ),
    ]
    baseline = api.IntentArbiter().arbitrate(intents, NOW)
    signatures = {
        (
            tuple(intent.id for intent in selection.ranked),
            tuple(sorted((key, tuple(sorted(units))) for key, units in selection.scopes.items())),
            tuple(sorted((unit, intent.id) for unit, intent in selection.winners.items())),
        )
        for selection in (
            api.IntentArbiter().arbitrate(list(permutation), NOW)
            for permutation in itertools.permutations(intents)
        )
    }
    assert signatures == {
        (
            ("manual", "agent", "schedule"),
            (("agent", ("rhs",)), ("manual", ("mid",)), ("schedule", ("lhs",))),
            (("lhs", "schedule"), ("mid", "manual"), ("rhs", "agent")),
        )
    }
    assert baseline.ranked[0].id == "manual"


@pytest.mark.parametrize("now_mono", [True, "100", float("nan"), float("inf")])
def test_per_unit_arbitration_validates_the_clock(api: SimpleNamespace, now_mono: Any) -> None:
    intent = make_intent(api, intent_id="clock-probe", source=api.IntentSource.MANUAL)
    with pytest.raises((TypeError, ValueError)):
        api.IntentArbiter().arbitrate([intent], now_mono)


def test_acknowledgement_releases_the_latched_stop_for_per_unit_arbitration(
    api: SimpleNamespace,
) -> None:
    """The acknowledgement path is shared: releasing the latch lets ordinary
    per-unit arbitration resume on the next cycle."""
    stop = make_intent(
        api,
        intent_id="stop-per-unit",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-a",
    )
    manual = make_intent(
        api,
        intent_id="manual-after",
        source=api.IntentSource.MANUAL,
        selected_unit_ids=frozenset({"lhs", "mid", "rhs"}),
    )
    repository = FakeIntentRepository(stop, manual)
    arbiter = api.IntentArbiter(intent_repository=repository)
    assert arbiter.arbitrate(repository.active(), NOW).emergency is stop

    arbiter.acknowledge_emergency_stop(
        intent_id="stop-per-unit",
        actor_identity="authorized-operator",
        operator_scopes=frozenset({STOP_ACKNOWLEDGE_SCOPE}),
    )

    released = arbiter.arbitrate(repository.active(), NOW + 1)
    assert released.emergency is None
    assert released.ranked == (manual,)
    assert dict(released.winners) == {"lhs": manual, "mid": manual, "rhs": manual}
