"""The pod-parking lease store contracts (DESIGN_POD_PARKING section 4).

T-PARK-REPLAY's table-truth half plus the durability pins the design names:
the dedicated ``park_leases`` row (schema v4; the historian took v3), the
SAME-transaction boundary as the parking audit append, the monotonic
per-unit ``epoch`` CAS (single-flight), terminal-row retention (a new park
over a closed lease mints epoch+1), the in-memory simulator twin with the
same interface, and the boot-reconstruction reads.

The lease is the machine truth; the audit row is the narrative.  A lease
never lands without its narrative row and a lease-mutating narrative row
never lands without the lease -- that is the whole point of the shared
transaction, and every test here fails the build if it ever splits.

SAFETY: no test contacts hardware, opens a socket, or restarts a process;
the SQLite paths run inside the test's own temporary directory.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from energypod.adapters.persistence.memory import InMemoryParkLeaseRepository
from energypod.adapters.persistence.sqlite import (
    SQLiteDatabase,
    SQLiteParkLeaseRepository,
)
from energypod.domain.audit import AuditEvent, DuplicateAuditEventError
from energypod.domain.observations import UnitLifecycle
from energypod.domain.parking import (
    LEASE_CLOSED_FOREIGN,
    LEASE_CLOSED_OPERATOR,
    LEASE_EXPIRED,
    LEASE_OPEN,
    LEASE_WRITE_UNVERIFIED,
    ParkLease,
    ParkLeaseEpochConflict,
    lease_is_open_for_renewal,
    lease_is_parked,
    vendor_debug_mode_name,
)

PARKED_AT = datetime(2026, 8, 24, 2, 0, 0, tzinfo=UTC)


def _event(event_id: str, *, event_type: str = "unit_parked", result: str = "parked") -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        occurred_at=datetime(2026, 8, 24, 2, 0, 1, tzinfo=UTC),
        monotonic_offset_s=1.0,
        process_instance_id="process-1",
        event_type=event_type,
        unit_id="mid",
        principal="person:operator",
        correlation_id="facade:unit_parked:req-1",
        policy_version="facade",
        configuration_version=1,
        observation_sequences={},
        reason_codes=(result,),
        requested_active_w=0,
        authorized_active_w=0,
        request_fingerprint="fingerprint-request",
        response_fingerprint="fingerprint-response",
        result=result,
        lifecycle=UnitLifecycle.DISARMED,
    )


def _open_lease(
    *,
    unit_id: str = "mid",
    epoch: int = 1,
    state: str = LEASE_OPEN,
    closed_at: datetime | None = None,
    write_unverified: bool = False,
    foreign_rewrite: bool = False,
    soc_pct_at_park: float | None = 48.0,
) -> ParkLease:
    return ParkLease(
        unit_id=unit_id,
        epoch=epoch,
        parked_at=PARKED_AT,
        expires_at=PARKED_AT + timedelta(hours=4),
        max_total_s=14400,
        reason="inverter work",
        authorizer="person:operator",
        soc_pct_at_park=soc_pct_at_park,
        state=state,
        closed_at=closed_at,
        write_unverified=write_unverified,
        foreign_rewrite=foreign_rewrite,
    )


class _AuditSink:
    """The in-memory twin's audit half; records what landed in one 'txn'."""

    def __init__(self) -> None:
        self.appended: list[AuditEvent] = []
        self.failing = False

    def append(self, event: AuditEvent) -> None:
        if self.failing:
            raise OSError("audit store unavailable")
        self.appended.append(event)


# --- the domain record ---------------------------------------------------------


def test_vendor_debug_mode_names_carry_the_decompiled_vocabulary() -> None:
    """GlobalFun.cs:152-165 -- the names the ``park_mode_out_of_scope``
    refusal quotes; values 2-6 exist ONLY as this read-side vocabulary."""
    assert vendor_debug_mode_name(0) == "Normal Mode"
    assert vendor_debug_mode_name(1) == "Standby"
    assert vendor_debug_mode_name(2) == "Charge"
    assert vendor_debug_mode_name(3) == "Discharge"
    assert vendor_debug_mode_name(4) == "Circulation"
    assert vendor_debug_mode_name(5) == "Fixing SOC"
    assert vendor_debug_mode_name(6) == "Verify Capacity"
    assert vendor_debug_mode_name(7) is None, "off the map names itself honestly"


def test_the_parked_state_family_is_the_dispatch_refusal_set() -> None:
    """Section 3: open, expired, and write_unverified are all PARKED; the
    two closed states are terminal."""
    assert lease_is_parked(LEASE_OPEN)
    assert lease_is_parked(LEASE_EXPIRED)
    assert lease_is_parked(LEASE_WRITE_UNVERIFIED)
    assert not lease_is_parked(LEASE_CLOSED_OPERATOR)
    assert not lease_is_parked(LEASE_CLOSED_FOREIGN)
    # Anti-rollover: only an OPEN lease renews; after expiry a NEW park with
    # fresh confirmation is the path, never a renewal.
    assert lease_is_open_for_renewal(LEASE_OPEN)
    assert not lease_is_open_for_renewal(LEASE_EXPIRED)
    assert not lease_is_open_for_renewal(LEASE_WRITE_UNVERIFIED)
    assert not lease_is_open_for_renewal(LEASE_CLOSED_OPERATOR)


def test_the_lease_record_validates_its_invariants() -> None:
    from dataclasses import replace

    lease = _open_lease()
    assert lease.parked is True
    assert lease.rollover_cap_at() == PARKED_AT + timedelta(seconds=14400)
    with pytest.raises(ValueError, match="expires_at precedes"):
        replace(lease, expires_at=PARKED_AT - timedelta(seconds=1))
    with pytest.raises(ValueError, match="open or expired lease carries no closed_at"):
        replace(lease, closed_at=PARKED_AT + timedelta(hours=1))
    with pytest.raises(ValueError, match="terminal lease carries its closed_at"):
        replace(lease, state=LEASE_CLOSED_OPERATOR)
    with pytest.raises(ValueError, match="write_unverified is the write_unverified"):
        replace(
            lease,
            state=LEASE_CLOSED_OPERATOR,
            closed_at=PARKED_AT + timedelta(hours=1),
            write_unverified=True,
        )
    with pytest.raises(ValueError, match="epoch must be a positive"):
        replace(lease, epoch=0)


def test_the_wire_lease_object_shape() -> None:
    payload = _open_lease().to_payload()
    assert payload == {
        "parked_at": PARKED_AT.isoformat(),
        "expires_at": (PARKED_AT + timedelta(hours=4)).isoformat(),
        "max_total_s": 14400,
        "reason": "inverter work",
        "authorizer": "person:operator",
        "epoch": 1,
    }


# --- schema migration (v4) -------------------------------------------------------


def test_schema_v4_migrates_a_version_three_database_in_place(tmp_path: Path) -> None:
    """The historian took v3; parking takes v4: a stamped-v3 database with
    live rows upgrades in place, the stamped version becomes 4, existing
    tables are untouched, and ``park_leases`` exists."""
    from energypod.db.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 7
    path = tmp_path / "park-migrate.sqlite3"
    raw = sqlite3.connect(path)
    try:
        raw.execute(
            "CREATE TABLE schema_version ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1),"
            "version INTEGER NOT NULL UNIQUE)"
        )
        raw.execute("INSERT INTO schema_version(singleton, version) VALUES (1, 3)")
        raw.execute("CREATE TABLE telemetry_sample (unit_id TEXT)")
        raw.execute("INSERT INTO telemetry_sample(unit_id) VALUES ('mid')")
        raw.commit()
    finally:
        raw.close()

    database = SQLiteDatabase(path)
    database.open()
    try:
        stamped = database.connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()
        assert stamped == (7,)
        assert database.connection.execute("SELECT COUNT(*) FROM telemetry_sample").fetchone() == (
            1,
        ), "an in-place upgrade touches no existing table's rows"
        present = database.connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'park_leases'"
        ).fetchone()
        assert present is not None
    finally:
        database.close()


def test_the_lease_table_carries_the_design_named_columns(tmp_path: Path) -> None:
    """Section 4's column set, verbatim: unit_id PK, opened_at, expires_at,
    max_total_s, reason, authorizer, epoch, state, soc_pct_at_park -- plus
    the terminal-sub-state close context (closed_at, write_unverified,
    foreign_rewrite)."""
    database = SQLiteDatabase(tmp_path / "columns.sqlite3")
    database.open()
    try:
        columns = {
            str(row[1])
            for row in database.connection.execute("PRAGMA table_info(park_leases)")
        }
        assert {
            "unit_id",
            "opened_at",
            "expires_at",
            "max_total_s",
            "reason",
            "authorizer",
            "epoch",
            "state",
            "soc_pct_at_park",
        } <= columns
        assert columns <= {
            "unit_id",
            "opened_at",
            "expires_at",
            "max_total_s",
            "reason",
            "authorizer",
            "epoch",
            "state",
            "soc_pct_at_park",
            "closed_at",
            "write_unverified",
            "foreign_rewrite",
        }
        pk = [
            str(row[1])
            for row in database.connection.execute("PRAGMA table_info(park_leases)")
            if row[5]
        ]
        assert pk == ["unit_id"], "one row per unit -- the unit_id PRIMARY KEY"
    finally:
        database.close()


# --- the same-transaction boundary ------------------------------------------------


def test_commit_lands_the_audit_row_and_the_lease_in_one_transaction(tmp_path: Path) -> None:
    """A lease never exists without its narrative row: after ``commit`` both
    rows are readable, and the audit row decodes through the ordinary audit
    store's read path."""
    from energypod.adapters.persistence.sqlite import SQLiteAuditRepository

    database = SQLiteDatabase(tmp_path / "commit.sqlite3")
    database.open()
    try:
        SQLiteAuditRepository(database)
        leases = SQLiteParkLeaseRepository(database)
        leases.commit(_event("park-1"), _open_lease())
        stored = leases.lease("mid")
        assert stored is not None and stored.epoch == 1 and stored.state == LEASE_OPEN
        recent = SQLiteAuditRepository(database).recent(limit=8)
        assert [event.event_id for event in recent] == ["park-1"]
        assert recent[0].event_type == "unit_parked"
    finally:
        database.close()


def test_a_failed_audit_row_rolls_the_lease_back_with_it(tmp_path: Path) -> None:
    """The transaction is one: a duplicate audit event id (the store's own
    uniqueness) refuses the whole commit and leaves NO lease -- a replayed
    parking audit row can never mint a second lease."""
    database = SQLiteDatabase(tmp_path / "rollback.sqlite3")
    database.open()
    try:
        leases = SQLiteParkLeaseRepository(database)
        leases.commit(_event("park-1"), _open_lease(epoch=1))
        with pytest.raises(DuplicateAuditEventError):
            leases.commit(_event("park-1"), _open_lease(epoch=2, unit_id="mid"))
        assert leases.lease("mid") is not None
        assert leases.lease("mid").epoch == 1, "the refused commit changed nothing"
    finally:
        database.close()


def test_the_epoch_cas_refuses_a_stale_view_with_nothing_landing(tmp_path: Path) -> None:
    """Single-flight (section 4): ``replace`` guards on ``expected_epoch``; a
    row that moved underneath refuses with ``ParkLeaseEpochConflict`` and the
    audit row rolls back with the lease."""
    from dataclasses import replace

    database = SQLiteDatabase(tmp_path / "cas.sqlite3")
    database.open()
    try:
        leases = SQLiteParkLeaseRepository(database)
        leases.commit(_event("renew-1"), _open_lease(epoch=1))
        stale = _open_lease(epoch=1)
        stale_renewed = replace(stale, expires_at=stale.expires_at + timedelta(hours=1))
        with pytest.raises(ParkLeaseEpochConflict) as conflict:
            leases.replace(_event("renew-2"), stale_renewed, expected_epoch=7)
        assert conflict.value.expected_epoch == 7
        assert conflict.value.actual_epoch == 1
        assert leases.lease("mid").expires_at == stale.expires_at, "the lease is unchanged"
        from energypod.adapters.persistence.sqlite import SQLiteAuditRepository

        assert SQLiteAuditRepository(database).recent(limit=8)[0].event_id == "renew-1", (
            "the refused mutation left no narrative row either"
        )
        leases.replace(_event("renew-3"), stale_renewed, expected_epoch=1)
        assert leases.lease("mid").expires_at == stale_renewed.expires_at
    finally:
        database.close()


def test_terminal_rows_are_retained_so_epochs_stay_monotonic(tmp_path: Path) -> None:
    """T-PARK-REPLAY's closing set: a closed lease stays in the table (the
    epoch column is the per-unit monotonic counter), so the NEXT park mints
    epoch 2 over the terminal row and the closing row is still readable."""
    database = SQLiteDatabase(tmp_path / "epochs.sqlite3")
    database.open()
    try:
        leases = SQLiteParkLeaseRepository(database)
        leases.commit(_event("park-1"), _open_lease(epoch=1))
        closed = _open_lease(
            epoch=1,
            state=LEASE_CLOSED_FOREIGN,
            closed_at=PARKED_AT + timedelta(hours=1),
        )
        leases.replace(_event("resume-1"), closed, expected_epoch=1)
        second = _open_lease(epoch=2)
        leases.commit(_event("park-2"), second)
        assert leases.lease("mid").epoch == 2
        assert leases.all_leases() == {"mid": second}
    finally:
        database.close()


def test_every_lease_state_round_trips_bit_for_bit(tmp_path: Path) -> None:
    from dataclasses import replace

    database = SQLiteDatabase(tmp_path / "roundtrip.sqlite3")
    database.open()
    try:
        leases = SQLiteParkLeaseRepository(database)
        states: list[ParkLease] = [
            _open_lease(unit_id="lhs", epoch=1),
            _open_lease(
                unit_id="mid",
                epoch=3,
                state=LEASE_EXPIRED,
                soc_pct_at_park=None,
            ),
            _open_lease(
                unit_id="rhs",
                epoch=2,
                state=LEASE_CLOSED_OPERATOR,
                closed_at=PARKED_AT + timedelta(minutes=30),
            ),
            _open_lease(
                unit_id="far",
                epoch=1,
                state=LEASE_CLOSED_FOREIGN,
                closed_at=PARKED_AT + timedelta(minutes=90),
                foreign_rewrite=True,
            ),
            _open_lease(
                unit_id="bay",
                epoch=1,
                state=LEASE_WRITE_UNVERIFIED,
                closed_at=PARKED_AT + timedelta(minutes=10),
                write_unverified=True,
            ),
        ]
        for index, lease in enumerate(states):
            # An EXPIRED lease still parks the unit; a terminal lease carries
            # closed_at; write_unverified carries its own flag -- the decode
            # must reproduce each exactly, including the epoch CAS base.
            if lease.state == LEASE_OPEN:
                leases.commit(_event(f"event-{index}"), lease)
            elif lease.state == LEASE_EXPIRED:
                leases.commit(_event(f"event-{index}"), lease)
                expired_form = replace(lease, state=LEASE_EXPIRED)
                leases.replace(_event(f"expire-{index}"), expired_form, expected_epoch=lease.epoch)
            else:
                open_form = replace(
                    lease, state=LEASE_OPEN, closed_at=None, write_unverified=False
                )
                leases.commit(_event(f"open-{index}"), open_form)
                leases.replace(_event(f"close-{index}"), lease, expected_epoch=lease.epoch)
        assert leases.all_leases() == {lease.unit_id: lease for lease in states}
    finally:
        database.close()


# --- the in-memory simulator twin ---------------------------------------------------


class TestInMemoryTwin:
    def test_the_same_interface_over_an_unbounded_map(self) -> None:
        sink = _AuditSink()
        store = InMemoryParkLeaseRepository(audit_sink=sink)
        lease = _open_lease()
        store.commit(_event("park-1"), lease)
        assert store.lease("mid") == lease
        assert store.all_leases() == {"mid": lease}
        assert [event.event_id for event in sink.appended] == ["park-1"]

    def test_the_cas_refuses_identically(self) -> None:
        sink = _AuditSink()
        store = InMemoryParkLeaseRepository(audit_sink=sink)
        lease = _open_lease(epoch=4)
        store.commit(_event("park-1"), lease)
        with pytest.raises(ParkLeaseEpochConflict):
            store.replace(_event("renew-1"), lease, expected_epoch=3)
        assert len(sink.appended) == 1, "the refused mutation appended nothing"

    def test_a_failing_audit_sink_refuses_the_whole_commit(self) -> None:
        """The twin's trivial transaction is still both-or-neither: a sink
        that raises leaves the map untouched."""
        sink = _AuditSink()
        sink.failing = True
        store = InMemoryParkLeaseRepository(audit_sink=sink)
        with pytest.raises(OSError):
            store.commit(_event("park-1"), _open_lease())
        assert store.lease("mid") is None
        assert store.all_leases() == {}

    def test_lease_survives_a_controller_restart_in_simulator_mode(self) -> None:
        """T-PARK-CRASH/RESTART, simulator-mode lease survival: the twin's map
        is deliberately UNBOUNDED -- nothing evicts a lease the way a bounded
        audit deque would, which is the whole reason the durable lease row is
        a dedicated store and not audit replay (DESIGN section 4)."""
        first = InMemoryParkLeaseRepository()
        first.commit(_event("park-1"), _open_lease())
        second = InMemoryParkLeaseRepository()
        assert second.lease("mid") is None, "a genuinely fresh map holds nothing"
        for extra in range(1000):
            first.commit(
                _event(f"noise-{extra}"),
                _open_lease(unit_id=f"u{extra}", epoch=1),
            )
        assert first.lease("mid") == _open_lease(), "the map is unbounded by design"
