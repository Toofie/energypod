"""Strict, immutable startup configuration.

Configuration is an authority boundary: values are never silently coerced and
unknown keys are rejected at every nesting level.
"""

from __future__ import annotations

import math
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)


class _FrozenModel(BaseModel):
    # JSON configuration naturally represents enums as strings and immutable
    # tuples as arrays. Individual scalar annotations remain strict.
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ControllerMode(StrEnum):
    OBSERVE_ONLY = "observe_only"
    WRITE_ENABLED = "write_enabled"


class TransportProfile(StrEnum):
    WAVESHARE_RTU_OVER_TCP = "waveshare_rtu_over_tcp"


class ProtocolProfile(StrEnum):
    IOT = "iot"
    LEGACY = "legacy"


NonEmpty = Annotated[StrictStr, Field(min_length=1)]
PositiveFiniteFloat = Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)]
NonNegativeFiniteFloat = Annotated[StrictFloat, Field(ge=0, allow_inf_nan=False)]
PositiveStrictInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeStrictInt = Annotated[StrictInt, Field(ge=0)]


def _plain(value: str, *, label: str) -> str:
    if value != value.strip() or not value:
        raise ValueError(f"{label} must be non-empty and have no surrounding whitespace")
    return value


class SiteConfig(_FrozenModel):
    site_id: NonEmpty
    timezone: NonEmpty
    expected_unit_count: PositiveStrictInt

    @field_validator("site_id")
    @classmethod
    def validate_site_id(cls, value: str) -> str:
        return _plain(value, label="site_id")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = _plain(value, label="timezone")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class EndpointConfig(_FrozenModel):
    host: NonEmpty
    port: Annotated[StrictInt, Field(ge=1, le=65535)]

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return _plain(value, label="host")


class UnitConfig(_FrozenModel):
    unit_id: NonEmpty
    display_name: NonEmpty
    endpoint: EndpointConfig
    transport_profile: TransportProfile
    protocol_profile: ProtocolProfile
    device_id: Annotated[StrictInt, Field(ge=1, le=247)]
    expected_identity: NonEmpty
    expected_cell_count: PositiveStrictInt

    @field_validator("unit_id", "display_name", "expected_identity")
    @classmethod
    def validate_identity_text(cls, value: str, info: object) -> str:
        name = getattr(info, "field_name", "identity")
        return _plain(value, label=name)


class TimingConfig(_FrozenModel):
    device_command_expiry_s: PositiveFiniteFloat
    device_command_expiry_evidence: NonEmpty
    control_period_s: PositiveFiniteFloat
    essential_read_timeout_s: PositiveFiniteFloat
    kernel_timeout_s: PositiveFiniteFloat
    audit_timeout_s: PositiveFiniteFloat
    write_timeout_s: PositiveFiniteFloat
    acknowledgement_timeout_s: PositiveFiniteFloat
    maximum_jitter_s: PositiveFiniteFloat
    renewal_margin_s: PositiveFiniteFloat
    # Inter-frame gap the RTU-over-TCP gateway needs between bus requests
    # (the prior integration's proven 0.1 s; see waveshare.py). Default keeps
    # the commissioned value so a config omitting it still behaves safely.
    inter_request_delay_s: NonNegativeFiniteFloat = 0.1

    @field_validator("device_command_expiry_evidence")
    @classmethod
    def validate_evidence(cls, value: str) -> str:
        return _plain(value, label="device_command_expiry_evidence")

    @model_validator(mode="after")
    def validate_complete_budget(self) -> Self:
        operation_budget = (
            self.essential_read_timeout_s
            + self.kernel_timeout_s
            + self.audit_timeout_s
            + self.write_timeout_s
            + self.acknowledgement_timeout_s
            + self.maximum_jitter_s
            + self.renewal_margin_s
        )
        cadence_budget = self.control_period_s + self.maximum_jitter_s + self.renewal_margin_s
        if operation_budget >= self.device_command_expiry_s:
            raise ValueError("complete timing budget must fit inside device command expiry")
        if cadence_budget >= self.device_command_expiry_s:
            raise ValueError("control renewal budget must fit inside device command expiry")
        # The composition root wires ``write_timeout_s`` as the actor's
        # heartbeat safety margin and ``control_period_s`` as its heartbeat
        # interval, so a write timeout that cannot fit strictly inside the
        # control period is a configuration the runtime can never compose.
        # Rejecting it here keeps ``check-config`` and ``build_runtime`` in
        # agreement instead of approving a timing budget that always fails.
        if self.write_timeout_s >= self.control_period_s:
            raise ValueError(
                "write timeout must fit strictly inside the control period: it is wired as "
                "the heartbeat safety margin inside the heartbeat interval"
            )
        return self


class PolicyConfig(_FrozenModel):
    version: PositiveStrictInt
    threshold_provenance: NonEmpty
    max_fleet_charge_w: PositiveStrictInt
    max_fleet_discharge_w: PositiveStrictInt
    max_unit_charge_w: PositiveStrictInt
    max_unit_discharge_w: PositiveStrictInt
    minimum_soc_pct: Annotated[StrictFloat, Field(ge=0, le=100)]
    maximum_soc_pct: Annotated[StrictFloat, Field(ge=0, le=100)]
    minimum_cell_v: PositiveFiniteFloat
    maximum_cell_v: PositiveFiniteFloat
    maximum_cell_imbalance_v: PositiveFiniteFloat
    minimum_temperature_c: StrictFloat
    maximum_temperature_c: StrictFloat
    maximum_soc_difference_pct: PositiveFiniteFloat
    maximum_soc_jump_pct: PositiveFiniteFloat
    maximum_telemetry_age_s: PositiveFiniteFloat
    maximum_cell_data_age_s: PositiveFiniteFloat
    authorization_lifetime_s: PositiveFiniteFloat
    ramp_limit_w_per_s: PositiveStrictInt
    stable_samples_to_rearm: PositiveStrictInt
    reactive_power_limit_var: NonNegativeStrictInt
    blocking_fault_codes: tuple[NonEmpty, ...]
    debug_modes_enabled: StrictBool

    @field_validator("threshold_provenance")
    @classmethod
    def validate_provenance(cls, value: str) -> str:
        return _plain(value, label="threshold_provenance")

    @field_validator("minimum_temperature_c", "maximum_temperature_c")
    @classmethod
    def validate_finite_temperature(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("temperature must be finite")
        return value

    @field_validator("blocking_fault_codes")
    @classmethod
    def validate_fault_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_plain(value, label="blocking fault code") for value in values)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("blocking fault codes must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        if self.minimum_soc_pct >= self.maximum_soc_pct:
            raise ValueError("minimum SOC must be below maximum SOC")
        if self.minimum_cell_v >= self.maximum_cell_v:
            raise ValueError("minimum cell voltage must be below maximum cell voltage")
        if self.minimum_temperature_c >= self.maximum_temperature_c:
            raise ValueError("minimum temperature must be below maximum temperature")
        if self.debug_modes_enabled:
            raise ValueError("debug modes are prohibited in the production controller")
        return self


class AuthenticationConfig(_FrozenModel):
    enabled: StrictBool
    operator_credential_ref: NonEmpty
    trusted_proxy_cidrs: tuple[NonEmpty, ...]

    @field_validator("operator_credential_ref")
    @classmethod
    def validate_secret_reference(cls, value: str) -> str:
        value = _plain(value, label="operator_credential_ref")
        if value.lower() in {"changeme", "default"} or not value.startswith("secret://"):
            raise ValueError("operator_credential_ref must be a non-default secret reference")
        if not value.removeprefix("secret://"):
            raise ValueError("operator_credential_ref must identify a secret")
        return value

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def validate_cidrs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw_value in values:
            value = _plain(raw_value, label="trusted proxy CIDR")
            network: IPv4Network | IPv6Network
            try:
                network = IPv4Network(value, strict=False)
            except ValueError:
                try:
                    network = IPv6Network(value, strict=False)
                except ValueError as exc:
                    raise ValueError(f"invalid trusted proxy CIDR: {value}") from exc
            normalized.append(str(network))
        if len(normalized) != len(set(normalized)):
            raise ValueError("trusted proxy CIDRs must be unique")
        return tuple(normalized)


class StorageConfig(_FrozenModel):
    database_path: Path
    busy_timeout_ms: PositiveStrictInt

    @field_validator("database_path")
    @classmethod
    def validate_database_path(cls, value: Path) -> Path:
        if value == Path(".") or str(value).strip() in {"", ":memory:"}:
            raise ValueError("database_path must identify a durable database file")
        return value


class ControllerConfig(_FrozenModel):
    schema_version: Annotated[StrictInt, Field(ge=1)]
    revision: Annotated[StrictInt, Field(ge=1)]
    mode: ControllerMode
    site: SiteConfig
    units: tuple[UnitConfig, ...]
    timing: TimingConfig
    policy: PolicyConfig | None = None
    authentication: AuthenticationConfig | None = None
    # API_CONTRACTS "Runtime composition and entry point" grants fully
    # in-memory persistence when no database path is configured, so storage
    # is optional; when present it must identify a durable database file.
    storage: StorageConfig | None = None

    @field_validator("units")
    @classmethod
    def validate_units(
        cls, units: tuple[UnitConfig, ...], info: ValidationInfo
    ) -> tuple[UnitConfig, ...]:
        site = info.data.get("site")
        if site is not None and len(units) != site.expected_unit_count:
            raise ValueError("units must match the site's declared expected unit count")
        unit_ids = [unit.unit_id for unit in units]
        identities = [unit.expected_identity for unit in units]
        endpoints = [(unit.endpoint.host, unit.endpoint.port) for unit in units]
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("duplicate unit_id in units")
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate expected_identity in units")
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("duplicate endpoint in units")
        return units

    @field_validator("policy")
    @classmethod
    def validate_policy(cls, policy: PolicyConfig | None, info: object) -> PolicyConfig | None:
        values = getattr(info, "data", {})
        if values.get("mode") is ControllerMode.WRITE_ENABLED and policy is None:
            raise ValueError("write-enabled mode requires policy")
        timing = values.get("timing")
        if policy is not None and timing is not None:
            if timing.control_period_s >= policy.authorization_lifetime_s:
                raise ValueError("authorization lifetime must exceed control period")
            if policy.authorization_lifetime_s >= timing.device_command_expiry_s:
                raise ValueError("authorization lifetime must be inside device expiry")
        return policy

    @field_validator("authentication")
    @classmethod
    def validate_authentication(
        cls, authentication: AuthenticationConfig | None, info: object
    ) -> AuthenticationConfig | None:
        if getattr(info, "data", {}).get("mode") is ControllerMode.WRITE_ENABLED and (
            authentication is None or not authentication.enabled
        ):
            raise ValueError("write-enabled mode requires enabled authentication")
        return authentication

    @model_validator(mode="after")
    def validate_write_topology(self) -> Self:
        if self.mode is ControllerMode.WRITE_ENABLED:
            unsafe_units = [
                unit.unit_id
                for unit in self.units
                if unit.protocol_profile is ProtocolProfile.IOT and unit.expected_cell_count > 60
            ]
            if unsafe_units:
                raise ValueError(
                    "write-enabled IoT topology exceeds the evidenced 60-cell register packing: "
                    + ", ".join(sorted(unsafe_units))
                )
        return self
