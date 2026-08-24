"""Repository contracts for bounded memory state and durable SQLite facts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest

try:
    from energypod.adapters.persistence.memory import (
        InMemoryAuthorizationRepository,
        InMemoryObservationRepository,
    )
    from energypod.adapters.persistence.sqlite import (
        PersistenceBusyError,
        SQLiteAuditRepository,
        SQLiteDatabase,
        SQLiteEnergyLedgerRepository,
        SQLiteScheduleRepository,
        SQLiteTelemetryHistoryRepository,
    )
    from energypod.domain.audit import AuditEvent, DuplicateAuditEventError
    from energypod.domain.authorization import (
        AuthorizationBatch,
        AuthorizedSetpoint,
        StaleGenerationError,
    )
    from energypod.domain.intents import Direction, IntentSource
    from energypod.domain.observations import DataQuality, Observation, UnitLifecycle
    from energypod.domain.schedule import (
        ScheduleEntry,
        SchedulePlan,
        ScheduleValidationError,
        ScheduleVersionConflict,
        Weekday,
    )
except ImportError as exc:  # pragma: no cover - initial red phase only
    InMemoryAuthorizationRepository: Any = None
    InMemoryObservationRepository: Any = None
    SQLiteAuditRepository: Any = None
    PersistenceBusyError: Any = None
    SQLiteDatabase: Any = None
    SQLiteEnergyLedgerRepository: Any = None
    SQLiteScheduleRepository: Any = None
    SQLiteTelemetryHistoryRepository: Any = None
    AuditEvent: Any = None
    DuplicateAuditEventError: Any = None
    AuthorizationBatch: Any = None
    AuthorizedSetpoint: Any = None
    StaleGenerationError: Any = None
    Direction: Any = None
    IntentSource: Any = None
    DataQuality: Any = None
    Observation: Any = None
    UnitLifecycle: Any = None
    ScheduleEntry: Any = None
    SchedulePlan: Any = None
    ScheduleValidationError: Any = None
    ScheduleVersionConflict: Any = None
    Weekday: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:
    _CONTRACT_IMPORT_ERROR = None


def _require_contract() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The intended repository contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


def _observation(unit_id: str, sequence: int, *, epoch: int = 1) -> Any:
    _require_contract()
    return Observation(
        unit_id=unit_id,
        device_identity=f"BEP-{unit_id.upper()}",
        wall_timestamp=datetime(2026, 8, 21, 1, sequence % 60, tzinfo=UTC),
        captured_at_mono=100.0 + sequence,
        connection_epoch=epoch,
        sequence=sequence,
        lifecycle=UnitLifecycle.DISARMED,
        protocol_profile="iot",
        system_soc_pct=50.0,
        bms_soc_pct=50.0,
        soh_pct=98.0,
        battery_watts=0.0,
        pack_voltage_v=205.0,
        pack_current_a=0.0,
        dynamic_charge_limit_w=2500.0,
        dynamic_discharge_limit_w=2500.0,
        expected_cell_count=3,
        cell_voltages_v=(3.40, 3.41, 3.39),
        expected_temperature_count=2,
        temperatures_c=(24.0, 25.0),
        active_faults=frozenset(),
        active_warnings=frozenset(),
        quality={
            "system_soc_pct": DataQuality.GOOD,
            "bms_soc_pct": DataQuality.GOOD,
            "soh_pct": DataQuality.GOOD,
            "battery_watts": DataQuality.GOOD,
            "pack_voltage_v": DataQuality.GOOD,
            "pack_current_a": DataQuality.GOOD,
            "dynamic_charge_limit_w": DataQuality.GOOD,
            "dynamic_discharge_limit_w": DataQuality.GOOD,
            "cell_voltages_v": DataQuality.GOOD,
            "temperatures_c": DataQuality.GOOD,
        },
    )


def _authorization(
    unit_id: str,
    generation: int,
    *,
    cycle_id: str | None = None,
    issued: float = 10.0,
    not_before: float = 10.0,
    expires: float = 11.0,
) -> Any:
    _require_contract()
    return AuthorizedSetpoint(
        unit_id=unit_id,
        connection_epoch=1,
        generation=generation,
        cycle_id=cycle_id or f"cycle-{generation}",
        intent_id=f"intent-{generation}",
        intent_revision=1,
        direction=Direction.DISCHARGE,
        watts=500,
        reactive_vars=0,
        issued_at_mono=issued,
        not_before_mono=not_before,
        expires_at_mono=expires,
        observation_sequence=12,
        maximum_observation_age_s=0.5,
        policy_version="3",
        configuration_version=7,
        decision_id=f"decision-{generation}",
    )


def _batch(*authorizations: Any) -> Any:
    _require_contract()
    if not authorizations:
        raise ValueError("test batch requires at least one authorization")
    first = authorizations[0]
    return AuthorizationBatch(
        cycle_id=first.cycle_id,
        generation=first.generation,
        authorizations=tuple(authorizations),
    )


def _audit_event(event_id: str, *, unit_id: str, offset: float) -> Any:
    _require_contract()
    return AuditEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 8, 21, 1, 0, tzinfo=UTC),
        monotonic_offset_s=offset,
        process_instance_id="process-1",
        event_type="authorization_decided",
        unit_id=unit_id,
        connection_epoch=2,
        generation=4,
        cycle_id="cycle-8",
        principal="operator@example.test",
        source=IntentSource.MANUAL,
        correlation_id="correlation-1",
        intent_id="intent-1",
        policy_version=3,
        configuration_version=7,
        observation_sequences={unit_id: 42},
        reason_codes=("AUTHORIZED",),
        requested_active_w=1000,
        authorized_active_w=800,
        request_fingerprint="sha256:request",
        response_fingerprint="sha256:response",
        result="accepted",
        lifecycle=UnitLifecycle.ACTIVE,
    )


def _schedule(version: int, *, action: Any = None, entry_id: str = "morning") -> Any:
    _require_contract()
    if action is None:
        action = Direction.CHARGE
    entry = ScheduleEntry(
        entry_id=entry_id,
        days=frozenset({Weekday.MONDAY, Weekday.TUESDAY}),
        start_local=time(9, 0),
        end_local=time(10, 0),
        action=action,
        watts=1400,
        unit_ids=frozenset({"mid", "lhs"}),
        effective_from=date(2026, 1, 1),
        effective_until=date(2026, 12, 31),
        priority=10,
        enabled=True,
    )
    return SchedulePlan(
        version=version,
        timezone="Australia/Brisbane",
        entries=(entry,),
    )


def _open_database(path: Path) -> Any:
    _require_contract()
    database = SQLiteDatabase(path)
    database.open()
    return database


def test_observation_repository_retains_a_bounded_history_per_unit() -> None:
    """T-UNIT-REPO-001 / REQ-OBS-001 / S1."""
    _require_contract()
    repository = InMemoryObservationRepository(max_history_per_unit=2)
    for sequence in (1, 2, 3):
        repository.append(_observation("mid", sequence))
    repository.append(_observation("rhs", 1))

    assert [item.sequence for item in repository.history("mid")] == [2, 3]
    assert [item.sequence for item in repository.history("rhs")] == [1]
    assert repository.latest("mid").sequence == 3
    assert set(repository.all_latest()) == {"mid", "rhs"}


def test_observation_repository_rejects_non_increasing_sequence_in_an_epoch() -> None:
    """T-UNIT-REPO-002 / INV-OBS-001 / S0."""
    _require_contract()
    repository = InMemoryObservationRepository(max_history_per_unit=3)
    repository.append(_observation("mid", 2, epoch=4))

    with pytest.raises(ValueError, match="sequence"):
        repository.append(_observation("mid", 2, epoch=4))
    with pytest.raises(ValueError, match="sequence"):
        repository.append(_observation("mid", 1, epoch=4))

    assert repository.latest("mid").sequence == 2


def test_new_connection_epoch_may_restart_observation_sequence() -> None:
    """T-UNIT-REPO-003 / REQ-OBS-002 / S1."""
    _require_contract()
    repository = InMemoryObservationRepository(max_history_per_unit=4)
    repository.append(_observation("mid", 50, epoch=1))
    repository.append(_observation("mid", 1, epoch=2))

    latest = repository.latest("mid")
    assert latest.connection_epoch == 2
    assert latest.sequence == 1


def test_authorization_is_not_visible_before_not_before_and_expires_at_deadline() -> None:
    """T-UNIT-REPO-004 / INV-AUTH-001 / S0."""
    _require_contract()
    repository = InMemoryAuthorizationRepository()
    repository.publish(_batch(_authorization("mid", 1)))

    assert repository.current("mid", now_monotonic=9.999) is None
    assert repository.current("mid", now_monotonic=10.0) is not None

    repository.publish(_batch(_authorization("rhs", 1)))
    assert repository.current("rhs", now_monotonic=11.0) is None


def test_authorization_is_single_use() -> None:
    """T-UNIT-REPO-005 / INV-AUTH-002 / S0."""
    _require_contract()
    repository = InMemoryAuthorizationRepository()
    expected = _authorization("mid", 1)
    repository.publish(_batch(expected))

    assert repository.current("mid", now_monotonic=10.5) == expected
    assert repository.current("mid", now_monotonic=10.5) is None


def test_new_generation_replaces_old_and_old_generation_cannot_be_republished() -> None:
    """T-UNIT-REPO-006 / INV-AUTH-003 / S0."""
    _require_contract()
    repository = InMemoryAuthorizationRepository()
    repository.publish(_batch(_authorization("mid", 4)))
    newest = _authorization("mid", 5)
    repository.publish(_batch(newest))

    assert repository.current("mid", now_monotonic=10.5) == newest
    assert StaleGenerationError is not None
    with pytest.raises(StaleGenerationError):
        repository.publish(_batch(_authorization("mid", 4)))


def test_consumed_cycle_cannot_be_republished_in_the_same_generation() -> None:
    """T-UNIT-REPO-006A / INV-AUTH-002 / S0: cycle consumption is irreversible."""
    repository = InMemoryAuthorizationRepository()
    authorization = _authorization("mid", 4)
    repository.publish(_batch(authorization))
    assert repository.current("mid", now_monotonic=10.5) == authorization

    assert StaleGenerationError is not None
    with pytest.raises(StaleGenerationError):
        repository.publish(_batch(authorization))


def test_successive_fresh_cycles_are_permitted_in_one_healthy_generation() -> None:
    """A fencing epoch remains stable while each renewal cycle is single-use."""
    repository = InMemoryAuthorizationRepository()
    first = _authorization("mid", 4, cycle_id="cycle-4-a")
    second = _authorization("mid", 4, cycle_id="cycle-4-b")

    repository.publish(_batch(first))
    assert repository.current("mid", now_monotonic=10.5) == first
    repository.publish(_batch(second))
    assert repository.current("mid", now_monotonic=10.5) == second


def test_newer_cycle_replaces_unconsumed_cycle_in_same_generation() -> None:
    """A later healthy renewal atomically supersedes an unused older cycle."""
    repository = InMemoryAuthorizationRepository()
    older = _authorization("mid", 4, cycle_id="cycle-4-a")
    newer = _authorization("mid", 4, cycle_id="cycle-4-b")

    repository.publish(_batch(older))
    repository.publish(_batch(newer))

    assert repository.current("mid", now_monotonic=10.5) == newer
    assert repository.current("mid", now_monotonic=10.5) is None
    with pytest.raises(StaleGenerationError):
        repository.publish(_batch(older))


def test_consumed_cycle_replay_is_rejected_after_later_cycle_in_same_generation() -> None:
    repository = InMemoryAuthorizationRepository()
    consumed = _authorization("mid", 4, cycle_id="cycle-4-a")
    later = _authorization("mid", 4, cycle_id="cycle-4-b")
    repository.publish(_batch(consumed))
    assert repository.current("mid", now_monotonic=10.5) == consumed
    repository.publish(_batch(later))

    with pytest.raises(StaleGenerationError):
        repository.publish(_batch(consumed))


def test_generation_order_dominates_later_issue_times() -> None:
    """T-UNIT-REPO-006B / INV-AUTH-003 / S0: wall or issue time cannot defeat fencing."""
    repository = InMemoryAuthorizationRepository()
    repository.publish(_batch(_authorization("mid", 8, issued=10.0)))

    assert StaleGenerationError is not None
    with pytest.raises(StaleGenerationError):
        repository.publish(
            _batch(_authorization("mid", 7, issued=1000.0, not_before=1000.0, expires=1001.0))
        )


def test_revocation_fences_same_and_older_generations() -> None:
    """T-UNIT-REPO-007 / INV-AUTH-004 / S0."""
    _require_contract()
    repository = InMemoryAuthorizationRepository()
    repository.publish(_batch(_authorization("mid", 7)))
    repository.revoke(unit_id="mid", generation=8)

    assert repository.current("mid", now_monotonic=10.5) is None
    assert StaleGenerationError is not None
    with pytest.raises(StaleGenerationError):
        repository.publish(_batch(_authorization("mid", 8)))
    generation_9 = _authorization("mid", 9)
    repository.publish(_batch(generation_9))
    assert repository.current("mid", now_monotonic=10.5) == generation_9


def test_authorization_batch_publication_is_atomic_when_one_generation_is_stale() -> None:
    """T-UNIT-REPO-008 / INV-AUTH-005 / S0."""
    _require_contract()
    repository = InMemoryAuthorizationRepository()
    repository.revoke(unit_id="mid", generation=3)
    mixed = _batch(_authorization("mid", 3), _authorization("rhs", 3))

    assert StaleGenerationError is not None
    with pytest.raises(StaleGenerationError):
        repository.publish(mixed)
    assert repository.current("rhs", now_monotonic=10.5) is None


def test_sqlite_database_enables_wal_foreign_keys_and_configured_busy_timeout(
    tmp_path: Path,
) -> None:
    """T-UNIT-REPO-009 / REQ-DB-001 / S1."""
    path = tmp_path / "state.sqlite3"
    _require_contract()
    database = SQLiteDatabase(path, busy_timeout_ms=37)
    database.open()
    try:
        assert database.pragma("journal_mode").lower() == "wal"
        assert database.pragma("foreign_keys") == 1
        assert database.pragma("busy_timeout") == 37
    finally:
        database.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_sqlite_audit_append_query_is_durable_ordered_and_filterable(tmp_path: Path) -> None:
    """T-UNIT-REPO-010 / REQ-AUDIT-001 / S1."""
    path = tmp_path / "audit.sqlite3"
    database = _open_database(path)
    repository = SQLiteAuditRepository(database)
    first = _audit_event("event-1", unit_id="mid", offset=1.0)
    second = _audit_event("event-2", unit_id="rhs", offset=2.0)
    third = _audit_event("event-3", unit_id="mid", offset=3.0)
    try:
        for event in (first, second, third):
            repository.append(event)
        assert repository.recent(limit=2) == (third, second)
        assert repository.recent(limit=10, unit_id="mid") == (third, first)
    finally:
        database.close()

    reopened = _open_database(path)
    try:
        assert SQLiteAuditRepository(reopened).recent(limit=3) == (third, second, first)
    finally:
        reopened.close()


def test_sqlite_json_encoding_of_sets_is_deterministic() -> None:
    # CONTINUITY deferred P2: a set's iteration order is process-randomized
    # (string hash randomization), so an unordered encode persisted different
    # bytes for the same event value in different processes.  Sets encode in
    # sorted (repr) order; sequences keep the caller's order.
    from energypod.adapters.persistence.sqlite import _json_value

    codes = frozenset({"PCS_Warning0_1", "DCDC_Warning0_1", "BMS_CRITICAL"})
    assert _json_value(codes) == ["BMS_CRITICAL", "DCDC_Warning0_1", "PCS_Warning0_1"]
    assert _json_value(set(codes)) == _json_value(frozenset(codes))
    assert _json_value((3, 1, 2)) == [3, 1, 2]


def test_sqlite_audit_rows_from_before_the_per_unit_fields_still_decode(tmp_path: Path) -> None:
    """A durable audit database written by an earlier build must stay readable.

    Rows persisted before the per-unit watt breakdown fields existed carry no
    such keys; decoding fills the documented defaults instead of refusing the
    whole page.  An UNKNOWN key is still refused -- only the omission of
    optional fields is tolerated.
    """
    import json as _json

    path = tmp_path / "audit.sqlite3"
    database = _open_database(path)
    repository = SQLiteAuditRepository(database)
    try:
        event = _audit_event("event-legacy", unit_id="mid", offset=1.0)
        repository.append(event)
        payload = database.connection.execute(
            "SELECT payload FROM audit_events WHERE event_id = 'event-legacy'"
        ).fetchone()[0]
        values = _json.loads(payload)
        legacy = {key: item for key, item in values.items() if "watts_by_unit" not in key}
        database.connection.execute(
            "UPDATE audit_events SET payload = ? WHERE event_id = 'event-legacy'",
            (_json.dumps(legacy, sort_keys=True, separators=(",", ":")),),
        )
        (decoded,) = repository.recent(limit=1)
        assert decoded.event_id == "event-legacy"
        assert decoded.requested_watts_by_unit is None
        assert decoded.authorized_watts_by_unit is None

        unknown = dict(legacy, requested_watts_by_unit={"mid": 100}, mystery_field=1)
        database.connection.execute(
            "UPDATE audit_events SET payload = ? WHERE event_id = 'event-legacy'",
            (_json.dumps(unknown, sort_keys=True, separators=(",", ":")),),
        )
        with pytest.raises(ValueError, match="invalid audit event keys"):
            repository.recent(limit=1)
    finally:
        database.close()


def test_audit_event_ids_are_append_only_and_unique(tmp_path: Path) -> None:
    """T-UNIT-REPO-011 / INV-AUDIT-001 / S1."""
    database = _open_database(tmp_path / "audit.sqlite3")
    repository = SQLiteAuditRepository(database)
    event = _audit_event("event-1", unit_id="mid", offset=1.0)
    try:
        repository.append(event)
        assert DuplicateAuditEventError is not None
        with pytest.raises(DuplicateAuditEventError):
            repository.append(event)
        later = _audit_event("event-2", unit_id="mid", offset=2.0)
        repository.append(later)
        assert repository.recent(limit=10) == (later, event)
    finally:
        database.close()


def test_locked_audit_database_surfaces_typed_backpressure_without_partial_append(
    tmp_path: Path,
) -> None:
    """T-UNIT-REPO-011A / INV-AUDIT-002 / S0: storage pressure fails explicitly."""
    path = tmp_path / "audit.sqlite3"
    database = SQLiteDatabase(path, busy_timeout_ms=1)
    database.open()
    repository = SQLiteAuditRepository(database)
    lock = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        lock.execute("BEGIN IMMEDIATE")
        assert PersistenceBusyError is not None
        with pytest.raises(PersistenceBusyError):
            repository.append(_audit_event("blocked", unit_id="mid", offset=1.0))
        lock.execute("ROLLBACK")
        assert repository.recent(limit=10) == ()
    finally:
        if lock.in_transaction:
            lock.execute("ROLLBACK")
        lock.close()
        database.close()


def test_versioned_schedule_replacement_is_durable_and_preserves_action(tmp_path: Path) -> None:
    """T-UNIT-REPO-012 / REQ-SCHED-REPO-001 / S1."""
    path = tmp_path / "schedule.sqlite3"
    database = _open_database(path)
    repository = SQLiteScheduleRepository(database)
    charge = _schedule(1, action=Direction.CHARGE)
    discharge = _schedule(2, action=Direction.DISCHARGE, entry_id="evening")
    try:
        assert repository.get() is None
        repository.replace(expected_version=0, replacement=charge)
        repository.replace(expected_version=1, replacement=discharge)
        assert repository.get() == discharge
        assert repository.get().entries[0].action is Direction.DISCHARGE
    finally:
        database.close()

    reopened = _open_database(path)
    try:
        assert SQLiteScheduleRepository(reopened).get() == discharge
    finally:
        reopened.close()


def test_stale_schedule_replacement_rolls_back_without_partial_change(tmp_path: Path) -> None:
    """T-UNIT-REPO-013 / INV-SCHED-REPO-001 / S1."""
    database = _open_database(tmp_path / "schedule.sqlite3")
    repository = SQLiteScheduleRepository(database)
    original = _schedule(1)
    stale_replacement = _schedule(2, action=Direction.DISCHARGE, entry_id="stale")
    try:
        repository.replace(expected_version=0, replacement=original)
        assert ScheduleVersionConflict is not None
        with pytest.raises(ScheduleVersionConflict):
            repository.replace(expected_version=0, replacement=stale_replacement)
        assert repository.get() == original
    finally:
        database.close()


def test_schedule_compare_and_swap_is_atomic_across_independent_connections(
    tmp_path: Path,
) -> None:
    """T-UNIT-REPO-013A / INV-SCHED-REPO-001 / S1."""
    path = tmp_path / "schedule.sqlite3"
    first_database = _open_database(path)
    second_database = _open_database(path)
    first = SQLiteScheduleRepository(first_database)
    second = SQLiteScheduleRepository(second_database)
    version_1 = _schedule(1)
    version_2 = _schedule(2, action=Direction.DISCHARGE, entry_id="winner")
    stale_version_2 = _schedule(2, entry_id="loser")
    try:
        first.replace(expected_version=0, replacement=version_1)
        second.replace(expected_version=1, replacement=version_2)
        assert ScheduleVersionConflict is not None
        with pytest.raises(ScheduleVersionConflict):
            first.replace(expected_version=1, replacement=stale_version_2)
        assert first.get() == second.get() == version_2
    finally:
        second_database.close()
        first_database.close()


def test_invalid_replacement_leaves_published_schedule_unchanged(tmp_path: Path) -> None:
    """T-UNIT-REPO-014 / INV-SCHED-REPO-002 / S1."""
    database = _open_database(tmp_path / "schedule.sqlite3")
    repository = SQLiteScheduleRepository(database)
    original = _schedule(1)
    try:
        repository.replace(expected_version=0, replacement=original)
        assert ScheduleValidationError is not None
        with pytest.raises(ScheduleValidationError):
            SchedulePlan(
                version=2,
                timezone="Australia/Brisbane",
                entries=(original.entries[0], original.entries[0]),
            )
        assert repository.get() == original
    finally:
        database.close()


# --- energy scorecard ledger (DESIGN_ENERGY_SCORECARD section 5, E3) -------------


def _energy_day(day: date, *, kind: str = "complete", import_kwh: float = 1.5) -> Any:
    _require_contract()
    from energypod.domain.energy import (
        CounterCrossCheck,
        EnergyDayRecord,
        FleetEnergyDay,
        UnitEnergyDay,
    )

    return EnergyDayRecord(
        date=day,
        timezone="Australia/Brisbane",
        utc_offset_minutes=600,
        kind=kind,
        units={
            "mid": UnitEnergyDay(
                grid_import_kwh=import_kwh,
                grid_export_kwh=2.5,
                battery_charged_kwh=0.8,
                battery_discharged_kwh=0.4,
                load_kwh=3.0,
                charged_from_surplus_kwh=0.6,
                coverage_pct=99.0,
                metric_flags=frozenset({"counter_reset:charge"}),
            )
        },
        fleet=FleetEnergyDay(
            grid_import_kwh=import_kwh,
            grid_export_kwh=2.5,
            battery_charged_kwh=0.8,
            battery_discharged_kwh=0.4,
            load_kwh=3.0,
            charged_from_surplus_kwh=0.6,
            coverage_pct=99.0,
        ),
        sources={
            "grid": "integrated_ct",
            "battery": "device_counter",
            "load": "device_counter",
            "surplus": "attributed_adviser",
        },
        counter_cross_check=CounterCrossCheck(
            grid_a_delta_kwh=import_kwh,
            grid_b_delta_kwh=2.5,
            consistent_with="vendor_labels",
            discriminating=True,
        ),
        solar_production_measured=False,
    )


def _baseline(day: date, unit: str = "mid") -> Any:
    _require_contract()
    from energypod.domain.energy import EnergyUnitBaseline

    return EnergyUnitBaseline(
        date=day,
        counter_start={"charge": 100.0},
        counter_last={"charge": 100.5},
        import_watt_seconds=5000.0,
        export_watt_seconds=2500.0,
        surplus_watt_seconds=0.0,
        sampled_seconds=5.0,
        last_capture_wall=datetime(2026, 8, 26, 4, 0, 0, tzinfo=UTC),
        last_grid_watts=-1000.0,
        flags=frozenset(),
    )


def test_memory_energy_ledger_round_trips_days_and_baseline() -> None:
    """T-UNIT-REPO-015 / DESIGN_ENERGY_SCORECARD section 5 / S1."""
    from energypod.adapters.persistence.memory import InMemoryEnergyLedgerRepository

    repository = InMemoryEnergyLedgerRepository()
    day = date(2026, 8, 26)
    record = _energy_day(day)
    repository.record_day(record)
    repository.record_day(_energy_day(day, kind="partial"))

    assert repository.get_day(day) == record, "record_day is idempotent by date"
    assert repository.latest_days(8) == (record,)
    assert repository.get_day(date(2026, 8, 27)) is None

    repository.save_baseline({"mid": _baseline(day)})
    restored = repository.load_baseline()
    assert set(restored) == {"mid"}
    assert restored["mid"] == _baseline(day)
    repository.save_baseline({})
    assert repository.load_baseline() == {}


def test_memory_energy_ledger_latest_days_is_bounded_and_newest_first() -> None:
    from energypod.adapters.persistence.memory import InMemoryEnergyLedgerRepository

    repository = InMemoryEnergyLedgerRepository()
    for index in range(5):
        repository.record_day(_energy_day(date(2026, 8, 22) + timedelta(days=index)))
    assert [record.date for record in repository.latest_days(3)] == [
        date(2026, 8, 26),
        date(2026, 8, 25),
        date(2026, 8, 24),
    ]
    assert len(repository.latest_days(31)) == 5
    assert repository.latest_days(0) == ()


def test_sqlite_energy_ledger_round_trips_and_is_idempotent_by_date(
    tmp_path: Path,
) -> None:
    """T-UNIT-REPO-016 / the durable day ledger + baseline / S0."""
    database = _open_database(tmp_path / "energy.sqlite3")
    repository = SQLiteEnergyLedgerRepository(database)
    day = date(2026, 8, 26)
    record = _energy_day(day)
    try:
        repository.record_day(record)
        repository.record_day(_energy_day(day, kind="partial", import_kwh=9.9))
        assert repository.get_day(day) == record, "the first record for a date stands"
        assert repository.get_day(date(2026, 8, 25)) is None

        repository.save_baseline({"mid": _baseline(day)})
        second_database = SQLiteDatabase(tmp_path / "energy.sqlite3")
        second_database.open()
        try:
            second = SQLiteEnergyLedgerRepository(second_database)
            restored = second.load_baseline()
            assert set(restored) == {"mid"}
            assert restored["mid"] == _baseline(day)
            assert second.get_day(day) == record
        finally:
            second_database.close()
    finally:
        database.close()


def test_sqlite_energy_ledger_orders_and_bounds_latest_days(tmp_path: Path) -> None:
    database = _open_database(tmp_path / "energy-days.sqlite3")
    repository = SQLiteEnergyLedgerRepository(database)
    try:
        for index in range(4):
            repository.record_day(
                _energy_day(date(2026, 8, 23) + timedelta(days=index), import_kwh=1.0 + index)
            )
        assert [record.date for record in repository.latest_days(2)] == [
            date(2026, 8, 26),
            date(2026, 8, 25),
        ]
        assert len(repository.latest_days(31)) == 4
    finally:
        database.close()


def test_energy_schema_migrates_a_version_one_database_in_place(
    tmp_path: Path,
) -> None:
    """T-UNIT-REPO-017 / schema_version migration from the prior version / S0.

    A database stamped at version 1 (the audit + schedule schema) upgrades in
    place to the energy-ledger schema without touching the existing rows.
    """
    from energypod.db.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION >= 2
    path = tmp_path / "migrate.sqlite3"
    raw = sqlite3.connect(path)
    try:
        raw.execute(
            "CREATE TABLE schema_version ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1),"
            "version INTEGER NOT NULL UNIQUE)"
        )
        raw.execute("INSERT INTO schema_version(singleton, version) VALUES (1, 1)")
        raw.execute(
            "CREATE TABLE audit_events ("
            "sequence INTEGER PRIMARY KEY AUTOINCREMENT,"
            "event_id TEXT NOT NULL UNIQUE,"
            "unit_id TEXT,"
            "monotonic_offset_s REAL NOT NULL,"
            "payload TEXT NOT NULL)"
        )
        raw.commit()
    finally:
        raw.close()

    database = _open_database(path)
    try:
        repository = SQLiteEnergyLedgerRepository(database)
        day = date(2026, 8, 26)
        repository.record_day(_energy_day(day))
        stamped = database.connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()
        assert stamped == (SCHEMA_VERSION,)
        assert database.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone() == (0,)
        assert repository.get_day(day) is not None
    finally:
        database.close()


def test_sqlite_energy_ledger_refuses_malformed_rows(tmp_path: Path) -> None:
    """An unreadable or unknown-key payload fails closed at decode."""
    database = _open_database(tmp_path / "energy-bad.sqlite3")
    repository = SQLiteEnergyLedgerRepository(database)
    try:
        with database.connection:
            database.connection.execute(
                "INSERT INTO energy_day(day, payload) VALUES ('2026-08-26', '{}')"
            )
        with pytest.raises(ValueError):
            repository.get_day(date(2026, 8, 26))
    finally:
        database.close()


# --- plant history (DESIGN_PLANT_HISTORY sections 2.2-2.4, H1) -------------------


def _sample_row(
    unit_id: str,
    sampled_at: datetime,
    *,
    battery_watts: float | None = -1500.0,
    bms_soc_pct: float | None = 55.0,
    quality: str = "good",
    health_state: str | None = "healthy",
    commanded_source: str | None = None,
    commanded_direction: str | None = None,
    commanded_w: int | None = None,
    run_mode_w: int | None = 1,
) -> Any:
    _require_contract()
    from energypod.domain.history import TelemetrySampleRow

    return TelemetrySampleRow(
        unit_id=unit_id,
        sampled_at=sampled_at,
        system_soc_pct=54.0 if bms_soc_pct is not None else None,
        bms_soc_pct=bms_soc_pct,
        soh_pct=98.0,
        battery_watts=battery_watts,
        grid_power_w=None,
        load_power_w=None,
        pack_voltage_v=205.5,
        pack_current_a=-7.3,
        cell_min_v=3.30,
        cell_max_v=3.35,
        cell_spread_mv=50.0,
        temperature_min_c=22.0,
        temperature_max_c=27.5,
        dynamic_charge_limit_w=2500.0,
        dynamic_discharge_limit_w=2500.0,
        lifecycle="disarmed",
        health_state=health_state,
        quality=quality,
        commanded_source=commanded_source,
        commanded_direction=commanded_direction,
        commanded_w=commanded_w,
        debug_mode_w=0,
        ctrl_mode_w=1,
        work_mode_w=None,
        run_mode_w=run_mode_w,
    )


def _history_sqlite(database: Any) -> Any:
    _require_contract()
    return SQLiteTelemetryHistoryRepository(database)


def _history_memory(retention_s: float = 14 * 86_400.0) -> Any:
    _require_contract()
    from energypod.adapters.persistence.memory import InMemoryTelemetryHistoryRepository

    return InMemoryTelemetryHistoryRepository(retention_full_resolution_s=retention_s)


def test_history_schema_migrates_a_version_two_database_in_place(tmp_path: Path) -> None:
    """DESIGN_PLANT_HISTORY section 2.2 / schema_version 3 migration in place.

    A database stamped at version 2 (the energy-ledger schema, with live rows)
    upgrades in place: the stamped version becomes the latest (v4 -- the
    parking lease table rode v4), the energy ledger rows are untouched, and
    both history tables exist.
    """
    from energypod.db.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 4
    path = tmp_path / "history-migrate.sqlite3"
    raw = sqlite3.connect(path)
    try:
        raw.execute(
            "CREATE TABLE schema_version ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1),"
            "version INTEGER NOT NULL UNIQUE)"
        )
        raw.execute("INSERT INTO schema_version(singleton, version) VALUES (1, 2)")
        raw.execute("CREATE TABLE energy_day (day TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        raw.execute("INSERT INTO energy_day(day, payload) VALUES ('2026-08-25', '{}')")
        raw.commit()
    finally:
        raw.close()

    database = _open_database(path)
    try:
        stamped = database.connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()
        assert stamped == (4,)
        assert database.connection.execute("SELECT COUNT(*) FROM energy_day").fetchone() == (1,)
        tables = {
            name
            for (name,) in database.connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert {"telemetry_sample", "telemetry_rollup_hourly"} <= tables
        # The sample table clusters on its primary key (WITHOUT ROWID): the
        # design's clustering pin, checked structurally.
        sql = str(
            database.connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name = 'telemetry_sample'"
            ).fetchone()[0]
        ).lower()
        assert "without rowid" in sql
    finally:
        database.close()


def test_telemetry_history_round_trips_rows_with_nulls_verbatim(tmp_path: Path) -> None:
    """DESIGN_PLANT_HISTORY sections 2.1-2.2 / append + windowed query / S0.

    One batch append (the per-tick executemany), then the windowed range scan:
    every null stays null, every value round-trips exactly, ordering is unit
    then time, and the window bounds are inclusive.  Both adapters answer
    identically.
    """
    rows = (
        _sample_row("lhs", datetime(2026, 8, 25, 6, 0, 30, tzinfo=UTC)),
        _sample_row("mid", datetime(2026, 8, 25, 6, 0, 30, tzinfo=UTC), battery_watts=None),
        _sample_row("mid", datetime(2026, 8, 25, 6, 30, 0, tzinfo=UTC)),
        _sample_row("mid", datetime(2026, 8, 25, 7, 0, 0, tzinfo=UTC), quality="stale"),
        _sample_row(
            "mid",
            datetime(2026, 8, 25, 7, 30, 0, tzinfo=UTC),
            commanded_source="night_adviser",
            commanded_direction="charge",
            commanded_w=2500,
        ),
        _sample_row("mid", datetime(2026, 8, 26, 8, 0, 0, tzinfo=UTC)),
    )
    database = _open_database(tmp_path / "history.sqlite3")
    try:
        repositories = [_history_sqlite(database), _history_memory()]
        for repository in repositories:
            repository.append_samples(rows)
        window = (
            datetime(2026, 8, 25, 6, 0, 30, tzinfo=UTC),
            datetime(2026, 8, 25, 7, 30, 0, tzinfo=UTC),
        )
        for repository in repositories:
            selected = repository.samples(("mid", "lhs"), window[0], window[1])
            assert selected == (rows[0], rows[1], rows[2], rows[3], rows[4]), (
                "ascending unit id, ascending time, inclusive bounds, nulls verbatim"
            )
            assert repository.samples(("mid",), window[0] + timedelta(seconds=1), window[1]) == (
                rows[2],
                rows[3],
                rows[4],
            )
            assert repository.oldest_full_res_at() == rows[0].sampled_at
            assert repository.last_sample_at(("mid", "lhs", "ghost")) == {
                "mid": rows[5].sampled_at,
                "lhs": rows[0].sampled_at,
                "ghost": None,
            }
        # An empty batch is a no-op and a re-append's duplicate primary key is
        # ignored (a retried tick can never corrupt the store).
        repositories[0].append_samples(())
        repositories[0].append_samples(rows)
        assert len(repositories[0].samples(("mid",), window[0], window[1])) == 4
    finally:
        database.close()


def test_history_maintain_rolls_up_then_prunes_idempotently(tmp_path: Path) -> None:
    """DESIGN_PLANT_HISTORY section 2.4 / rollup + prune in one transaction / S0."""
    rows = [
        _sample_row("mid", datetime(2026, 8, 24, 10, 0, 0, tzinfo=UTC) + timedelta(minutes=m))
        for m in range(0, 60, 15)
    ] + [
        # The 12:00 hour holds only two samples (partial coverage is honest
        # coverage: sample_count says so) and one missing-quality row.
        _sample_row(
            "mid",
            datetime(2026, 8, 24, 12, 0, 30, tzinfo=UTC),
            quality="missing",
            battery_watts=None,
        ),
        _sample_row("mid", datetime(2026, 8, 24, 12, 20, 0, tzinfo=UTC), battery_watts=-2000.0),
        # 14:30 stays inside the full-resolution window (never rolled).
        _sample_row("mid", datetime(2026, 8, 24, 14, 30, 0, tzinfo=UTC)),
    ]
    now = datetime(2026, 9, 7, 13, 0, 0, tzinfo=UTC)  # 14 days later: horizon 08-24T13:00
    database = _open_database(tmp_path / "history-maintain.sqlite3")
    try:
        for repository in (_history_sqlite(database), _history_memory()):
            for row in rows:
                repository.append_samples((row,))
            first = repository.maintain(now)
            hours = repository.rollup_hours(
                ("mid",),
                datetime(2026, 8, 24, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC),
            )
            assert [rollup.hour_start for rollup in hours] == [
                datetime(2026, 8, 24, 10, 0, 0, tzinfo=UTC),
                datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC),
            ], "hour 11:00 holds no rows, so no rollup row exists"
            ten, twelve = hours
            assert ten.sample_count == 4
            assert ten.metrics["battery_watts"] == (-1500.0, -1500.0, -1500.0)
            assert ten.metrics["grid_power_w"] == (None, None, None), "all-null rolls to null"
            assert ten.worst_quality == "good"
            assert twelve.sample_count == 2
            assert twelve.metrics["battery_watts"] == (-2000.0, -2000.0, -2000.0)
            assert twelve.worst_quality == "missing"
            remaining = repository.samples(
                ("mid",),
                datetime(2026, 8, 24, 0, 0, 0, tzinfo=UTC),
                datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC),
            )
            assert [row.sampled_at for row in remaining] == [
                datetime(2026, 8, 24, 14, 30, 0, tzinfo=UTC)
            ], "prune deletes exactly the rolled hours (both were < 13:00)"

            second = repository.maintain(now)
            assert second.rolled_hours == 0 and second.pruned_samples == 0, "idempotent"
            assert repository.oldest_full_res_at() == datetime(2026, 8, 24, 14, 30, 0, tzinfo=UTC)
            assert first.rolled_hours == 2 and first.pruned_samples == 6
    finally:
        database.close()


def test_history_prune_only_after_rollup_a_forced_failure_deletes_nothing(
    tmp_path: Path,
) -> None:
    """DESIGN_PLANT_HISTORY section 2.4 step 2 / prune-only-inside-the-commit / S0."""
    database = _open_database(tmp_path / "history-prune.sqlite3")
    try:
        repository = _history_sqlite(database)
        repository.append_samples(
            tuple(
                _sample_row(
                    "mid", datetime(2026, 8, 10, 6, 0, 0, tzinfo=UTC) + timedelta(minutes=m)
                )
                for m in range(0, 90, 30)
            )
        )
        with database.lock:
            database.connection.execute(
                "CREATE TRIGGER refuse_rollup BEFORE INSERT ON telemetry_rollup_hourly"
                " BEGIN SELECT RAISE(ABORT, 'forced rollup failure'); END"
            )
        with pytest.raises(Exception, match="forced rollup failure"):
            repository.maintain(datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC))
        assert (
            len(
                repository.samples(
                    ("mid",),
                    datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC),
                    datetime(2026, 8, 11, 0, 0, 0, tzinfo=UTC),
                )
            )
            == 3
        ), "a failed rollup can never delete the only copy"
    finally:
        database.close()


def test_history_rollup_prunes_old_hours_when_a_rollup_horizon_is_configured(
    tmp_path: Path,
) -> None:
    """DESIGN_PLANT_HISTORY section 2.3 / ``retention_rollup_days`` 0 = forever."""
    database = _open_database(tmp_path / "history-rollup-prune.sqlite3")
    try:
        keep_all = SQLiteTelemetryHistoryRepository(database, retention_full_resolution_s=3600.0)
        bounded = SQLiteTelemetryHistoryRepository(
            database, retention_full_resolution_s=3600.0, retention_rollup_s=7200.0
        )
        base = datetime(2026, 8, 24, 6, 0, 0, tzinfo=UTC)
        for hour in range(4):
            for minute in range(0, 60, 30):
                keep_all.append_samples(
                    (_sample_row("mid", base + timedelta(hours=hour, minutes=minute)),)
                )
        now = base + timedelta(hours=5)
        keep_all.maintain(now)
        assert len(keep_all.rollup_hours(("mid",), base, now)) == 4
        # A bounded rollup retention prunes only hours strictly older than
        # its own horizon: 11:00 - 2 h = 09:00, so hours 06:00..08:00 go.
        bounded.maintain(now)
        remaining = [rollup.hour_start for rollup in bounded.rollup_hours(("mid",), base, now)]
        assert remaining == [base + timedelta(hours=3)]
    finally:
        database.close()
