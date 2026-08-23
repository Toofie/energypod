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

# API_CONTRACTS "Write-enabled run mode" (live control): the commissioned
# renewal cadence may not exceed the corroborated envelope — the vendor 1 s and
# prior-integration 1.5 s cadences — and the device command expiry a write-
# enabled deployment commissions against must be backed by the measured live
# watchdog trial (the 2026-08-22 direction trial, unrenewed expiry ~3.5-4.0 s).
_MAXIMUM_WRITE_ENABLED_CONTROL_PERIOD_S = 1.5
_LIVE_TRIAL_EVIDENCE_SCHEME = "live-trial://"
_LIVE_TRIAL_PLACEHOLDER_SEGMENTS = frozenset({"tbd", "todo", "placeholder", "none", "unknown"})


def _references_measured_live_trial(evidence: str) -> bool:
    """Whether an expiry-evidence reference names a measured live trial.

    The commissioned spelling is ``live-trial://direction-2026-08-22/rev-1``:
    the scheme asserts the reference is a measured live watchdog trial, and
    every ``/``-separated segment of the reference must be concrete — the
    observe-only placeholder spellings (``commissioning://...``), bare TODOs,
    and ``live-trial://tbd`` all fail, because none of them names the trial
    whose measurement the commissioned expiry stands on.
    """
    if not evidence.startswith(_LIVE_TRIAL_EVIDENCE_SCHEME):
        return False
    segments = evidence[len(_LIVE_TRIAL_EVIDENCE_SCHEME) :].split("/")
    if not segments or any(not segment for segment in segments):
        return False
    return all(segment.lower() not in _LIVE_TRIAL_PLACEHOLDER_SEGMENTS for segment in segments)


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
        if operation_budget >= self.device_command_expiry_s:
            raise ValueError("complete timing budget must fit inside device command expiry")
        # The renewal-cadence obligation (control period + jitter + margin
        # strictly inside the device command expiry) is mode-scoped: only a
        # composition that writes owes the device's watchdog a renewal cadence,
        # so that check lives with the mode in ``ControllerConfig`` (observe
        # -only deployments keep slower cadences against their placeholder
        # expiry doctrine; API_CONTRACTS "Write-enabled run mode").
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
    # SYNC_RESILIENCE_AUDIT S1 (2026-08-24): the decoder generates fault and
    # warning codes as "{prefix}_{bit}" over the fault catalog's word
    # prefixes -- the EE-calibration signals are WARNING bits decoded as
    # PCS_Warning0_1 and DCDC_Warning0_1 (PROTOCOL_EVIDENCE 9) -- so warning-
    # tier blocks need their own configurable set.  Empty by default: the
    # commissioning decision of WHICH warning bits block belongs to the
    # operator (both calibration bits are standing-active on this fleet, so
    # enabling them as blocking denies every dispatch).
    blocking_warning_codes: tuple[NonEmpty, ...] = ()
    # ADD-1 (2026-08-24 live blocker): the commissioned ceiling on the
    # magnitude the controller attributes to a pod's OWN autonomous charge
    # objective at the arm-time sole-writer preflight.  The pods'
    # self-consumption was measured at ~-520..-560 W daytime CT-following and
    # up to ~-2.27 kW deep self-charge; the band must cover that observed
    # autonomous range while staying inside the unit's static charge limit,
    # so a beyond-band (or discharge/positive, or reactive) objective still
    # latches external_writer exactly as before.  ``None``/absent keeps the
    # strict preflight: every nonzero objective this process did not write is
    # foreign.
    autonomous_charge_signature_max_w: PositiveStrictInt | None = None
    # Self-healing awareness layer (2026-08-24 recovery research R4, promoted
    # P1 vi): the actuation-coherence watchdog's commissioning knobs -- how
    # many consecutive authorized-but-still cycles alarm, and the absolute
    # movement floor below which a cycle is judged "not moving" (and above
    # which tiny setpoints are never concluded against; see
    # energypod.application.recovery).  Detection only: no control path
    # consumes these.
    actuation_coherence_cycles: PositiveStrictInt = 4
    actuation_coherence_min_movement_w: PositiveStrictInt = 150
    # P1 iii companion (unexpected-autonomy evidence): the commissioned
    # EXPECTED autonomous battery-power envelope -- negative self-charge up to
    # a small positive float.  Measured power outside this band while no
    # intent claims the unit is timestamped evidence, never a block.
    expected_autonomy_band_w: tuple[StrictInt, StrictInt] = (-2600, 300)
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

    @field_validator("blocking_warning_codes")
    @classmethod
    def validate_warning_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_plain(value, label="blocking warning code") for value in values)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("blocking warning codes must be unique")
        return cleaned

    @field_validator("expected_autonomy_band_w")
    @classmethod
    def validate_autonomy_band(cls, values: tuple[int, int]) -> tuple[int, int]:
        """The band must span the pods' own self-charge region.

        The commissioned envelope is negative self-charge (~-520 W daytime
        CT-following up to ~-2.6 kW deep charge) to a small positive float, so
        a well-formed band is a strictly ascending integer pair whose lower
        bound is at or below zero and whose upper bound is at or above zero:
        it brackets the uncommanded operating region instead of excluding it.
        """
        low, high = values
        if not low < high:
            raise ValueError("expected_autonomy_band_w must be strictly ascending (low, high)")
        if low > 0:
            raise ValueError(
                "expected_autonomy_band_w lower bound must be at or below zero: the pods "
                "self-charge with negative battery power"
            )
        if high < 0:
            raise ValueError(
                "expected_autonomy_band_w upper bound must be at or above zero: an idle pod "
                "floats near zero watts"
            )
        return (low, high)

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


class ExcessChargingConfig(_FrozenModel):
    """API_CONTRACTS "Excess-solar accelerated charging (advisory)" +
    DESIGN_EXCESS_ACTIVATION P6.

    An ABSENT block composes nothing (no adviser, no export triple, no
    projection, no toggle); a PRESENT block composes the machinery with
    ``enabled`` gating PARTICIPATION — an explicit ``enabled: false``
    composes suspended and is enableable at runtime, which is what makes
    the console's first enable possible without a config edit.  Every key
    carries its commissioned default so a present block states only what
    commissioning chose to override; the cross-fleet relations (cap inside
    the static unit charge limit, a freshness bound the polling loop can
    actually satisfy, a hysteresis band below the entry margin, a bounded
    renewable TTL) are validated on ``ControllerConfig``, where the policy
    and timing they relate to live.
    """

    enabled: StrictBool = False
    export_headroom_margin_w: PositiveStrictInt = 200
    max_charge_from_export_w: PositiveStrictInt = 2500
    export_telemetry_max_age_s: PositiveFiniteFloat = 3.0
    assumed_autonomous_charge_w: PositiveStrictInt = 520
    min_acceleration_w: PositiveStrictInt = 100
    exit_hysteresis_w: NonNegativeStrictInt = 50
    intent_ttl_s: PositiveFiniteFloat = 10.0


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
    # Declared LAST so its commissioning validator below sees the already
    # validated mode, timing, and policy fields through ``info.data``.
    excess_charging: ExcessChargingConfig | None = None

    @field_validator("timing")
    @classmethod
    def validate_write_enabled_timing(cls, timing: TimingConfig, info: object) -> TimingConfig:
        """The write-enabled commissioning gates (API_CONTRACTS "Write-enabled
        run mode", bullet 1).

        Only a composition that writes owes the device's watchdog a renewal
        cadence, so every check here is scoped to ``write_enabled`` and
        observe-only deployments keep their slower cadences against the
        placeholder expiry doctrine (the 9.0 s observe-only spelling,
        PROTOCOL_EVIDENCE 4b).  Write-enabled control commissions only against
        the measured live watchdog trial — the 2026-08-22 direction trial that
        measured the unrenewed objective expiry at ~3.5-4.0 s — and its
        renewal cadence may not exceed the corroborated envelope (the vendor
        1 s and prior-integration 1.5 s cadences).
        """
        if getattr(info, "data", {}).get("mode") is not ControllerMode.WRITE_ENABLED:
            return timing
        cadence_budget = timing.control_period_s + timing.maximum_jitter_s + timing.renewal_margin_s
        if cadence_budget >= timing.device_command_expiry_s:
            raise ValueError("control renewal budget must fit inside device command expiry")
        if timing.control_period_s > _MAXIMUM_WRITE_ENABLED_CONTROL_PERIOD_S:
            raise ValueError(
                "timing.control_period_s exceeds the corroborated write-enabled renewal "
                f"cadence envelope (vendor 1 s and prior-integration "
                f"{_MAXIMUM_WRITE_ENABLED_CONTROL_PERIOD_S} s): the commissioned cadence "
                "must not exceed them"
            )
        if not _references_measured_live_trial(timing.device_command_expiry_evidence):
            raise ValueError(
                "timing.device_command_expiry_evidence must reference the measured live "
                "watchdog trial (a live-trial:// reference, e.g. the 2026-08-22 direction "
                "trial that measured the ~3.5-4.0 s unrenewed objective expiry) to "
                "commission write-enabled control"
            )
        return timing

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

    @field_validator("excess_charging")
    @classmethod
    def validate_excess_charging(
        cls, excess: ExcessChargingConfig | None, info: ValidationInfo
    ) -> ExcessChargingConfig | None:
        """The commissioning gates for a PRESENT advisory block (API_CONTRACTS
        "Excess-solar accelerated charging (advisory)" + DESIGN_EXCESS_ACTIVATION
        P6).

        Every gate applies whenever the block is PRESENT, not only when
        ``enabled: true``: an explicit disabled block that could never be
        enabled SAFELY (a toggle can raise participation but never a cap,
        bound, or mode — P5) is refused at validation time.  An ABSENT block
        changes nothing anywhere.  This is a field validator on the block —
        not a mode="after" model validator — so the refusal stays attributed
        to ``excess_charging`` itself even when a sibling field has already
        failed (a write-enabled deployment missing its policy block), which
        pydantic would otherwise never reach.
        """
        if excess is None:
            return excess
        values = info.data
        if values.get("mode") is not ControllerMode.WRITE_ENABLED:
            raise ValueError(
                "excess_charging requires mode write_enabled: an observe-only "
                "composition can never actuate, so an advisory block is refused "
                "at validation time whether enabled or explicitly disabled"
            )
        policy = values.get("policy")
        if policy is None:
            raise ValueError(
                "excess_charging requires a policy block: the export bound derives "
                "from the commissioned static charge limits"
            )
        if excess.max_charge_from_export_w > policy.max_unit_charge_w:
            raise ValueError(
                "excess_charging.max_charge_from_export_w must not exceed the policy "
                "max_unit_charge_w: the export cap is one additional min() term and "
                "can never authorize more than the unit limit"
            )
        timing = values.get("timing")
        if timing is not None:
            poll_bound = timing.control_period_s + timing.essential_read_timeout_s
            if excess.export_telemetry_max_age_s <= poll_bound:
                raise ValueError(
                    "excess_charging.export_telemetry_max_age_s must exceed "
                    "timing.control_period_s + timing.essential_read_timeout_s: a "
                    "fresher demand than the polling loop can ever serve would "
                    "collapse the bound permanently"
                )
            if excess.intent_ttl_s <= timing.control_period_s:
                raise ValueError(
                    "excess_charging.intent_ttl_s must exceed timing.control_period_s: "
                    "the adviser renews exactly once per fleet cycle"
                )
        if not 0 < excess.intent_ttl_s <= 300:
            raise ValueError(
                "excess_charging.intent_ttl_s must stay inside (0, 300] seconds — the "
                "REST dispatch cap; the adviser may not out-live ordinary intents"
            )
        if excess.exit_hysteresis_w >= excess.min_acceleration_w:
            raise ValueError(
                "excess_charging.exit_hysteresis_w must stay strictly below "
                "min_acceleration_w: the hysteresis band is what keeps a dip between "
                "the entry and exit thresholds from oscillating"
            )
        return excess

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
