"""S1 contract tests for deterministic intent arbitration and stop latching."""

from __future__ import annotations

import importlib
import itertools
from types import SimpleNamespace
from typing import Any

import pytest


NOW = 100.0


@pytest.fixture(scope="module")
def api() -> SimpleNamespace:
    domain = importlib.import_module("energypod.domain")
    application = importlib.import_module("energypod.application")
    required_domain = ("Direction", "IntentSource", "PowerIntent")
    missing = [name for name in required_domain if not hasattr(domain, name)]
    if not hasattr(application, "IntentArbiter"):
        missing.append("IntentArbiter")
    assert not missing, f"public arbiter contract is not implemented: {', '.join(missing)}"
    return SimpleNamespace(
        **{name: getattr(domain, name) for name in required_domain},
        IntentArbiter=application.IntentArbiter,
    )


def make_intent(
    api: SimpleNamespace,
    *,
    intent_id: str,
    source: Any,
    created_at_mono: float = 90.0,
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
        created_at_mono=created_at_mono,
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
        created_at_mono=100.0,
        duration_s=10.0,
    )
    assert api.IntentArbiter().select([intent], now) is None


def test_intent_is_live_immediately_before_its_deadline(api: SimpleNamespace) -> None:
    intent = make_intent(
        api,
        intent_id="live",
        source=api.IntentSource.MANUAL,
        created_at_mono=100.0,
        duration_s=10.0,
    )
    assert api.IntentArbiter().select([intent], 109.999).id == "live"


def test_future_dated_intent_is_not_yet_eligible(api: SimpleNamespace) -> None:
    future = make_intent(
        api,
        intent_id="future",
        source=api.IntentSource.MANUAL,
        created_at_mono=100.001,
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
def test_each_adjacent_priority_dominates_regardless_of_creation_time(
    api: SimpleNamespace, winner_source: str, loser_source: str
) -> None:
    higher = make_intent(
        api,
        intent_id="higher",
        source=getattr(api.IntentSource, winner_source),
        created_at_mono=80.0,
        duration_s=30.0,
    )
    newer_lower = make_intent(
        api,
        intent_id="newer-lower",
        source=getattr(api.IntentSource, loser_source),
        created_at_mono=99.0,
    )
    assert api.IntentArbiter().select([newer_lower, higher], NOW).id == "higher"


def test_newest_creation_time_wins_within_equal_priority(api: SimpleNamespace) -> None:
    older = make_intent(
        api,
        intent_id="older",
        source=api.IntentSource.MANUAL,
        created_at_mono=90.0,
    )
    newer = make_intent(
        api,
        intent_id="newer",
        source=api.IntentSource.MANUAL,
        created_at_mono=99.0,
    )
    assert api.IntentArbiter().select([newer, older], NOW).id == "newer"


def test_lexicographically_smallest_id_is_stable_final_tie_break(
    api: SimpleNamespace,
) -> None:
    alpha = make_intent(
        api,
        intent_id="intent-a",
        source=api.IntentSource.AGENT,
        created_at_mono=99.0,
    )
    zulu = make_intent(
        api,
        intent_id="intent-z",
        source=api.IntentSource.AGENT,
        created_at_mono=99.0,
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
        created_at_mono=90.0,
        duration_s=5.0,
    )
    schedule = make_intent(
        api,
        intent_id="schedule",
        source=api.IntentSource.SCHEDULE,
        created_at_mono=90.0,
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
        created_at_mono=99.0,
        duration_s=1.5,
        actor_identity="safety-owner",
    )
    manual = make_intent(
        api,
        intent_id="manual",
        source=api.IntentSource.MANUAL,
        created_at_mono=109.0,
    )

    assert arbiter.select([stop], NOW).id == "stop-1"
    assert arbiter.select([manual], 110.0).id == "stop-1"


def test_expired_unseen_emergency_stop_does_not_latch(api: SimpleNamespace) -> None:
    stop = make_intent(
        api,
        intent_id="already-expired",
        source=api.IntentSource.EMERGENCY_STOP,
        created_at_mono=90.0,
        duration_s=5.0,
    )
    assert api.IntentArbiter().select([stop], NOW) is None


def test_only_latch_owner_can_clear_emergency_stop(api: SimpleNamespace) -> None:
    arbiter = api.IntentArbiter()
    stop = make_intent(
        api,
        intent_id="stop-owned",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-a",
    )
    arbiter.select([stop], NOW)

    with pytest.raises(PermissionError, match="owner"):
        arbiter.clear_emergency_stop(
            intent_id="stop-owned", actor_identity="owner-b"
        )

    assert arbiter.select([], NOW + 1).id == "stop-owned"


def test_clear_requires_the_current_latched_intent_id(api: SimpleNamespace) -> None:
    arbiter = api.IntentArbiter()
    stop = make_intent(
        api,
        intent_id="stop-current",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-a",
    )
    arbiter.select([stop], NOW)

    with pytest.raises(KeyError, match="stop-current"):
        arbiter.clear_emergency_stop(
            intent_id="different-stop", actor_identity="owner-a"
        )


def test_owner_clear_releases_latch_and_allows_normal_arbitration(
    api: SimpleNamespace,
) -> None:
    arbiter = api.IntentArbiter()
    stop = make_intent(
        api,
        intent_id="stop-clearable",
        source=api.IntentSource.EMERGENCY_STOP,
        actor_identity="owner-a",
    )
    manual = make_intent(
        api,
        intent_id="manual-after-clear",
        source=api.IntentSource.MANUAL,
        created_at_mono=99.0,
    )
    arbiter.select([stop], NOW)

    arbiter.clear_emergency_stop(
        intent_id="stop-clearable", actor_identity="owner-a"
    )

    assert arbiter.select([manual], NOW + 1).id == "manual-after-clear"


def test_new_stop_relatches_after_a_previous_stop_was_cleared(api: SimpleNamespace) -> None:
    arbiter = api.IntentArbiter()
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
        created_at_mono=101.0,
    )
    arbiter.select([first], NOW)
    arbiter.clear_emergency_stop(intent_id="first-stop", actor_identity="owner-a")

    assert arbiter.select([second], 101.0).id == "second-stop"
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
            created_at_mono=NOW,
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
