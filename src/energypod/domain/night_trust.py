"""The night-charge trust day record (DESIGN_NIGHT_CHARGE_V2 section 3.2).

One durable row per SCORED morning: the forecast surplus that actually drove
the window's target (archived verbatim in the ``night_target_set`` audit row),
the historian's recorded PRE-BATTERY surplus integrated over
``[window_end, midday)``, the normalized error and signed bias, the refusal
share of the landing attribution (amendment A1), and the morning's regime
bucket -- cut at scoring time against the terciles of the site's own recorded
distribution (amendment A4) and then AUTHORITATIVE: the gate reads the stored
bucket, so a morning's regime never re-buckets under later evidence.

Persisted as one JSON payload per civil date (the energy-day precedent): the
``night_trust_day`` table, schema version 5.  Excluded days -- a full-posture
morning, a fallback night, an incomplete historian record -- deliberately
leave NO row: they fail to inform, never to pass (A12).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, cast

RegimeBucket = Literal["low", "middle", "high"]

REGIME_BUCKETS: frozenset[str] = frozenset({"low", "middle", "high"})

_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "date",
        "provider",
        "target_policy",
        "quantile",
        "window_end_local",
        "midday_local",
        "e_surplus_forecast_kwh",
        "e_deficit_kwh",
        "e_recorded_kwh",
        "coverage_pct",
        "err_pct",
        "bias_pct",
        "refusal_share",
        "regime_bucket",
        "evaluated_at",
    }
)


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


@dataclass(frozen=True, slots=True)
class NightTrustDayRecord:
    """One scored morning's durable trust evidence."""

    date: date
    provider: str
    target_policy: str
    quantile: float | None
    window_end_local: str
    midday_local: str
    e_surplus_forecast_kwh: float
    e_deficit_kwh: float
    e_recorded_kwh: float
    coverage_pct: float
    err_pct: float
    bias_pct: float
    refusal_share: float | None
    regime_bucket: RegimeBucket
    evaluated_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.date, date):
            raise ValueError("date must be a civil date")
        for name in ("provider", "target_policy", "window_end_local", "midday_local"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty normalized string")
        if self.quantile is not None:
            quantile = _finite(self.quantile, "quantile")
            if not 0.0 <= quantile <= 1.0:
                raise ValueError("quantile must lie in [0.0, 1.0]")
            object.__setattr__(self, "quantile", quantile)
        for name in (
            "e_surplus_forecast_kwh",
            "e_deficit_kwh",
            "e_recorded_kwh",
            "err_pct",
            "bias_pct",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        coverage = _finite(self.coverage_pct, "coverage_pct")
        if not 0.0 <= coverage <= 100.0:
            raise ValueError("coverage_pct must lie in [0.0, 100.0]")
        object.__setattr__(self, "coverage_pct", coverage)
        if self.refusal_share is not None:
            share = _finite(self.refusal_share, "refusal_share")
            if not 0.0 <= share <= 1.0:
                raise ValueError("refusal_share must lie in [0.0, 1.0]")
            object.__setattr__(self, "refusal_share", share)
        if self.regime_bucket not in REGIME_BUCKETS:
            raise ValueError(f"regime_bucket must be one of {sorted(REGIME_BUCKETS)}")
        if not isinstance(self.evaluated_at, str) or not self.evaluated_at:
            raise ValueError("evaluated_at must be a non-empty ISO timestamp")

    def payload(self) -> dict[str, Any]:
        """The JSON-native row (sorted keys at the persistence boundary)."""
        return {
            "date": self.date.isoformat(),
            "provider": self.provider,
            "target_policy": self.target_policy,
            "quantile": self.quantile,
            "window_end_local": self.window_end_local,
            "midday_local": self.midday_local,
            "e_surplus_forecast_kwh": self.e_surplus_forecast_kwh,
            "e_deficit_kwh": self.e_deficit_kwh,
            "e_recorded_kwh": self.e_recorded_kwh,
            "coverage_pct": self.coverage_pct,
            "err_pct": self.err_pct,
            "bias_pct": self.bias_pct,
            "refusal_share": self.refusal_share,
            "regime_bucket": self.regime_bucket,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> NightTrustDayRecord:
        actual = frozenset(value)
        if actual != _RECORD_KEYS:
            missing = sorted(_RECORD_KEYS - actual)
            extra = sorted(actual - _RECORD_KEYS)
            raise ValueError(
                f"invalid night trust day record keys; missing={missing}, extra={extra}"
            )
        bucket = str(value["regime_bucket"])
        if bucket not in REGIME_BUCKETS:
            raise ValueError(f"regime_bucket must be one of {sorted(REGIME_BUCKETS)}")
        return cls(
            date=date.fromisoformat(str(value["date"])),
            provider=str(value["provider"]),
            target_policy=str(value["target_policy"]),
            quantile=None if value["quantile"] is None else float(value["quantile"]),
            window_end_local=str(value["window_end_local"]),
            midday_local=str(value["midday_local"]),
            e_surplus_forecast_kwh=float(value["e_surplus_forecast_kwh"]),
            e_deficit_kwh=float(value["e_deficit_kwh"]),
            e_recorded_kwh=float(value["e_recorded_kwh"]),
            coverage_pct=float(value["coverage_pct"]),
            err_pct=float(value["err_pct"]),
            bias_pct=float(value["bias_pct"]),
            refusal_share=None if value["refusal_share"] is None else float(
                value["refusal_share"]
            ),
            regime_bucket=cast("RegimeBucket", bucket),
            evaluated_at=str(value["evaluated_at"]),
        )


__all__ = ["REGIME_BUCKETS", "NightTrustDayRecord", "RegimeBucket"]
