"""Strict, immutable startup configuration.

Configuration is an authority boundary: values are never silently coerced and
unknown keys are rejected at every nesting level.
"""

from __future__ import annotations

import math
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Annotated, Literal, Self
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
    # The pod-parking mode write's OWN budget (DESIGN_POD_PARKING section 5;
    # commissioned 2026-08-24 after the live smoke: this gateway's FC16-to-
    # 0x8000 turnaround outran the cadence-commissioned write timeout, while a
    # direct script at 1.0 s succeeded -- docs/evidence/standby-cycle-
    # 2026-08-24.md).  The mode write is a ONE-SHOT operator act, never
    # cadence-bound: it neither rides nor extends the complete operation
    # budget below (which must keep fitting inside the device command
    # expiry); the named operation's total is its prior read + this write +
    # its readback, each leg inside its own budget.  It only has to out-live
    # the inter-frame gap the write still pays and the ordinary write budget
    # it replaces for its one FC16, and stay inside the sane 5 s ceiling.
    mode_write_timeout_s: Annotated[StrictFloat, Field(gt=0, le=5.0)] = 2.0

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
        # The mode write's budget stands alone (above): it must EXCEED the
        # ordinary write budget it replaces for its one FC16 and the
        # inter-frame gap that write still pays, and never a narrowing of
        # either -- a budget that cannot cover the gateway's measured
        # debug-mode turnaround is the live-smoke failure this key exists to
        # commission away.
        if self.mode_write_timeout_s <= self.write_timeout_s:
            raise ValueError(
                "mode write timeout must exceed write_timeout_s: it is the one-shot "
                "debug-mode FC16 budget that REPLACES the cadence-bound write budget "
                "for that single write, never a narrowing of it"
            )
        if self.mode_write_timeout_s <= self.inter_request_delay_s:
            raise ValueError(
                "mode write timeout must exceed inter_request_delay_s: the named mode "
                "write still pays the commissioned inter-frame gap inside its own budget"
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
    # the recalibrated positive evening edge.  Measured power outside this
    # band while no intent claims the unit is timestamped evidence, never a
    # block.  The positive edge is the documented commissioned value (+1000,
    # config rev 5, 2026-08-23 evening): the original +300 was commissioned
    # from DAYTIME float evidence only, before the fleet's evening behavior
    # had ever been observed; lhs's own firmware then held a steady benign
    # 695-914 W CT-following discharge (99 -> 76% SOC, imbalance closing),
    # so +1000 keeps ~10-17% margin over the observed legitimate hold while
    # staying below the ±1.2 kW anomaly class -- mirroring the -2600 negative
    # edge's commissioning margin over the observed -2.27 kW deep self-charge.
    expected_autonomy_band_w: tuple[StrictInt, StrictInt] = (-2600, 1000)
    # Night-writer detector (API_CONTRACTS "Night-writer detector"): the
    # zero-extra-frames foreign-objective watch's knobs, all defaulted so an
    # unchanged policy keeps the pinned posture.  Detection only: no control
    # path consumes them.
    foreign_objective_sample_interval_s: Annotated[StrictFloat, Field(ge=1.0, le=3600.0)] = 30.0
    foreign_objective_sustained_samples: Annotated[StrictInt, Field(ge=1, le=100)] = 3
    foreign_objective_self_charge_class_w: Annotated[StrictInt, Field(ge=1, le=50000)] = 1000
    foreign_objective_handback_grace_s: Annotated[StrictFloat, Field(ge=1.0, le=300.0)] = 12.0
    # The site's own scheduled night writer, commissioned as the EXPECTED
    # nightly charge (all batteries at -<figure> W for its nightly window):
    # None is the strict default -- nothing is expected until the operator
    # says so, and every sustained beyond-class charge without PV evidence
    # escalates.
    foreign_objective_expected_charge_w: Annotated[StrictInt, Field(ge=1, le=50000)] | None = None
    # How many units must hold the synchronized in-class charge for the
    # expected-writer recognition (the scheduler charges the batteries it
    # chooses -- observed live: a synchronized pair while a full third
    # floated).  A lone pod never qualifies.
    foreign_objective_expected_min_units: Annotated[StrictInt, Field(ge=2, le=50)] = 2
    # The always-false tombstone, superseded by NOTHING except the parking
    # block below (DESIGN_POD_PARKING section 5.1): ``policy.debug_modes_enabled``
    # can never compose the 0x8000 debug-mode write -- the ``parking:`` block
    # is the one and only policy flag that ever can, and it says {0, 1}, never
    # the vendor values 2-6.
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
            raise ValueError(
                "debug_modes_enabled stays false forever: the parking block "
                "(DESIGN_POD_PARKING section 5.1) is the one and only composer of the "
                "0x8000 debug-mode write, and it writes {0, 1} only -- the vendor values "
                "2-6 are permanently unexposed"
            )
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
    # DESIGN_SCHEDULES §4 (yield_to_schedule, default true): while a published
    # schedule claims the adviser's target battery, the adviser stands down on
    # that battery only — per unit, never fleet-wide.  The flag exists so the
    # opt-out (today's behavior: the adviser outranks schedules and starves
    # them invisibly) is an explicit, auditable commissioning choice.
    yield_to_schedule: StrictBool = True


def _valid_policy_wall(value: str) -> str:
    """Validate one "HH:MM" civil wall clock through the canonical parser."""
    from energypod.application.scheduling import parse_hhmm

    return parse_hhmm(value).strftime("%H:%M")


class ScheduleConfig(_FrozenModel):
    """API_CONTRACTS "Schedule" + DESIGN_SCHEDULES §3 (the block-presence
    doctrine, symmetric with ``excess_charging``).

    A PRESENT block composes the whole surface — both REST routes, the
    ``ScheduleRunner`` in the fleet cycle, and the ``schedule_state``
    snapshot projection.  An ABSENT block composes NOTHING: byte-identical
    behavior, and both routes answer 409 ``schedule_not_commissioned``.  A
    schedule can never act on a site that did not commission scheduling.

    There is deliberately NO ``enabled`` key: the plan IS the state (an
    empty plan is off; an entry's own ``enabled`` flag is its pause), and a
    second master switch would be a second way to be silently off.  The
    ``allowed_windows_local`` check is a PUBLISH gate only, never an
    evaluator gate: a plan already in the store before a config NARROWING
    still evaluates (the config revision is the operator's act; the next
    publish is the one refused).
    """

    allowed_windows_local: tuple[tuple[NonEmpty, NonEmpty], ...] = (("06:00", "20:00"),)
    intent_ttl_s: PositiveFiniteFloat = 10.0

    @field_validator("allowed_windows_local")
    @classmethod
    def validate_windows(cls, values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        cleaned: list[tuple[str, str]] = []
        for start_raw, end_raw in values:
            start = _valid_policy_wall(start_raw)
            end = _valid_policy_wall(end_raw)
            if start == end:
                raise ValueError(
                    "allowed_windows_local pairs must not be zero-length (a window that "
                    "commands no minute is ambiguous)"
                )
            cleaned.append((start, end))
        if not cleaned:
            raise ValueError("allowed_windows_local must name at least one window")
        return tuple(cleaned)


class EnergyTariffConfig(_FrozenModel):
    """DESIGN_ENERGY_SCORECARD section 7: the OPTIONAL tariff keys.

    Absent means kWh-only, no money figures anywhere.  Present means the
    operator's own rates, labeled as theirs.
    """

    currency: Annotated[StrictStr, Field(min_length=3, max_length=3)]
    import_cents_per_kwh: NonNegativeFiniteFloat
    export_cents_per_kwh: NonNegativeFiniteFloat

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        return _plain(value.upper(), label="currency")


class EnergyScorecardConfig(_FrozenModel):
    """API_CONTRACTS "Energy scorecard" + DESIGN_ENERGY_SCORECARD section 7.

    Block-presence doctrine, symmetric with ``excess_charging``/``schedule``:
    a PRESENT block composes the accountant into the fleet loop, the
    ``energy_today`` snapshot key, and the days route; an ABSENT block
    composes nothing (byte-identical snapshot, 409 on the route, no energy
    decode).  There is deliberately NO ``enabled`` key: the scorecard is
    advisory-only (no safety interaction), and a second master switch would
    be a second way to be silently off.

    The A-1 gate is structural at validation time: ``grid_source:
    device_counter`` is REFUSED while ``grid_counter_roles`` is ``unpinned``
    (the role-open pair must never become the display source).  A PINNED
    roles value additionally requires the durable
    ``energy_counter_roles_pinned`` audit fact at boot -- the composition
    root's keyed existence check (the excess-economics precedent); a roles
    key without the recorded evidence is a config error naming the missing
    fact.
    """

    grid_source: Literal["integrated", "device_counter"] = "integrated"
    grid_counter_roles: Literal["unpinned", "vendor_labels", "swapped"] = "unpinned"
    integration_max_gap_s: PositiveFiniteFloat = 10.0
    min_day_coverage_pct: Annotated[StrictFloat, Field(gt=0, le=100)] = 95.0
    tariff: EnergyTariffConfig | None = None

    @model_validator(mode="after")
    def validate_a1_gate(self) -> Self:
        if self.grid_source == "device_counter" and self.grid_counter_roles == "unpinned":
            raise ValueError(
                "energy_scorecard.grid_source device_counter is refused while "
                "grid_counter_roles is unpinned: the role-open counter pair must never "
                "become the display source (the A-1 gate; pin the roles first)"
            )
        return self


class NightChargingConfig(_FrozenModel):
    """DESIGN_NIGHT_CHARGE §3.1 + API_CONTRACTS "Off-peak night charge".

    Block-presence doctrine, symmetric with ``excess_charging``/``schedule``:
    a PRESENT block composes the adviser, the ``night_charge_state``
    projection, the ``night_charge.state_changed`` events, the guarded toggle
    route, and the PCS live-block promotion; an ABSENT block composes
    NOTHING — byte-identical to today.  ``enabled`` defaults to ``false``
    and gates PARTICIPATION only (the excess activation doctrine verbatim:
    runtime state never persists, boot recomposes from this file).  The
    cross-fleet relations (the cap inside the static unit charge limit, the
    hold strictly between zero and the cap, the hysteresis band strictly
    inside the threshold, a freshness bound the polling loop can satisfy, a
    bounded renewable TTL, the capacity map exactly when the pacing rule
    needs it, and the PARTITION grant against the schedule policy) are
    validated on ``ControllerConfig``, where the blocks they relate to live.
    """

    # REQUIRED: the off-peak window is a civil-time fact the operator
    # confirms (§8 item 1); there is no default zone to fall back to.
    timezone: NonEmpty
    window_local: tuple[tuple[NonEmpty, NonEmpty], ...] = (("00:00", "06:00"),)
    enabled: StrictBool = False
    rate_cap_w: PositiveStrictInt = 2500
    demand_threshold_w: PositiveStrictInt = 1000
    demand_exit_hysteresis_w: PositiveStrictInt = 200
    # The EVIDENCE-FAILURE fallback rate alone (never a demand behavior):
    # a missing/bad/stale demand word HOLDS at this small positive charge
    # — the stand-down answers measured demand only, so bad evidence must
    # never silently free-run the fleet into autonomy drain.
    hold_rate_w: PositiveStrictInt = 100
    demand_scope: Literal["fleet", "per_phase"] = "fleet"
    pacing: Literal["cap_first", "even"] = "cap_first"
    # REQUIRED iff ``pacing: even`` (key set exactly the fleet units,
    # validated on ControllerConfig); a stray map under cap_first is refused
    # — the estimate only shapes pacing, never safety.
    assumed_capacity_wh: dict[NonEmpty, PositiveStrictInt] | None = None
    demand_telemetry_max_age_s: PositiveFiniteFloat = 3.0
    intent_ttl_s: PositiveFiniteFloat = 10.0

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = _plain(value, label="timezone")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("window_local")
    @classmethod
    def validate_windows(cls, values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        cleaned: list[tuple[str, str]] = []
        for start_raw, end_raw in values:
            start = _valid_policy_wall(start_raw)
            end = _valid_policy_wall(end_raw)
            if start == end:
                raise ValueError(
                    "window_local pairs must not be zero-length (a window that "
                    "commands no minute is ambiguous)"
                )
            cleaned.append((start, end))
        if not cleaned:
            raise ValueError("window_local must name at least one window")
        return tuple(cleaned)


class PlantHistoryConfig(_FrozenModel):
    """DESIGN_PLANT_HISTORY section 2.5 (the block-presence doctrine).

    A PRESENT block composes the historian into the fleet loop, the query
    route, the read-only MCP tool, and the snapshot's feature-detected
    ``history_state``; an ABSENT block composes nothing (byte-identical
    snapshot, 409 on the route, no samples taken).  There is deliberately NO
    ``enabled`` key: a second master switch is a second way to be silently
    off.  A PRESENT block additionally REQUIRES the ``storage`` block --
    durable history is the entire point, and a historian silently keeping
    its rows in memory is exactly the invisible-off class this project
    refuses (cross-validated on ``ControllerConfig`` where storage lives).
    """

    sample_interval_s: Annotated[StrictFloat, Field(gt=0, le=3600)] = 30.0
    retention_full_resolution_days: Annotated[StrictInt, Field(ge=1, le=3650)] = 14
    # 0 keeps the hourly rollups forever (the commissioned default, ~10.5 MB
    # per year); a positive day count bounds them.
    retention_rollup_days: Annotated[StrictInt, Field(ge=0, le=36500)] = 0


class OpenMeteoPvConfig(_FrozenModel):
    """The plane declaration the Open-Meteo PV derivation needs.

    ``capacity_kw`` is the installed kWp and ``derate`` the plane-to-AC loss
    factor the operator folds temperature, soiling, and inverter losses into.
    ``azimuth_deg`` uses Open-Meteo's docs-page convention (0 = south, -90 =
    east, +90 = west, +-180 = north) -- live-verified, and deliberately NOT
    the OpenAPI YAML's wrong "North=0" wording.
    """

    tilt_deg: Annotated[StrictFloat, Field(ge=0, le=90)]
    azimuth_deg: Annotated[StrictFloat, Field(ge=-180, le=180)]
    capacity_kw: PositiveFiniteFloat
    derate: Annotated[StrictFloat, Field(gt=0, le=1)] = 0.9


class OpenMeteoProviderConfig(_FrozenModel):
    """The keyless Open-Meteo site location (weather always, PV iff ``pv``).

    ``refresh_interval_s`` optionally overrides the stack's shared wire
    budget for this family alone (the keyless tier has no scarcity to
    ration; absent keeps the shared ``forecast_providers.refresh_interval_s``).
    """

    latitude: Annotated[StrictFloat, Field(ge=-90, le=90)]
    longitude: Annotated[StrictFloat, Field(ge=-180, le=180)]
    forecast_days: Annotated[StrictInt, Field(ge=1, le=16)] = 2
    pv: OpenMeteoPvConfig | None = None
    refresh_interval_s: PositiveFiniteFloat | None = None


class SolcastProviderConfig(_FrozenModel):
    """The keyed Solcast rooftop-site declaration (the live-verified tier).

    The API key is a REFERENCE, never material: ``api_key_env`` names the
    environment variable the composition root resolves at boot (the
    architecture's "provider credentials by secret reference").  The site
    record registered at Solcast holds the geography and the plane
    (latitude, longitude, capacity, azimuth, tilt, loss factor) -- the
    hobbyist tier's rooftop-sites flow is addressed by ``resource_id``
    ALONE, so those keys are deliberately absent here: a client-side copy
    could only drift from what Solcast actually models.  ``period`` is
    Solcast's documented averaging enum.

    ``refresh_interval_s`` optionally overrides the stack's shared wire
    budget for this family alone: hobbyist keys allow 10 requests per UTC
    day (each process start also fetches once), so this family routinely
    needs a far slower gate than the shared default.
    """

    api_key_env: NonEmpty
    resource_id: NonEmpty
    hours: Annotated[StrictInt, Field(ge=1, le=336)] = 48
    period: Literal["PT5M", "PT10M", "PT15M", "PT20M", "PT30M", "PT60M"] = "PT30M"
    refresh_interval_s: PositiveFiniteFloat | None = None

    @field_validator("api_key_env")
    @classmethod
    def validate_api_key_env(cls, value: str) -> str:
        return _plain(
            value,
            label="api_key_env",
        )

    @field_validator("resource_id")
    @classmethod
    def validate_resource_id(cls, value: str) -> str:
        value = _plain(value, label="resource_id")
        # The identifier rides the request PATH (rooftop_sites/{id}/forecasts),
        # so anything that would escape a path segment is refused, never
        # silently encoded into a request somewhere else.
        offenders = sorted(set(value) & frozenset("/?#&=%"))
        if offenders:
            raise ValueError(
                f"resource_id must be a single URL path segment (carries {offenders}; "
                "it names the registered rooftop site, e.g. b6bf-9d1d-0680-4078)"
            )
        return value


class LoadBaselineConfig(_FrozenModel):
    """The historian-statistics baseline's slot grid and look-back."""

    slot_s: Annotated[StrictFloat, Field(gt=0, le=86400)] = 1800.0
    horizon_s: Annotated[StrictFloat, Field(gt=0, le=14 * 86400)] = 43200.0
    weeks_back: Annotated[StrictInt, Field(ge=1, le=8)] = 1


class TariffWindowConfig(_FrozenModel):
    """One declared day-local tariff rate window (midnight-crossing allowed)."""

    window_local: tuple[NonEmpty, NonEmpty]
    import_cents_per_kwh: NonNegativeFiniteFloat
    export_cents_per_kwh: NonNegativeFiniteFloat

    @field_validator("window_local")
    @classmethod
    def validate_window(cls, values: tuple[str, str]) -> tuple[str, str]:
        start = _valid_policy_wall(values[0])
        end = _valid_policy_wall(values[1])
        if start == end:
            raise ValueError("window_local pairs must not be zero-length")
        return (start, end)


class StaticTariffConfig(_FrozenModel):
    """The operator's declared schedule: windows over a flat default.

    ``market``/``quality`` metadata are the provider's own honesty (every
    interval says ``static-config``/``static``); here only the numbers and
    the day-local windows are the operator's.
    """

    currency: NonEmpty
    default_import_cents_per_kwh: NonNegativeFiniteFloat
    default_export_cents_per_kwh: NonNegativeFiniteFloat
    windows: tuple[TariffWindowConfig, ...] = ()
    horizon_s: Annotated[StrictFloat, Field(gt=0, le=31 * 86400)] = 172800.0

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = _plain(value.upper(), label="currency")
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        return value


class ForecastProvidersConfig(_FrozenModel):
    """The advisory forecast-provider stack (ARCHITECTURE sections 17 and 19).

    Block-presence doctrine: a PRESENT block DECLARES the stack; an ABSENT
    block composes nothing.  ``enabled`` defaults to ``false`` and gates
    composition only -- the staged posture for a family whose wire sources
    need operator decisions (the Solcast key, the PV plane numbers, the
    tariff rates); runtime state never persists, boot recomposes from this
    file.  The shared wire budget: ``request_timeout_s`` bounds one leg,
    ``refresh_interval_s`` is the minimum spacing between legs (the
    rate-limit budget), and ``stale_after_s`` is when the staleness report
    calls the data stale.  A wire family (``open_meteo``, ``solcast``) may
    carry its own ``refresh_interval_s`` to slow or quicken ITS legs alone
    -- the 10-requests-per-UTC-day hobbyist Solcast budget against the
    keyless Open-Meteo tier is the canonical split -- and the staleness
    floor then binds to every DECLARED family's effective refresh: data
    may not turn stale before that family's first allowed reread.

    Providers are ADVISORY-ONLY: nothing in the control path reads them, and
    no provider failure may affect anything but its own data's availability.
    """

    enabled: StrictBool = False
    request_timeout_s: PositiveFiniteFloat = 10.0
    refresh_interval_s: PositiveFiniteFloat = 900.0
    stale_after_s: PositiveFiniteFloat = 3600.0
    open_meteo: OpenMeteoProviderConfig | None = None
    solcast: SolcastProviderConfig | None = None
    load_baseline: LoadBaselineConfig | None = None
    tariff: StaticTariffConfig | None = None

    @model_validator(mode="after")
    def validate_provider_relations(self) -> Self:
        # The staleness floor binds to every declared wire family's EFFECTIVE
        # refresh: a family whose data turns stale before its first allowed
        # reread would report stale forever, so the slowest declared gate
        # (the shared default, or a family override above it) sets the floor.
        budgets: list[tuple[str, float]] = [("refresh_interval_s", self.refresh_interval_s)]
        if self.open_meteo is not None and self.open_meteo.refresh_interval_s is not None:
            budgets.append(
                ("open_meteo.refresh_interval_s", self.open_meteo.refresh_interval_s)
            )
        if self.solcast is not None and self.solcast.refresh_interval_s is not None:
            budgets.append(("solcast.refresh_interval_s", self.solcast.refresh_interval_s))
        for budget_name, refresh_s in budgets:
            if self.stale_after_s < refresh_s:
                raise ValueError(
                    f"forecast_providers.stale_after_s must be at least {budget_name} "
                    f"({refresh_s} s: data turns stale no earlier than that family's "
                    "first allowed reread)"
                )
        if (
            self.open_meteo is not None
            and self.open_meteo.pv is not None
            and (self.solcast is not None)
        ):
            raise ValueError(
                "forecast_providers: open_meteo.pv and solcast are two PV truths for "
                "one site -- declare exactly one PV source"
            )
        if self.enabled and not any(
            (self.open_meteo, self.solcast, self.load_baseline, self.tariff)
        ):
            raise ValueError(
                "forecast_providers.enabled requires at least one provider family: "
                "an enabled block that composes nothing is the invisible-off class "
                "this project refuses"
            )
        return self


class ParkingConfig(_FrozenModel):
    """DESIGN_POD_PARKING section 5.1: the ``parking:`` commissioning block.

    Block-presence doctrine, the night pattern: a PRESENT block commissions the
    sanctioned standby write -- the park/resume surface, the lease ledger, and
    the transport's named ``write_debug_mode`` are composed from it and from
    nothing else; an ABSENT block composes nothing (byte-identical to the
    pre-parking controller, and both routes answer ``park_not_commissioned``).
    There is deliberately NO ``enabled`` key: commissioning the block IS the
    operator's standing decision, and a second master switch would be a second
    way to be silently off.  The lease arithmetic is validated here because
    both bounds live in this block; the mode/policy presence gates live on
    ``ControllerConfig`` (the night pattern), where the mode and policy the
    write depends on are already validated.
    """

    # The anti-rollover cap (ISA-TR84): renewal may never extend a lease past
    # ``parked_at + max_lease_s``, and the cap itself sits inside the
    # commissioned 360 s..86400 s window (six minutes to one day).
    max_lease_s: Annotated[StrictInt, Field(ge=360, le=86400)] = 14400
    # The default lease a PARK without an explicit ``lease_s`` carries: at
    # least a minute (a lease shorter than an operator's glance is theater)
    # and never past the cap.
    default_lease_s: Annotated[StrictInt, Field(ge=60, le=86400)] = 14400

    @model_validator(mode="after")
    def validate_lease_bounds(self) -> Self:
        if self.default_lease_s > self.max_lease_s:
            raise ValueError(
                "parking.default_lease_s must not exceed parking.max_lease_s: the default "
                "lease a PARK carries can never reach past the anti-rollover cap"
            )
        return self


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
    # DESIGN_SCHEDULES §3/B5: declared last beside ``excess_charging`` so its
    # commissioning validator sees the already-validated timing.
    schedule: ScheduleConfig | None = None
    # DESIGN_ENERGY_SCORECARD §7 (E4): the daily energy scorecard block,
    # declared last beside its siblings so its commissioning validator sees
    # the already-validated timing.
    energy_scorecard: EnergyScorecardConfig | None = None
    # DESIGN_NIGHT_CHARGE §3.1 (B1): declared LAST of all so its commissioning
    # validator sees the already-validated policy, timing, units, and the
    # schedule block the PARTITION grant is judged against.
    night_charging: NightChargingConfig | None = None
    # DESIGN_PLANT_HISTORY §2.5: the telemetry historian block, declared last
    # beside its siblings so its commissioning validator sees the
    # already-validated timing and storage blocks.
    plant_history: PlantHistoryConfig | None = None
    # The advisory forecast-provider stack (ARCHITECTURE section 17),
    # declared last beside its siblings so its cross-block validator sees
    # the already-validated plant_history block the load baseline reads.
    forecast_providers: ForecastProvidersConfig | None = None
    # DESIGN_POD_PARKING section 5.1: the parking commissioning block,
    # declared last beside its siblings so its commissioning validator sees
    # the already-validated mode and policy the sanctioned write depends on.
    parking: ParkingConfig | None = None

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

    @field_validator("schedule")
    @classmethod
    def validate_schedule(
        cls, schedule: ScheduleConfig | None, info: ValidationInfo
    ) -> ScheduleConfig | None:
        """The commissioning gates for a PRESENT schedule block
        (DESIGN_SCHEDULES §3 — the same TTL bounds as the adviser's)."""
        if schedule is None:
            return schedule
        timing = info.data.get("timing")
        if timing is not None and schedule.intent_ttl_s <= timing.control_period_s:
            raise ValueError(
                "schedule.intent_ttl_s must exceed timing.control_period_s: "
                "the runner renews exactly once per fleet cycle"
            )
        if not 0 < schedule.intent_ttl_s <= 300:
            raise ValueError(
                "schedule.intent_ttl_s must stay inside (0, 300] seconds — the "
                "REST dispatch cap; a schedule window may not out-live ordinary "
                "intents"
            )
        return schedule

    @field_validator("energy_scorecard")
    @classmethod
    def validate_energy_scorecard(
        cls, scorecard: EnergyScorecardConfig | None, info: ValidationInfo
    ) -> EnergyScorecardConfig | None:
        """The commissioning gates for a PRESENT scorecard block.

        The numeric relations that need the timing block live here: the
        integration gap must sit strictly above the control period (a gap
        bound the polling loop cannot satisfy would starve coverage forever)
        and at or below 60 s (a gap larger than a minute is an outage, not a
        sampling cadence).
        """
        if scorecard is None:
            return scorecard
        timing = info.data.get("timing")
        if timing is not None:
            if scorecard.integration_max_gap_s <= timing.control_period_s:
                raise ValueError(
                    "energy_scorecard.integration_max_gap_s must exceed "
                    "timing.control_period_s: the CT stream samples once per control "
                    "cycle, so a smaller gap would exclude every interval"
                )
            if scorecard.integration_max_gap_s > 60:
                raise ValueError(
                    "energy_scorecard.integration_max_gap_s must stay at or below 60 "
                    "seconds: a wider spacing is an outage, not a sampling cadence"
                )
        return scorecard

    @field_validator("night_charging")
    @classmethod
    def validate_night_charging(
        cls, night: NightChargingConfig | None, info: ValidationInfo
    ) -> NightChargingConfig | None:
        """DESIGN_NIGHT_CHARGE §3.1/§3.2: the commissioning gates for a
        PRESENT night block (every gate binds to block-PRESENCE — a disabled
        block that could never be enabled safely is refused, because the
        runtime toggle can raise participation but never a cap, window,
        pacing choice, or grant).

        The PARTITION grant is the one deliberate-revision rule: the union of
        a PRESENT ``schedule.allowed_windows_local`` must cover
        ``night_charging.window_local`` ENTIRELY, so commissioning night
        charge and granting the night partition are one config revision plus
        one restart — the refusal names the widening path.
        """
        if night is None:
            return night
        values = info.data
        if values.get("mode") is not ControllerMode.WRITE_ENABLED:
            raise ValueError(
                "night_charging requires mode write_enabled: an observe-only "
                "composition can never actuate, so the night strategy is refused "
                "at validation time whether enabled or explicitly disabled"
            )
        policy = values.get("policy")
        if policy is None:
            raise ValueError(
                "night_charging requires a policy block: the charge plan derives "
                "from the commissioned SOC ceiling and static charge limits"
            )
        if night.rate_cap_w > policy.max_unit_charge_w:
            raise ValueError(
                "night_charging.rate_cap_w must not exceed the policy "
                "max_unit_charge_w: the night ask sits exactly at the per-unit "
                "static cap and can never exceed it"
            )
        if not 0 < night.hold_rate_w < night.rate_cap_w:
            raise ValueError(
                "night_charging.hold_rate_w must be a positive charge strictly "
                "below rate_cap_w: it is the EVIDENCE-FAILURE fallback rate alone "
                "(a missing/bad/stale demand word holds at it — the stand-down "
                "answers measured demand only), so a zero hold hands the pod back "
                "to matching autonomy on exactly the evidence it cannot see — "
                "and a hold at the cap is no hold at all"
            )
        if not 0 < night.demand_exit_hysteresis_w < night.demand_threshold_w:
            raise ValueError(
                "night_charging.demand_exit_hysteresis_w must be strictly positive "
                "and strictly below demand_threshold_w: the hysteresis band is "
                "what keeps a load oscillating around the threshold from toggling "
                "the rate every tick"
            )
        timing = values.get("timing")
        if timing is not None:
            poll_bound = timing.control_period_s + timing.essential_read_timeout_s
            if night.demand_telemetry_max_age_s <= poll_bound:
                raise ValueError(
                    "night_charging.demand_telemetry_max_age_s must exceed "
                    "timing.control_period_s + timing.essential_read_timeout_s: a "
                    "fresher demand word than the polling loop can ever serve "
                    "would hold the fleet forever on stale evidence"
                )
            if night.intent_ttl_s <= timing.control_period_s:
                raise ValueError(
                    "night_charging.intent_ttl_s must exceed timing.control_period_s: "
                    "the adviser renews exactly once per fleet cycle"
                )
        if not 0 < night.intent_ttl_s <= 300:
            raise ValueError(
                "night_charging.intent_ttl_s must stay inside (0, 300] seconds — the "
                "REST dispatch cap; the adviser may not out-live ordinary intents"
            )
        fleet_units = {unit.unit_id for unit in values.get("units", ()) or ()}
        if night.pacing == "even":
            if night.assumed_capacity_wh is None:
                raise ValueError(
                    "night_charging.assumed_capacity_wh is required when pacing is "
                    "'even': the deadline rule paces from the per-unit capacity "
                    "estimate (the estimate only shapes pacing, never safety)"
                )
            if set(night.assumed_capacity_wh) != fleet_units:
                raise ValueError(
                    "night_charging.assumed_capacity_wh keys must be exactly the "
                    "fleet units: a unit without an estimate has no deadline to "
                    "pace and a stray key names no battery"
                )
        elif night.assumed_capacity_wh is not None:
            raise ValueError(
                "night_charging.assumed_capacity_wh must be present only when "
                "pacing is 'even': cap_first paces from the cap alone and a stray "
                "map misstates which pacing rule runs"
            )
        schedule = values.get("schedule")
        if schedule is None:
            raise ValueError(
                "night_charging requires the PARTITION grant: a PRESENT "
                "schedule block whose allowed_windows_local union covers the "
                "night window entirely — widen schedule.allowed_windows_local "
                "(one deliberate config revision, both blocks, then restart): "
                "the night window belongs to the site's other writer "
                "applications until the partition is granted"
            )
        from energypod.application.scheduling import parse_hhmm, window_inside_union

        allowed = tuple(
            (parse_hhmm(start), parse_hhmm(end)) for start, end in schedule.allowed_windows_local
        )
        uncovered = [
            f"{start}-{end}"
            for start, end in night.window_local
            if not window_inside_union(parse_hhmm(start), parse_hhmm(end), allowed)
        ]
        if uncovered:
            raise ValueError(
                "night_charging.window_local is not covered by the union of "
                "schedule.allowed_windows_local (" + ", ".join(uncovered) + ") — "
                "widen schedule.allowed_windows_local to cover the night window "
                "entirely (one deliberate config revision, both blocks, then "
                "restart): the night window belongs to the site's other writer "
                "applications until the partition is granted"
            )
        return night

    @field_validator("plant_history")
    @classmethod
    def validate_plant_history(
        cls, plant_history: PlantHistoryConfig | None, info: ValidationInfo
    ) -> PlantHistoryConfig | None:
        """The commissioning gates for a PRESENT plant-history block.

        Durable history is the entire point: the block REQUIRES the
        ``storage`` block, and the sampling cadence must sit strictly above
        the control period (a historian tick per fleet cycle samples at most
        once per cycle, so a cadence the loop cannot out-run would record
        nothing but gaps).
        """
        if plant_history is None:
            return plant_history
        values = info.data
        if values.get("storage") is None:
            raise ValueError(
                "plant_history requires the storage block: durable history is the "
                "entire point, and a historian silently keeping its rows in the "
                "memory of a database-less deployment is exactly the invisible-off "
                "class this project refuses (energypod simulate composes the "
                "in-memory adapter explicitly, for scenario tests)"
            )
        timing = values.get("timing")
        if timing is not None and plant_history.sample_interval_s <= timing.control_period_s:
            raise ValueError(
                "plant_history.sample_interval_s must exceed timing.control_period_s: "
                "the historian ticks once per fleet cycle and samples at most once "
                "per cycle, so a cadence at or below the control period could never "
                "govern anything"
            )
        return plant_history

    @field_validator("forecast_providers")
    @classmethod
    def validate_forecast_providers(
        cls, providers: ForecastProvidersConfig | None, info: ValidationInfo
    ) -> ForecastProvidersConfig | None:
        """The forecast stack's one cross-block gate.

        The load baseline READS the telemetry historian, so declaring it
        without the ``plant_history`` block would compose a provider over a
        historian that never samples -- a baseline silently empty forever.
        The gate binds to block-PRESENCE (not ``enabled``), so the refusal
        surfaces at validation time however the stack is staged.
        """
        if providers is None or providers.load_baseline is None:
            return providers
        if info.data.get("plant_history") is None:
            raise ValueError(
                "forecast_providers.load_baseline requires the plant_history block: "
                "the baseline is the same slot last week read from the historian, and "
                "without the historian it would be silently empty forever"
            )
        return providers

    @field_validator("parking")
    @classmethod
    def validate_parking(
        cls, parking: ParkingConfig | None, info: ValidationInfo
    ) -> ParkingConfig | None:
        """DESIGN_POD_PARKING section 5.1: the commissioning gates for a
        PRESENT parking block (the night pattern, verbatim in shape).

        The block is the one and only policy flag that can ever compose the
        0x8000 debug-mode write, so it is validated on block-PRESENCE: a
        present block on a site that cannot actuate -- observe-only mode, or a
        write-enabled mode without its policy block -- is a validation error
        naming its own cause, never a silently-incapable site.  An ABSENT
        block changes nothing anywhere.
        """
        if parking is None:
            return parking
        values = info.data
        if values.get("mode") is not ControllerMode.WRITE_ENABLED:
            raise ValueError(
                "parking requires mode write_enabled: an observe-only composition can "
                "never actuate, so a present parking block is refused at validation "
                "time -- the site is not silently incapable, it is told"
            )
        if values.get("policy") is None:
            raise ValueError(
                "parking requires a policy block: the park surface composes against the "
                "commissioned control policy (armed/latched refusal checks and audit)"
            )
        return parking

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
