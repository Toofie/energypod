"""Safety contracts for deterministic fleet-total power allocation.

The documentation requires exact totals after clamping, scope containment,
per-unit headroom, permutation invariance, and unsigned domain magnitudes. It
does not prescribe max-min fairness, so this suite does not freeze an
undocumented distribution algorithm.
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
) -> object:
    return models.PowerIntent(
        id="intent-allocation",
        source=models.IntentSource.OPTIMIZER,
        selected_unit_ids=selected,
        direction=getattr(models.Direction, direction),
        watts=watts,
        duration_s=5.0,
        created_at_mono=100.0,
        actor_identity="optimizer:test",
    )


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
