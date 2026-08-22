"""SQLite WAL repositories for audit facts and versioned schedules."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn

from energypod.db.schema import apply_pending_migrations
from energypod.domain.audit import AuditEvent, DuplicateAuditEventError
from energypod.domain.intents import Direction, IntentSource
from energypod.domain.observations import UnitLifecycle
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
    if isinstance(value, tuple | list | set | frozenset):
        return [_json_value(item) for item in value]
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
        try:
            with self._database.lock:
                self._database.connection.execute("BEGIN IMMEDIATE")
                try:
                    self._database.connection.execute(
                        """INSERT INTO audit_events(
                               event_id, unit_id, monotonic_offset_s, payload
                           ) VALUES (?, ?, ?, ?)""",
                        (event.event_id, event.unit_id, event.monotonic_offset_s, payload),
                    )
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
        expected = frozenset(AuditEvent.model_fields)
        _require_exact_keys(values, expected, "audit event")
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
            _require_exact_keys(item, entry_keys, "schedule entry")
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
            )
            for item in value["entries"]
        )
        return SchedulePlan(version=value["version"], timezone=value["timezone"], entries=entries)
