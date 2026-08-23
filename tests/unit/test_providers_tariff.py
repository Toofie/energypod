"""The config-declared static tariff schedule contract (TariffProvider).

The tariff family's first provider is the operator's own declared schedule:
day-local windows over a flat default, expanded over the horizon in the
site's civil time.  These tests pin the expansion: contiguous half-open
intervals covering the horizon exactly (including across a DST boundary and
a midnight-crossing window), the right rates in the right windows, honest
metadata (``static-config`` market, ``static`` quality, the operator's
currency), and refusal of overlapping declarations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest

try:
    from energypod.adapters.providers.tariff_static import (
        StaticTariffProvider,
        TariffRateWindow,
    )
except ImportError as exc:  # pragma: no cover - initial red phase only
    StaticTariffProvider: Any = None
    TariffRateWindow: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None

try:
    from energypod.adapters.providers.ports import TariffProvider
except ImportError:  # pragma: no cover - initial red phase only
    TariffProvider: Any = None


def test_the_static_tariff_contract_is_implemented() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The static tariff contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


HORIZON_S = 36 * 3600.0
# 2026-08-23 08:00 local Brisbane (a no-DST zone, so the plain-day tests are
# stable); the DST cases name their own zone and dates explicitly.
NOW = datetime(2026, 8, 22, 22, 0, tzinfo=UTC)
ZONE = "Australia/Brisbane"


class ManualClock:
    def __init__(self, wall: datetime) -> None:
        self.wall = wall

    def monotonic(self) -> float:
        return 0.0

    def wall_now(self) -> datetime:
        return self.wall


def _local_now(zone: str = ZONE) -> datetime:
    from zoneinfo import ZoneInfo

    return NOW.astimezone(ZoneInfo(zone))


OFF_PEAK = TariffRateWindow(
    window_local=("00:00", "06:00"), import_cents_per_kwh=12.0, export_cents_per_kwh=5.0
)
EVENING = TariffRateWindow(
    window_local=("17:00", "21:00"), import_cents_per_kwh=45.0, export_cents_per_kwh=12.0
)


def _provider(
    *,
    windows: tuple[Any, ...] = (OFF_PEAK, EVENING),
    zone: str = ZONE,
    now: datetime | None = None,
    horizon_s: float = HORIZON_S,
) -> Any:
    return StaticTariffProvider(
        clock=ManualClock(now or _local_now(zone)),
        timezone_name=zone,
        currency="AUD",
        default_import_cents_per_kwh=28.0,
        default_export_cents_per_kwh=9.0,
        windows=windows,
        horizon_s=horizon_s,
    )


class TestExpansion:
    async def test_intervals_cover_the_whole_horizon_contiguously(self) -> None:
        provider = _provider()
        intervals = await provider.tariff_intervals()
        assert intervals, "the expansion always produces intervals"
        now = _local_now()
        horizon_end = (now.astimezone(UTC) + timedelta(seconds=HORIZON_S)).astimezone(now.tzinfo)
        assert intervals[0].interval_start <= now
        assert intervals[-1].interval_end >= horizon_end
        for earlier, later in pairwise(intervals):
            assert earlier.interval_end == later.interval_start, "no gaps, no overlaps"

    async def test_the_right_rates_land_in_the_right_windows(self) -> None:
        provider = _provider()
        intervals = await provider.tariff_intervals()

        def rate_at(moment: datetime) -> tuple[float, float]:
            match = next(
                item for item in intervals if item.interval_start <= moment < item.interval_end
            )
            return match.import_cents_per_kwh, match.export_cents_per_kwh

        from zoneinfo import ZoneInfo

        zone = ZoneInfo(ZONE)
        # 03:00 local is inside the off-peak window; 12:00 is the default;
        # 18:00 is the evening peak (all on TOMORROW, safely inside the
        # horizon that starts at today 08:00 local).
        day = (_local_now() + timedelta(days=1)).astimezone(zone).date()
        assert rate_at(datetime(day.year, day.month, day.day, 3, 0, tzinfo=zone)) == (12.0, 5.0)
        assert rate_at(datetime(day.year, day.month, day.day, 12, 0, tzinfo=zone)) == (28.0, 9.0)
        assert rate_at(datetime(day.year, day.month, day.day, 18, 0, tzinfo=zone)) == (45.0, 12.0)

    async def test_adjacent_equal_rates_merge_into_one_interval(self) -> None:
        night = TariffRateWindow(
            window_local=("00:00", "06:00"), import_cents_per_kwh=12.0, export_cents_per_kwh=5.0
        )
        morning = TariffRateWindow(
            window_local=("06:00", "09:00"), import_cents_per_kwh=12.0, export_cents_per_kwh=5.0
        )
        provider = _provider(windows=(night, morning))
        intervals = await provider.tariff_intervals()
        # The two windows meet at 06:00 with identical rates; the expansion
        # must not emit a boundary there.
        for earlier, later in pairwise(intervals):
            assert not (
                earlier.import_cents_per_kwh == later.import_cents_per_kwh
                and earlier.export_cents_per_kwh == later.export_cents_per_kwh
            ), "equal-rate neighbours merge into one interval"
        merged = [item for item in intervals if item.import_cents_per_kwh == 12.0]
        assert any(
            (item.interval_end - item.interval_start) >= timedelta(hours=9) for item in merged
        ), "00:00-09:00 lands as one interval on at least one day"

    async def test_everything_clipped_to_the_horizon(self) -> None:
        provider = _provider(horizon_s=6 * 3600.0)
        intervals = await provider.tariff_intervals()
        now = _local_now()
        horizon_end = (now.astimezone(UTC) + timedelta(hours=6)).astimezone(now.tzinfo)
        assert intervals[-1].interval_end == horizon_end
        assert all(item.interval_end <= horizon_end for item in intervals)

    async def test_a_midnight_crossing_window_wraps_into_the_next_day(self) -> None:
        night = TariffRateWindow(
            window_local=("22:00", "04:00"), import_cents_per_kwh=10.0, export_cents_per_kwh=4.0
        )
        provider = _provider(windows=(night,))
        intervals = await provider.tariff_intervals()
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(ZONE)
        day = (_local_now() + timedelta(hours=2)).astimezone(zone).date()
        late = datetime(day.year, day.month, day.day, 23, 0, tzinfo=zone)
        early = datetime(day.year, day.month, day.day, 2, 0, tzinfo=zone) + timedelta(days=1)
        assert any(
            item.interval_start <= late < item.interval_end and item.import_cents_per_kwh == 10.0
            for item in intervals
        )
        assert any(
            item.interval_start <= early < item.interval_end and item.import_cents_per_kwh == 10.0
            for item in intervals
        )

    async def test_the_metadata_is_honest_about_being_a_declaration(self) -> None:
        intervals = await _provider().tariff_intervals()
        assert {item.currency for item in intervals} == {"AUD"}
        assert {item.market for item in intervals} == {"static-config"}
        assert {item.quality for item in intervals} == {"static"}
        assert all(item.published_at is None for item in intervals)


class TestDstHonesty:
    async def test_a_spring_forward_day_stays_contiguous(self) -> None:
        # Sydney springs forward on 2026-10-04 (02:00 AEST -> 03:00 AEDT).
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("Australia/Sydney")
        now = datetime(2026, 10, 3, 8, 0, tzinfo=zone)
        provider = _provider(zone="Australia/Sydney", now=now, horizon_s=48 * 3600.0)
        intervals = await provider.tariff_intervals()
        horizon_end = (now.astimezone(UTC) + timedelta(hours=48)).astimezone(now.tzinfo)
        for earlier, later in pairwise(intervals):
            assert earlier.interval_end == later.interval_start
        assert intervals[-1].interval_end == horizon_end
        # 48 real hours across a 23 h day; the sum of the interval spans is
        # exactly the horizon (contiguity + exact total is the DST proof).
        spans = sum((item.interval_end - item.interval_start).total_seconds() for item in intervals)
        assert spans == pytest.approx(48 * 3600.0)

    async def test_an_autumn_repeat_day_stays_contiguous(self) -> None:
        # Sydney falls back on 2026-04-05 (03:00 AEDT -> 02:00 AEST).
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("Australia/Sydney")
        now = datetime(2026, 4, 4, 8, 0, tzinfo=zone)
        provider = _provider(zone="Australia/Sydney", now=now, horizon_s=48 * 3600.0)
        intervals = await provider.tariff_intervals()
        for earlier, later in pairwise(intervals):
            assert earlier.interval_end == later.interval_start
        spans = sum((item.interval_end - item.interval_start).total_seconds() for item in intervals)
        assert spans == pytest.approx(48 * 3600.0)  # 48 real hours over the 25 h day


class TestValidation:
    def test_overlapping_windows_are_refused(self) -> None:
        first = TariffRateWindow(
            window_local=("00:00", "06:00"), import_cents_per_kwh=12.0, export_cents_per_kwh=5.0
        )
        second = TariffRateWindow(
            window_local=("05:00", "07:00"), import_cents_per_kwh=20.0, export_cents_per_kwh=6.0
        )
        with pytest.raises(ValueError, match="overlap"):
            _provider(windows=(first, second))

    def test_window_walls_and_rates_are_validated(self) -> None:
        with pytest.raises(ValueError, match="window_local"):
            TariffRateWindow(
                window_local=("00:00", "00:00"),
                import_cents_per_kwh=12.0,
                export_cents_per_kwh=5.0,
            )
        with pytest.raises(ValueError, match="wall"):
            TariffRateWindow(
                window_local=("25:00", "06:00"),
                import_cents_per_kwh=12.0,
                export_cents_per_kwh=5.0,
            )
        with pytest.raises(ValueError, match="non-negative"):
            TariffRateWindow(
                window_local=("00:00", "06:00"),
                import_cents_per_kwh=-1.0,
                export_cents_per_kwh=5.0,
            )

    def test_the_horizon_and_default_rates_are_validated(self) -> None:
        with pytest.raises(ValueError, match="horizon_s"):
            _provider(horizon_s=0.0)
        with pytest.raises(ValueError, match="default_import"):
            StaticTariffProvider(
                clock=ManualClock(_local_now()),
                timezone_name=ZONE,
                currency="AUD",
                default_import_cents_per_kwh=-1.0,
                default_export_cents_per_kwh=9.0,
                windows=(),
                horizon_s=HORIZON_S,
            )


def test_the_static_schedule_satisfies_the_tariff_port() -> None:
    assert isinstance(_provider(), TariffProvider)
