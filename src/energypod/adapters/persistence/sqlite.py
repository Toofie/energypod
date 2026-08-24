"""SQLite WAL repositories for audit facts and versioned schedules."""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

from energypod.db.schema import apply_pending_migrations
from energypod.domain.audit import AuditEvent, DuplicateAuditEventError
from energypod.domain.energy import (
    CounterCrossCheck,
    EnergyDayRecord,
    EnergyUnitBaseline,
    FleetEnergyDay,
    UnitEnergyDay,
)
from energypod.domain.history import (
    HISTORY_NUMERIC_FIELDS,
    MaintenanceResult,
    TelemetryRollupHour,
    TelemetrySampleRow,
    format_history_timestamp,
    hour_start_of,
    parse_history_timestamp,
)
from energypod.domain.intents import Direction, IntentSource
from energypod.domain.observations import UnitLifecycle
from energypod.domain.parking import ParkLease, ParkLeaseEpochConflict
from energypod.domain.schedule import (
    ScheduleEntry,
    SchedulePlan,
    ScheduleVersionConflict,
    Weekday,
)


class PersistenceBusyError(RuntimeError):
    """SQLite could not durably accept work within its bounded wait."""


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number is prohibited: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _load_json_object(payload: str) -> dict[str, Any]:
    value = json.loads(
        payload,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    if type(value) is not dict:
        raise ValueError("persisted JSON root must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"invalid {label} keys; missing={missing}, extra={extra}")


class SQLiteDatabase:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 250) -> None:
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("database is not open")
        return self._connection

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            try:
                connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
                connection.execute("PRAGMA foreign_keys = ON")
                journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                connection.execute("PRAGMA synchronous = FULL")
                if str(journal_mode).lower() != "wal":
                    raise RuntimeError("SQLite refused WAL journal mode")
                if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise RuntimeError("SQLite refused foreign-key enforcement")
                if connection.execute("PRAGMA busy_timeout").fetchone()[0] != self.busy_timeout_ms:
                    raise RuntimeError("SQLite refused configured busy timeout")
                # schema_version from day one: every open runs the same
                # migration path ``energypod db migrate`` uses, so a fresh
                # database and a pre-schema database both converge on the
                # stamped version before any store reads or writes.
                apply_pending_migrations(connection)
            except BaseException as exc:
                connection.close()
                if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
                    raise PersistenceBusyError("database is busy during startup") from exc
                raise
            self._connection = connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def pragma(self, name: str) -> Any:
        if name not in {"journal_mode", "foreign_keys", "busy_timeout"}:
            raise ValueError("unsupported pragma")
        with self._lock:
            return self.connection.execute(f"PRAGMA {name}").fetchone()[0]


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, set | frozenset):
        # A set's iteration order is process-randomized (string hashing), so
        # an unordered encode would persist different bytes for the same
        # event in different processes.  Order by repr: deterministic for
        # any mix of stored types without requiring comparability.
        return [_json_value(item) for item in sorted(value, key=repr)]
    return value


class SQLiteAuditRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        try:
            with database.lock:
                database.connection.execute(
                    """CREATE TABLE IF NOT EXISTS audit_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        unit_id TEXT,
                        monotonic_offset_s REAL NOT NULL,
                        payload TEXT NOT NULL
                    )"""
                )
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("audit database is busy during initialization") from exc
            raise

    def append(self, event: AuditEvent) -> None:
        try:
            with self._database.lock:
                self._database.connection.execute("BEGIN IMMEDIATE")
                try:
                    _insert_audit_event(self._database.connection, event)
                    self._database.connection.execute("COMMIT")
                except BaseException:
                    self._database.connection.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode in {
                sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            }:
                raise DuplicateAuditEventError(event.event_id) from exc
            raise
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("audit database is busy") from exc
            raise

    def recent(self, *, limit: int, unit_id: str | None = None) -> tuple[AuditEvent, ...]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be positive")
        if unit_id is not None and (
            not isinstance(unit_id, str) or not unit_id or unit_id != unit_id.strip()
        ):
            raise ValueError("unit_id must be non-empty and normalized")
        query = "SELECT payload FROM audit_events"
        parameters: list[Any] = []
        if unit_id is not None:
            query += " WHERE unit_id = ?"
            parameters.append(unit_id)
        query += " ORDER BY sequence DESC LIMIT ?"
        parameters.append(limit)
        try:
            with self._database.lock:
                rows = self._database.connection.execute(query, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("audit database is busy") from exc
            raise
        return tuple(self._decode(row[0]) for row in rows)

    @staticmethod
    def _decode(payload: str) -> AuditEvent:
        values = _load_json_object(payload)
        # Optional fields (added after the first durable deployments, e.g. the
        # 2026-08-23 per-unit watt breakdowns) may be absent from rows an
        # earlier build wrote; the model's defaults are the documented
        # reading.  An unknown key is still refused, and a missing REQUIRED
        # key is still refused -- only known-optional omissions decode.
        fields = AuditEvent.model_fields
        optional = frozenset(name for name, field in fields.items() if not field.is_required())
        known = frozenset(fields)
        actual = frozenset(values)
        if actual - known:
            raise ValueError(f"invalid audit event keys; extra={sorted(actual - known)}")
        missing = known - actual
        if missing - optional:
            raise ValueError(f"invalid audit event keys; missing={sorted(missing - optional)}")
        values["occurred_at"] = datetime.fromisoformat(values["occurred_at"])
        if values["source"] is not None:
            values["source"] = IntentSource(values["source"])
        values["lifecycle"] = UnitLifecycle(values["lifecycle"])
        if type(values["observation_sequences"]) is not dict:
            raise ValueError("audit observation_sequences must be a JSON object")
        if type(values["reason_codes"]) is not list:
            raise ValueError("audit reason_codes must be a JSON array")
        values["reason_codes"] = tuple(values["reason_codes"])
        return AuditEvent(**values)


def _insert_audit_event(connection: sqlite3.Connection, event: AuditEvent) -> None:
    """Encode and insert one audit row inside the CALLER's open transaction.

    The parking lease store appends its audit row and its ``park_leases`` row
    in ONE transaction boundary (DESIGN_POD_PARKING section 4), so the insert
    itself is shared here: the row's encoding is the audit store's own, and
    the transaction -- BEGIN/COMMIT/ROLLBACK -- stays each caller's to own.
    """
    if hasattr(event, "model_dump"):
        # Avoid Pydantic attempting to serialize immutable MappingProxyType
        # values; the explicit encoder below owns all persistence encoding.
        record = {name: getattr(event, name) for name in type(event).model_fields}
    elif is_dataclass(event):
        record = {field.name: getattr(event, field.name) for field in fields(event)}
    else:  # pragma: no cover - defensive port boundary
        raise TypeError("audit event must be an immutable record")
    payload = json.dumps(
        {name: _json_value(value) for name, value in record.items()},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    connection.execute(
        """INSERT INTO audit_events(
               event_id, unit_id, monotonic_offset_s, payload
           ) VALUES (?, ?, ?, ?)""",
        (event.event_id, event.unit_id, event.monotonic_offset_s, payload),
    )


class SQLiteParkLeaseRepository:
    """The durable parking-lease store (DESIGN_POD_PARKING section 4).

    One row per unit (``unit_id`` PRIMARY KEY): the machine truth beside the
    audit trail's narrative.  ``commit`` appends the parking audit row AND
    upserts the lease row in ONE ``BEGIN IMMEDIATE`` transaction -- a lease
    never exists without its narrative row and a narrative row for a lease
    mutation never exists without the lease.  Terminal rows are kept (the
    epoch column is the per-unit monotonic single-flight counter), and
    ``replace`` performs the compare-and-set on ``epoch`` so a mutation on a
    closed epoch refuses instead of acting on a stale view.
    """

    _LEASE_COLUMNS: tuple[str, ...] = (
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
    )

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        try:
            with database.lock:
                # The audit table is created here too (idempotently, in the
                # audit store's own shape) because ``commit`` writes BOTH rows
                # in one transaction: this repository must not depend on the
                # audit repository having been constructed first.
                database.connection.execute(
                    """CREATE TABLE IF NOT EXISTS audit_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        unit_id TEXT,
                        monotonic_offset_s REAL NOT NULL,
                        payload TEXT NOT NULL
                    )"""
                )
                database.connection.execute(
                    """CREATE TABLE IF NOT EXISTS park_leases (
                        unit_id TEXT PRIMARY KEY,
                        opened_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        max_total_s INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        authorizer TEXT NOT NULL,
                        epoch INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        soc_pct_at_park REAL,
                        closed_at TEXT,
                        write_unverified INTEGER NOT NULL DEFAULT 0,
                        foreign_rewrite INTEGER NOT NULL DEFAULT 0
                    )"""
                )
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError(
                    "park lease database is busy during initialization"
                ) from exc
            raise

    def commit(self, event: AuditEvent, lease: ParkLease) -> None:
        """One transaction: the audit row and the lease row land together."""
        if type(lease) is not ParkLease:
            raise TypeError("lease must be a ParkLease")
        try:
            with self._database.lock:
                connection = self._database.connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _insert_audit_event(connection, event)
                    self._upsert_lease(connection, lease)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode in {
                sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            }:
                raise DuplicateAuditEventError(event.event_id) from exc
            raise
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("park lease database is busy") from exc
            raise

    def replace(
        self, event: AuditEvent, lease: ParkLease, *, expected_epoch: int
    ) -> ParkLease:
        """CAS upsert: the audit row and the epoch-guarded lease row together.

        ``expected_epoch`` is the epoch the caller read under its critical
        section; a row that moved underneath (another process, a restart)
        refuses with :class:`ParkLeaseEpochConflict` and NOTHING lands -- the
        audit row rolls back with the lease, so a refused mutation leaves no
        narrative behind either.
        """
        if type(lease) is not ParkLease:
            raise TypeError("lease must be a ParkLease")
        if type(expected_epoch) is not int or expected_epoch < 1:
            raise ValueError("expected_epoch must be a positive integer")
        try:
            with self._database.lock:
                connection = self._database.connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT epoch FROM park_leases WHERE unit_id = ?", (lease.unit_id,)
                    ).fetchone()
                    actual = 0 if row is None else int(row[0])
                    if actual != expected_epoch:
                        raise ParkLeaseEpochConflict(lease.unit_id, expected_epoch, actual)
                    _insert_audit_event(connection, event)
                    self._upsert_lease(connection, lease)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as exc:
            if exc.sqlite_errorcode in {
                sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                sqlite3.SQLITE_CONSTRAINT_UNIQUE,
            }:
                raise DuplicateAuditEventError(event.event_id) from exc
            raise
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("park lease database is busy") from exc
            raise
        return lease

    def lease(self, unit_id: str) -> ParkLease | None:
        try:
            with self._database.lock:
                row = self._database.connection.execute(
                    f"SELECT {', '.join(self._LEASE_COLUMNS)} FROM park_leases"  # noqa: S608
                    " WHERE unit_id = ?",
                    (unit_id,),
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("park lease database is busy") from exc
            raise
        return None if row is None else self._decode(row)

    def all_leases(self) -> dict[str, ParkLease]:
        try:
            with self._database.lock:
                rows = self._database.connection.execute(
                    f"SELECT {', '.join(self._LEASE_COLUMNS)} FROM park_leases"  # noqa: S608
                    " ORDER BY unit_id"
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("park lease database is busy") from exc
            raise
        return {str(row[0]): self._decode(row) for row in rows}

    @staticmethod
    def _upsert_lease(connection: sqlite3.Connection, lease: ParkLease) -> None:
        connection.execute(
            """INSERT INTO park_leases(
                   unit_id, opened_at, expires_at, max_total_s, reason, authorizer,
                   epoch, state, soc_pct_at_park, closed_at, write_unverified,
                   foreign_rewrite
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(unit_id) DO UPDATE SET
                 opened_at = excluded.opened_at,
                 expires_at = excluded.expires_at,
                 max_total_s = excluded.max_total_s,
                 reason = excluded.reason,
                 authorizer = excluded.authorizer,
                 epoch = excluded.epoch,
                 state = excluded.state,
                 soc_pct_at_park = excluded.soc_pct_at_park,
                 closed_at = excluded.closed_at,
                 write_unverified = excluded.write_unverified,
                 foreign_rewrite = excluded.foreign_rewrite""",
            (
                lease.unit_id,
                lease.parked_at.astimezone(UTC).isoformat(),
                lease.expires_at.astimezone(UTC).isoformat(),
                int(lease.max_total_s),
                lease.reason,
                lease.authorizer,
                int(lease.epoch),
                lease.state,
                None if lease.soc_pct_at_park is None else float(lease.soc_pct_at_park),
                None if lease.closed_at is None else lease.closed_at.astimezone(UTC).isoformat(),
                1 if lease.write_unverified else 0,
                1 if lease.foreign_rewrite else 0,
            ),
        )

    @classmethod
    def _decode(cls, row: Sequence[Any]) -> ParkLease:
        values = dict(zip(cls._LEASE_COLUMNS, row, strict=True))
        soc = values["soc_pct_at_park"]
        return ParkLease(
            unit_id=str(values["unit_id"]),
            epoch=int(values["epoch"]),
            parked_at=datetime.fromisoformat(str(values["opened_at"])),
            expires_at=datetime.fromisoformat(str(values["expires_at"])),
            max_total_s=int(values["max_total_s"]),
            reason=str(values["reason"]),
            authorizer=str(values["authorizer"]),
            soc_pct_at_park=None if soc is None else float(soc),
            state=str(values["state"]),
            closed_at=(
                None if values["closed_at"] is None
                else datetime.fromisoformat(str(values["closed_at"]))
            ),
            write_unverified=bool(values["write_unverified"]),
            foreign_rewrite=bool(values["foreign_rewrite"]),
        )


class SQLiteScheduleRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        try:
            with database.lock:
                database.connection.execute(
                    """CREATE TABLE IF NOT EXISTS active_schedule (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        version INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    )"""
                )
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError(
                    "schedule database is busy during initialization"
                ) from exc
            raise

    def get(self) -> SchedulePlan | None:
        try:
            with self._database.lock:
                row = self._database.connection.execute(
                    "SELECT payload FROM active_schedule WHERE singleton = 1"
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("schedule database is busy") from exc
            raise
        return None if row is None else self._decode(row[0])

    def replace(self, *, expected_version: int, replacement: SchedulePlan) -> None:
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if type(replacement) is not SchedulePlan:
            raise TypeError("replacement must be a SchedulePlan")
        if replacement.version != expected_version + 1:
            raise ScheduleVersionConflict(expected_version, replacement.version)
        payload = self._encode(replacement)
        try:
            with self._database.lock:
                connection = self._database.connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        "SELECT version FROM active_schedule WHERE singleton = 1"
                    ).fetchone()
                    actual = 0 if row is None else int(row[0])
                    if actual != expected_version:
                        raise ScheduleVersionConflict(expected_version, actual)
                    connection.execute(
                        """INSERT INTO active_schedule(singleton, version, payload)
                           VALUES (1, ?, ?)
                           ON CONFLICT(singleton) DO UPDATE SET
                             version = excluded.version, payload = excluded.payload""",
                        (replacement.version, payload),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("schedule database is busy") from exc
            raise

    @staticmethod
    def _encode(schedule: SchedulePlan) -> str:
        return json.dumps(
            {
                "version": schedule.version,
                "timezone": schedule.timezone,
                "entries": [
                    {
                        "entry_id": entry.entry_id,
                        "days": [int(day) for day in sorted(entry.days)],
                        "start_local": entry.start_local.isoformat(),
                        "end_local": entry.end_local.isoformat(),
                        "action": entry.action.value,
                        "watts": entry.watts,
                        "unit_ids": sorted(entry.unit_ids),
                        "effective_from": entry.effective_from.isoformat(),
                        "effective_until": entry.effective_until.isoformat(),
                        "priority": entry.priority,
                        "enabled": entry.enabled,
                        # DESIGN_SCHEDULES §1: the per-unit watt form rides the
                        # durable payload; null keeps every scalar entry's
                        # stored bytes exactly as before.
                        "watts_by_unit": (
                            None
                            if entry.watts_by_unit is None
                            else dict(sorted(entry.watts_by_unit.items()))
                        ),
                    }
                    for entry in schedule.entries
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _decode(payload: str) -> SchedulePlan:
        value = _load_json_object(payload)
        _require_exact_keys(value, frozenset({"version", "timezone", "entries"}), "schedule")
        if type(value["entries"]) is not list:
            raise ValueError("schedule entries must be a JSON array")
        entry_keys = frozenset(
            {
                "entry_id",
                "days",
                "start_local",
                "end_local",
                "action",
                "watts",
                "unit_ids",
                "effective_from",
                "effective_until",
                "priority",
                "enabled",
            }
        )
        for item in value["entries"]:
            if type(item) is not dict:
                raise ValueError("schedule entry must be a JSON object")
            # Optional key: ``watts_by_unit`` (null or the per-unit mapping).
            # Rows written before the per-unit form carry no key and every
            # scalar entry decodes unchanged.
            if not entry_keys <= item.keys() <= entry_keys | {"watts_by_unit"}:
                raise ValueError(f"invalid schedule entry keys: {sorted(item.keys())}")
            if type(item["days"]) is not list or any(type(day) is not int for day in item["days"]):
                raise ValueError("schedule days must be an integer JSON array")
            if len(item["days"]) != len(set(item["days"])):
                raise ValueError("schedule days must not contain duplicates")
            if type(item["unit_ids"]) is not list or any(
                type(unit_id) is not str for unit_id in item["unit_ids"]
            ):
                raise ValueError("schedule unit_ids must be a string JSON array")
            if len(item["unit_ids"]) != len(set(item["unit_ids"])):
                raise ValueError("schedule unit_ids must not contain duplicates")
            if item.get("watts_by_unit") is not None and (
                type(item["watts_by_unit"]) is not dict
                or any(type(watts) is not int for watts in item["watts_by_unit"].values())
            ):
                raise ValueError("schedule watts_by_unit must be a unit-to-watts JSON object")
        entries = tuple(
            ScheduleEntry(
                entry_id=item["entry_id"],
                days=frozenset(Weekday(day) for day in item["days"]),
                start_local=time.fromisoformat(item["start_local"]),
                end_local=time.fromisoformat(item["end_local"]),
                action=Direction(item["action"]),
                watts=item["watts"],
                unit_ids=frozenset(item["unit_ids"]),
                effective_from=date.fromisoformat(item["effective_from"]),
                effective_until=date.fromisoformat(item["effective_until"]),
                priority=item["priority"],
                enabled=item["enabled"],
                watts_by_unit=item.get("watts_by_unit"),
            )
            for item in value["entries"]
        )
        return SchedulePlan(version=value["version"], timezone=value["timezone"], entries=entries)


class SQLiteEnergyLedgerRepository:
    """The durable energy scorecard ledger (DESIGN_ENERGY_SCORECARD section 5).

    ``energy_day`` holds one payload per local date (the first record for a
    site-day stands -- the accountant finalizes a day exactly once, so a
    re-record is the idempotent no-op the memory adapter performs too);
    ``energy_baseline`` holds the live-day baseline per unit so a mid-day
    restart re-baselines from the last seen cumulatives.  Day records are
    projections, not acts: the audit store is deliberately not overloaded.
    """

    _UNIT_METRIC_KEYS = frozenset(
        {
            "grid_import_kwh",
            "grid_export_kwh",
            "battery_charged_kwh",
            "battery_discharged_kwh",
            "load_kwh",
            "charged_from_surplus_kwh",
            "coverage_pct",
            "metric_flags",
        }
    )
    _FLEET_KEYS = _UNIT_METRIC_KEYS - {"metric_flags"}
    _BASELINE_KEYS = frozenset(
        {
            "date",
            "counter_start",
            "counter_last",
            "import_watt_seconds",
            "export_watt_seconds",
            "surplus_watt_seconds",
            "sampled_seconds",
            "last_capture_wall",
            "last_grid_watts",
            "flags",
        }
    )

    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database
        try:
            with database.lock:
                database.connection.execute(
                    """CREATE TABLE IF NOT EXISTS energy_day (
                        day TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    )"""
                )
                database.connection.execute(
                    """CREATE TABLE IF NOT EXISTS energy_baseline (
                        unit_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL
                    )"""
                )
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError(
                    "energy ledger database is busy during initialization"
                ) from exc
            raise

    def record_day(self, record: EnergyDayRecord) -> None:
        if type(record) is not EnergyDayRecord:
            raise TypeError("record must be an EnergyDayRecord")
        payload = json.dumps(
            record.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        day = record.date.isoformat()
        try:
            with self._database.lock:
                connection = self._database.connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # The first record for a date stands (finalize-once).
                    connection.execute(
                        "INSERT INTO energy_day(day, payload) VALUES (?, ?)"
                        " ON CONFLICT(day) DO NOTHING",
                        (day, payload),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("energy ledger is busy") from exc
            raise

    def get_day(self, day: date) -> EnergyDayRecord | None:
        if type(day) is not date:
            raise TypeError("day must be a civil date")
        try:
            with self._database.lock:
                row = self._database.connection.execute(
                    "SELECT payload FROM energy_day WHERE day = ?", (day.isoformat(),)
                ).fetchone()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("energy ledger is busy") from exc
            raise
        return None if row is None else self._decode_day(row[0])

    def latest_days(self, limit: int) -> tuple[EnergyDayRecord, ...]:
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        if limit == 0:
            return ()
        try:
            with self._database.lock:
                rows = self._database.connection.execute(
                    "SELECT payload FROM energy_day ORDER BY day DESC LIMIT ?", (limit,)
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("energy ledger is busy") from exc
            raise
        return tuple(self._decode_day(row[0]) for row in rows)

    def load_baseline(self) -> dict[str, EnergyUnitBaseline]:
        try:
            with self._database.lock:
                rows = self._database.connection.execute(
                    "SELECT unit_id, payload FROM energy_baseline"
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("energy ledger is busy") from exc
            raise
        return {str(unit_id): self._decode_baseline(payload) for unit_id, payload in rows}

    def save_baseline(self, baselines: Mapping[str, EnergyUnitBaseline]) -> None:
        if not isinstance(baselines, Mapping) or any(
            type(value) is not EnergyUnitBaseline for value in baselines.values()
        ):
            raise TypeError("baselines must map unit ids to EnergyUnitBaseline values")
        encoded = {
            unit: json.dumps(
                self._baseline_payload(baseline),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            for unit, baseline in baselines.items()
        }
        try:
            with self._database.lock:
                connection = self._database.connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # The baseline is a whole-fleet projection: rows for
                    # units no longer configured must not linger.
                    connection.execute("DELETE FROM energy_baseline")
                    connection.executemany(
                        "INSERT INTO energy_baseline(unit_id, payload) VALUES (?, ?)",
                        sorted(encoded.items()),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("energy ledger is busy") from exc
            raise

    @staticmethod
    def _baseline_payload(baseline: EnergyUnitBaseline) -> dict[str, Any]:
        return {
            "date": baseline.date.isoformat(),
            "counter_start": dict(baseline.counter_start),
            "counter_last": dict(baseline.counter_last),
            "import_watt_seconds": baseline.import_watt_seconds,
            "export_watt_seconds": baseline.export_watt_seconds,
            "surplus_watt_seconds": baseline.surplus_watt_seconds,
            "sampled_seconds": baseline.sampled_seconds,
            "last_capture_wall": (
                None
                if baseline.last_capture_wall is None
                else baseline.last_capture_wall.isoformat()
            ),
            "last_grid_watts": baseline.last_grid_watts,
            "flags": sorted(baseline.flags),
        }

    def _decode_baseline(self, payload: str) -> EnergyUnitBaseline:
        value = _load_json_object(payload)
        _require_exact_keys(value, self._BASELINE_KEYS, "energy baseline")
        return EnergyUnitBaseline(
            date=date.fromisoformat(value["date"]),
            counter_start={
                str(key): float(number) for key, number in value["counter_start"].items()
            },
            counter_last={str(key): float(number) for key, number in value["counter_last"].items()},
            import_watt_seconds=float(value["import_watt_seconds"]),
            export_watt_seconds=float(value["export_watt_seconds"]),
            surplus_watt_seconds=float(value["surplus_watt_seconds"]),
            sampled_seconds=float(value["sampled_seconds"]),
            last_capture_wall=(
                None
                if value["last_capture_wall"] is None
                else datetime.fromisoformat(value["last_capture_wall"])
            ),
            last_grid_watts=(
                None if value["last_grid_watts"] is None else float(value["last_grid_watts"])
            ),
            flags=frozenset(str(flag) for flag in value["flags"]),
        )

    def _decode_day(self, payload: str) -> EnergyDayRecord:
        value = _load_json_object(payload)
        _require_exact_keys(
            value,
            frozenset(
                {
                    "date",
                    "timezone",
                    "utc_offset_minutes",
                    "kind",
                    "units",
                    "fleet",
                    "sources",
                    "counter_cross_check",
                    "solar_production_measured",
                }
            ),
            "energy day record",
        )
        if type(value["units"]) is not dict:
            raise ValueError("energy day units must be a JSON object")
        units = {
            str(unit): self._decode_unit_metrics(unit, metrics)
            for unit, metrics in value["units"].items()
        }
        if type(value["fleet"]) is not dict:
            raise ValueError("energy day fleet must be a JSON object")
        _require_exact_keys(value["fleet"], self._FLEET_KEYS, "energy fleet metrics")
        fleet = FleetEnergyDay(**{key: value["fleet"][key] for key in self._FLEET_KEYS})
        cross_check = value["counter_cross_check"]
        if cross_check is not None:
            if type(cross_check) is not dict:
                raise ValueError("counter cross-check must be a JSON object")
            _require_exact_keys(
                cross_check,
                frozenset(
                    {"grid_a_delta_kwh", "grid_b_delta_kwh", "consistent_with", "discriminating"}
                ),
                "counter cross-check",
            )
            cross_check = CounterCrossCheck(**cross_check)
        return EnergyDayRecord(
            date=date.fromisoformat(value["date"]),
            timezone=str(value["timezone"]),
            utc_offset_minutes=int(value["utc_offset_minutes"]),
            kind=str(value["kind"]),
            units=units,
            fleet=fleet,
            sources={str(key): str(source) for key, source in value["sources"].items()},
            counter_cross_check=cross_check,
            solar_production_measured=bool(value["solar_production_measured"]),
        )

    def _decode_unit_metrics(self, unit: str, metrics: Any) -> UnitEnergyDay:
        if type(metrics) is not dict:
            raise ValueError(f"energy day unit {unit!r} metrics must be a JSON object")
        _require_exact_keys(metrics, self._UNIT_METRIC_KEYS, f"energy unit {unit!r} metrics")
        if type(metrics["metric_flags"]) is not list:
            raise ValueError("metric_flags must be a JSON array")
        return UnitEnergyDay(
            grid_import_kwh=metrics["grid_import_kwh"],
            grid_export_kwh=metrics["grid_export_kwh"],
            battery_charged_kwh=metrics["battery_charged_kwh"],
            battery_discharged_kwh=metrics["battery_discharged_kwh"],
            load_kwh=metrics["load_kwh"],
            charged_from_surplus_kwh=metrics["charged_from_surplus_kwh"],
            coverage_pct=metrics["coverage_pct"],
            metric_flags=frozenset(str(flag) for flag in metrics["metric_flags"]),
        )


class SQLiteTelemetryHistoryRepository:
    """The durable plant-history store (DESIGN_PLANT_HISTORY sections 2.2-2.4).

    ``telemetry_sample`` holds the append-only sample rows keyed
    ``(unit_id, sampled_at)``; ``telemetry_rollup_hourly`` holds the hourly
    projections keyed ``(unit_id, hour_start)``.  Both tables are created by
    the schema-v3 migration, so this repository opens nothing and owns only
    its statements.  Samples are projections, not acts: no audit store is
    touched and no control path reads these rows.
    """

    _SAMPLE_COLUMNS: tuple[str, ...] = (
        "unit_id",
        "sampled_at",
        *HISTORY_NUMERIC_FIELDS,
        "lifecycle",
        "health_state",
        "quality",
        "commanded_source",
        "commanded_direction",
        "commanded_w",
        "debug_mode_w",
        "ctrl_mode_w",
        "work_mode_w",
        "run_mode_w",
    )
    # The interpolated fragments are this class's own frozen column tuples,
    # never caller input; every value rides a bound parameter.
    _INSERT_SAMPLE: str = (
        f"INSERT INTO telemetry_sample({', '.join(_SAMPLE_COLUMNS)}) "  # noqa: S608
        f"VALUES ({', '.join('?' * len(_SAMPLE_COLUMNS))}) ON CONFLICT DO NOTHING"
    )
    _ROLLUP_COLUMNS: tuple[str, ...] = (
        "unit_id",
        "hour_start",
        *(
            f"{field}_{suffix}"
            for field in HISTORY_NUMERIC_FIELDS
            for suffix in ("min", "max", "mean")
        ),
        "sample_count",
        "worst_quality",
    )
    # One statement rolls every not-yet-rolled hour older than the horizon:
    # min/max/avg ignore NULLs (an all-NULL field rolls to NULL -- never a
    # zero), count(*) counts ROWS (the coverage marker), and the worst
    # quality is computed in-pass with the pinned precedence ranking.
    _QUALITY_RANK: str = (
        "CASE quality WHEN 'missing' THEN 5 WHEN 'bad' THEN 4 WHEN 'stale' THEN 3"
        " WHEN 'suspect' THEN 2 ELSE 1 END"
    )
    _ROLLUP_SELECT: str = (
        "INSERT INTO telemetry_rollup_hourly("  # noqa: S608 -- frozen column tuples
        + ", ".join(_ROLLUP_COLUMNS)
        + ") SELECT unit_id, substr(sampled_at, 1, 13) || ':00:00+00:00', "
        + ", ".join(
            f"{aggregate}({field})"
            for field in HISTORY_NUMERIC_FIELDS
            for aggregate in ("min", "max", "avg")
        )
        + f", count(*), CASE max({_QUALITY_RANK}) WHEN 5 THEN 'missing' WHEN 4 THEN 'bad'"
        " WHEN 3 THEN 'stale' WHEN 2 THEN 'suspect' ELSE 'good' END"
        " FROM telemetry_sample WHERE substr(sampled_at, 1, 13) < ?"
        " GROUP BY unit_id, substr(sampled_at, 1, 13) ON CONFLICT DO NOTHING"
    )

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        retention_full_resolution_s: float = 14 * 86_400.0,
        retention_rollup_s: float | None = None,
    ) -> None:
        if not isinstance(retention_full_resolution_s, int | float) or (
            not math.isfinite(float(retention_full_resolution_s))
            or retention_full_resolution_s <= 0
        ):
            raise ValueError("retention_full_resolution_s must be a positive finite number")
        if retention_rollup_s is not None and (
            not isinstance(retention_rollup_s, int | float)
            or not math.isfinite(float(retention_rollup_s))
            or retention_rollup_s <= 0
        ):
            raise ValueError("retention_rollup_s must be a positive finite number or None")
        self._database = database
        self._retention_full_resolution_s = float(retention_full_resolution_s)
        self._retention_rollup_s = None if retention_rollup_s is None else float(retention_rollup_s)

    # --- append ---------------------------------------------------------------

    def append_samples(self, rows: Sequence[TelemetrySampleRow]) -> None:
        """One transaction per tick: ``BEGIN IMMEDIATE`` + ``executemany``.

        Duplicate primary keys are ignored (a retried tick after a suppressed
        failure can never corrupt the store), and busy surfaces as
        ``PersistenceBusyError`` -- a gap the caller survives, never a crash.
        """
        if any(type(row) is not TelemetrySampleRow for row in rows):
            raise TypeError("rows must be TelemetrySampleRow values")
        if not rows:
            return
        payload = [
            (
                row.unit_id,
                format_history_timestamp(row.sampled_at),
                *(getattr(row, field) for field in HISTORY_NUMERIC_FIELDS),
                row.lifecycle,
                row.health_state,
                row.quality,
                row.commanded_source,
                row.commanded_direction,
                row.commanded_w,
                row.debug_mode_w,
                row.ctrl_mode_w,
                row.work_mode_w,
                row.run_mode_w,
            )
            for row in rows
        ]
        try:
            with self._database.lock:
                connection = self._database.connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.executemany(self._INSERT_SAMPLE, payload)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("telemetry history is busy") from exc
            raise

    # --- reads ----------------------------------------------------------------

    def samples(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[TelemetrySampleRow, ...]:
        """The full-resolution rows in the inclusive window, unit-major."""
        normalized = self._require_units(unit_ids)
        start, end = format_history_timestamp(from_at), format_history_timestamp(to_at)
        placeholders = ", ".join("?" * len(normalized))
        query = (
            f"SELECT {', '.join(self._SAMPLE_COLUMNS)} FROM telemetry_sample"  # noqa: S608
            f" WHERE unit_id IN ({placeholders}) AND sampled_at >= ? AND sampled_at <= ?"
            " ORDER BY unit_id, sampled_at"
        )
        rows = self._fetch(query, (*normalized, start, end))
        return tuple(self._decode_sample(row) for row in rows)

    def rollup_hours(
        self, unit_ids: Sequence[str], from_at: datetime, to_at: datetime
    ) -> tuple[TelemetryRollupHour, ...]:
        """The hourly rollups whose hour starts fall in the inclusive window."""
        normalized = self._require_units(unit_ids)
        start = format_history_timestamp(hour_start_of(from_at))
        end = format_history_timestamp(to_at)
        placeholders = ", ".join("?" * len(normalized))
        query = (
            f"SELECT {', '.join(self._ROLLUP_COLUMNS)} FROM telemetry_rollup_hourly"  # noqa: S608
            f" WHERE unit_id IN ({placeholders}) AND hour_start >= ? AND hour_start <= ?"
            " ORDER BY unit_id, hour_start"
        )
        rows = self._fetch(query, (*normalized, start, end))
        return tuple(self._decode_rollup(row) for row in rows)

    def oldest_full_res_at(self) -> datetime | None:
        """The oldest retained full-resolution sample (the data horizon)."""
        row = self._fetch("SELECT min(sampled_at) FROM telemetry_sample", ())
        return None if row[0][0] is None else parse_history_timestamp(row[0][0])

    def last_sample_at(self, unit_ids: Sequence[str]) -> dict[str, datetime | None]:
        """Each unit's newest sample (one indexed read per unit; None = unsampled)."""
        normalized = self._require_units(unit_ids)
        latest: dict[str, datetime | None] = dict.fromkeys(normalized)
        for unit_id in normalized:
            row = self._fetch(
                "SELECT max(sampled_at) FROM telemetry_sample WHERE unit_id = ?", (unit_id,)
            )
            if row[0][0] is not None:
                latest[unit_id] = parse_history_timestamp(row[0][0])
        return latest

    # --- maintenance ----------------------------------------------------------

    def maintain(self, now: datetime) -> MaintenanceResult:
        """Rollup then prune, in ONE transaction (DESIGN section 2.4).

        Rollup covers every hour whose UTC hour start precedes the retention
        horizon's hour bucket; prune then deletes only rows strictly inside
        those rolled hours, so a failed rollup (the whole transaction rolls
        back) can never delete the only copy.  ``retention_rollup_s`` of
        ``None`` keeps hourly rollups forever.
        """
        horizon = format_history_timestamp(
            hour_start_of(
                now.astimezone(UTC) - timedelta(seconds=self._retention_full_resolution_s)
            )
        )
        rolled = pruned = pruned_rollups = 0
        try:
            with self._database.lock:
                connection = self._database.connection
                connection.execute("BEGIN IMMEDIATE")
                try:
                    before = connection.total_changes
                    connection.execute(self._ROLLUP_SELECT, (horizon[:13],))
                    rolled = connection.total_changes - before
                    pruned = connection.execute(
                        "DELETE FROM telemetry_sample WHERE sampled_at < ?", (horizon,)
                    ).rowcount
                    if self._retention_rollup_s is not None:
                        rollup_horizon = format_history_timestamp(
                            hour_start_of(
                                now.astimezone(UTC) - timedelta(seconds=self._retention_rollup_s)
                            )
                        )
                        pruned_rollups = connection.execute(
                            "DELETE FROM telemetry_rollup_hourly WHERE hour_start < ?",
                            (rollup_horizon,),
                        ).rowcount
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("telemetry history is busy") from exc
            raise
        return MaintenanceResult(
            rolled_hours=rolled,
            pruned_samples=max(pruned, 0),
            pruned_rollups=max(pruned_rollups, 0),
        )

    # --- internals ------------------------------------------------------------

    def _fetch(self, query: str, parameters: tuple[Any, ...]) -> Sequence[Sequence[Any]]:
        try:
            with self._database.lock:
                return self._database.connection.execute(query, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise PersistenceBusyError("telemetry history is busy") from exc
            raise

    @staticmethod
    def _require_units(unit_ids: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(unit_ids)
        if not normalized or any(
            not isinstance(unit, str) or not unit or unit != unit.strip() for unit in normalized
        ):
            raise ValueError("unit_ids must be non-empty normalized identifiers")
        if len(set(normalized)) != len(normalized):
            raise ValueError("unit_ids must be unique")
        return normalized

    @classmethod
    def _decode_sample(cls, row: Sequence[Any]) -> TelemetrySampleRow:
        values = dict(zip(cls._SAMPLE_COLUMNS, row, strict=True))
        values["sampled_at"] = parse_history_timestamp(values["sampled_at"])
        for name in HISTORY_NUMERIC_FIELDS:
            values[name] = None if values[name] is None else float(values[name])
        return TelemetrySampleRow(**values)

    @classmethod
    def _decode_rollup(cls, row: Sequence[Any]) -> TelemetryRollupHour:
        values = dict(zip(cls._ROLLUP_COLUMNS, row, strict=True))
        metrics: dict[str, tuple[float | None, float | None, float | None]] = {}
        for field in HISTORY_NUMERIC_FIELDS:
            low, high, mean = (
                (
                    None
                    if values[f"{field}_{suffix}"] is None
                    else float(values[f"{field}_{suffix}"])
                )
                for suffix in ("min", "max", "mean")
            )
            metrics[field] = (low, high, mean)
        return TelemetryRollupHour(
            unit_id=values["unit_id"],
            hour_start=parse_history_timestamp(values["hour_start"]),
            metrics=metrics,
            sample_count=int(values["sample_count"]),
            worst_quality=str(values["worst_quality"]),
        )
