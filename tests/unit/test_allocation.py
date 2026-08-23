"""Safety contracts for deterministic fleet-total power allocation.

The documentation requires exact totals after clamping, scope containment,
per-unit headroom, permutation invariance, and unsigned domain magnitudes. It
does not prescribe max-min fairness; it does freeze the capacity-weighted
distribution contract (API_CONTRACTS "Safety kernel and arbitration"): a
request under scarce headroom reaches every participating unit, weighted by
that unit's remaining direction headroom.
"""

from __future__ import annotations

import importlib
import math
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


def _models() -> ModuleType:
    return importlib.import_module("energypod.domain.models")


def _allocation() -> ModuleType:
    return importlib.import_module("energypod.domain.allocation")


@pytest.fixture(scope="module")
def allocation_api() -> SimpleNamespace:
    """Resolve the test-first API before Hypothesis starts generating cases.

    A missing production module is an ordinary red-suite setup failure, not a
    generated counterexample for Hypothesis to shrink or explain.
    """
    try:
        models = _models()
        allocation = _allocation()
    except (ImportError, AttributeError) as error:
        pytest.fail(f"fleet-allocation contract is not implemented: {error}", pytrace=False)
    return SimpleNamespace(models=models, allocation=allocation)


def _intent(
    models: ModuleType,
    *,
    direction: str = "DISCHARGE",
    watts: int = 10,
    selected: frozenset[str] = frozenset({"a", "b"}),
    per_unit: dict[str, int] | None = None,
) -> object:
    values: dict[str, Any] = {
        "id": "intent-allocation",
        "source": models.IntentSource.OPTIMIZER,
        "selected_unit_ids": selected,
        "direction": getattr(models.Direction, direction),
        "watts": watts,
        "duration_s": 5.0,
        "accepted_at_mono": 100.0,
        "acceptance_revision": 7,
        "actor_identity": "optimizer:test",
    }
    if per_unit is not None:
        values["watts_by_unit"] = per_unit
    return models.PowerIntent(**values)


def _headroom(
    allocation: ModuleType,
    unit_id: str,
    charge: int,
    discharge: int,
    *,
    eligible: bool = True,
) -> object:
    return allocation.UnitHeadroom(
        unit_id=unit_id,
        charge_watts=charge,
        discharge_watts=discharge,
        eligible=eligible,
    )


def _assert_exact(
    result: object,
    *,
    selected: frozenset[str],
    capacities: dict[str, int],
    requested: int,
) -> None:
    expected = min(requested, sum(capacities.values()))
    assert set(result.allocations) == selected
    assert all(type(value) is int and value >= 0 for value in result.allocations.values())
    assert all(result.allocations[unit] <= capacities[unit] for unit in selected)
    assert sum(result.allocations.values()) == expected
    assert result.requested_watts == requested
    assert result.allocated_watts == expected
    assert result.unallocated_watts == requested - expected


def test_required_allocation_api_is_importable() -> None:
    module = _allocation()
    assert hasattr(module, "UnitHeadroom")
    assert hasattr(module, "FleetAllocation")
    assert callable(module.allocate_fleet_power)


def test_unit_headroom_is_immutable_and_not_wire_bounded() -> None:
    allocation = _allocation()
    capability = _headroom(allocation, "mid", 100_000, 200_000)
    assert (capability.charge_watts, capability.discharge_watts) == (100_000, 200_000)
    assert capability.eligible is True
    with pytest.raises((AttributeError, TypeError, ValueError)):
        capability.charge_watts = 1
    assert capability.charge_watts == 100_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit_id", ""),
        ("unit_id", " mid "),
        ("unit_id", True),
        ("charge_watts", -1),
        ("charge_watts", 1.0),
        ("charge_watts", True),
        ("charge_watts", math.nan),
        ("charge_watts", math.inf),
        ("discharge_watts", -1),
        ("discharge_watts", 1.0),
        ("discharge_watts", True),
        ("discharge_watts", math.nan),
        ("discharge_watts", math.inf),
        ("eligible", 1),
        ("eligible", "yes"),
    ],
)
def test_unit_headroom_rejects_malformed_or_coerced_values(field: str, value: object) -> None:
    allocation = _allocation()
    values: dict[str, Any] = {
        "unit_id": "mid",
        "charge_watts": 5_000,
        "discharge_watts": 4_000,
        "eligible": True,
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        allocation.UnitHeadroom(**values)


def test_zero_headroom_is_valid() -> None:
    allocation = _allocation()
    capability = _headroom(allocation, "mid", 0, 0)
    assert capability.charge_watts == capability.discharge_watts == 0


def test_headroom_validation_messages_are_stable() -> None:
    allocation = _allocation()
    pattern = r"Value error, unit id must be non-empty and normalized \[type=value_error"
    with pytest.raises(ValueError, match=pattern):
        allocation.UnitHeadroom(unit_id=" mid ", charge_watts=1, discharge_watts=1)
    with pytest.raises(
        ValueError, match=r"Value error, headroom must be non-negative \[type=value_error"
    ):
        allocation.UnitHeadroom(unit_id="mid", charge_watts=-1, discharge_watts=1)


def test_exact_sum_does_not_depend_on_an_undocumented_remainder_policy() -> None:
    models = _models()
    allocation = _allocation()
    selected = frozenset({"a", "b"})
    result = allocation.allocate_fleet_power(
        _intent(models, watts=11),
        (_headroom(allocation, "b", 100, 100), _headroom(allocation, "a", 100, 100)),
    )
    assert result.direction is models.Direction.DISCHARGE
    _assert_exact(result, selected=selected, capacities={"a": 100, "b": 100}, requested=11)


def test_constrained_units_never_overallocate_and_total_is_exact() -> None:
    models = _models()
    allocation = _allocation()
    selected = frozenset({"a", "b", "c"})
    result = allocation.allocate_fleet_power(
        _intent(models, watts=13, selected=selected),
        (
            _headroom(allocation, "c", 100, 10),
            _headroom(allocation, "a", 100, 2),
            _headroom(allocation, "b", 100, 10),
        ),
    )
    _assert_exact(result, selected=selected, capacities={"a": 2, "b": 10, "c": 10}, requested=13)


def test_insufficient_headroom_is_reported_without_overallocation() -> None:
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, watts=20),
        (_headroom(allocation, "a", 7, 4), _headroom(allocation, "b", 7, 5)),
    )
    _assert_exact(result, selected=frozenset({"a", "b"}), capacities={"a": 4, "b": 5}, requested=20)


@pytest.mark.parametrize(
    ("direction", "capacities"),
    [("CHARGE", {"a": 3, "b": 7}), ("DISCHARGE", {"a": 30, "b": 70})],
)
def test_direction_selects_headroom_but_allocations_remain_unsigned(
    direction: str, capacities: dict[str, int]
) -> None:
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, direction=direction, watts=50),
        (_headroom(allocation, "a", 3, 30), _headroom(allocation, "b", 7, 70)),
    )
    assert result.direction is getattr(models.Direction, direction)
    _assert_exact(result, selected=frozenset({"a", "b"}), capacities=capacities, requested=50)


def test_large_charge_request_is_not_wrapped_or_made_negative() -> None:
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, direction="CHARGE", watts=100_000),
        (_headroom(allocation, "a", 60_000, 0), _headroom(allocation, "b", 60_000, 0)),
    )
    assert result.direction is models.Direction.CHARGE
    _assert_exact(
        result,
        selected=frozenset({"a", "b"}),
        capacities={"a": 60_000, "b": 60_000},
        requested=100_000,
    )


def test_idle_requires_no_headroom_and_returns_explicit_zero_for_scope() -> None:
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(_intent(models, direction="IDLE", watts=0), ())
    assert result.direction is models.Direction.IDLE
    assert dict(result.allocations) == {"a": 0, "b": 0}
    assert result.allocated_watts == result.unallocated_watts == 0


def test_ineligible_units_receive_zero_and_cannot_absorb_power() -> None:
    models = _models()
    allocation = _allocation()
    selected = frozenset({"a", "b", "c"})
    result = allocation.allocate_fleet_power(
        _intent(models, watts=9, selected=selected),
        (
            _headroom(allocation, "a", 100, 100, eligible=False),
            _headroom(allocation, "b", 100, 100),
            _headroom(allocation, "c", 100, 100),
        ),
    )
    assert result.allocations["a"] == 0
    _assert_exact(result, selected=selected, capacities={"a": 0, "b": 100, "c": 100}, requested=9)


def test_all_ineligible_units_leave_request_unallocated() -> None:
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, watts=500),
        (
            _headroom(allocation, "a", 1_000, 1_000, eligible=False),
            _headroom(allocation, "b", 1_000, 1_000, eligible=False),
        ),
    )
    _assert_exact(
        result, selected=frozenset({"a", "b"}), capacities={"a": 0, "b": 0}, requested=500
    )


def test_unselected_units_are_omitted() -> None:
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, selected=frozenset({"a", "c"})),
        tuple(_headroom(allocation, unit, 100, 100) for unit in ("a", "b", "c")),
    )
    assert set(result.allocations) == {"a", "c"}


def test_missing_selected_headroom_fails_closed_for_nonzero_power() -> None:
    models = _models()
    allocation = _allocation()
    with pytest.raises((TypeError, ValueError)):
        allocation.allocate_fleet_power(
            _intent(models, selected=frozenset({"a", "missing"})),
            (_headroom(allocation, "a", 100, 100),),
        )


def test_duplicate_headroom_is_rejected_even_when_unselected() -> None:
    models = _models()
    allocation = _allocation()
    with pytest.raises((TypeError, ValueError)):
        allocation.allocate_fleet_power(
            _intent(models, selected=frozenset({"a"})),
            (
                _headroom(allocation, "a", 100, 100),
                _headroom(allocation, "b", 100, 100),
                _headroom(allocation, "b", 50, 50),
            ),
        )


def test_allocation_is_permutation_invariant() -> None:
    models = _models()
    allocation = _allocation()
    intent = _intent(models, watts=17, selected=frozenset({"a", "b", "c"}))
    units = tuple(
        _headroom(allocation, unit, cap, cap) for unit, cap in (("a", 3), ("b", 20), ("c", 20))
    )
    expected = allocation.allocate_fleet_power(intent, units)
    for ordered in (tuple(reversed(units)), units[1:] + units[:1], units[2:] + units[:2]):
        assert allocation.allocate_fleet_power(intent, ordered) == expected


def test_result_is_deeply_immutable() -> None:
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models),
        (_headroom(allocation, "a", 100, 100), _headroom(allocation, "b", 100, 100)),
    )
    with pytest.raises(TypeError):
        result.allocations["a"] = 0
    with pytest.raises((AttributeError, TypeError, ValueError)):
        result.requested_watts = 1


def test_headroom_defaults_to_eligible_and_participates_in_allocation(
    allocation_api: SimpleNamespace,
) -> None:
    allocation = allocation_api.allocation
    capability = allocation.UnitHeadroom(unit_id="mid", charge_watts=10, discharge_watts=20)
    assert capability.eligible is True
    result = allocation.allocate_fleet_power(
        _intent(allocation_api.models, watts=15, selected=frozenset({"mid"})),
        (capability,),
    )
    assert result.allocations["mid"] == 15


def test_allocation_model_is_strict_and_rejects_negative_entries_or_totals(
    allocation_api: SimpleNamespace,
) -> None:
    allocation = allocation_api.allocation
    models = allocation_api.models
    with pytest.raises((TypeError, ValueError)):
        allocation.FleetAllocation(
            direction=models.Direction.DISCHARGE,
            allocations={"a": "5"},
            requested_watts=5,
            allocated_watts=5,
            unallocated_watts=0,
        )
    with pytest.raises(
        ValueError, match=r"Value error, allocations must be non-negative \[type=value_error"
    ):
        allocation.FleetAllocation(
            direction=models.Direction.DISCHARGE,
            allocations={"a": -1},
            requested_watts=-1,
            allocated_watts=-1,
            unallocated_watts=0,
        )
    with pytest.raises(
        ValueError, match=r"Value error, allocation totals must be non-negative \[type=value_error"
    ):
        allocation.FleetAllocation(
            direction=models.Direction.DISCHARGE,
            allocations={"a": 0},
            requested_watts=-1,
            allocated_watts=0,
            unallocated_watts=-1,
        )


def test_allocation_model_rejects_incoherent_or_nonnormalized_construction(
    allocation_api: SimpleNamespace,
) -> None:
    allocation = allocation_api.allocation
    models = allocation_api.models
    pattern = r"Value error, allocation unit identifiers must be normalized \[type=value_error"
    with pytest.raises(ValueError, match=pattern):
        allocation.FleetAllocation(
            direction=models.Direction.DISCHARGE,
            allocations={"": 5},
            requested_watts=5,
            allocated_watts=5,
            unallocated_watts=0,
        )
    pattern = r"Value error, allocation entries must sum to the allocated total \[type=value_error"
    with pytest.raises(ValueError, match=pattern):
        allocation.FleetAllocation(
            direction=models.Direction.DISCHARGE,
            allocations={"a": 4},
            requested_watts=5,
            allocated_watts=5,
            unallocated_watts=0,
        )
    pattern = (
        r"Value error, allocated and unallocated totals must equal the request"
        r" \[type=value_error"
    )
    with pytest.raises(ValueError, match=pattern):
        allocation.FleetAllocation(
            direction=models.Direction.DISCHARGE,
            allocations={"a": 5},
            requested_watts=6,
            allocated_watts=5,
            unallocated_watts=0,
        )
    pattern = r"Value error, idle allocation must contain only zero power \[type=value_error"
    with pytest.raises(ValueError, match=pattern):
        allocation.FleetAllocation(
            direction=models.Direction.IDLE,
            allocations={"a": 1},
            requested_watts=1,
            allocated_watts=1,
            unallocated_watts=0,
        )
    pattern = r"Value error, active allocation requires a positive request \[type=value_error"
    with pytest.raises(ValueError, match=pattern):
        allocation.FleetAllocation(
            direction=models.Direction.DISCHARGE,
            allocations={"a": 0},
            requested_watts=0,
            allocated_watts=0,
            unallocated_watts=0,
        )


def test_allocate_arguments_are_exact_types_not_merely_iterable(
    allocation_api: SimpleNamespace,
) -> None:
    allocation = allocation_api.allocation
    models = allocation_api.models
    capability = _headroom(allocation, "a", 100, 100)
    selected_one = _intent(models, watts=10, selected=frozenset({"a"}))
    with pytest.raises(TypeError, match=r"^intent must be a PowerIntent$"):
        allocation.allocate_fleet_power(object(), (capability,))
    with pytest.raises(TypeError, match=r"^headrooms must be a tuple of UnitHeadroom values$"):
        allocation.allocate_fleet_power(selected_one, [capability])
    with pytest.raises(ValueError, match=r"^duplicate headroom for a$"):
        allocation.allocate_fleet_power(selected_one, (capability, capability))
    with pytest.raises(ValueError, match=r"^missing headroom for selected units: \['zz'\]$"):
        allocation.allocate_fleet_power(
            _intent(models, watts=10, selected=frozenset({"a", "zz"})), (capability,)
        )


@st.composite
def _fleet_cases(draw: st.DrawFn) -> tuple[list[tuple[str, int, int, bool]], frozenset[str], int]:
    count = draw(st.integers(min_value=1, max_value=12))
    ids = [f"unit-{index:02d}" for index in range(count)]
    charge = draw(st.lists(st.integers(0, 100_000), min_size=count, max_size=count))
    discharge = draw(st.lists(st.integers(0, 100_000), min_size=count, max_size=count))
    eligible = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    selected = frozenset(
        draw(st.lists(st.sampled_from(ids), min_size=1, max_size=count, unique=True))
    )
    request = draw(st.integers(1, 1_000_000))
    return list(zip(ids, charge, discharge, eligible, strict=True)), selected, request


@given(_fleet_cases(), st.sampled_from(("CHARGE", "DISCHARGE")))
@settings(max_examples=250)
def test_exact_sum_scope_headroom_and_no_multiplication_property(
    allocation_api: SimpleNamespace,
    case: tuple[list[tuple[str, int, int, bool]], frozenset[str], int],
    direction: str,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    raw, selected, request = case
    units = tuple(
        _headroom(allocation, unit, charge, discharge, eligible=ok)
        for unit, charge, discharge, ok in raw
    )
    result = allocation.allocate_fleet_power(
        _intent(models, direction=direction, watts=request, selected=selected), units
    )
    index = 1 if direction == "CHARGE" else 2
    capacities = {row[0]: row[index] if row[3] else 0 for row in raw if row[0] in selected}
    _assert_exact(result, selected=selected, capacities=capacities, requested=request)


@given(_fleet_cases(), st.sampled_from(("CHARGE", "DISCHARGE")))
@settings(max_examples=150)
def test_generated_allocations_are_input_order_independent(
    allocation_api: SimpleNamespace,
    case: tuple[list[tuple[str, int, int, bool]], frozenset[str], int],
    direction: str,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    raw, selected, request = case
    units = tuple(
        _headroom(allocation, unit, charge, discharge, eligible=ok)
        for unit, charge, discharge, ok in raw
    )
    intent = _intent(models, direction=direction, watts=request, selected=selected)
    expected = allocation.allocate_fleet_power(intent, units)
    assert allocation.allocate_fleet_power(intent, tuple(reversed(units))) == expected
    assert allocation.allocate_fleet_power(intent, units[1:] + units[:1]) == expected


# ---------------------------------------------------------------------------
# Capacity-weighted fleet distribution (the 2026-08-23 operator complaint:
# "I am powering each device at 1,000 watts and only one is being powered" --
# a fleet request under scarce headroom must reach every participating unit,
# weighted by each unit's remaining direction headroom, instead of being
# absorbed whole by the first sorted unit with headroom).
# ---------------------------------------------------------------------------


def _live_like_headrooms(allocation: ModuleType) -> tuple[object, ...]:
    """The 2026-08-23 live fleet discharge shape: one static-capped pod, two BMS-rich."""
    return (
        _headroom(allocation, "lhs", 2_500, 2_500),
        _headroom(allocation, "mid", 8_056, 8_056),
        _headroom(allocation, "rhs", 6_752, 6_752),
    )


def test_fleet_request_powers_every_eligible_unit_instead_of_one() -> None:
    """The operator scenario: 3000 W over three pods must reach all three.

    The greedy predecessor handed the entire request to the first sorted unit
    with headroom (lhs, whose 2500 W static cap absorbed it whole) and left mid
    and rhs at legal zero-watt non-participation -- exactly "only one is being
    powered at a time".
    """
    models = _models()
    allocation = _allocation()
    selected = frozenset({"lhs", "mid", "rhs"})
    result = allocation.allocate_fleet_power(
        _intent(models, watts=3_000, selected=selected),
        _live_like_headrooms(allocation),
    )
    assert all(result.allocations[unit] > 0 for unit in selected)
    _assert_exact(
        result,
        selected=selected,
        capacities={"lhs": 2_500, "mid": 8_056, "rhs": 6_752},
        requested=3_000,
    )
    # No unit monopolizes a scarce request: each stays within two watts of its
    # headroom-weighted share (one for the participation floor, one for the
    # largest-remainder rounding).
    total_capacity = 2_500 + 8_056 + 6_752
    for unit, capacity in (("lhs", 2_500), ("mid", 8_056), ("rhs", 6_752)):
        assert result.allocations[unit] <= 3_000 * capacity // total_capacity + 2


def test_capacity_weighted_shares_are_proportional_and_exact() -> None:
    """Equal headroom splits a request into equal integer shares, exact sum."""
    models = _models()
    allocation = _allocation()
    selected = frozenset({"a", "b", "c"})
    equal = tuple(_headroom(allocation, unit, 100, 100) for unit in ("a", "b", "c"))
    divisible = allocation.allocate_fleet_power(_intent(models, watts=99, selected=selected), equal)
    assert dict(divisible.allocations) == {"a": 33, "b": 33, "c": 33}
    indivisible = allocation.allocate_fleet_power(
        _intent(models, watts=100, selected=selected), equal
    )
    # The one unassignable watt goes to the stable tie-break: larger residual
    # headroom, then unit id.
    assert dict(indivisible.allocations) == {"a": 34, "b": 33, "c": 33}
    assert indivisible.allocated_watts == 100


def test_participation_floor_keeps_small_headroom_units_off_zero() -> None:
    """A tiny unit still participates while larger units run below headroom."""
    models = _models()
    allocation = _allocation()
    selected = frozenset({"a", "b", "c"})
    result = allocation.allocate_fleet_power(
        _intent(models, watts=300, selected=selected),
        (
            _headroom(allocation, "a", 10, 10),
            _headroom(allocation, "b", 5_000, 5_000),
            _headroom(allocation, "c", 5_000, 5_000),
        ),
    )
    # One reserved watt for a, then 297 distributed 9/4999/4999 by largest
    # remainder: the tie at 148 goes to the lower unit id.
    assert dict(result.allocations) == {"a": 1, "b": 150, "c": 149}
    assert result.allocated_watts == 300


def test_sub_unit_count_request_concentrates_by_capacity_priority() -> None:
    """The concentration boundary: below one watt per participating unit.

    A request smaller than the number of participating units cannot give every
    unit its first watt, so it fills by capacity priority (largest headroom
    first, ties by unit id) and the unfillable tail receives explicit
    zero-watt proposals.
    """
    models = _models()
    allocation = _allocation()
    equal = tuple(_headroom(allocation, unit, 100, 100) for unit in ("a", "b", "c"))
    result = allocation.allocate_fleet_power(
        _intent(models, watts=2, selected=frozenset({"a", "b", "c"})), equal
    )
    assert dict(result.allocations) == {"a": 2, "b": 0, "c": 0}
    skewed = allocation.allocate_fleet_power(
        _intent(models, watts=1, selected=frozenset({"a", "b"})),
        (_headroom(allocation, "a", 5, 5), _headroom(allocation, "b", 100, 100)),
    )
    assert dict(skewed.allocations) == {"a": 0, "b": 1}


def test_request_equal_to_participating_count_gives_each_unit_one_watt() -> None:
    """The boundary is inclusive: three watts over three units is one each."""
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, watts=3, selected=frozenset({"a", "b", "c"})),
        tuple(_headroom(allocation, unit, 100, 100) for unit in ("a", "b", "c")),
    )
    assert dict(result.allocations) == {"a": 1, "b": 1, "c": 1}


def test_request_above_total_headroom_fills_every_unit_and_reports_shortfall() -> None:
    """No scarcity: every unit runs at its full direction headroom."""
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, watts=350, selected=frozenset({"a", "b", "c"})),
        (
            _headroom(allocation, "a", 100, 100),
            _headroom(allocation, "b", 100, 100),
            _headroom(allocation, "c", 100, 100),
        ),
    )
    assert dict(result.allocations) == {"a": 100, "b": 100, "c": 100}
    assert result.allocated_watts == 300
    assert result.unallocated_watts == 50
    assert result.requested_watts == 350


def test_weighted_distribution_selects_headroom_by_direction() -> None:
    """The weighted split uses the charge/discharge headroom the direction names."""
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, direction="CHARGE", watts=150, selected=frozenset({"a", "b"})),
        (
            _headroom(allocation, "a", 100, 9_999),
            _headroom(allocation, "b", 200, 9_999),
        ),
    )
    # 150 over 100/200: one reserved watt each, then 148 across 99/199.
    assert dict(result.allocations) == {"a": 50, "b": 100}


def test_export_cap_bounds_the_distributed_total_and_keeps_exact_sums() -> None:
    """The measured-export min() term caps demand before distribution."""
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _intent(models, direction="CHARGE", watts=3_000, selected=frozenset({"a", "b"})),
        (
            _headroom(allocation, "a", 1_500, 0),
            _headroom(allocation, "b", 1_500, 0),
        ),
        export_cap_w=1_000,
    )
    assert dict(result.allocations) == {"a": 500, "b": 500}
    assert result.requested_watts == 3_000
    assert result.allocated_watts == 1_000
    assert result.unallocated_watts == 2_000


def test_ineligible_and_zero_headroom_units_stay_zero_while_eligible_units_share() -> None:
    """Non-participation is unchanged: ineligible or headroom-less units get 0 W."""
    models = _models()
    allocation = _allocation()
    selected = frozenset({"a", "b", "c"})
    result = allocation.allocate_fleet_power(
        _intent(models, watts=60, selected=selected),
        (
            _headroom(allocation, "a", 1_000, 1_000, eligible=False),
            _headroom(allocation, "b", 0, 0),
            _headroom(allocation, "c", 100, 100),
        ),
    )
    assert result.allocations["a"] == 0
    assert result.allocations["b"] == 0
    assert result.allocations["c"] == 60
    _assert_exact(result, selected=selected, capacities={"a": 0, "b": 0, "c": 100}, requested=60)


def test_weighted_distribution_is_permutation_invariant() -> None:
    models = _models()
    allocation = _allocation()
    intent = _intent(models, watts=3_000, selected=frozenset({"lhs", "mid", "rhs"}))
    units = _live_like_headrooms(allocation)
    expected = allocation.allocate_fleet_power(intent, units)
    assert all(expected.allocations[unit] > 0 for unit in ("lhs", "mid", "rhs"))
    for ordered in (tuple(reversed(units)), units[1:] + units[:1]):
        assert allocation.allocate_fleet_power(intent, ordered) == expected


@given(_fleet_cases(), st.sampled_from(("CHARGE", "DISCHARGE")))
@settings(max_examples=250)
def test_generated_requests_reach_every_participating_unit(
    allocation_api: SimpleNamespace,
    case: tuple[list[tuple[str, int, int, bool]], frozenset[str], int],
    direction: str,
) -> None:
    """Property: a scarce request at or above the participating count leaves
    no participating unit at a zero-watt proposal, and no unit exceeds a
    two-watt band around its headroom-weighted share."""
    models = allocation_api.models
    allocation = allocation_api.allocation
    raw, selected, request = case
    units = tuple(
        _headroom(allocation, unit, charge, discharge, eligible=ok)
        for unit, charge, discharge, ok in raw
    )
    result = allocation.allocate_fleet_power(
        _intent(models, direction=direction, watts=request, selected=selected), units
    )
    index = 1 if direction == "CHARGE" else 2
    capacities: dict[str, int] = {
        row[0]: int(row[index]) if row[3] else 0 for row in raw if row[0] in selected
    }
    total_capacity = sum(capacities.values())
    participating = {unit for unit, capacity in capacities.items() if capacity > 0}
    _assert_exact(result, selected=selected, capacities=capacities, requested=request)
    if 0 < request < total_capacity and request >= len(participating):
        assert all(result.allocations[unit] > 0 for unit in participating)
        for unit in participating:
            ideal = request * capacities[unit] // total_capacity
            assert result.allocations[unit] <= ideal + 2


# ---------------------------------------------------------------------------
# Per-unit watt targets (the 2026-08-23 operator ruling: "I asked for each
# setting to be one thousand, not a total of 1,000").  A watts_by_unit intent
# names a different watt CAP per battery; watts stays the fleet total (the
# sum of the targets), and the allocation never exceeds either bound.
# ---------------------------------------------------------------------------


def _per_unit_intent(
    models: ModuleType,
    targets: dict[str, int],
    *,
    direction: str = "DISCHARGE",
) -> object:
    return _intent(
        models,
        direction=direction,
        watts=sum(targets.values()),
        selected=frozenset(targets),
        per_unit=dict(targets),
    )


def test_per_unit_targets_are_honored_exactly_under_ample_headroom() -> None:
    """The operator scenario: different targets per battery, all deliverable.

    Each unit runs at exactly its own target -- the capacity-weighted share is
    clamped to the unit's target, and the watts released by that clamping are
    redistributed to the units still below their targets, so the fleet total
    lands exactly on the sum of the targets.
    """
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _per_unit_intent(models, {"lhs": 1_200, "mid": 400, "rhs": 800}),
        _live_like_headrooms(allocation),
    )
    assert dict(result.allocations) == {"lhs": 1_200, "mid": 400, "rhs": 800}
    assert result.requested_watts == 2_400
    assert result.allocated_watts == 2_400
    assert result.unallocated_watts == 0


def test_per_unit_target_above_headroom_clamps_and_reports_the_shortfall() -> None:
    """A target beyond a unit's headroom stops at that headroom.

    lhs asks for 1_200 W against 1_000 W of headroom: it runs at 1_000 W, the
    other units stay at their own (already fully met) targets and stop
    absorbing redistribution, and the undeliverable remainder is reported as
    unallocated rather than forced onto any unit.
    """
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _per_unit_intent(models, {"lhs": 1_200, "mid": 400, "rhs": 800}),
        (
            _headroom(allocation, "lhs", 1_000, 1_000),
            _headroom(allocation, "mid", 8_056, 8_056),
            _headroom(allocation, "rhs", 6_752, 6_752),
        ),
    )
    assert dict(result.allocations) == {"lhs": 1_000, "mid": 400, "rhs": 800}
    assert result.allocated_watts == 2_200
    assert result.unallocated_watts == 200


def test_per_unit_clamp_shortfall_redistributes_to_under_target_units() -> None:
    """Watts released by clamping flow to units still below their targets.

    Two equal-headroom units asking 10 W and 50 W: the capacity-weighted split
    gives 30/30, a clamps to its 10 W target, and the 20 W it released moves to
    b (the only unit with a remaining target-versus-allocation gap).
    """
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _per_unit_intent(models, {"a": 10, "b": 50}),
        (_headroom(allocation, "a", 1_000, 1_000), _headroom(allocation, "b", 1_000, 1_000)),
    )
    assert dict(result.allocations) == {"a": 10, "b": 50}
    assert result.allocated_watts == 60


def test_per_unit_redistribution_is_bounded_by_targets_headroom_and_total() -> None:
    """No redistribution may push a unit past its own target or headroom.

    a asks for 100 W with only 30 W of headroom and b for 200 W with plenty:
    a can never exceed 30 W, b can never exceed its 200 W target, and the fleet
    total can never exceed the request even though both units would absorb more.
    """
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _per_unit_intent(models, {"a": 100, "b": 200}),
        (_headroom(allocation, "a", 30, 30), _headroom(allocation, "b", 5_000, 5_000)),
    )
    assert dict(result.allocations) == {"a": 30, "b": 200}
    assert result.allocated_watts == 230
    assert result.unallocated_watts == 70


def test_per_unit_intent_composes_with_the_export_cap() -> None:
    """The measured-export min() term bounds the fleet demand, not the targets."""
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _per_unit_intent(models, {"a": 20, "b": 100}, direction="CHARGE"),
        (_headroom(allocation, "a", 1_000, 0), _headroom(allocation, "b", 1_000, 0)),
        export_cap_w=60,
    )
    # 60 W capacity-weighted over equal headroom is 30/30; a clamps to its 20 W
    # target and the released 10 W redistributes to b, still under its target.
    assert dict(result.allocations) == {"a": 20, "b": 40}
    assert result.requested_watts == 120
    assert result.allocated_watts == 60
    assert result.unallocated_watts == 60


def test_per_unit_ineligible_units_keep_explicit_zero_proposals() -> None:
    """The zero-watt non-participation doctrine is unchanged per unit."""
    models = _models()
    allocation = _allocation()
    result = allocation.allocate_fleet_power(
        _per_unit_intent(models, {"a": 50, "b": 50}),
        (
            _headroom(allocation, "a", 1_000, 1_000, eligible=False),
            _headroom(allocation, "b", 1_000, 1_000),
        ),
    )
    assert dict(result.allocations) == {"a": 0, "b": 50}
    assert result.allocated_watts == 50
    assert result.unallocated_watts == 50


def test_per_unit_allocation_is_permutation_invariant() -> None:
    models = _models()
    allocation = _allocation()
    intent = _per_unit_intent(models, {"lhs": 1_200, "mid": 400, "rhs": 800})
    units = (
        _headroom(allocation, "lhs", 1_000, 1_000),
        _headroom(allocation, "mid", 8_056, 8_056),
        _headroom(allocation, "rhs", 6_752, 6_752),
    )
    expected = allocation.allocate_fleet_power(intent, units)
    for ordered in (tuple(reversed(units)), units[1:] + units[:1], units[2:] + units[:2]):
        assert allocation.allocate_fleet_power(intent, ordered) == expected


def test_per_unit_concentration_boundary_below_one_watt_per_target() -> None:
    """The concentration boundary, per-target: a demand smaller than the
    participating-unit count concentrates by capacity priority, and the
    unfillable tail receives explicit zero-watt proposals."""
    models = _models()
    allocation = _allocation()
    every_watt = allocation.allocate_fleet_power(
        _per_unit_intent(models, {"a": 1, "b": 1, "c": 1}),
        tuple(_headroom(allocation, unit, 100, 100) for unit in ("a", "b", "c")),
    )
    assert dict(every_watt.allocations) == {"a": 1, "b": 1, "c": 1}
    # An export cap below the target sum concentrates the demand: two watts
    # over three one-watt targets fill a and b (capacity ties by unit id).
    capped = allocation.allocate_fleet_power(
        _per_unit_intent(models, {"a": 1, "b": 1, "c": 1}),
        tuple(_headroom(allocation, unit, 100, 100) for unit in ("a", "b", "c")),
        export_cap_w=2,
    )
    assert dict(capped.allocations) == {"a": 1, "b": 1, "c": 0}
    assert capped.allocated_watts == 2


def test_scalar_path_is_unchanged_when_watts_by_unit_is_absent() -> None:
    """No regression: the scalar fleet-total split keeps its exact shape."""
    models = _models()
    allocation = _allocation()
    intent = _intent(models, watts=2_400, selected=frozenset({"lhs", "mid", "rhs"}))
    assert intent.watts_by_unit is None
    result = allocation.allocate_fleet_power(intent, _live_like_headrooms(allocation))
    assert dict(result.allocations) == {"lhs": 347, "mid": 1_117, "rhs": 936}
    assert result.allocated_watts == 2_400


@st.composite
def _per_unit_cases(
    draw: st.DrawFn,
) -> tuple[list[tuple[str, int, int, bool]], dict[str, int]]:
    count = draw(st.integers(min_value=1, max_value=8))
    ids = [f"unit-{index:02d}" for index in range(count)]
    charge = draw(st.lists(st.integers(0, 100_000), min_size=count, max_size=count))
    discharge = draw(st.lists(st.integers(0, 100_000), min_size=count, max_size=count))
    eligible = draw(st.lists(st.booleans(), min_size=count, max_size=count))
    targets = dict(
        zip(
            ids,
            draw(st.lists(st.integers(1, 100_000), min_size=count, max_size=count)),
            strict=True,
        )
    )
    rows = [
        (unit, cap, dis, ok)
        for unit, cap, dis, ok in zip(ids, charge, discharge, eligible, strict=True)
        if unit in targets
    ]
    return rows, targets


@given(_per_unit_cases(), st.sampled_from(("CHARGE", "DISCHARGE")))
@settings(max_examples=250)
def test_generated_per_unit_allocations_respect_every_bound_exactly(
    allocation_api: SimpleNamespace,
    case: tuple[list[tuple[str, int, int, bool]], dict[str, int]],
    direction: str,
) -> None:
    """Property: every per-unit allocation lands at or below the unit's own
    target and headroom, and the fleet allocates exactly the deliverable
    total: min(fleet demand, sum of per-unit serving capacities)."""
    models = allocation_api.models
    allocation = allocation_api.allocation
    rows, targets = case
    units = tuple(
        _headroom(allocation, unit, charge, discharge, eligible=ok)
        for unit, charge, discharge, ok in rows
    )
    result = allocation.allocate_fleet_power(
        _per_unit_intent(models, targets, direction=direction), units
    )
    index = 1 if direction == "CHARGE" else 2
    selected = frozenset(targets)
    headrooms = {row[0]: (int(row[index]) if row[3] else 0) for row in rows if row[0] in selected}
    assert set(result.allocations) == selected
    assert all(
        type(value) is int and 0 <= value <= min(targets[unit], headrooms[unit])
        for unit, value in result.allocations.items()
    )
    serving = sum(min(target, headrooms[unit]) for unit, target in targets.items())
    expected_allocated = min(sum(targets.values()), serving)
    assert result.allocated_watts == expected_allocated
    assert result.unallocated_watts == sum(targets.values()) - expected_allocated


# --- surviving-scope allocation (2026-08-24 concurrent operations) -------------
#
# Per-unit arbitration erodes an intent's scope: the units higher-priority
# intents claimed are gone from its allocation.  ``allocate_fleet_power``
# therefore accepts an explicit ``unit_ids`` scope -- the intent's SURVIVING
# units -- and allocates only over it: a scalar intent distributes its whole
# demand across the survivors (the capacity-weighted contract unchanged, just
# over fewer units), a per-unit intent carries each surviving unit's own
# target as its cap with the demand re-summed over the survivors.  The
# intent's own ``requested_watts`` stays its full request; the eroded share
# surfaces as unallocated.


def test_surviving_scope_narrows_a_scalar_intents_distribution(
    allocation_api: SimpleNamespace,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    intent = _intent(models, watts=1_000, selected=frozenset({"a", "b"}))
    headrooms = (
        _headroom(allocation, "a", 2_000, 2_000),
        _headroom(allocation, "b", 2_000, 2_000),
    )

    result = allocation.allocate_fleet_power(intent, headrooms, unit_ids=frozenset({"b"}))

    assert dict(result.allocations) == {"b": 1_000}
    assert result.requested_watts == 1_000
    assert result.allocated_watts == 1_000
    assert result.unallocated_watts == 0


def test_surviving_scope_scalar_shortfall_surfaces_unallocated(
    allocation_api: SimpleNamespace,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    intent = _intent(models, watts=1_000, selected=frozenset({"a", "b"}))
    headrooms = (
        _headroom(allocation, "a", 2_000, 2_000),
        _headroom(allocation, "b", 2_000, 300),
    )

    result = allocation.allocate_fleet_power(intent, headrooms, unit_ids=frozenset({"b"}))

    assert dict(result.allocations) == {"b": 300}
    assert result.requested_watts == 1_000
    assert result.unallocated_watts == 700


def test_surviving_scope_restricts_per_unit_targets_to_the_survivors(
    allocation_api: SimpleNamespace,
) -> None:
    """A per-unit intent's demand over its surviving scope is the sum of the
    SURVIVING targets: unit a's target left with a's claimer never leaks into
    unit b's allocation."""
    models = allocation_api.models
    allocation = allocation_api.allocation
    intent = _intent(
        models,
        watts=500,
        selected=frozenset({"a", "b"}),
        per_unit={"a": 300, "b": 200},
    )
    headrooms = (
        _headroom(allocation, "a", 2_000, 2_000),
        _headroom(allocation, "b", 2_000, 2_000),
    )

    result = allocation.allocate_fleet_power(intent, headrooms, unit_ids=frozenset({"b"}))

    assert dict(result.allocations) == {"b": 200}
    assert result.allocated_watts == 200
    # The intent's own request is unchanged; a's eroded 300 W is unallocated.
    assert result.requested_watts == 500
    assert result.unallocated_watts == 300


def test_full_surviving_scope_equals_the_default_allocation(
    allocation_api: SimpleNamespace,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    intent = _intent(
        models,
        watts=500,
        selected=frozenset({"a", "b"}),
        per_unit={"a": 300, "b": 200},
    )
    headrooms = (
        _headroom(allocation, "a", 2_000, 2_000),
        _headroom(allocation, "b", 2_000, 2_000),
    )

    scoped = allocation.allocate_fleet_power(intent, headrooms, unit_ids=frozenset({"a", "b"}))
    default = allocation.allocate_fleet_power(intent, headrooms)

    assert dict(scoped.allocations) == dict(default.allocations)
    assert scoped.requested_watts == default.requested_watts
    assert scoped.unallocated_watts == default.unallocated_watts


def test_surviving_scope_composes_with_the_export_cap(
    allocation_api: SimpleNamespace,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    intent = _intent(models, watts=1_000, selected=frozenset({"a", "b"}))
    headrooms = (
        _headroom(allocation, "a", 2_000, 2_000),
        _headroom(allocation, "b", 2_000, 2_000),
    )

    result = allocation.allocate_fleet_power(
        intent, headrooms, export_cap_w=250, unit_ids=frozenset({"b"})
    )

    assert dict(result.allocations) == {"b": 250}
    assert result.unallocated_watts == 750


def test_surviving_scope_must_name_only_selected_units(
    allocation_api: SimpleNamespace,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    intent = _intent(models, watts=1_000, selected=frozenset({"a", "b"}))
    headrooms = (
        _headroom(allocation, "a", 2_000, 2_000),
        _headroom(allocation, "b", 2_000, 2_000),
        _headroom(allocation, "c", 2_000, 2_000),
    )

    with pytest.raises(ValueError, match="scope"):
        allocation.allocate_fleet_power(intent, headrooms, unit_ids=frozenset({"b", "c"}))


def test_surviving_scope_still_requires_headroom_for_its_units(
    allocation_api: SimpleNamespace,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    intent = _intent(models, watts=1_000, selected=frozenset({"a", "b"}))
    headrooms = (_headroom(allocation, "b", 2_000, 2_000),)

    with pytest.raises(ValueError, match="missing headroom"):
        allocation.allocate_fleet_power(intent, headrooms, unit_ids=frozenset({"a", "b"}))


def test_surviving_scope_for_an_idle_intent_keeps_explicit_zeros(
    allocation_api: SimpleNamespace,
) -> None:
    models = allocation_api.models
    allocation = allocation_api.allocation
    intent = _intent(models, direction="IDLE", watts=0, selected=frozenset({"a", "b"}))

    result = allocation.allocate_fleet_power(intent, (), unit_ids=frozenset({"b"}))

    assert dict(result.allocations) == {"b": 0}
