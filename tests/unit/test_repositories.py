"""Repository contracts for bounded memory state and durable SQLite facts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time
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
        SQLiteScheduleRepository,
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
    SQLiteScheduleRepository: Any = None
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
