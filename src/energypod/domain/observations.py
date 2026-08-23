"""Immutable observations and explicit telemetry quality."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class DataQuality(StrEnum):
    GOOD = "good"
    STALE = "stale"
    MISSING = "missing"
    BAD = "bad"
    SUSPECT = "suspect"


class UnitLifecycle(StrEnum):
    BOOT = "boot"
    OBSERVE_ONLY = "observe_only"
    DISARMED = "disarmed"
    ARMED_IDLE = "armed_idle"
    ACTIVE = "active"
    INHIBITED = "inhibited"
    STOPPING = "stopping"
    DISCONNECTED = "disconnected"


class _FrozenStringMapping[ValueT](Mapping[str, ValueT]):
    """Small, deterministic and hashable immutable mapping for value objects."""

    __slots__ = ("_items", "_values")

    def __init__(self, values: Mapping[str, ValueT]) -> None:
        self._items = tuple(sorted(values.items()))
        self._values = dict(self._items)

    def __getitem__(self, key: str) -> ValueT:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __hash__(self) -> int:
        return hash(self._items)


class Observation(BaseModel):
    QUALITY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "system_soc_pct",
            "bms_soc_pct",
            "soh_pct",
            "battery_watts",
            "pack_voltage_v",
            "pack_current_a",
            "dynamic_charge_limit_w",
            "dynamic_discharge_limit_w",
            "cell_voltages_v",
            "temperatures_c",
        }
    )
    # API_CONTRACTS "Excess-solar accelerated charging (advisory)": the two
    # per-pod CT power words (PCS 0x1000+17/+20, PROTOCOL_EVIDENCE 4c) are
    # ADVISORY telemetry.  They gate export-bounded control through their own
    # fail-closed bound, never through the safety-critical completeness set,
    # so a deployment whose read plan does not serve the PCS block keeps fully
    # qualified observations for ordinary control.
    #
    # API_CONTRACTS "Energy scorecard" (DESIGN_ENERGY_SCORECARD section 5):
    # the six cumulative-energy words (cold-ring totals block 0x4101,
    # field-mapping S2.7) join the same ADVISORY set -- quality-map keys, so
    # the twelve-key shape extends to eighteen by the same mechanism -- while
    # staying OUTSIDE every safety completeness set: an unserved energy block
    # must never refuse power.  The grid pair keeps NEUTRAL A/B names until
    # the ``grid_counter_roles`` config gate licenses vendor labels (A-1).
    # The advisory set grows by feature, never by partial extension: the CT
    # pair (excess-solar) and then the six cumulative-energy words (the energy
    # scorecard).  Producers keep emitting every key they know, so the
    # twelve-key shape stays valid alongside the eighteen-key one.
    CT_QUALITY_FIELDS: ClassVar[frozenset[str]] = frozenset({"grid_power_w", "load_power_w"})
    ENERGY_QUALITY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "energy_grid_a_kwh",
            "energy_grid_b_kwh",
            "energy_load_kwh",
            "energy_pv_kwh",
            "energy_charge_kwh",
            "energy_discharge_kwh",
        }
    )
    ADVISORY_QUALITY_FIELDS: ClassVar[frozenset[str]] = CT_QUALITY_FIELDS | ENERGY_QUALITY_FIELDS
    # SYNC_RESILIENCE_AUDIT B1 (2026-08-24): the system controller's SOC word
    # (0x0100+17) is ADVISORY telemetry too.  The tiered read plan serves its
    # block once per process and merges it from cache thereafter, so its
    # quality can be hours stale -- or permanently BAD/SUSPECT from one bad
    # cycle-1 decode -- while the battery's own BMS SOC (0x5000+9) reads fresh
    # and GOOD at the control rate.  No bound consumes the system figure any
    # more (the BMS SOC is authoritative), so its quality gate is demoted from
    # the safety-critical set: the quality-map KEY stays (honest inventory;
    # the decoder keeps emitting it), only the required membership moves.
    REQUIRED_SAFETY_QUALITY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        QUALITY_FIELDS - {"system_soc_pct"}
    )
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", arbitrary_types_allowed=True
    )

    unit_id: str
    device_identity: str | None = None
    connection_epoch: int = 0
    wall_timestamp: datetime
    captured_at_mono: float
    sequence: int
    lifecycle: UnitLifecycle
    protocol_profile: str
    system_soc_pct: float | None
    bms_soc_pct: float | None
    soh_pct: float | None
    battery_watts: float | None
    pack_voltage_v: float | None
    pack_current_a: float | None
    dynamic_charge_limit_w: float | None
    dynamic_discharge_limit_w: float | None
    # Advisory per-pod CT power (signed; negative grid = import, positive =
    # export — PROTOCOL_EVIDENCE 4b/4c).  ``None`` when the poll did not
    # serve the PCS live block; never zero-filled.
    grid_power_w: float | None = None
    load_power_w: float | None = None
    # Advisory device-mode words (2026-08-23 incident 1, field-mapping
    # S2.13/S2.14/S2): the debug-mode readback 0x8100+0, the system
    # overview's ctrlMode 0x0100+1 and workMode +2, and the PCS live runMode
    # 0x1000+2.  They carry the vendor's dispatch preconditions but stay
    # OUTSIDE the quality map exactly like the CT words: a read plan without
    # their blocks keeps fully qualified observations, and absent evidence
    # (``None``) never refuses anything.
    debug_mode_w: int | None = None
    ctrl_mode_w: int | None = None
    work_mode_w: int | None = None
    run_mode_w: int | None = None
    # Advisory cumulative energy (DESIGN_ENERGY_SCORECARD section 5): the six
    # low-word-first uint32 x 0.1 kWh counters of the totals block 0x4101, in
    # vendor pair order.  The GRID PAIR names are deliberately NEUTRAL (A/B):
    # the pair ORDER is vendor-confirmed but which pair accumulates imports
    # and which exports is evidence-open (field-mapping S5 A-1) -- renaming to
    # bought/sold happens only behind the ``grid_counter_roles`` config gate.
    # ``None`` when the poll did not serve the totals block; never zero-filled.
    energy_grid_a_kwh: float | None = None
    energy_grid_b_kwh: float | None = None
    energy_load_kwh: float | None = None
    energy_pv_kwh: float | None = None
    energy_charge_kwh: float | None = None
    energy_discharge_kwh: float | None = None
    expected_cell_count: int | None = None
    cell_voltages_v: tuple[float, ...]
    cell_captured_at_mono: float | None = None
    cell_sequence: int | None = None
    expected_temperature_count: int | None = None
    temperatures_c: tuple[float, ...]
    active_faults: frozenset[str]
    active_warnings: frozenset[str]
    quality: Mapping[str, DataQuality]

    @field_validator("unit_id", "protocol_profile")
    @classmethod
    def _normalized_text(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("text must be non-empty and normalized")
        return value

    @field_validator("device_identity")
    @classmethod
    def _optional_identity(cls, value: str | None) -> str | None:
        if value is not None and (not value or value != value.strip()):
            raise ValueError("device identity must be non-empty and normalized when present")
        return value

    @field_validator("connection_epoch", "sequence")
    @classmethod
    def _nonnegative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("counter must be non-negative")
        return value

    @field_validator("expected_cell_count", "expected_temperature_count")
    @classmethod
    def _optional_positive_count(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("count must be positive")
        return value

    @field_validator("cell_sequence")
    @classmethod
    def _optional_nonnegative_sequence(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("sequence must be non-negative")
        return value

    @field_validator("wall_timestamp")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("wall timestamp must be UTC")
        return value

    @field_validator("captured_at_mono", "cell_captured_at_mono")
    @classmethod
    def _finite_timestamp(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        return value

    @field_validator("system_soc_pct", "bms_soc_pct", "soh_pct")
    @classmethod
    def _percentage(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError("percentage must be finite and between zero and 100")
        return value

    @field_validator(
        "battery_watts",
        "pack_voltage_v",
        "pack_current_a",
        "grid_power_w",
        "load_power_w",
    )
    @classmethod
    def _finite_measurement(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("measurement must be finite")
        return value

    @field_validator(
        "energy_grid_a_kwh",
        "energy_grid_b_kwh",
        "energy_load_kwh",
        "energy_pv_kwh",
        "energy_charge_kwh",
        "energy_discharge_kwh",
    )
    @classmethod
    def _cumulative_energy(cls, value: float | None) -> float | None:
        # Cumulative counters decode from uint32 x 0.1 words: finite and
        # non-negative, with ``bool`` refused by strict mode alongside every
        # other coercion.
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("cumulative energy must be finite and non-negative")
        return value

    @field_validator("dynamic_charge_limit_w", "dynamic_discharge_limit_w")
    @classmethod
    def _nonnegative_measurement(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("limit must be finite and non-negative")
        return value

    @field_validator("debug_mode_w", "ctrl_mode_w", "work_mode_w", "run_mode_w")
    @classmethod
    def _mode_word(cls, value: int | None) -> int | None:
        if value is not None and (type(value) is not int or not 0 <= value <= 0xFFFF):
            raise ValueError("a mode word must be an unsigned 16-bit register value")
        return value

    @field_validator("cell_voltages_v", "temperatures_c")
    @classmethod
    def _finite_tuple(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("measurements must be finite")
        return value

    @field_validator("active_faults", "active_warnings")
    @classmethod
    def _codes(cls, value: frozenset[str]) -> frozenset[str]:
        if any(not code or code != code.strip() for code in value):
            raise ValueError("codes must be non-empty and normalized")
        return value

    @field_validator("quality")
    @classmethod
    def _quality(cls, value: Mapping[str, DataQuality]) -> Mapping[str, DataQuality]:
        copied = dict(value)
        # Exactly the ten safety-critical fields, those plus the two advisory
        # CT fields, or those plus the full advisory set (the CT pair and the
        # six cumulative-energy fields -- DESIGN_ENERGY_SCORECARD section 5):
        # the wire decoder always emits every key it knows the plan may serve
        # (MISSING for unserved sources), while older producers and the
        # ten-field test fixtures keep the original shapes.  A partial
        # advisory extension is a malformed quality map, not a view to forgive.
        allowed = (
            cls.QUALITY_FIELDS,
            cls.QUALITY_FIELDS | cls.CT_QUALITY_FIELDS,
            cls.QUALITY_FIELDS | cls.ADVISORY_QUALITY_FIELDS,
        )
        if set(copied) not in allowed:
            raise ValueError("quality must contain exactly the declared telemetry fields")
        if any(type(item) is not DataQuality for item in copied.values()):
            raise TypeError("quality values must be DataQuality members")
        return _FrozenStringMapping(copied)

    @model_validator(mode="after")
    def _cell_metadata_defaults(self) -> Observation:
        if self.cell_captured_at_mono is None:
            object.__setattr__(self, "cell_captured_at_mono", self.captured_at_mono)
        if self.cell_sequence is None:
            object.__setattr__(self, "cell_sequence", self.sequence)
        return self

    def age_seconds(self, now_mono: float) -> float:
        if type(now_mono) is not float or not math.isfinite(now_mono):
            raise TypeError("current monotonic time must be a finite float")
        age = now_mono - self.captured_at_mono
        if age < 0:
            raise ValueError("current monotonic time precedes capture")
        return age

    @property
    def cell_min_voltage_v(self) -> float | None:
        return min(self.cell_voltages_v, default=None)

    @property
    def cell_max_voltage_v(self) -> float | None:
        return max(self.cell_voltages_v, default=None)

    @property
    def cell_imbalance_v(self) -> float | None:
        low, high = self.cell_min_voltage_v, self.cell_max_voltage_v
        return None if low is None or high is None else high - low

    @property
    def temperature_min_c(self) -> float | None:
        return min(self.temperatures_c, default=None)

    @property
    def temperature_max_c(self) -> float | None:
        return max(self.temperatures_c, default=None)

    @property
    def cells_complete(self) -> bool:
        return (
            self.expected_cell_count is not None
            and len(self.cell_voltages_v) == self.expected_cell_count
            and self.quality["cell_voltages_v"] is DataQuality.GOOD
        )

    @property
    def temperatures_complete(self) -> bool:
        return (
            self.expected_temperature_count is not None
            and len(self.temperatures_c) == self.expected_temperature_count
            and self.quality["temperatures_c"] is DataQuality.GOOD
        )

    @property
    def debug_mode_active(self) -> bool | None:
        """The vendor's PQ-dispatch precondition, MiniESapp.cs:2180-2184.

        ``True`` when the debug-mode readback is nonzero (the vendor app
        refuses sends); ``False`` when it reads zero (Normal Mode); ``None``
        when the poll served no debug-mode word -- absent evidence is never
        a refusal.
        """
        return None if self.debug_mode_w is None else self.debug_mode_w != 0

    @property
    def ctrl_mode_remote(self) -> bool | None:
        """ctrlMode enum 1 Remote / 2 Local (GlobalFun.cs:204-211).

        ``None`` when the poll served no system-overview block.
        """
        return None if self.ctrl_mode_w is None else self.ctrl_mode_w == 1

    @property
    def authoritative_soc_pct(self) -> float | None:
        """The battery's own BMS SOC is the authoritative SOC (2026-08-24).

        The operator's ruling: "If there's a disagreement, re-sync based on
        whatever the battery says."  Every SOC-based policy bound -- the
        discharge floor, the charge ceiling, the SOC-jump check -- is judged
        on this figure, and the system controller's SOC word (0x0100+17,
        served once per connection by the tiered read plan and therefore
        potentially hours stale on a cycled unit) is never a second safety
        opinion.  With the system block absent the wire decoder already
        stands the BMS SOC in for both views with honest quality, so this
        property is that doctrine made explicit for advisory consumers.
        """
        return self.bms_soc_pct

    @property
    def safety_data_complete(self) -> bool:
        # The system SOC figure and its quality are advisory (B1): the BMS SOC
        # stands in as the authoritative SOC everywhere a bound consumes one,
        # and an untrusted system word surfaces as an informational note
        # rather than incomplete safety data.
        required = (
            self.bms_soc_pct,
            self.soh_pct,
            self.battery_watts,
            self.pack_voltage_v,
            self.pack_current_a,
            self.dynamic_charge_limit_w,
            self.dynamic_discharge_limit_w,
        )
        return (
            all(
                self.quality[field] is DataQuality.GOOD
                for field in self.REQUIRED_SAFETY_QUALITY_FIELDS
            )
            and all(value is not None for value in required)
            and self.cells_complete
            and self.temperatures_complete
        )
