"""Deterministic, protocol-independent fleet power allocation."""

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
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": the
    # measured-export bound is ONE additional min() term on the effective
    # demand.  It can only lower power below today's limits and — crucially
    # for the all-zero doctrine — a cap of 0 stays a legitimate allocation
    # (every selected unit proposes explicit non-participation), never an
    # error and never a reversal.
    demand = intent.watts if export_cap_w is None else min(intent.watts, export_cap_w)
    remaining = demand
    allocations: dict[str, int] = {}
    for unit_id in selected:
        headroom = by_id[unit_id]
        capacity = 0
        if headroom.eligible:
            capacity = (
                headroom.charge_watts
                if intent.direction is Direction.CHARGE
                else headroom.discharge_watts
            )
        allocations[unit_id] = min(remaining, capacity)
        remaining -= allocations[unit_id]
    # The unallocated remainder keeps absorbing whatever the cap (or headroom)
    # denied, so the exact-sum invariants are unchanged: allocated plus
    # unallocated equals the intent's own request.
    allocated_watts = demand - remaining
    return FleetAllocation(
        direction=intent.direction,
        allocations=allocations,
        requested_watts=intent.watts,
        allocated_watts=allocated_watts,
        unallocated_watts=intent.watts - allocated_watts,
    )
