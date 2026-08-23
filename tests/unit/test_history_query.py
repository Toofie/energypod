"""The plant-history query engine (DESIGN_PLANT_HISTORY section 3, H4).

The pinned contracts under test: the parameter bounds (every rule a 422),
the resolution choice at the data horizon, server-side LTTB (membership,
endpoint retention, spike preservation, tie-to-earlier determinism), the
window extremes that survive downsampling, server-computed gaps at both
resolutions, the fleet all-present summation rule, and the step encodings.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from energypod.adapters.persistence.memory import InMemoryTelemetryHistoryRepository
from energypod.domain.history import TelemetrySampleRow

try:
    from energypod.application.history import PlantHistoryControl
except ImportError as exc:  # pragma: no cover - initial red phase only
    PlantHistoryControl: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


UNIT_A = "mid"
UNIT_B = "rhs"
INTERVAL_S = 30.0
BASE = datetime(2026, 8, 25, 6, 0, 0, tzinfo=UTC)


def _require_contract() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The plant-history query contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


def _row(
    unit_id: str,
    sampled_at: datetime,
    *,
    battery_watts: float | None = -1000.0,
    grid_power_w: float | None = 500.0,
    bms_soc_pct: float | None = 50.0,
    quality: str = "good",
    lifecycle: str = "disarmed",
    health_state: str | None = "healthy",
    commanded_source: str | None = None,
    commanded_direction: str | None = None,
    commanded_w: int | None = None,
) -> TelemetrySampleRow:
    return TelemetrySampleRow(
        unit_id=unit_id,
        sampled_at=sampled_at,
        system_soc_pct=bms_soc_pct,
        bms_soc_pct=bms_soc_pct,
        soh_pct=98.0,
        battery_watts=battery_watts,
        grid_power_w=grid_power_w,
        load_power_w=None,
        pack_voltage_v=205.0,
        pack_current_a=1.0,
        cell_min_v=3.3,
        cell_max_v=3.35,
        cell_spread_mv=50.0,
        temperature_min_c=22.0,
        temperature_max_c=27.0,
        dynamic_charge_limit_w=2500.0,
        dynamic_discharge_limit_w=2500.0,
        lifecycle=lifecycle,
        health_state=health_state,
        quality=quality,
        commanded_source=commanded_source,
        commanded_direction=commanded_direction,
        commanded_w=commanded_w,
    )


def _control(repository: InMemoryTelemetryHistoryRepository | None = None) -> Any:
    _require_contract()
    return PlantHistoryControl(
        unit_ids=(UNIT_A, UNIT_B),
        sample_interval_s=INTERVAL_S,
        retention_full_resolution_days=14,
        repository=repository if repository is not None else InMemoryTelemetryHistoryRepository(),
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat()


def _seed_ramp(
    repository: InMemoryTelemetryHistoryRepository,
    *,
    unit_id: str = UNIT_A,
    count: int = 100,
    start: datetime = BASE,
    step_s: float = INTERVAL_S,
    skip: int | None = None,
) -> list[TelemetrySampleRow]:
    rows = [
        _row(unit_id, start + timedelta(seconds=step_s * index), battery_watts=-1000.0 - index)
        for index in range(count)
        if index != skip
    ]
    repository.append_samples(rows)
    return rows


def test_lttb_pins_membership_endpoints_spikes_and_tie_breaks() -> None:
    """DESIGN section 3.2: every emitted point is a REAL stored sample, the
    first and last samples of the window are always retained, a lone spike
    survives (the largest triangle wins), and ties break toward the earlier
    sample."""
    _require_contract()
    repository = InMemoryTelemetryHistoryRepository()
    # A ramp with one lone spike at index 50, far off the ramp.
    rows = [
        _row(
            UNIT_A,
            BASE + timedelta(seconds=INTERVAL_S * index),
            battery_watts=-50_000.0 if index == 50 else -1000.0 - index,
        )
        for index in range(100)
    ]
    repository.append_samples(tuple(rows))

    from energypod.application.history import _lttb_indices

    moments = [BASE + timedelta(seconds=INTERVAL_S * index) for index in range(100)]
    xs = [moment.timestamp() for moment in moments]
    ys = [-50_000.0 if index == 50 else -1000.0 - index for index in range(100)]
    selected = _lttb_indices(xs, ys, 5)
    assert len(selected) == 5
    assert selected[0] == 0 and selected[-1] == 99, "no edge erosion"
    assert 50 in selected, "the spike's triangle wins its bucket"
    assert len(set(selected)) == len(selected), "one point per bucket"

    emitted = [(moments[index], ys[index]) for index in selected]
    stored = {(row.sampled_at, row.battery_watts) for row in rows}
    for moment, value in emitted:
        assert (moment, value) in stored, "every emitted point is a stored sample"

    # A perfectly flat series ties everywhere: every bucket must select its
    # EARLIEST member -- the tie-to-earlier pin, checked by exact timestamps.
    flat_repository = InMemoryTelemetryHistoryRepository()
    for index in range(100):
        flat_repository.append_samples(
            (_row(UNIT_A, BASE + timedelta(seconds=INTERVAL_S * index), battery_watts=-7.0),)
        )
    from energypod.application.history import _lttb_indices

    flat_selected = _lttb_indices(xs, [-7.0] * 100, 5)
    assert flat_selected[1:4] == [1, 33, 66], (
        "the earliest member of each bucket wins the all-equal tie"
    )
    # The same pinned behavior through the public surface at the API floor.
    flat = _control(flat_repository).query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(seconds=INTERVAL_S * 99)),
        fields=("battery_watts",),
        points=50,
    )
    flat_points = [
        point["t"] for point in flat["units"][UNIT_A]["series"]["battery_watts"]["points"]
    ]
    assert flat_points[0] == _iso(BASE)
    assert flat_points[-1] == _iso(BASE + timedelta(seconds=INTERVAL_S * 99))
    assert len(flat_points) == 50


def test_window_extremes_cover_every_row_even_off_the_downsample() -> None:
    """DESIGN section 3.2: window_min/max (+ their timestamps) and the
    series sample_count come from EVERY row in the window, so a peak the
    downsample drops is still reported."""
    _require_contract()
    repository = InMemoryTelemetryHistoryRepository()
    rows = _seed_ramp(repository, count=600, skip=137)
    peak = _row(UNIT_A, BASE + timedelta(seconds=INTERVAL_S * 137), battery_watts=-9_999.0)
    repository.append_samples((peak,))

    body = _control(repository).query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(seconds=INTERVAL_S * 599)),
        fields=("battery_watts",),
        points=50,
    )
    series = body["units"][UNIT_A]["series"]["battery_watts"]
    assert series["sample_count"] == 600
    assert series["window_min"] == -9_999.0
    assert series["window_min_at"] == _iso(peak.sampled_at)
    # The descending ramp's maximum sits at the FIRST row.
    assert series["window_max"] == rows[0].battery_watts
    assert series["window_max_at"] == _iso(rows[0].sampled_at)
    assert len(series["points"]) == 50


def test_null_values_are_skipped_never_zeroed() -> None:
    """DESIGN section 3.3: a series whose field was absent in a sample skips
    that sample's point -- an absent datum is not zero."""
    _require_contract()
    repository = InMemoryTelemetryHistoryRepository()
    for index in range(10):
        repository.append_samples(
            (
                _row(
                    UNIT_A,
                    BASE + timedelta(seconds=INTERVAL_S * index),
                    grid_power_w=None if index in (3, 4, 5) else 500.0,
                ),
            )
        )

    body = _control(repository).query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(seconds=INTERVAL_S * 9)),
        fields=("grid_power_w",),
        points=50,
    )
    series = body["units"][UNIT_A]["series"]["grid_power_w"]
    timestamps = [point["t"] for point in series["points"]]
    assert _iso(BASE + timedelta(seconds=INTERVAL_S * 3)) not in timestamps
    assert all(point["v"] != 0 for point in series["points"])
    assert series["sample_count"] == 7


def test_the_resolution_boundary_sits_at_the_data_horizon() -> None:
    """DESIGN section 3.1: ``from`` at or after the oldest retained
    full-resolution sample answers ``full``; anything earlier answers
    ``hourly`` for the whole window -- one resolution per response."""
    _require_contract()
    repository = InMemoryTelemetryHistoryRepository(retention_full_resolution_s=3_600.0)
    # Six rows in the 04:00 hour (rolled + pruned by the pass below) and two
    # retained rows at 06:00 -- the data horizon sits at 06:00:00.
    for index in range(6):
        repository.append_samples(
            (
                _row(
                    UNIT_A,
                    BASE - timedelta(hours=2) + timedelta(seconds=INTERVAL_S * index),
                    battery_watts=-1000.0 - index,
                ),
            )
        )
    for index in range(2):
        repository.append_samples((_row(UNIT_A, BASE + timedelta(seconds=INTERVAL_S * index)),))
    repository.maintain(BASE + timedelta(hours=1, minutes=30))
    assert repository.oldest_full_res_at() == BASE

    control = _control(repository)
    full = control.query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(minutes=10)),
        fields=("battery_watts",),
    )
    assert full["resolution"] == "full"
    assert full["units"][UNIT_A]["sample_count"] == 2

    hourly = control.query_payload(
        range_from=_iso(BASE - timedelta(hours=3)),
        range_to=_iso(BASE + timedelta(minutes=10)),
        fields=("battery_watts",),
    )
    assert hourly["resolution"] == "hourly", (
        "a window opening before the horizon serves hourly for its entirety"
    )
    hours = hourly["units"][UNIT_A]["series"]["battery_watts"]["points"]
    assert [point["t"] for point in hours] == [_iso(BASE - timedelta(hours=2))]
    assert hours[0]["n"] == 6
    assert hours[0]["min"] == -1005.0
    assert hours[0]["max"] == -1000.0
    assert hours[0]["v"] == pytest.approx(-1002.5)
    # An empty window before any data is a 200 with nulls, never an error.
    empty = _control().query_payload(
        range_from=_iso(BASE - timedelta(days=2)),
        range_to=_iso(BASE - timedelta(days=1)),
        fields=("battery_watts",),
    )
    assert empty["resolution"] == "full"
    assert empty["units"][UNIT_A]["first_sample_at"] is None
    assert empty["units"][UNIT_A]["series"]["battery_watts"]["points"] == []


def test_gaps_are_server_computed_at_both_resolutions() -> None:
    """DESIGN section 3.4: full resolution -- row spacing beyond 3 x the
    cadence is a gap (2 x is not); hourly -- any missing hour strictly
    between the first and last rollup hours is a gap; leading/trailing
    emptiness is never a gap entry."""
    _require_contract()
    repository = InMemoryTelemetryHistoryRepository()
    # 06:00:00 .. 06:02:00, a 105 s hole (3.5 x), then 06:03:45 .. 06:04:15
    # with a 60 s hole (2 x, not a gap), ending before the window's end.
    stamps = [0, 30, 60, 165, 195, 225]
    for index in stamps:
        repository.append_samples((_row(UNIT_A, BASE + timedelta(seconds=index)),))

    control = _control(repository)
    full = control.query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(minutes=10)),
        fields=("battery_watts",),
    )
    assert full["units"][UNIT_A]["gaps"] == [
        {
            "from": _iso(BASE + timedelta(seconds=60)),
            "to": _iso(BASE + timedelta(seconds=165)),
        }
    ]

    seeded = InMemoryTelemetryHistoryRepository()
    for hour in (2, 3, 5, 6):
        for minute in range(0, 60, 30):
            seeded.append_samples((_row(UNIT_A, BASE + timedelta(hours=hour, minutes=minute)),))
    seeded.maintain(BASE + timedelta(days=30))
    rolled = _control(seeded).query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(hours=7)),
        fields=("battery_watts",),
    )
    assert rolled["resolution"] == "hourly"
    assert rolled["units"][UNIT_A]["gaps"] == [
        {
            "from": _iso(BASE + timedelta(hours=4)),
            "to": _iso(BASE + timedelta(hours=5)),
        }
    ]


def test_fleet_sums_only_where_every_unit_has_a_row() -> None:
    """DESIGN section 3.5: the sum runs over RAW rows first, then LTTB
    downsamples the summed series; a fleet point exists only where EVERY
    unit in the set has a row -- one unreadable unit is never zero."""
    _require_contract()
    repository = InMemoryTelemetryHistoryRepository()
    shared = [0, 30, 60]
    for index in shared:
        repository.append_samples(
            (
                _row(UNIT_A, BASE + timedelta(seconds=index), grid_power_w=100.0),
                _row(UNIT_B, BASE + timedelta(seconds=index), grid_power_w=50.0),
            )
        )
    # UNIT_B alone at 90 s and 120 s: no fleet point may exist there.
    for index in (90, 120):
        repository.append_samples(
            (_row(UNIT_B, BASE + timedelta(seconds=index), grid_power_w=7.0),)
        )

    body = _control(repository).query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(seconds=150)),
        fields=("grid_power_w", "battery_watts"),
    )
    fleet = body["fleet"]
    grid = fleet["series"]["grid_power_w"]
    assert [(point["t"], point["v"]) for point in grid["points"]] == [
        (_iso(BASE + timedelta(seconds=index)), 150.0) for index in shared
    ]
    assert grid["window_min"] == 150.0 and grid["window_max"] == 150.0
    # battery_watts was requested but grid was not the only flow field: both
    # requested flow fields appear in the fleet block.
    assert set(fleet["series"]) == {"grid_power_w", "battery_watts"}
    # A 60 s hole in the INTERSECTION (3.5 x boundary at 90 s cadence) -- no
    # fleet gap here because the intersection spacing stays under the bound.
    assert fleet["gaps"] == []


def test_step_encodings_carry_first_sample_and_change_points() -> None:
    """DESIGN section 3.3: lifecycle/health_state/commanded arrive as
    change-point arrays; the first sample is always present, an entry only
    where the value differs from the previous one, and the commanded null
    triple is itself a recorded state."""
    _require_contract()
    repository = InMemoryTelemetryHistoryRepository()
    for index in range(6):
        repository.append_samples(
            (
                _row(
                    UNIT_A,
                    BASE + timedelta(seconds=INTERVAL_S * index),
                    lifecycle="disarmed" if index < 3 else "active",
                    health_state="healthy" if index < 4 else "self_healing",
                    commanded_source=None
                    if index < 2
                    else ("night_adviser" if index < 5 else "manual"),
                    commanded_direction=None if index < 2 else "charge",
                    commanded_w=None if index < 2 else 2500,
                ),
            )
        )

    body = _control(repository).query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(seconds=INTERVAL_S * 5)),
        fields=("lifecycle", "health_state", "commanded"),
        points=50,
    )
    unit = body["units"][UNIT_A]
    assert [entry["t"] for entry in unit["lifecycle_changes"]] == [
        _iso(BASE),
        _iso(BASE + timedelta(seconds=INTERVAL_S * 3)),
    ]
    assert [entry["v"] for entry in unit["lifecycle_changes"]] == ["disarmed", "active"]
    assert [entry["v"] for entry in unit["health_state_changes"]] == [
        "healthy",
        "self_healing",
    ]
    assert unit["commanded_changes"] == [
        {
            "t": _iso(BASE),
            "source": None,
            "direction": None,
            "watts": None,
        },
        {
            "t": _iso(BASE + timedelta(seconds=INTERVAL_S * 2)),
            "source": "night_adviser",
            "direction": "charge",
            "watts": 2500,
        },
        {
            "t": _iso(BASE + timedelta(seconds=INTERVAL_S * 5)),
            "source": "manual",
            "direction": "charge",
            "watts": 2500,
        },
    ]
    assert unit["series"] == {}, "meta-fields select step encodings, not point series"


@pytest.mark.parametrize(
    "overrides",
    [
        {"range_from": "2026-08-25T06:00:00"},  # naive: an implicit zone is a silent lie
        {"range_to": "2026-08-25T07:00:00"},
        {"range_from": "not-a-timestamp"},
        {"range_from": "2026-08-25T07:00:00Z", "range_to": "2026-08-25T06:00:00Z"},
        {"range_from": "2026-08-24T06:00:00Z", "range_to": "2026-09-30T06:00:00Z"},
        {"unit_ids": ("ghost",)},
        {"fields": ("watts",)},
        {"points": 49},
        {"points": 2001},
        {"points": 0},
    ],
)
def test_every_parameter_rule_is_a_validation_error(overrides: dict[str, Any]) -> None:
    """DESIGN section 3.1: the 422 matrix -- offsets required, from < to,
    window <= 31 days, known units, known fields, points 50..2000."""
    _require_contract()
    request: dict[str, Any] = {
        "range_from": "2026-08-25T06:00:00Z",
        "range_to": "2026-08-25T07:00:00Z",
    }
    request.update(overrides)
    with pytest.raises(ValueError) as caught:
        _control().query_payload(**request)
    message = str(caught.value)
    expected_subject = {
        "range_from": "from",
        "range_to": "to",
        "unit_ids": "unit",
        "fields": "field",
        "points": "points",
    }
    subject = expected_subject[next(key for key in overrides if key in expected_subject)]
    assert subject in message.lower(), f"the refusal must name the parameter: {message!r}"


def test_the_default_fields_and_unit_set_and_points_echo() -> None:
    """DESIGN section 3.1: the defaults (five fields, all configured units,
    600 points) and the exact request echo in the response."""
    _require_contract()
    repository = InMemoryTelemetryHistoryRepository()
    _seed_ramp(repository, count=3)
    body = _control(repository).query_payload(
        range_from=_iso(BASE), range_to=_iso(BASE + timedelta(hours=1))
    )
    assert body["fields"] == [
        "bms_soc_pct",
        "battery_watts",
        "grid_power_w",
        "temperature_min_c",
        "temperature_max_c",
    ]
    assert set(body["units"]) == {UNIT_A, UNIT_B}
    assert body["points"] == 600
    assert body["resolution"] == "full"
    assert body["from"] == "2026-08-25T06:00:00+00:00"
    assert body["to"] == "2026-08-25T07:00:00+00:00"
    unit = body["units"][UNIT_A]
    assert unit["sample_count"] == 3
    assert unit["quality_worst"] == "good"
    assert unit["first_sample_at"] == _iso(BASE)
    # A unit filter narrows both the unit block and the fleet intersection.
    narrowed = _control(repository).query_payload(
        range_from=_iso(BASE),
        range_to=_iso(BASE + timedelta(hours=1)),
        unit_ids=(UNIT_A,),
        fields=("battery_watts",),
    )
    assert set(narrowed["units"]) == {UNIT_A}
    assert narrowed["fleet"]["series"]["battery_watts"]["window_max"] == -1000.0
