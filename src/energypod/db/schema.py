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

SCHEMA_VERSION: Final[int] = 5
BASELINE_VERSION: Final[int] = 0

_CREATE_VERSION_TABLE: Final[str] = """CREATE TABLE IF NOT EXISTS schema_version (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL UNIQUE
)"""
_STAMP_VERSION: Final[str] = (
    "INSERT INTO schema_version(singleton, version) VALUES (1, ?)"
    " ON CONFLICT(singleton) DO UPDATE SET version = excluded.version"
)
# DESIGN_ENERGY_SCORECARD section 5 (E3): the per-day energy ledger keyed by
# local date and the durable live-day baseline keyed by unit.  Day records
# are projections, not acts -- the audit store is deliberately not
# overloaded for this.
_CREATE_ENERGY_DAY_TABLE: Final[str] = """CREATE TABLE IF NOT EXISTS energy_day (
    day TEXT PRIMARY KEY,
    payload TEXT NOT NULL
)"""
_CREATE_ENERGY_BASELINE_TABLE: Final[str] = """CREATE TABLE IF NOT EXISTS energy_baseline (
    unit_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
)"""
# DESIGN_PLANT_HISTORY section 2.2: the telemetry historian's append-only
# sample rows, clustered on (unit_id, sampled_at) with WITHOUT ROWID so both
# the append right-edge and the windowed range scan ride the primary key
# with no secondary index to maintain.  Every numeric column is NULL when
# the observation's datum was absent -- never zero-filled.
_CREATE_TELEMETRY_SAMPLE_TABLE: Final[str] = """CREATE TABLE IF NOT EXISTS telemetry_sample (
    unit_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    system_soc_pct REAL, bms_soc_pct REAL, soh_pct REAL,
    battery_watts REAL, grid_power_w REAL, load_power_w REAL,
    pack_voltage_v REAL, pack_current_a REAL,
    cell_min_v REAL, cell_max_v REAL, cell_spread_mv REAL,
    temperature_min_c REAL, temperature_max_c REAL,
    dynamic_charge_limit_w REAL, dynamic_discharge_limit_w REAL,
    lifecycle TEXT NOT NULL,
    health_state TEXT,
    quality TEXT NOT NULL,
    commanded_source TEXT,
    commanded_direction TEXT,
    commanded_w INTEGER,
    debug_mode_w INTEGER, ctrl_mode_w INTEGER, work_mode_w INTEGER, run_mode_w INTEGER,
    PRIMARY KEY (unit_id, sampled_at)
) WITHOUT ROWID"""
# DESIGN_PLANT_HISTORY section 2.3: the hourly rollups keyed by UTC hour (no
# DST ambiguity in storage; the console renders site-local).  An hour with
# zero samples writes NO row -- an absent hour is a gap, never a zeroed
# hour -- and each numeric field carries the hour's min/max/mean triple.
_ROLLUP_METRIC_COLUMNS: Final[str] = ",\n    ".join(
    f"{field}_min REAL, {field}_max REAL, {field}_mean REAL"
    for field in (
        "system_soc_pct",
        "bms_soc_pct",
        "soh_pct",
        "battery_watts",
        "grid_power_w",
        "load_power_w",
        "pack_voltage_v",
        "pack_current_a",
        "cell_min_v",
        "cell_max_v",
        "cell_spread_mv",
        "temperature_min_c",
        "temperature_max_c",
        "dynamic_charge_limit_w",
        "dynamic_discharge_limit_w",
    )
)
_CREATE_TELEMETRY_ROLLUP_TABLE: Final[str] = (
    """CREATE TABLE IF NOT EXISTS telemetry_rollup_hourly (
    unit_id TEXT NOT NULL,
    hour_start TEXT NOT NULL,
    """
    + _ROLLUP_METRIC_COLUMNS
    + """,
    sample_count INTEGER NOT NULL,
    worst_quality TEXT NOT NULL,
    PRIMARY KEY (unit_id, hour_start)
) WITHOUT ROWID"""
)
# DESIGN_POD_PARKING section 4: the dedicated durable parking-lease table --
# one row per unit, the machine truth beside the audit trail's narrative (the
# in-memory audit store is a bounded deque and would evict leases in simulator
# mode, so audit replay is deliberately NOT the lease store).  ``opened_at``
# is the wire's ``parked_at`` (the same instant; both spellings live in the
# contracts).  Wall-clock ISO-8601 timestamps because downtime spans restarts
# and boot reconstruction must judge "expired while we were down" from the row
# alone.  The close-context columns (``closed_at``, ``write_unverified``,
# ``foreign_rewrite``) carry the terminal sub-states of DESIGN section 3; a
# terminal row is KEPT -- the ``epoch`` column is the per-unit monotonic
# single-flight counter, so a new park over a closed lease mints epoch+1 and a
# mutation on a closed epoch refuses instead of acting on a stale view.
_CREATE_PARK_LEASE_TABLE: Final[str] = """CREATE TABLE IF NOT EXISTS park_leases (
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
# DESIGN_NIGHT_CHARGE_V2 section 3.2: the per-day durable trust records --
# one row per SCORED morning (the energy_day precedent: the civil date is the
# key, the payload is the record's whole JSON).  Excluded days (full-posture
# mornings, fallback nights, incomplete historian records) deliberately
# leave NO row: they fail to inform, never to pass.
_CREATE_NIGHT_TRUST_DAY_TABLE: Final[str] = """CREATE TABLE IF NOT EXISTS night_trust_day (
    day TEXT PRIMARY KEY,
    payload TEXT NOT NULL
)"""


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
# pre-schema database upgrades in place without any data change.  Version 2
# adds the energy scorecard's ledger tables (E3): energy_day keyed by local
# date, energy_baseline keyed by unit.  Version 3 adds the telemetry
# historian's tables (DESIGN_PLANT_HISTORY section 2.2): telemetry_sample
# and telemetry_rollup_hourly -- an in-place upgrade that touches no
# existing table.  Version 4 adds the pod-parking lease table
# (DESIGN_POD_PARKING section 4): park_leases keyed by unit, written in the
# same transaction boundary as the parking audit append -- again an
# in-place upgrade that touches no existing table.  Version 5 adds the
# night-charge trust day table (DESIGN_NIGHT_CHARGE_V2 section 3.2):
# night_trust_day keyed by civil date, one payload per scored morning.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, statements=(_CREATE_VERSION_TABLE,)),
    Migration(
        version=2,
        statements=(_CREATE_ENERGY_DAY_TABLE, _CREATE_ENERGY_BASELINE_TABLE),
    ),
    Migration(
        version=3,
        statements=(_CREATE_TELEMETRY_SAMPLE_TABLE, _CREATE_TELEMETRY_ROLLUP_TABLE),
    ),
    Migration(version=4, statements=(_CREATE_PARK_LEASE_TABLE,)),
    Migration(version=5, statements=(_CREATE_NIGHT_TRUST_DAY_TABLE,)),
)


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
