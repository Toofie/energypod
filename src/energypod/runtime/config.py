"""Strict, immutable startup configuration.

Configuration is an authority boundary: values are never silently coerced and
unknown keys are rejected at every nesting level.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path
from typing import Annotated, Final, Literal, Self
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

# DESIGN_BATTERY_HEALTH_WATCH §4 (A1): the worst-case stage bounds the
# program-fit arithmetic budgets.  Census is one bounded pass over historian
# rows; a probe leg (settle + sustain + echo + cancel + return + gap) sits
# inside ~2 min per unit; a recovery cycle inside ~5 min per unit (three
# units worst case ~21 min against a 45-minute budget — the contract's own
# arithmetic, restated as the constants the validation computes with).
_CENSUS_WORST_CASE_S: Final[int] = 60
_PROBE_WORST_CASE_S_PER_UNIT: Final[int] = 120
_RECOVERY_WORST_CASE_S_PER_UNIT: Final[int] = 300
# The worst-case SINGLE in-flight act (a recovery cycle, ~5 min): deadline
# plus this bound must land before the night window opens (A1 check b).
_WORST_IN_FLIGHT_ACT_S: Final[int] = _RECOVERY_WORST_CASE_S_PER_UNIT


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
    # DESIGN_BATTERY_HEALTH_WATCH §3.1 (Wave 0, W0-1): the self-charge float
    # deadband.  The old exact-zero test on the autonomous_self_charge
    # classification flapped the health state 158 transitions in one night
    # at 96-99% SoC, where the pods float ACROSS zero (observed rhs straddle
    # -16/0/+33 W); |measured| below the deadband renders neither
    # self-charging nor flap -- floating at the top is the steady state of a
    # full pack.  25 sits above the metering noise floor (tens of watts) and
    # an order below the genuine CT-following self-charge class
    # (-520..-560 W); the ceiling is 100 because beyond it the deadband
    # would begin to eat the legitimate float class.  Detection only.
    self_charge_deadband_w: Annotated[StrictFloat, Field(gt=0.0, le=100.0)] = 25.0
    # §3.2 (Wave 0, W0-2a): the authorization-gap grace the coherence
    # baseline survives.  An authorization gap shorter than this (intent
    # renewal lapse, telemetry_stale dip), with delivery continuing at the
    # commanded level, is the SAME coherence episode: the pre-command
    # baseline does not move and is never re-anchored onto the watts the pod
    # was already delivering -- that re-anchoring is exactly the mechanism
    # behind both live 2026-08-24 actuation_incoherent false positives
    # (steady 87-96%-of-command delivery read as "no movement").  12 s is
    # the night-writer detector's handback-grace precedent (bounds mirrored),
    # covering the observed ~4-8 s watchdog hand-back with margin.  Detection
    # only: no control path consumes this key.
    coherence_gap_grace_s: Annotated[StrictFloat, Field(ge=1.0, le=300.0)] = 12.0
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


def _covers_minute(start: int, end: int, minute: int) -> bool:
    """Wrap-aware civil-minute containment, the tariff window's own rule."""
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _span_rates(
    default_rate: float,
    windows: Sequence[tuple[int, int, float]],
    spans: Sequence[tuple[int, int]],
) -> list[tuple[int, float]]:
    """Resolve the rate in force at every civil minute of the spans.

    The static-tariff provider's own precedence (``_rates_at``): the FIRST
    declared window covering a minute wins, else the flat default.  Pure
    minute-of-day arithmetic, so config validation stays offline-pure while
    judging exactly the rates the composed provider would serve.
    """
    resolved: list[tuple[int, float]] = []
    for span_start, span_end in spans:
        minute = span_start
        while True:
            rate = float(default_rate)
            for window_start, window_end, window_rate in windows:
                if _covers_minute(window_start, window_end, minute):
                    rate = float(window_rate)
                    break
            resolved.append((minute, rate))
            minute = (minute + 1) % 1440
            if minute == span_end:
                break
    return resolved


class NightTrustBlockConfig(_FrozenModel):
    """DESIGN_NIGHT_CHARGE_V2 section 6: the trust gate's keys.

    The bounds are the panel-sharpened compound gate (A4): the bias is
    ASYMMETRIC with over-forecast the dangerous, tighter direction, so the
    over bound must sit strictly below the under bound — a symmetric pair is
    a different gate than the one this contract ships.
    """

    required_days: PositiveStrictInt = 14
    tolerance_pct: PositiveFiniteFloat = 30.0
    max_overforecast_bias_pct: PositiveFiniteFloat = 10.0
    max_underforecast_bias_pct: PositiveFiniteFloat = 20.0
    min_regime_days: PositiveStrictInt = 3

    @model_validator(mode="after")
    def validate_bias_asymmetry(self) -> Self:
        if self.max_overforecast_bias_pct >= self.max_underforecast_bias_pct:
            raise ValueError(
                "night_charging.trust.max_overforecast_bias_pct must be strictly below "
                "max_underforecast_bias_pct: over-forecast is the dangerous direction "
                "that under-charges, so its tolerance is the tighter half (A4)"
            )
        return self


class NightChargingConfig(_FrozenModel):
    """DESIGN_NIGHT_CHARGE §3.1 + DESIGN_NIGHT_CHARGE_V2 §6.

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
    bounded renewable TTL, the capacity map exactly when the pacing rule or
    the forecast target needs it, the forecast prerequisites of a forecast
    posture, and the PARTITION grant against the schedule policy) are
    validated on ``ControllerConfig``, where the blocks they relate to live.

    V2's keys all carry their §6 defaults so ``target_policy: full`` (the
    absent-key default) is exact v1 identity: no forecast stack is required,
    no capacity map under ``cap_first``, byte-identical behavior.
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
    # REQUIRED iff ``pacing: even`` OR ``target_policy != full`` (key set
    # exactly the fleet units, validated on ControllerConfig); a stray map
    # under cap_first-full is refused — the estimate only shapes pacing and
    # the target, never safety.
    assumed_capacity_wh: dict[NonEmpty, PositiveStrictInt] | None = None
    demand_telemetry_max_age_s: PositiveFiniteFloat = 3.0
    intent_ttl_s: PositiveFiniteFloat = 10.0
    # --- V2 (DESIGN_NIGHT_CHARGE_V2 section 6) ----------------------------------
    # The three-state posture: full = v1 identity (the default an absent key
    # means); forecast_suggest computes and displays but charges v1;
    # forecast_act lets the computed target govern, entered only by config
    # revision + restart after the operator accepts the section 3.2 evidence.
    target_policy: Literal["full", "forecast_suggest", "forecast_act"] = "full"
    forecast_quantile: Annotated[StrictFloat, Field(ge=0, le=1, allow_inf_nan=False)] = 0.1
    midday_local: NonEmpty = "12:00"
    floor_pct: Annotated[StrictFloat, Field(gt=0, allow_inf_nan=False)] = 50.0
    charge_efficiency: Annotated[StrictFloat, Field(gt=0, le=1, allow_inf_nan=False)] = 0.9
    retarget_threshold_pct: Annotated[StrictFloat, Field(gt=0, le=100, allow_inf_nan=False)] = 20.0
    retarget_min_gap_min: Annotated[StrictInt, Field(ge=15)] = 60
    trust: NightTrustBlockConfig = NightTrustBlockConfig()

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

    @field_validator("midday_local")
    @classmethod
    def validate_midday(cls, value: str) -> str:
        return _valid_policy_wall(value)

    @model_validator(mode="after")
    def validate_midday_after_window_ends(self) -> Self:
        # The operator's declared finish line is a CIVIL fact: strictly after
        # every configured window's end wall on the same civil morning (a
        # window ending at or past midday leaves the morning no span).
        midday = _valid_policy_wall(self.midday_local)
        midday_minute = int(midday[:2]) * 60 + int(midday[3:5])
        for _start, end in self.window_local:
            end_minute = int(end[:2]) * 60 + int(end[3:5])
            if midday_minute <= end_minute:
                raise ValueError(
                    "night_charging.midday_local must sit strictly after every "
                    f"window_local end wall ({midday} is not after {end}): the "
                    "morning surplus span [window_end, midday) would be empty or "
                    "reversed"
                )
        return self


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


class HealthWatchStuckConfig(_FrozenModel):
    """DESIGN_BATTERY_HEALTH_WATCH §5/§9: the census's stuck-signature knobs.

    Every threshold is its own key so a wrong one is discoverable in the
    census row's predicate vector, never hidden inside a composite.
    """

    evidence_window_h: Annotated[StrictInt, Field(ge=1, le=48)] = 6
    soc_floor_pct: Annotated[StrictFloat, Field(ge=0, le=100)] = 95.0
    soc_hold_frac: Annotated[StrictFloat, Field(gt=0, le=1)] = 0.9
    min_soc_hours: Annotated[StrictInt, Field(ge=1, le=48)] = 6
    still_w: PositiveStrictInt = 50
    still_frac: Annotated[StrictFloat, Field(gt=0, le=1)] = 0.9
    grid_import_w: PositiveStrictInt = 500
    sibling_flow_w: PositiveStrictInt = 500
    flow_frac: Annotated[StrictFloat, Field(gt=0, le=1)] = 0.5
    load_floor_w: PositiveStrictInt = 30
    sibling_load_w: PositiveStrictInt = 100
    load_frac: Annotated[StrictFloat, Field(gt=0, le=1)] = 0.8
    # §5/A9: notice -> alert promotion after this many consecutive stuck
    # nights (one noisy night is evidence, two is a pattern; >= 1).
    flag_persistence_nights: Annotated[StrictInt, Field(ge=1, le=90)] = 2

    @model_validator(mode="after")
    def validate_soc_horizon(self) -> Self:
        # A9: the full-at-top horizon the S1 continuous-hours test needs must
        # fit inside the evidence window — equality (6 = 6, the defaults) is
        # INTENDED and allowed; strict < would force a wider window for no
        # semantic gain.
        if self.min_soc_hours > self.evidence_window_h:
            raise ValueError(
                "battery_health_watch.stuck.min_soc_hours must not exceed "
                f"evidence_window_h ({self.min_soc_hours} > {self.evidence_window_h}): "
                "the S1 continuous-hours test cannot demand a longer horizon than "
                "the historian lookback it is judged over — equality (the 6 = 6 "
                "defaults) is intended and allowed"
            )
        return self


class HealthWatchProbeConfig(_FrozenModel):
    """DESIGN_BATTERY_HEALTH_WATCH §6/§9: the actuation probe's knobs.

    The sustained-delivery judgment these keys shape is DEFINED by the
    contract (no public standard exists): the delivery band is
    ``pass_fraction x probe_w`` on >= ``pass_sample_frac`` of core samples,
    and the fractions share the coherence watchdog's own movement band.
    """

    probe_w: Annotated[StrictInt, Field(ge=100, le=500)] = 300
    settle_s: Annotated[StrictInt, Field(ge=10, le=60)] = 20
    sustain_s: Annotated[StrictInt, Field(ge=20, le=60)] = 30
    pass_fraction: Annotated[StrictFloat, Field(gt=0, le=1)] = 0.5
    pass_sample_frac: Annotated[StrictFloat, Field(gt=0, le=1)] = 0.8
    return_band_w: PositiveStrictInt = 150
    baseline_return_s: Annotated[StrictInt, Field(ge=10, le=120)] = 30
    # A8: the quiet-load gate reads the fleet-mean grid-IMPORT magnitude (the
    # control-grade PCS word), at probe start AND re-checked at cancel.
    quiet_load_w: PositiveStrictInt = 1000
    # A8: a mid-leg demand move beyond this bound confounds the baseline
    # judgment -> inconclusive_baseline_confounded.
    load_move_w: PositiveStrictInt = 300
    inter_unit_gap_s: NonNegativeStrictInt = 30

    @model_validator(mode="after")
    def validate_leg_bound(self) -> Self:
        # §9: the whole measured leg (settle + sustain) stays at or below 90 s
        # — the bounded actuation the §4 program-fit arithmetic budgets ~2 min
        # per unit for.
        if self.settle_s + self.sustain_s > 90:
            raise ValueError(
                "battery_health_watch.probe.settle_s + sustain_s must stay at or "
                f"below 90 seconds ({self.settle_s} + {self.sustain_s}): the probe "
                "leg is a bounded actuation, and the program-fit validation "
                "budgets it inside the window by this bound"
            )
        return self


class HealthWatchRecoveryConfig(_FrozenModel):
    """DESIGN_BATTERY_HEALTH_WATCH §7/§9: Stage R's keys — RECOGNIZED here,
    implemented in a later wave.

    ``mode: advise`` is the standing posture (writes nothing, ever).  ``auto``
    requires the A6 receipt map; validation enforces the supervised-
    verification sequencing offline-pure, and boot degrades a missing
    receipt's unit to advise loudly (never a boot failure).
    """

    mode: Literal["advise", "auto"] = "advise"
    hold_s: Annotated[StrictInt, Field(ge=60, le=120)] = 90
    consecutive_fail_limit: Annotated[StrictInt, Field(ge=1, le=90)] = 3
    # A6: mode auto requires a supervised-verification receipt (a
    # docs/evidence/ path, the §16 step-5 file) for EVERY fleet unit, or the
    # literal "excluded" for a unit that stays advise.  Shape validated here;
    # existence checked at boot.
    auto_receipts: dict[NonEmpty, NonEmpty] | None = None

    @field_validator("auto_receipts")
    @classmethod
    def validate_receipt_values(
        cls, values: dict[str, str] | None
    ) -> dict[str, str] | None:
        if values is None:
            return values
        cleaned: dict[str, str] = {}
        for unit_raw, path_raw in values.items():
            unit = _plain(unit_raw, label="auto_receipts key")
            path = _plain(path_raw, label=f"auto_receipts[{unit!r}]")
            if path != "excluded" and not path.startswith("docs/evidence/"):
                raise ValueError(
                    f"battery_health_watch.recovery.auto_receipts[{unit!r}] must be "
                    "the literal 'excluded' or a docs/evidence/ path (the §16 "
                    "step-5 supervised-verification receipt); got "
                    f"{path!r} — the config file itself enforces the "
                    "supervised-verification sequencing (A6)"
                )
            cleaned[unit] = path
        return cleaned


class BatteryHealthWatchConfig(_FrozenModel):
    """DESIGN_BATTERY_HEALTH_WATCH §4/§9: the ``battery_health_watch:`` block.

    Block-presence doctrine, the night pattern: a PRESENT block composes the
    ``HealthWatchController`` (one tick per fleet cycle inside the existing
    bounded supervision pass), the ``health_watch_state`` snapshot
    projection, the health events, and the status route; an ABSENT block
    composes NOTHING (byte-identical snapshot, 409 on the route).  There is
    deliberately NO ``enabled`` key and NO runtime toggle for any stage or
    for ``recovery.mode`` — the authority ladder is climbed by config
    revision plus restart only, so the console can never flip authority into
    existence.
    """

    # REQUIRED — the window is a civil-time fact (one truth per site,
    # cross-checked against ``site.timezone`` on ``ControllerConfig``, A9).
    timezone: NonEmpty
    window_local: NonEmpty = "23:00"
    deadline_local: NonEmpty = "23:45"
    # A strict prefix of [census, probe, recovery]: no probe without a
    # census, no recovery without both (§2's progressive-earning posture).
    stages: tuple[Literal["census", "probe", "recovery"], ...] = ("census",)
    stuck: HealthWatchStuckConfig = HealthWatchStuckConfig()
    probe: HealthWatchProbeConfig = HealthWatchProbeConfig()
    recovery: HealthWatchRecoveryConfig = HealthWatchRecoveryConfig()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = _plain(value, label="timezone")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("window_local", "deadline_local")
    @classmethod
    def validate_walls(cls, value: str, info: object) -> str:
        name = getattr(info, "field_name", "wall")
        return _valid_policy_wall_named(value, label=name)

    @model_validator(mode="after")
    def validate_window_shape(self) -> Self:
        window = _wall_minute(self.window_local)
        deadline = _wall_minute(self.deadline_local)
        if window >= deadline:
            raise ValueError(
                "battery_health_watch.window_local must sit strictly before "
                f"deadline_local ({self.window_local} >= {self.deadline_local}): the "
                "deadline is the no-new-ACT boundary and a program with no span "
                "can never fit inside one (A1)"
            )
        if self.probe.return_band_w <= self.stuck.still_w:
            raise ValueError(
                "battery_health_watch.probe.return_band_w must exceed "
                f"stuck.still_w ({self.probe.return_band_w} <= {self.stuck.still_w}): "
                "a return band inside the still band could never distinguish "
                "'returned to idle' from 'never moved'"
            )
        stage_order: tuple[str, ...] = ("census", "probe", "recovery")
        if not self.stages:
            raise ValueError(
                "battery_health_watch.stages must name at least one stage: an "
                "empty prefix composes nothing, and a present block that composes "
                "nothing is the invisible-off class this project refuses"
            )
        for position, stage in enumerate(self.stages):
            expected = stage_order[position] if position < len(stage_order) else None
            if expected is None or stage != expected:
                raise ValueError(
                    "battery_health_watch.stages must be a strict prefix of "
                    f"[census, probe, recovery] (got {list(self.stages)}): no site "
                    "ever runs a probe without a census or a recovery without both"
                )
        return self


def _valid_policy_wall_named(value: str, *, label: str) -> str:
    """One "HH:MM" wall through the canonical parser, attributed by name."""
    from energypod.application.scheduling import parse_hhmm

    try:
        return parse_hhmm(value).strftime("%H:%M")
    except Exception as exc:
        raise ValueError(f"{label} must be an HH:MM wall clock ({value!r})") from exc


def _wall_minute(value: str) -> int:
    """The minute-of-day of one canonical "HH:MM" wall."""
    return int(value[:2]) * 60 + int(value[3:5])


# DESIGN_CALIBRATION_CYCLING §1.1: Victron documents a per-module ~1 A
# (~50 W) sensing threshold — sub-threshold currents report as 0 W — and the
# traverse must clear 3 modules' worth with margin (the named floor).
_CALIBRATION_SENSING_FLOOR_W: Final[int] = 3 * 50


class CalibrationRequestMeasurementConfig(_FrozenModel):
    """C6's guarded one-shot: a config-borne, audited operator request that
    the named unit's NEXT ``plan_local`` select it as target, waiving the
    ``due`` requirement ONLY (every class gate still applies).

    Consumed at the plan tick it names; the waiver rides the trigger row.
    Absent key = no request stands.  This is SELECTION authority only: it
    never flips mode, never bypasses a guard, never outlives one consumption.
    """

    unit: NonEmpty
    note: NonEmpty

    @field_validator("unit", "note")
    @classmethod
    def validate_text(cls, value: str, info: object) -> str:
        name = getattr(info, "field_name", "request_measurement")
        return _plain(value, label=name)


class CalibrationTriggerBlockConfig(_FrozenModel):
    """DESIGN_CALIBRATION_CYCLING §7: the ``trigger:`` keys (§3's X/N)."""

    trigger_floor_pct: Annotated[StrictFloat, Field(ge=0, le=100)] = 30.0
    trigger_after_days: Annotated[StrictInt, Field(ge=1, le=3650)] = 60
    eligibility_window_days: Annotated[StrictInt, Field(ge=1, le=90)] = 14
    cycles_daily_floor_pct: Annotated[StrictFloat, Field(ge=0, le=100)] = 25.0
    cycles_daily_min_days: Annotated[StrictInt, Field(ge=1, le=90)] = 5
    min_daily_throughput_wh: Annotated[StrictFloat, Field(gt=0)] = 1000.0
    throughput_min_days: Annotated[StrictInt, Field(ge=1, le=90)] = 3
    probe_pass_window_days: Annotated[StrictInt, Field(ge=1, le=90)] = 7


class CalibrationTraverseBlockConfig(_FrozenModel):
    """DESIGN_CALIBRATION_CYCLING §7: the ``traverse:`` keys (§4's rate and
    the C8/C9 bounds)."""

    floor_pct: Annotated[StrictFloat, Field(ge=5, le=10)] = 10.0
    discharge_w: PositiveStrictInt = 800
    min_discharge_w: PositiveStrictInt = 200
    intent_ttl_s: Annotated[StrictFloat, Field(gt=0, le=300)] = 10.0
    assumed_delivery_frac: Annotated[StrictFloat, Field(gt=0, le=1)] = 0.8
    integration_max_gap_s: Annotated[StrictFloat, Field(gt=0, le=60)] = 10.0
    energy_margin_wh: Annotated[StrictFloat, Field(gt=0)] = 100.0
    metering_allowance_wh: Annotated[StrictFloat, Field(ge=0)] = 100.0
    assumed_capacity_wh: dict[NonEmpty, PositiveStrictInt] | None = None


class CalibrationTopAnchorBlockConfig(_FrozenModel):
    """DESIGN_CALIBRATION_CYCLING §7: the ``top_anchor:`` keys (§5)."""

    taper_deadline_local: NonEmpty = "12:00"
    taper_soc_pct: Annotated[StrictFloat, Field(ge=50, lt=100)] = 99.0
    taper_limit_w: NonNegativeStrictInt = 0
    taper_sustain_s: Annotated[StrictInt, Field(ge=60, le=3600)] = 600
    hold_min_s: Annotated[StrictInt, Field(ge=1800, le=3600)] = 2700
    hold_float_w: PositiveStrictInt = 100
    poor_surplus_kwh: PositiveFiniteFloat = 3.0

    @field_validator("taper_deadline_local")
    @classmethod
    def validate_deadline(cls, value: str) -> str:
        return _valid_policy_wall(value)


class CalibrationMeasurementBlockConfig(_FrozenModel):
    """DESIGN_CALIBRATION_CYCLING §7: the ``measurement:`` keys (§6)."""

    reanchor_delta_pct: Annotated[StrictFloat, Field(gt=0, le=20)] = 2.0
    floor_epsilon_pct: Annotated[StrictFloat, Field(gt=0, le=5)] = 2.0


class BatteryCalibrationConfig(_FrozenModel):
    """DESIGN_CALIBRATION_CYCLING §7: the ``battery_calibration:`` block.

    Block-presence doctrine, the night pattern: a PRESENT block composes the
    ``CalibrationAdviser`` (one tick per fleet cycle inside the existing
    bounded supervision pass), the ``calibration_state`` snapshot key, the
    ``calibration.cycle`` events, and the status route; an ABSENT block
    composes NOTHING (byte-identical snapshot, 409 on the route).  There is
    deliberately NO ``enabled`` key and NO runtime toggle for ``mode`` — the
    authority ladder is climbed by config revision plus restart only, so the
    console can never flip a traverse into existence.  ``mode: advise`` (the
    default) computes and displays the trigger/eligibility and submits
    NOTHING, ever; ``act`` is the operator's later revision (C15: no receipt
    gate — the act is ordinary OPTIMIZER dispatch, the night-charge
    precedent, no mode register, no arm).
    """

    # REQUIRED; one civil-time truth per site (A9), cross-checked against
    # ``site.timezone`` on ``ControllerConfig``.
    timezone: NonEmpty
    mode: Literal["advise", "act"] = "advise"
    window_local: NonEmpty = "15:00"
    traverse_end_local: NonEmpty = "22:30"
    plan_local: NonEmpty = "14:00"
    request_measurement: CalibrationRequestMeasurementConfig | None = None
    trigger: CalibrationTriggerBlockConfig = CalibrationTriggerBlockConfig()
    traverse: CalibrationTraverseBlockConfig = CalibrationTraverseBlockConfig()
    top_anchor: CalibrationTopAnchorBlockConfig = CalibrationTopAnchorBlockConfig()
    measurement: CalibrationMeasurementBlockConfig = CalibrationMeasurementBlockConfig()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = _plain(value, label="timezone")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("window_local", "traverse_end_local", "plan_local")
    @classmethod
    def validate_walls(cls, value: str, info: object) -> str:
        name = getattr(info, "field_name", "wall")
        return _valid_policy_wall_named(value, label=f"battery_calibration.{name}")

    @model_validator(mode="after")
    def validate_wall_order(self) -> Self:
        plan = _wall_minute(self.plan_local)
        window = _wall_minute(self.window_local)
        end = _wall_minute(self.traverse_end_local)
        if not plan < window:
            raise ValueError(
                "battery_calibration.plan_local must sit strictly before window_local "
                f"({self.plan_local} >= {self.window_local}): the plan resolves the "
                "morning's taper deadline before the window opens"
            )
        if not window < end:
            raise ValueError(
                "battery_calibration.window_local must sit strictly before "
                f"traverse_end_local ({self.window_local} >= {self.traverse_end_local}): "
                "a window with no span can never fit a traverse"
            )
        if end >= plan + 24 * 60:
            raise ValueError(
                "battery_calibration.traverse_end_local must land before plan_local + 24h "
                f"({self.traverse_end_local} does not): the traverse is one civil day's fact"
            )
        return self


def _wrap_minutes_ahead(from_minute: int, to_minute: int) -> int:
    """Civil minutes from one wall to a later-or-wrapping one, wrap-aware."""
    return (to_minute - from_minute) % 1440


def _wrap_overlaps(window: int, deadline: int, start: int, end: int) -> bool:
    """Whether the non-wrapping [window, deadline) overlaps one civil pair.

    The pair may wrap midnight (start > end); both sides are half-open, so a
    pair ending exactly at the window's start (or starting exactly at its
    deadline) does NOT overlap.
    """
    if start < end:
        return start < deadline and end > window
    # A wrapping pair covers [start, midnight) + [midnight, end).
    return start < deadline or end > window


#: The PVOutput extended-value slots the custom per-unit fields may own (the
#: addstatus specification's ``v7``..``v12`` — donation-tier "Extended Value"
#: parameters, number-typed, user-defined units).
_PVOUTPUT_SLOT_NAMES: Final[tuple[str, ...]] = tuple(f"v{number}" for number in range(7, 13))

#: One environment-variable NAME the credential references resolve through:
#: POSIX identifier shape (a letter or underscore, then letters, digits, or
#: underscores) so a reference can never smuggle whitespace or shell syntax
#: into ``os.environ`` lookups.
_ENV_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _valid_pvoutput_slot(value: str) -> str:
    """Validate one ``v7``..``v12`` slot spelling (the wire's own enum)."""
    cleaned = _plain(value, label="pvoutput slot")
    if cleaned not in _PVOUTPUT_SLOT_NAMES:
        raise ValueError(
            f"pvoutput slot must be one of {list(_PVOUTPUT_SLOT_NAMES)} (the addstatus "
            f"specification's extended-value parameters); got {cleaned!r}"
        )
    return cleaned


class PvOutputConfig(_FrozenModel):
    """The pvoutput.org reporting block (the retiring Docker writer's replacement).

    Block-presence doctrine, the night pattern: a PRESENT block composes the
    reporting surface — the uploader control in the fleet cycle, the health
    snapshot at ``GET /api/v1/pvoutput/status``, and the guarded runtime
    toggle — with ``enabled`` gating whether any POST is made; an ABSENT block
    composes NOTHING and the status route answers 409
    ``pvoutput_not_commissioned``.

    The reporter is OBSERVABILITY-ONLY: it reads observations through the same
    port the energy accountant reads and never touches a register, an intent,
    or the control path.  Secrets are REFERENCES (``api_key_env`` /
    ``system_id_env`` name environment variables the composition root resolves
    at boot); an unset reference composes the surface with a registry note,
    never a boot failure (the Solcast pattern).

    Field mapping (PINNED for dashboard continuity — the old container's exact
    layout, byd/config.py): each commissioned unit owns ONE extended-value pair
    ``unit_slots`` — ``[soc_slot, power_slot]`` — with SoC from the REAL BMS
    word ``bms_soc_pct`` (the old container posted a voltage-curve estimate;
    strictly better) and power from the signed ``battery_watts``.  The solar
    inverter's own integration owns v1-v6 on this site: this block never
    writes generation, consumption, temperature, or voltage.

    Sign continuity (PROTOCOL_EVIDENCE 4b): the battery power family (BMS
    0x5008 / DCDC 0x2009 / system 0x0114 — and the PCS grid P at 0x1007 the
    operator remembers as "reg 4103") is NEGATIVE = CHARGE, POSITIVE =
    DISCHARGE on live-proven firmware, which is exactly pod-manager's
    ``battery_watts`` convention — so v8/v10/v12 post the word UNNEGATED and
    the existing dashboard graphs stay continuous.  PVOutput's native battery
    fields are the opposite (``b1`` positive = charge per the addstatus
    specification), so the adapter flips the fleet aggregate at its boundary.
    """

    enabled: StrictBool = False
    api_key_env: NonEmpty = "PVOUTPUT_API_KEY"
    system_id_env: NonEmpty = "PVOUTPUT_SYSTEM_ID"
    # One POST per slot: the interval may never be finer than the pinned
    # 5-minute slot grid (PVOutput rounds ``t`` to the site's status interval;
    # posting finer would double-post slots) nor coarser than hourly (coarser
    # is a schedule, not a cadence — take the block out instead).
    interval_s: Annotated[StrictFloat, Field(ge=300.0, le=3600.0)] = 300.0
    max_sample_age_s: PositiveFiniteFloat = 120.0
    request_timeout_s: PositiveFiniteFloat = 10.0
    # Transient-failure retries per slot, spaced (never bursts); a slot that
    # still fails is a gap -- no backfill, ever.
    retry_max: Annotated[StrictInt, Field(ge=0, le=10)] = 1
    # The old container's exact per-unit layout (byd/config.py): lhs v7/v8,
    # rhs v9/v10, mid v11/v12 -- [SoC slot, power slot] per unit.
    unit_slots: dict[NonEmpty, tuple[NonEmpty, NonEmpty]] = {
        "lhs": ("v7", "v8"),
        "rhs": ("v9", "v10"),
        "mid": ("v11", "v12"),
    }
    # b1/b2 fleet aggregates ride the SAME POST (b1 = sum of per-pod
    # battery_watts with the spec's sign flip, b2 = mean SoC over identical
    # capacities); b1 is mandatory when any battery field is sent, so b2 is
    # omitted together with b1 whenever b1 cannot be computed fresh.
    native_battery_fields: StrictBool = True

    @field_validator("api_key_env", "system_id_env")
    @classmethod
    def validate_env_reference(cls, value: str, info: object) -> str:
        name = getattr(info, "field_name", "environment reference")
        value = _plain(value, label=name)
        if _ENV_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"{name} must be an environment variable NAME (a letter or underscore, "
                "then letters, digits, or underscores)"
            )
        return value

    @field_validator("unit_slots")
    @classmethod
    def validate_unit_slots(
        cls, values: dict[str, tuple[str, str]]
    ) -> dict[str, tuple[str, str]]:
        if not values:
            raise ValueError(
                "pvoutput.unit_slots must name at least one unit (each commissioned unit "
                "owns one [SoC slot, power slot] pair)"
            )
        cleaned: dict[str, tuple[str, str]] = {}
        for unit_raw, (soc_raw, power_raw) in values.items():
            unit = _plain(unit_raw, label="pvoutput.unit_slots key")
            soc = _valid_pvoutput_slot(soc_raw)
            power = _valid_pvoutput_slot(power_raw)
            if soc == power:
                raise ValueError(
                    f"pvoutput.unit_slots[{unit!r}] must pair two DISTINCT slots "
                    f"(got {soc!r} twice: SoC and power need one slot each)"
                )
            cleaned[unit] = (soc, power)
        slots = [slot for pair in cleaned.values() for slot in pair]
        if len(set(slots)) != len(slots):
            raise ValueError(
                "pvoutput.unit_slots slots must not collide: every v7..v12 slot is owned "
                "by exactly one field of one unit (a collision would make the dashboard "
                "slot mean two batteries at once)"
            )
        return cleaned


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
    # DESIGN_PLANT_HISTORY §2.5: the telemetry historian block, declared last
    # beside its siblings so its commissioning validator sees the
    # already-validated timing and storage blocks.
    plant_history: PlantHistoryConfig | None = None
    # The advisory forecast-provider stack (ARCHITECTURE section 17),
    # declared last beside its siblings so its cross-block validator sees
    # the already-validated plant_history block the load baseline reads.
    forecast_providers: ForecastProvidersConfig | None = None
    # DESIGN_NIGHT_CHARGE §3.1 (B1) + DESIGN_NIGHT_CHARGE_V2 §6: declared
    # AFTER forecast_providers (V2) so its commissioning validator sees the
    # advisory stack a forecast posture requires, beside the already-
    # validated policy, timing, units, and the schedule block the PARTITION
    # grant is judged against.
    night_charging: NightChargingConfig | None = None
    # DESIGN_POD_PARKING section 5.1: the parking commissioning block,
    # declared last beside its siblings so its commissioning validator sees
    # the already-validated mode and policy the sanctioned write depends on.
    parking: ParkingConfig | None = None
    # The pvoutput.org reporting block, declared last beside its siblings so
    # its commissioning validator sees the already-validated units, timing,
    # and storage blocks the reporter's slot layout, freshness bound, and
    # durable runtime toggle depend on.
    pvoutput: PvOutputConfig | None = None
    # DESIGN_BATTERY_HEALTH_WATCH §9: the nightly health-watch block, declared
    # LAST so its commissioning validator sees every block the stages compose
    # against — the site's timezone, the units the program-fit arithmetic and
    # the A6 receipt map count, the mode/policy the probe's dispatch needs,
    # the plant_history block the census's evidence window reads, the parking
    # block Stage R composes, and the night/schedule windows the program's
    # quiet hour must be disjoint from.
    battery_health_watch: BatteryHealthWatchConfig | None = None
    # DESIGN_CALIBRATION_CYCLING §7: the calibration-cycling block, declared
    # LAST so its commissioning validator sees every block the program reads
    # against — the site's timezone, the policy whose kernel bounds order the
    # floor, the timing the TTL and integration bounds ride, the fleet whose
    # capacities the bound sizes, the historian the trigger reads, the night
    # and health-watch windows the traverse must land before, and the night
    # capacity map that must stay ONE physical truth with this block's.
    battery_calibration: BatteryCalibrationConfig | None = None

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
        # V2 section 2.1's widened IFF: ONE capacity truth — the map is
        # required with exactly the fleet units IFF pacing 'even' OR a
        # forecast target posture (the same Wh paces the deadline and sizes
        # the target's headroom; two keys for one physical fact would drift).
        capacity_needed = night.pacing == "even" or night.target_policy != "full"
        if capacity_needed:
            if night.assumed_capacity_wh is None:
                raise ValueError(
                    "night_charging.assumed_capacity_wh is required when pacing is "
                    "'even' or target_policy is not 'full': the deadline rule and "
                    "the forecast target both pace from the per-unit capacity "
                    "estimate (the estimate only shapes pacing and targeting, "
                    "never safety)"
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
                "pacing is 'even' or target_policy is not 'full': cap_first-full "
                "paces from the cap alone and a stray map misstates which pacing "
                "rule runs"
            )
        # --- V2 section 6: the forecast posture's prerequisites (each
        # refusal names the missing block by path).  ``full`` requires none
        # of this — the v1 identity.
        if night.target_policy != "full":
            providers = values.get("forecast_providers")
            if providers is None:
                raise ValueError(
                    "night_charging.target_policy != full requires the "
                    "forecast_providers block: the target is computed from the PV "
                    "forecast and the load baseline the block declares"
                )
            if not providers.enabled:
                raise ValueError(
                    "night_charging.target_policy != full requires "
                    "forecast_providers.enabled: an advisory stack that composes "
                    "nothing would fall the adviser back to full targets every "
                    "night, silently"
                )
            if providers.solcast is None and (
                providers.open_meteo is None or providers.open_meteo.pv is None
            ):
                raise ValueError(
                    "night_charging.target_policy != full requires a PV forecast "
                    "source in forecast_providers (solcast, or open_meteo.pv): "
                    "without a PV truth there is no morning credit to net"
                )
            if providers.load_baseline is None:
                raise ValueError(
                    "night_charging.target_policy != full requires "
                    "forecast_providers.load_baseline: E_credit is NET surplus "
                    "(PV minus the load baseline), and there is deliberately NO "
                    "degrade-to-PV-only path (gross PV over-credits the morning "
                    "and under-charges, the refused direction)"
                )
            policy_block = values.get("policy")
            if policy_block is not None and night.floor_pct >= policy_block.maximum_soc_pct:
                raise ValueError(
                    "night_charging.floor_pct must sit strictly below the policy "
                    "maximum_soc_pct: the reserve floor is a lower clamp inside "
                    "the charge ceiling, never a second ceiling"
                )
        if night.target_policy == "forecast_act":
            # A3's file-level economics gate: the feature moves stored kWh off
            # the overnight buy onto morning surplus, paying (import - FIT)/eta
            # per stored kWh -- it pays only while FIT < off-peak import, and a
            # legacy 44-52 c QLD FiT against ~12 c off-peak LOSES ~35 c per
            # stored kWh.  The controller does not automate a per-stored-kWh
            # loss; staying kWh-only keeps SUGGEST running and ACT refused.
            providers = values.get("forecast_providers")
            tariff = None if providers is None else providers.tariff
            if tariff is None:
                raise ValueError(
                    "night_charging.target_policy forecast_act requires "
                    "forecast_providers.tariff (the tariff keys: off-peak import "
                    "and export FIT): every stored kWh moved off the overnight "
                    "buy onto morning surplus pays (import - FIT)/eta, and the "
                    "economics must be a file fact before ACT is granted"
                )
            from energypod.application.scheduling import parse_hhmm

            def _minute(wall: str) -> int:
                moment = parse_hhmm(wall)
                return moment.hour * 60 + moment.minute

            night_imports = _span_rates(
                tariff.default_import_cents_per_kwh,
                [
                    (
                        _minute(window.window_local[0]),
                        _minute(window.window_local[1]),
                        float(window.import_cents_per_kwh),
                    )
                    for window in tariff.windows
                ],
                [(_minute(start), _minute(end)) for start, end in night.window_local],
            )
            morning_exports = _span_rates(
                tariff.default_export_cents_per_kwh,
                [
                    (
                        _minute(window.window_local[0]),
                        _minute(window.window_local[1]),
                        float(window.export_cents_per_kwh),
                    )
                    for window in tariff.windows
                ],
                [
                    (_minute(end), _minute(night.midday_local))
                    for _start, end in night.window_local
                ],
            )
            if not night_imports or not morning_exports:
                raise ValueError(  # pragma: no cover - minute spans are non-empty
                    "night_charging.target_policy forecast_act could not resolve the "
                    "tariff rates over the night window and the morning span"
                )
            dearest_fit = max(rates[1] for rates in morning_exports)
            cheapest_import = min(rates[1] for rates in night_imports)
            if dearest_fit >= cheapest_import:
                raise ValueError(
                    "night_charging.target_policy forecast_act is refused on "
                    f"physics: the export FIT in force over the morning span "
                    f"({dearest_fit} c/kWh) is not strictly below the off-peak "
                    f"import rate in force over the night window "
                    f"({cheapest_import} c/kWh).  Every stored kWh moved off the "
                    "overnight buy onto morning surplus pays "
                    "(import - FIT)/efficiency, so the feature only pays while "
                    "FIT < import -- on a legacy 44-52 c QLD FiT against ~12 c "
                    "off-peak it LOSES ~35 c per stored kWh.  Stay kWh-only: "
                    "keep target_policy forecast_suggest and ACT stays refused"
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

    @field_validator("pvoutput")
    @classmethod
    def validate_pvoutput(
        cls, pvoutput: PvOutputConfig | None, info: ValidationInfo
    ) -> PvOutputConfig | None:
        """The commissioning gates for a PRESENT reporting block.

        The gates bind to block-PRESENCE (the night pattern: a disabled block
        that could never be enabled safely is refused at validation time,
        because the runtime toggle can raise participation but never a slot
        layout or a freshness bound):

        - ``unit_slots`` must cover EXACTLY the configured units — every
          commissioned battery reports, and a stray key names no battery;
        - ``max_sample_age_s`` must exceed ``timing.control_period_s`` — a
          freshness bound tighter than one fleet cycle could never hold, and
          the uploader would gap every slot forever;
        - the ``storage`` block is REQUIRED — the runtime toggle's whole point
          is surviving a restart, and a deployment without a durable store
          would silently reset the operator's choice on every boot (the
          invisible-off class this project refuses).
        """
        if pvoutput is None:
            return pvoutput
        values = info.data
        fleet_units = {unit.unit_id for unit in values.get("units", ()) or ()}
        declared = set(pvoutput.unit_slots)
        if declared != fleet_units:
            missing = sorted(fleet_units - declared)
            stray = sorted(declared - fleet_units)
            raise ValueError(
                "pvoutput.unit_slots must cover exactly the configured units "
                f"(missing: {missing or []}; names no battery: {stray or []}): every "
                "commissioned battery owns one [SoC slot, power slot] pair"
            )
        timing = values.get("timing")
        if timing is not None and pvoutput.max_sample_age_s <= timing.control_period_s:
            raise ValueError(
                "pvoutput.max_sample_age_s must exceed timing.control_period_s: the "
                "uploader judges observations once per fleet cycle, so a freshness "
                "bound at or below the cycle could never hold and every slot would "
                "be a gap"
            )
        if values.get("storage") is None:
            raise ValueError(
                "pvoutput requires the storage block: the runtime enable/disable "
                "toggle is a durable fact that must survive a controller restart, "
                "and a deployment without a durable store would silently reset the "
                "operator's choice on every boot"
            )
        return pvoutput

    @field_validator("battery_health_watch")
    @classmethod
    def validate_battery_health_watch(
        cls, watch: BatteryHealthWatchConfig | None, info: ValidationInfo
    ) -> BatteryHealthWatchConfig | None:
        """DESIGN_BATTERY_HEALTH_WATCH §9: the commissioning gates for a
        PRESENT health-watch block (every gate binds to block-PRESENCE, the
        night pattern; an ABSENT block changes nothing anywhere).

        Each refusal names its own rule — the prefix rule, the quiet-hour
        disjointness, the TWO deadline-arithmetic checks (A1), the one-zone
        truth (A9), and each stage's prerequisites.  The program-fit
        arithmetic uses the contract's own worst-case stage bounds (§4:
        census one bounded pass, probe <= ~2 min per unit, recovery <= ~5 min
        per unit — three units worst case ~21 min against a 45-minute
        budget).
        """
        if watch is None:
            return watch
        values = info.data
        site = values.get("site")
        if site is not None and watch.timezone != site.timezone:
            raise ValueError(
                "battery_health_watch.timezone must equal site.timezone "
                f"({watch.timezone!r} != {site.timezone!r}): the program window is "
                "a civil-time fact and a site keeps ONE civil-time truth (A9)"
            )
        units = tuple(values.get("units", ()) or ())
        unit_count = max(1, len(units))
        window = _wall_minute(watch.window_local)
        deadline = _wall_minute(watch.deadline_local)
        night = values.get("night_charging")
        night_starts: list[int] = []
        if night is not None:
            for start_wall, _end_wall in night.window_local:
                night_starts.append(_wall_minute(start_wall))
            for start_wall, end_wall in night.window_local:
                pair = (_wall_minute(start_wall), _wall_minute(end_wall))
                if _wrap_overlaps(window, deadline, *pair):
                    raise ValueError(
                        "battery_health_watch's [window_local, deadline_local] "
                        f"({watch.window_local}-{watch.deadline_local}) overlaps the "
                        f"night_charging window {start_wall}-{end_wall}: the program "
                        "lives in the idle hour BEFORE the night charge by "
                        "construction, never inside it"
                    )
        schedule = values.get("schedule")
        if schedule is not None:
            for start_wall, end_wall in schedule.allowed_windows_local:
                pair = (_wall_minute(start_wall), _wall_minute(end_wall))
                if _wrap_overlaps(window, deadline, *pair):
                    raise ValueError(
                        "battery_health_watch's [window_local, deadline_local] "
                        f"({watch.window_local}-{watch.deadline_local}) overlaps the "
                        f"schedule allowed window {start_wall}-{end_wall}: the "
                        "program lives in the quiet hour by construction — widen or "
                        "move the schedule windows, never the program"
                    )
        # --- the TWO deadline-arithmetic checks (A1), each naming its own
        # arithmetic so a refused revision teaches its own fix.
        probe_bound_s = (
            unit_count * (_PROBE_WORST_CASE_S_PER_UNIT + watch.probe.inter_unit_gap_s)
            if "probe" in watch.stages
            else 0
        )
        recovery_bound_s = (
            unit_count * _RECOVERY_WORST_CASE_S_PER_UNIT if "recovery" in watch.stages else 0
        )
        program_bound_s = _CENSUS_WORST_CASE_S + probe_bound_s + recovery_bound_s
        span_minutes = deadline - window
        if program_bound_s > span_minutes * 60:
            raise ValueError(
                "battery_health_watch: the whole-program bound does not fit before "
                f"the deadline — window {watch.window_local} + worst-case program "
                f"{program_bound_s} s (census {_CENSUS_WORST_CASE_S} s"
                + (
                    f" + {unit_count} units x probe "
                    f"{_PROBE_WORST_CASE_S_PER_UNIT + watch.probe.inter_unit_gap_s} s"
                    if probe_bound_s
                    else ""
                )
                + (
                    f" + {unit_count} units x recovery {_RECOVERY_WORST_CASE_S_PER_UNIT} s"
                    if recovery_bound_s
                    else ""
                )
                + f") exceeds the {watch.deadline_local} deadline by "
                f"{program_bound_s - span_minutes * 60} s (A1: window start + the "
                "whole-program bound must land at or before the deadline)"
            )
        for night_start in night_starts:
            clear_minutes = _wrap_minutes_ahead(deadline, night_start)
            if clear_minutes * 60 < _WORST_IN_FLIGHT_ACT_S:
                raise ValueError(
                    "battery_health_watch: the worst-case in-flight act does not "
                    f"land before the night window opens — deadline {watch.deadline_local}"
                    f" + worst-case act {_WORST_IN_FLIGHT_ACT_S} s exceeds the night "
                    f"window open at {night_start // 60:02d}:{night_start % 60:02d} by "
                    f"{_WORST_IN_FLIGHT_ACT_S - clear_minutes * 60} s (A1: a started "
                    "act completes inside its own bound and the program can never "
                    "bleed into the night charge)"
                )
        # --- per-stage prerequisites -------------------------------------------
        if "census" in watch.stages and values.get("plant_history") is None:
            raise ValueError(
                "battery_health_watch.stages including census requires the "
                "plant_history block: the stuck predicates are judged over an "
                "N-hours evidence window from the historian, and there is "
                "deliberately NO degrade-to-single-instant path (a one-look stuck "
                "verdict is the refused direction)"
            )
        if "probe" in watch.stages:
            if values.get("mode") is not ControllerMode.WRITE_ENABLED:
                raise ValueError(
                    "battery_health_watch.stages including probe requires mode "
                    "write_enabled: a probe is dispatch through the ordinary intent "
                    "path, and an observe-only composition can never actuate"
                )
            policy = values.get("policy")
            if policy is None:
                raise ValueError(
                    "battery_health_watch.stages including probe requires a policy "
                    "block: the probe's discharge sits inside the commissioned "
                    "static discharge limits and every standing guard judges it"
                )
            if watch.probe.probe_w > policy.max_unit_discharge_w:
                raise ValueError(
                    "battery_health_watch.probe.probe_w must not exceed the policy "
                    f"max_unit_discharge_w ({watch.probe.probe_w} > "
                    f"{policy.max_unit_discharge_w}): the probe is one bounded "
                    "ordinary intent, never a path around the static limit"
                )
        if "recovery" in watch.stages:
            if values.get("parking") is None:
                raise ValueError(
                    "battery_health_watch.stages including recovery requires the "
                    "parking block: Stage R composes the park/resume primitive "
                    "(it adds no transport path), and a site without the block is "
                    "refused at validation, never silently incapable"
                )
            if values.get("mode") is not ControllerMode.WRITE_ENABLED:
                raise ValueError(
                    "battery_health_watch.stages including recovery requires mode "
                    "write_enabled: the standby cycle is the one commissioned "
                    "automation mode write, gated behind the parking block's own "
                    "write-enabled commissioning"
                )
        if watch.recovery.mode == "auto":
            if "probe" not in watch.stages:
                raise ValueError(
                    "battery_health_watch.recovery.mode auto requires probe in "
                    "stages: there is no auto recovery without the probe that both "
                    "triggers and verifies it (§9)"
                )
            fleet_units = {unit.unit_id for unit in units}
            receipts = watch.recovery.auto_receipts or {}
            missing = sorted(fleet_units - set(receipts))
            if missing:
                raise ValueError(
                    "battery_health_watch.recovery.mode auto requires auto_receipts "
                    f"with a key for EVERY fleet unit (missing: {missing}): each "
                    "value is a docs/evidence/ supervised-verification receipt or "
                    "the literal 'excluded' — the config file itself enforces the "
                    "supervised-verification sequencing, so no operator revision "
                    "can skip advise and the supervised night in one edit (A6)"
                )
        return watch

    @field_validator("battery_calibration")
    @classmethod
    def validate_battery_calibration(
        cls, calibration: BatteryCalibrationConfig | None, info: ValidationInfo
    ) -> BatteryCalibrationConfig | None:
        """DESIGN_CALIBRATION_CYCLING §7: the commissioning gates for a
        PRESENT calibration block (every gate binds to block-PRESENCE, the
        night pattern; an ABSENT block changes nothing anywhere).

        Each refusal names its own rule — the floor-band AND kernel-ordering
        rule, the C12 set, the C8 SUM sizing, the C13 delivery-floor fit
        check, and BOTH window checks with their arithmetic.  The rhs-class
        path's prerequisite (the health watch with probe staged) is
        deliberately NOT refused here: its absence refuses that PATH only —
        the affected units defer ``no_control_evidence`` and the projection
        says so (§3.2).
        """
        if calibration is None:
            return calibration
        values = info.data
        fleet_units = {unit.unit_id for unit in values.get("units", ()) or ()}
        request = calibration.request_measurement
        if request is not None and request.unit not in fleet_units:
            # C6's one-shot names a battery that exists: the refusal names
            # the rule and the fleet (the pair is consumed at the next
            # plan_local and never outlives one consumption).
            raise ValueError(
                "battery_calibration.request_measurement.unit must name a configured "
                f"unit ({request.unit!r} is not in the fleet {sorted(fleet_units)}): "
                "the guarded one-shot selects a target, never mints a battery (C6)"
            )
        site = values.get("site")
        if site is not None and calibration.timezone != site.timezone:
            raise ValueError(
                "battery_calibration.timezone must equal site.timezone "
                f"({calibration.timezone!r} != {site.timezone!r}): the traverse window is "
                "a civil-time fact and a site keeps ONE civil-time truth (A9)"
            )
        traverse = calibration.traverse
        trigger = calibration.trigger
        policy = values.get("policy")
        if calibration.mode == "act":
            # The act is ordinary OPTIMIZER dispatch (C15: no receipt gate),
            # but it is still DISPATCH — an observe-only composition can never
            # actuate, and a site without its policy block has no bounds to
            # traverse inside.
            if values.get("mode") is not ControllerMode.WRITE_ENABLED:
                raise ValueError(
                    "battery_calibration.mode act requires mode write_enabled: the "
                    "traverse is ordinary dispatch through the intent path, and an "
                    "observe-only composition can never actuate (advise — the "
                    "default — composes as the display-only program)"
                )
            if policy is None:
                raise ValueError(
                    "battery_calibration.mode act requires a policy block: the "
                    "floor's kernel-ordering rule and the discharge bound derive "
                    "from the commissioned policy"
                )
        if policy is not None:
            # §4.3's ordering, a validated config fact: the adviser's stop and
            # the kernel's backstop are ordered by construction — the refusal
            # names both values and the science band.
            if traverse.floor_pct < policy.minimum_soc_pct:
                raise ValueError(
                    "battery_calibration.traverse.floor_pct must sit at or above the "
                    f"policy minimum_soc_pct ({traverse.floor_pct} < "
                    f"{policy.minimum_soc_pct}): the floor fires pre-submission so the "
                    "kernel's soc_below_discharge_floor never sees an at-or-below-floor "
                    "intent — at the commissioned policy the only commissionable floor "
                    "is EXACTLY the policy floor, the sourced band's [5, 10] conservative "
                    "edge (documented cell-undervoltage risk at reported low SoC argues "
                    "for keeping it)"
                )
            if traverse.discharge_w > policy.max_unit_discharge_w:
                raise ValueError(
                    "battery_calibration.traverse.discharge_w must not exceed the policy "
                    f"max_unit_discharge_w ({traverse.discharge_w} > "
                    f"{policy.max_unit_discharge_w}): the traverse is one bounded "
                    "ordinary intent, never a path around the static limit"
                )
        # C12: a completed anchor must reset the trigger — the predicate is
        # ``<=``, so equality still lets a cycle ending AT the floor reset it.
        if trigger.trigger_floor_pct < traverse.floor_pct:
            raise ValueError(
                "battery_calibration.trigger.trigger_floor_pct must sit at or above "
                f"traverse.floor_pct ({trigger.trigger_floor_pct} < {traverse.floor_pct}): "
                "the trigger's predicate is <=, so below it a cycle ending AT the "
                "floor would leave the pod immediately due again (C12)"
            )
        if trigger.eligibility_window_days < trigger.cycles_daily_min_days:
            raise ValueError(
                "battery_calibration.trigger.eligibility_window_days must sit at or "
                f"above cycles_daily_min_days ({trigger.eligibility_window_days} < "
                f"{trigger.cycles_daily_min_days}): the lhs-class exclusion cannot "
                "demand more distinct dates than its own lookback holds (C12)"
            )
        if traverse.min_discharge_w < _CALIBRATION_SENSING_FLOOR_W:
            raise ValueError(
                "battery_calibration.traverse.min_discharge_w must sit at or above "
                f"{_CALIBRATION_SENSING_FLOOR_W} W (got {traverse.min_discharge_w}): "
                "Victron documents a per-module ~1 A / ~50 W sensing threshold — "
                "sub-threshold currents report as 0 W — so the commanded rate must "
                "clear 3 modules' worth with margin (rhs lives in that band)"
            )
        if traverse.discharge_w < traverse.min_discharge_w:
            raise ValueError(
                "battery_calibration.traverse.discharge_w must sit at or above "
                f"min_discharge_w ({traverse.discharge_w} < {traverse.min_discharge_w}): "
                "the rate clamp would be empty"
            )
        timing = values.get("timing")
        if timing is not None:
            if traverse.intent_ttl_s <= timing.control_period_s:
                raise ValueError(
                    "battery_calibration.traverse.intent_ttl_s must exceed "
                    f"timing.control_period_s ({traverse.intent_ttl_s} <= "
                    f"{timing.control_period_s}): the adviser renews exactly once per "
                    "fleet cycle"
                )
            if traverse.integration_max_gap_s <= timing.control_period_s:
                raise ValueError(
                    "battery_calibration.traverse.integration_max_gap_s must exceed "
                    f"timing.control_period_s ({traverse.integration_max_gap_s} <= "
                    f"{timing.control_period_s}): the CT stream samples once per "
                    "control cycle, so a smaller gap would exclude every interval (C9)"
                )
        # --- the capacity map: present, exactly the fleet, and ONE physical
        # truth with the night block's (night-V2 §2.1's ruling, extended).
        fleet_units = {unit.unit_id for unit in values.get("units", ()) or ()}
        capacity = traverse.assumed_capacity_wh
        if capacity is None:
            raise ValueError(
                "battery_calibration.traverse.assumed_capacity_wh is required: the "
                "deadline rate and the lying-word energy bound both pace from the "
                "per-unit capacity estimate (the estimate shapes pacing and the "
                "bound, never safety)"
            )
        if set(capacity) != fleet_units:
            missing = sorted(fleet_units - set(capacity))
            stray = sorted(set(capacity) - fleet_units)
            raise ValueError(
                "battery_calibration.traverse.assumed_capacity_wh keys must be exactly "
                f"the fleet units (missing: {missing or []}; names no battery: "
                f"{stray or []}): a unit without an estimate has no traverse to pace"
            )
        night = values.get("night_charging")
        if (
            night is not None
            and night.assumed_capacity_wh is not None
            and dict(night.assumed_capacity_wh) != dict(capacity)
        ):
            raise ValueError(
                    "battery_calibration.traverse.assumed_capacity_wh must EQUAL "
                    "night_charging.assumed_capacity_wh when that block carries one "
                    f"({dict(sorted(capacity.items()))} != "
                    f"{dict(sorted(night.assumed_capacity_wh.items()))}): one physical "
                    "fact, two keys would drift (night-V2 §2.1's ruling, extended to "
                    "this consumer)"
                )
        # C8's SUM rule: the frozen-word stop must not cross the science
        # band's 5% edge even behind an undercounting meter — the refusal
        # names the whole sizing.
        min_capacity = min(int(value) for value in capacity.values())
        sum_wh = traverse.energy_margin_wh + traverse.metering_allowance_wh
        band_edge_wh = (traverse.floor_pct - 5.0) / 100.0 * min_capacity
        if sum_wh > band_edge_wh:
            raise ValueError(
                "battery_calibration.traverse: energy_margin_wh + "
                f"metering_allowance_wh ({traverse.energy_margin_wh} + "
                f"{traverse.metering_allowance_wh} = {sum_wh} Wh) must stay at or "
                f"below (floor_pct - 5)/100 x min(assumed_capacity_wh) "
                f"({traverse.floor_pct} - 5)/100 x {min_capacity} = "
                f"{band_edge_wh:.1f} Wh: the frozen-word stop's physical depth is "
                "floor - (margin + undercount)/capacity x 100, and a 2%-undercounting "
                "meter's ~94 Wh across a ~4.7 kWh leg must still land inside the "
                "sourced band's 5% edge (C8)"
            )
        # --- the C13 fit check at the DELIVERY floor: commanded watts are
        # not delivered watts, and the divide names itself (the divide's
        # natural unit is HOURS — Wh over W — so the window compares in
        # seconds after the x 3600).
        window_s = (
            _wall_minute(calibration.traverse_end_local) - _wall_minute(calibration.window_local)
        ) * 60
        worst_case_s = (
            (100.0 - traverse.floor_pct)
            / 100.0
            * min_capacity
            / (traverse.discharge_w * traverse.assumed_delivery_frac)
            * 3600.0
        )
        if worst_case_s > window_s:
            raise ValueError(
                "battery_calibration: the worst-case traverse does not FIT at the "
                f"delivery floor — (100 - floor)/100 x min(capacity) / (discharge_w x "
                f"assumed_delivery_frac) = (100 - {traverse.floor_pct})/100 x "
                f"{min_capacity} / ({traverse.discharge_w} x "
                f"{traverse.assumed_delivery_frac}) = {worst_case_s / 3600.0:.1f} h "
                f"against the {window_s / 3600.0:.1f} h window "
                f"({calibration.window_local}-{calibration.traverse_end_local}): widen "
                "the window or lower the rate (C13)"
            )
        # --- BOTH window checks (§4.1b): the last intent dies by TTL long
        # before any sibling window opens.
        end_s = _wall_minute(calibration.traverse_end_local) * 60
        expiry_s = end_s + traverse.intent_ttl_s
        watch = values.get("battery_health_watch")
        if watch is not None:
            watch_open_s = _wall_minute(watch.window_local) * 60
            boundary = watch_open_s if watch_open_s >= end_s else watch_open_s + 86_400
            if expiry_s > boundary:
                raise ValueError(
                    "battery_calibration: traverse_end_local + intent_ttl_s "
                    f"({calibration.traverse_end_local} + {traverse.intent_ttl_s} s) "
                    f"must land at or before battery_health_watch.window_local "
                    f"({watch.window_local}): the program may not bleed into the "
                    "sibling's watch"
                )
        if night is not None:
            for start_wall, _end_wall in night.window_local:
                night_open_s = _wall_minute(start_wall) * 60
                boundary = night_open_s if night_open_s >= end_s else night_open_s + 86_400
                if expiry_s > boundary:
                    raise ValueError(
                        "battery_calibration: traverse_end_local + intent_ttl_s "
                        f"({calibration.traverse_end_local} + {traverse.intent_ttl_s} s) "
                        f"must land at or before the night_charging window open "
                        f"({start_wall}): the program may not bleed into the night "
                        "charge"
                    )
                if (
                    _wall_minute(calibration.top_anchor.taper_deadline_local)
                    <= _wall_minute(_end_wall)
                ):
                    raise ValueError(
                        "battery_calibration.top_anchor.taper_deadline_local must sit "
                        f"strictly after the night_charging window end wall "
                        f"({calibration.top_anchor.taper_deadline_local} <= "
                        f"{_end_wall}): the taper is a MORNING fact"
                    )
        # The trigger and the measurement are historian-backed: no
        # degrade-to-single-instant path (the health-watch §9 precedent).
        if values.get("plant_history") is None:
            raise ValueError(
                "battery_calibration requires the plant_history block: the trigger "
                "(days-since-below-X over any horizon) and the measurement record are "
                "historian-backed, and there is deliberately NO "
                "degrade-to-single-instant path"
            )
        return calibration

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
