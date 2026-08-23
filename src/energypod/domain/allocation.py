"""Deterministic, protocol-independent fleet power allocation.

The allocation contract (API_CONTRACTS "Safety kernel and arbitration") is
capacity-weighted: a fleet-total request under scarce headroom is distributed
across every participating unit, proportional to each unit's remaining
direction headroom, with an exact integer sum after clamping.  A unit never
monopolizes a request another unit could share, and a unit with no usable
headroom keeps an explicit zero-watt proposal (the non-participation
doctrine).
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .intents import Direction, PowerIntent
from .models import _FrozenStringMapping


class UnitHeadroom(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")
    unit_id: str
    charge_watts: int
    discharge_watts: int
    eligible: bool = True

    @field_validator("unit_id")
    @classmethod
    def _unit_id(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("unit id must be non-empty and normalized")
        return value

    @field_validator("charge_watts", "discharge_watts")
    @classmethod
    def _headroom(cls, value: int) -> int:
        if value < 0:
            raise ValueError("headroom must be non-negative")
        return value


class FleetAllocation(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", arbitrary_types_allowed=True
    )
    direction: Direction
    allocations: Mapping[str, int]
    requested_watts: int
    allocated_watts: int
    unallocated_watts: int

    @field_validator("allocations")
    @classmethod
    def _freeze_allocations(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        copied = dict(value)
        if any(not key or key != key.strip() for key in copied):
            raise ValueError("allocation unit identifiers must be normalized")
        if any(item < 0 for item in copied.values()):
            raise ValueError("allocations must be non-negative")
        return _FrozenStringMapping(copied)

    @field_validator("requested_watts", "allocated_watts", "unallocated_watts")
    @classmethod
    def _nonnegative_totals(cls, value: int) -> int:
        if value < 0:
            raise ValueError("allocation totals must be non-negative")
        return value

    @model_validator(mode="after")
    def _coherent(self) -> FleetAllocation:
        if sum(self.allocations.values()) != self.allocated_watts:
            raise ValueError("allocation entries must sum to the allocated total")
        if self.allocated_watts + self.unallocated_watts != self.requested_watts:
            raise ValueError("allocated and unallocated totals must equal the request")
        if self.direction is Direction.IDLE:
            if self.requested_watts != 0 or any(self.allocations.values()):
                raise ValueError("idle allocation must contain only zero power")
        elif self.requested_watts <= 0:
            raise ValueError("active allocation requires a positive request")
        return self


def _distribute_demand(demand: int, capacities: Mapping[str, int]) -> dict[str, int]:
    """Capacity-weighted integer distribution of one fleet demand.

    Pure and deterministic in ``demand`` and the unit-keyed capacities alone
    (unit ids are processed in sorted order and every tie-break ends in the
    unit id, so the result is invariant under input permutation).  Three
    regimes, from least to most scarce:

    - ``demand >= total capacity``: no scarcity; every unit runs at its full
      capacity and any shortfall is reported by the caller as unallocated.
    - ``demand < number of participating units`` (units with capacity > 0):
      the request cannot give every participating unit its first watt, so it
      CONCENTRATES by capacity priority — largest capacity first, ties by
      unit id — and the unfillable tail receives zero.  This is the only
      regime in which a participating unit may end at zero.
    - otherwise: every participating unit is reserved one watt (the
      participation floor: no eligible unit is left at a zero-watt proposal
      while another runs below its own headroom), and the remainder is split
      across the REMAINING capacity proportional to it, by exact integer
      largest-remainder with ties broken by residual capacity then unit id.
    """
    selected = sorted(capacities)
    total_capacity = sum(capacities.values())
    participating = [unit for unit in selected if capacities[unit] > 0]
    if demand >= total_capacity:
        return dict(capacities)
    if demand < len(participating):
        concentrated = {unit: 0 for unit in selected}
        remaining = demand
        for unit in sorted(participating, key=lambda item: (-capacities[item], item)):
            concentrated[unit] = min(remaining, capacities[unit])
            remaining -= concentrated[unit]
        return concentrated
    reserved = len(participating)
    base = {unit: 1 if capacities[unit] > 0 else 0 for unit in selected}
    remaining = demand - reserved
    residual = {unit: capacities[unit] - base[unit] for unit in selected}
    residual_total = total_capacity - reserved
    floors = {unit: remaining * residual[unit] // residual_total for unit in selected}
    leftover = remaining - sum(floors.values())
    # The exact fractional remainder (never a float): the leftover watts go
    # one each to the largest remainders.  A unit whose residual capacity is
    # exhausted can never carry a positive remainder, so it is never bumped
    # past its capacity.
    bumpable = sorted(
        (unit for unit in selected if residual[unit] > floors[unit]),
        key=lambda unit: (
            -(remaining * residual[unit] - floors[unit] * residual_total),
            -residual[unit],
            unit,
        ),
    )
    bumped = set(bumpable[:leftover])
    return {unit: base[unit] + floors[unit] + (1 if unit in bumped else 0) for unit in selected}


def _distribute_per_unit_targets(
    demand: int, capacities: Mapping[str, int], targets: Mapping[str, int]
) -> dict[str, int]:
    """Distribute one demand where every unit names its own watt target.

    A target is a per-unit CAP (the 2026-08-23 operator ruling: each setting
    is that battery's own request), never a floor.  The capacity-weighted
    share is clamped to the unit's own target; the watts that clamping
    released — chiefly the shortfall of units whose target exceeds their
    headroom — are redistributed across the units still below their targets,
    bounded by each unit's own target-versus-allocation gap and headroom.  A
    unit whose target is fully met stops absorbing redistribution, and the
    fleet total never exceeds the demand, so the exact-sum invariants and the
    exported result ``allocated == min(demand, sum of serving capacities)``
    hold by construction.  Concentration below one watt per participating
    unit is inherited from :func:`_distribute_demand`, now per-target.
    """
    initial = _distribute_demand(demand, capacities)
    serving = {unit: min(targets[unit], capacities[unit]) for unit in initial}
    clamped = {unit: min(initial[unit], targets[unit]) for unit in initial}
    released = sum(initial.values()) - sum(clamped.values())
    # A unit whose weighted share already exceeds its target cannot also hold
    # a serving gap: the share never exceeds headroom, so target < share
    # implies target < headroom and the unit was clamped exactly to target.
    gaps = {unit: serving[unit] - clamped[unit] for unit in initial}
    redistributed = _distribute_demand(released, gaps)
    return {unit: clamped[unit] + redistributed[unit] for unit in initial}


def allocate_fleet_power(
    intent: PowerIntent,
    headrooms: tuple[UnitHeadroom, ...],
    *,
    export_cap_w: int | None = None,
) -> FleetAllocation:
    if type(intent) is not PowerIntent:
        raise TypeError("intent must be a PowerIntent")
    if type(headrooms) is not tuple or any(type(item) is not UnitHeadroom for item in headrooms):
        raise TypeError("headrooms must be a tuple of UnitHeadroom values")
    if export_cap_w is not None and (type(export_cap_w) is not int or export_cap_w < 0):
        raise ValueError("export cap must be a non-negative integer")
    by_id: dict[str, UnitHeadroom] = {}
    for item in headrooms:
        if item.unit_id in by_id:
            raise ValueError(f"duplicate headroom for {item.unit_id}")
        by_id[item.unit_id] = item
    selected = sorted(intent.selected_unit_ids)
    if intent.direction is Direction.IDLE:
        return FleetAllocation(
            direction=intent.direction,
            allocations={unit: 0 for unit in selected},
            requested_watts=0,
            allocated_watts=0,
            unallocated_watts=0,
        )
    missing = set(selected) - set(by_id)
    if missing:
        raise ValueError(f"missing headroom for selected units: {sorted(missing)}")
    targets = intent.watts_by_unit
    if targets is not None and set(targets) != set(selected):
        # The domain model already refuses this shape; the allocator stays
        # fail-closed against any future bypass of that validation.
        raise ValueError("per-unit targets must name exactly the selected units")
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": the
    # measured-export bound is ONE additional min() term on the effective
    # demand.  It can only lower power below today's limits and — crucially
    # for the all-zero doctrine — a cap of 0 stays a legitimate allocation
    # (every selected unit proposes explicit non-participation), never an
    # error and never a reversal.
    demand = intent.watts if export_cap_w is None else min(intent.watts, export_cap_w)
    capacities = {
        unit_id: (
            0
            if not by_id[unit_id].eligible
            else (
                by_id[unit_id].charge_watts
                if intent.direction is Direction.CHARGE
                else by_id[unit_id].discharge_watts
            )
        )
        for unit_id in selected
    }
    if targets is None:
        allocations = _distribute_demand(demand, capacities)
    else:
        allocations = _distribute_per_unit_targets(demand, capacities, targets)
    # The unallocated remainder keeps absorbing whatever the cap (or headroom)
    # denied, so the exact-sum invariants are unchanged: allocated plus
    # unallocated equals the intent's own request.
    allocated_watts = sum(allocations.values())
    return FleetAllocation(
        direction=intent.direction,
        allocations=allocations,
        requested_watts=intent.watts,
        allocated_watts=allocated_watts,
        unallocated_watts=intent.watts - allocated_watts,
    )
