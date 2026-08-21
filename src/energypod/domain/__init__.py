"""Protocol-independent EnergyPod domain contracts."""

from .allocation import FleetAllocation, UnitHeadroom, allocate_fleet_power
from .authorization import AuthorizationBatch, AuthorizedSetpoint, StaleGenerationError
from .intents import Direction, IntentSource, PowerIntent
from .models import ControlPolicy, DataQuality, DecisionStatus, UnitLifecycle, UnitSetpoint
from .observations import Observation

__all__ = [
    "AuthorizationBatch",
    "AuthorizedSetpoint",
    "ControlPolicy",
    "DataQuality",
    "DecisionStatus",
    "Direction",
    "FleetAllocation",
    "IntentSource",
    "Observation",
    "PowerIntent",
    "StaleGenerationError",
    "UnitHeadroom",
    "UnitLifecycle",
    "UnitSetpoint",
    "allocate_fleet_power",
]
