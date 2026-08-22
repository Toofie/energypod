"""Schema versioning and transactional migrations for the durable store.

API_CONTRACTS "Operations surface (Milestone C)": SQLite durable stores carry
a ``schema_version`` from day one. Every connection the controller opens runs
the same migration path, so a fresh database, a pre-schema database, and a
database maintained by ``energypod db migrate`` all converge on one stamped
version. Unknown or newer stored versions are refused fail-closed rather than
guessed at; migrations apply inside one ``BEGIN IMMEDIATE`` transaction each,
so a failed migration leaves the previously stamped version and schema
untouched.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SCHEMA_VERSION: Final[int] = 1
BASELINE_VERSION: Final[int] = 0

_CREATE_VERSION_TABLE: Final[str] = """CREATE TABLE IF NOT EXISTS schema_version (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL UNIQUE
)"""
_STAMP_VERSION: Final[str] = (
    "INSERT INTO schema_version(singleton, version) VALUES (1, ?)"
    " ON CONFLICT(singleton) DO UPDATE SET version = excluded.version"
)


@dataclass(frozen=True)
class Migration:
    """One atomic step from the previous version to ``version``."""

    version: int
    statements: tuple[str, ...]


@dataclass(frozen=True)
class MigrationResult:
    """The outcome of one migration pass over a database."""

    version: int
    applied: tuple[int, ...]


# The migration chain. Version 1 is the schema the shipped stores already
# create (audit_events, active_schedule) plus the version table itself, so a
# pre-schema database upgrades in place without any data change.
MIGRATIONS: tuple[Migration, ...] = (Migration(version=1, statements=(_CREATE_VERSION_TABLE,)),)


class DatabaseLifecycleError(RuntimeError):
    """Base for every structured database-maintenance failure."""


class SchemaVersionError(DatabaseLifecycleError):
    """The stored schema version cannot be handled by this controller."""


class UnknownSchemaVersionError(SchemaVersionError):
    """The stored version is not part of the known migration chain."""


class NewerSchemaVersionError(SchemaVersionError):
    """The stored version is newer than this controller understands."""


class DatabaseNotFoundError(DatabaseLifecycleError):
    """The configured database file is absent; maintenance refuses to create one."""


class DatabaseBusyError(DatabaseLifecycleError):
    """Another process (typically a running server) holds the database."""


def _is_busy(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _known_versions() -> frozenset[int]:
    return frozenset(BASELINE_VERSION + index + 1 for index in range(len(MIGRATIONS)))


def _validate_chain() -> None:
    expected = tuple(BASELINE_VERSION + index + 1 for index in range(len(MIGRATIONS)))
    if tuple(migration.version for migration in MIGRATIONS) != expected:
        raise SchemaVersionError(
            f"the migration chain is malformed: {[m.version for m in MIGRATIONS]}"
        )


def read_schema_version(connection: sqlite3.Connection) -> int | None:
    """The stamped schema version, or ``None`` when no version table exists."""
    present = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if present is None:
        return None
    try:
        stamped = connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SchemaVersionError(f"the schema_version table is unreadable: {exc}") from exc
    if stamped is None:
        raise UnknownSchemaVersionError(
            "the schema_version table exists but carries no version row"
        )
    value = stamped[0]
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnknownSchemaVersionError(f"unreadable schema version: {value!r}")
    return int(value)


def ensure_supported_version(version: int) -> None:
    """Refuse versions this controller cannot account for, fail-closed."""
    if version > SCHEMA_VERSION:
        raise NewerSchemaVersionError(
            f"database schema version {version} is newer than this controller "
            f"supports (latest {SCHEMA_VERSION})"
        )
    if version not in _known_versions():
        raise UnknownSchemaVersionError(f"unknown database schema version: {version}")


def _apply_in_transaction(connection: sqlite3.Connection, migration: Migration) -> None:
    """Apply one migration atomically, stamping its version in the same txn."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_CREATE_VERSION_TABLE)
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(_STAMP_VERSION, (migration.version,))
        connection.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise


def apply_pending_migrations(connection: sqlite3.Connection) -> MigrationResult:
    """Bring one open connection up to the latest known schema version."""
    _validate_chain()
    current = read_schema_version(connection)
    if current is not None:
        ensure_supported_version(current)
    start = BASELINE_VERSION if current is None else current
    applied = tuple(migration.version for migration in MIGRATIONS if migration.version > start)
    for migration in MIGRATIONS:
        if migration.version <= start:
            continue
        _apply_in_transaction(connection, migration)
    return MigrationResult(version=applied[-1] if applied else start, applied=applied)


def migrate_file(path: Path, *, busy_timeout_ms: int = 250) -> MigrationResult:
    """Migrate the database file at ``path`` and close it again.

    The file must already exist: ``db migrate`` maintains a durable store, it
    never provisions one. A database held open by a server is refused with a
    clear error instead of waited on silently.
    """
    if not path.is_file():
        raise DatabaseNotFoundError(f"database not found: {path}")
    try:
        connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    except sqlite3.Error as exc:
        raise DatabaseLifecycleError(f"cannot open database {path}: {exc}") from exc
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        return apply_pending_migrations(connection)
    except sqlite3.DatabaseError as exc:
        if _is_busy(exc):
            raise DatabaseBusyError(
                f"the database {path} is busy (is a server holding it open?): {exc}"
            ) from exc
        raise DatabaseLifecycleError(f"cannot migrate database {path}: {exc}") from exc
    finally:
        connection.close()
