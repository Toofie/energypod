"""The normalized forecast value model contract (architecture section 17).

The provider families normalize onto ONE immutable value shape before any
adviser ever sees them: half-open intervals, an optional quantile, the
variable with its unit in the name, the source, and both time stamps — when
WE fetched it and (when the service says) when the source issued it.  These
tests pin that shape, its validation, and the advisory-only boundary before
any concrete adapter exists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

try:
    from energypod.adapters.providers.model import (
        ForecastSeries,
        ForecastValue,
        ForecastVariable,
        TariffInterval,
    )
except ImportError as exc:  # pragma: no cover - initial red phase only
    ForecastSeries: Any = None
    ForecastValue: Any = None
    ForecastVariable: Any = None
    TariffInterval: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def test_the_provider_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The provider model contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


def _moment(minute: int) -> datetime:
    return datetime(2026, 8, 24, 10, 0, tzinfo=UTC) + timedelta(minutes=minute)


def _value(**overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "variable": "pv_power_w",
        "interval_start": _moment(0),
        "interval_end": _moment(30),
        "value": 1234.0,
        "quantile": 0.5,
        "source": "open-meteo",
        "fetched_at": datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return ForecastValue(**fields)


class TestForecastValue:
    def test_the_normalized_shape_carries_every_contract_field(self) -> None:
        value = _value()
        assert value.variable == "pv_power_w"
        assert value.interval_start == _moment(0)
        assert value.interval_end == _moment(30)
        assert value.value == 1234.0
        assert value.quantile == 0.5
        assert value.source == "open-meteo"
        assert value.fetched_at == datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
        assert value.issued_at is None

    def test_quantile_is_optional_a_deterministic_forecast_carries_none(self) -> None:
        assert _value(quantile=None).quantile is None

    def test_intervals_are_half_open_and_must_be_ordered(self) -> None:
        with pytest.raises(ValueError, match="interval_start"):
            _value(interval_start=_moment(30), interval_end=_moment(0))
        with pytest.raises(ValueError, match="zero-length"):
            _value(interval_end=_moment(0))

    def test_naive_datetimes_are_refused_at_every_stamp(self) -> None:
        naive = datetime(2026, 8, 24, 10, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            _value(interval_start=naive)
        with pytest.raises(ValueError, match="timezone-aware"):
            _value(interval_end=naive)
        with pytest.raises(ValueError, match="timezone-aware"):
            _value(fetched_at=naive)
        with pytest.raises(ValueError, match="timezone-aware"):
            _value(issued_at=naive)

    def test_non_utc_offsets_are_normalized_to_utc(self) -> None:
        plus10 = datetime.fromisoformat("2026-08-24T20:00:00+10:00")
        value = _value(interval_start=plus10, interval_end=plus10 + timedelta(minutes=30))
        assert value.interval_start == _moment(0)
        assert value.interval_start.tzinfo is UTC

    @pytest.mark.parametrize(
        ("field", "raw"),
        [
            ("variable", "made_up_variable"),
            ("variable", ""),
            ("source", ""),
            ("source", " padded "),
            ("quantile", -0.01),
            ("quantile", 1.01),
            ("value", float("inf")),
            ("value", float("nan")),
        ],
    )
    def test_the_vocabulary_and_ranges_are_pinned(self, field: str, raw: Any) -> None:
        with pytest.raises((ValueError, TypeError)):
            _value(**{field: raw})

    def test_the_variable_vocabulary_names_the_reserved_families(self) -> None:
        names = {item.value for item in ForecastVariable}
        assert {
            "pv_power_w",
            "load_power_w",
            "irradiance_w_m2",
            "cloud_cover_pct",
            "temperature_c",
        } <= names

    def test_the_model_is_immutable(self) -> None:
        value = _value()
        with pytest.raises(Exception):  # noqa: B017 - frozen dataclass contract
            value.value = 99.0  # type: ignore[misc]


class TestForecastSeries:
    def _series_values(self) -> list[Any]:
        base = {
            "variable": "pv_power_w",
            "source": "solcast",
            "fetched_at": datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
        }
        return [
            ForecastValue(
                interval_start=_moment(0),
                interval_end=_moment(30),
                value=1000.0,
                quantile=0.1,
                **base,
            ),
            ForecastValue(
                interval_start=_moment(0),
                interval_end=_moment(30),
                value=2000.0,
                quantile=0.5,
                **base,
            ),
            ForecastValue(
                interval_start=_moment(0),
                interval_end=_moment(30),
                value=3000.0,
                quantile=0.9,
                **base,
            ),
            ForecastValue(
                interval_start=_moment(30),
                interval_end=_moment(60),
                value=2500.0,
                quantile=0.5,
                **base,
            ),
        ]

    def test_the_series_groups_one_fetch_of_one_variable(self) -> None:
        series = ForecastSeries(
            variable="pv_power_w",
            source="solcast",
            fetched_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
            issued_at=None,
            values=tuple(self._series_values()),
        )
        assert series.variable == "pv_power_w"
        assert series.source == "solcast"
        assert len(series.values) == 4
        assert series.issued_at is None

    def test_a_value_disagreeing_with_its_series_is_refused(self) -> None:
        with pytest.raises(ValueError, match="variable"):
            ForecastSeries(
                variable="load_power_w",
                source="solcast",
                fetched_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
                issued_at=None,
                values=tuple(self._series_values()),
            )
        with pytest.raises(ValueError, match="fetched_at"):
            ForecastSeries(
                variable="pv_power_w",
                source="solcast",
                fetched_at=datetime(2026, 8, 24, 9, 5, tzinfo=UTC),
                issued_at=None,
                values=tuple(self._series_values()),
            )

    def test_duplicate_interval_quantile_rows_are_refused(self) -> None:
        values = self._series_values()
        duplicate = ForecastValue(
            interval_start=_moment(0),
            interval_end=_moment(30),
            value=9999.0,
            quantile=0.5,
            variable="pv_power_w",
            source="solcast",
            fetched_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="duplicate"):
            ForecastSeries(
                variable="pv_power_w",
                source="solcast",
                fetched_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
                issued_at=None,
                values=(*values, duplicate),
            )

    def test_values_must_arrive_interval_sorted(self) -> None:
        values = self._series_values()
        with pytest.raises(ValueError, match="sorted"):
            ForecastSeries(
                variable="pv_power_w",
                source="solcast",
                fetched_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
                issued_at=None,
                values=tuple(reversed(values)),
            )

    def test_at_quantile_selects_one_probability_slice(self) -> None:
        series = ForecastSeries(
            variable="pv_power_w",
            source="solcast",
            fetched_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
            issued_at=None,
            values=tuple(self._series_values()),
        )
        median = series.at_quantile(0.5)
        assert [item.value for item in median.values] == [2000.0, 2500.0]

    def test_point_series_expose_their_deterministic_values(self) -> None:
        base = {
            "variable": "load_power_w",
            "source": "historian-baseline",
            "fetched_at": datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
            "quantile": None,
        }
        series = ForecastSeries(
            variable="load_power_w",
            source="historian-baseline",
            fetched_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
            issued_at=None,
            values=(
                ForecastValue(
                    interval_start=_moment(0), interval_end=_moment(30), value=400.0, **base
                ),
                ForecastValue(
                    interval_start=_moment(30), interval_end=_moment(60), value=500.0, **base
                ),
            ),
        )
        point = series.at_quantile(None)
        assert [item.value for item in point.values] == [400.0, 500.0]


class TestTariffInterval:
    def _interval(self, **overrides: Any) -> Any:
        fields: dict[str, Any] = {
            "interval_start": _moment(0),
            "interval_end": _moment(30),
            "import_cents_per_kwh": 28.0,
            "export_cents_per_kwh": 9.0,
            "currency": "AUD",
            "market": "static-config",
            "quality": "static",
        }
        fields.update(overrides)
        return TariffInterval(**fields)

    def test_the_normalized_tariff_shape(self) -> None:
        interval = self._interval()
        assert interval.import_cents_per_kwh == 28.0
        assert interval.export_cents_per_kwh == 9.0
        assert interval.currency == "AUD"
        assert interval.market == "static-config"
        assert interval.quality == "static"
        assert interval.published_at is None

    def test_currency_is_normalized_and_iso_4217_shaped(self) -> None:
        assert self._interval(currency="aud").currency == "AUD"
        for bad in ("", "AU", "AUDD", "A1D", "aud ", "US$"):
            with pytest.raises(ValueError, match="currency"):
                self._interval(currency=bad)

    def test_prices_are_non_negative_and_intervals_ordered(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            self._interval(import_cents_per_kwh=-1.0)
        with pytest.raises(ValueError, match="non-negative"):
            self._interval(export_cents_per_kwh=-0.5)
        with pytest.raises(ValueError, match="interval_start"):
            self._interval(interval_start=_moment(30), interval_end=_moment(0))

    def test_the_market_and_quality_vocabularies_are_pinned(self) -> None:
        with pytest.raises(ValueError, match="market"):
            self._interval(market="guessing")
        with pytest.raises(ValueError, match="quality"):
            self._interval(quality="vibes")


def test_the_provider_layer_is_advisory_only_no_control_module_imports_it() -> None:
    """ARCHITECTURE section 24's fitness rule, pinned for this family.

    The domain and application packages must never import an adapter, and the
    provider adapters in particular must be unreachable from every control
    path: a grep-level pin is the honest cheap check that a future edit has
    not quietly wired a forecast into the kernel's inputs.
    """
    root = Path(__file__).resolve().parents[2] / "src" / "energypod"
    offenders: list[str] = []
    for package in ("domain", "application"):
        for path in sorted((root / package).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "adapters.providers" in text or "adapters\\providers" in text:
                offenders.append(str(path))
    assert offenders == [], f"control-path modules must not import providers: {offenders}"
