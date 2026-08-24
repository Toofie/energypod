"""Database lifecycle CLI contracts (API_CONTRACTS "Operations surface").

``energypod db`` owns the durable store's lifecycle: every SQLite database
the controller opens carries a ``schema_version`` from day one; ``db migrate``
applies pending migrations transactionally and refuses unknown/newer
versions; ``db backup --out FILE`` produces a consistent snapshot through the
SQLite backup API (never a mid-write file copy) and refuses to overwrite an
existing file; ``db restore --in FILE`` validates schema version and
integrity before an atomic temp-file + rename swap and refuses while a server
holds the database open. Database commands never compose a runtime, start a
server, or touch hardware. The production modules are imported lazily so
this red-phase suite collects before they exist; every missing contract
surfaces as an ordinary test failure, never a collection error.
"""

from __future__ import annotations

import importlib
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

try:
    from energypod.adapters.persistence.sqlite import (
        SQLiteAuditRepository,
        SQLiteDatabase,
        SQLiteScheduleRepository,
    )
    from energypod.db.schema import (
        MIGRATIONS,
        SCHEMA_VERSION,
        Migration,
        NewerSchemaVersionError,
        UnknownSchemaVersionError,
    )
except ImportError as exc:  # pragma: no cover - initial red phase only
    SQLiteAuditRepository: Any = None
    SQLiteDatabase: Any = None
    SQLiteScheduleRepository: Any = None
    MIGRATIONS: Any = None
    SCHEMA_VERSION: Any = None
    Migration: Any = None
    NewerSchemaVersionError: Any = None
    UnknownSchemaVersionError: Any = None
    _CONTRACT_IMPORT_ERROR: ImportError | None = exc
else:  # pragma: no cover - import succeeded
    _CONTRACT_IMPORT_ERROR = None


def _require_contract() -> None:
    assert _CONTRACT_IMPORT_ERROR is None, (
        f"The intended database-lifecycle contract is not implemented: {_CONTRACT_IMPORT_ERROR}"
    )


# ---------------------------------------------------------------------------
# Side-effect audit: database commands may write database files and nothing
# else. Any network, process, or serving activity fails the suite.
# ---------------------------------------------------------------------------

_FORBIDDEN_EVENT_PREFIXES = (
    "socket.",
    "subprocess.",
    "os.system",
    "os.posix_spawn",
    "os.exec",
    "os.spawn",
    "os.fork",
)
_AUDIT_EVENTS: list[str] = []


def _record_audit_event(event: str, args: tuple[Any, ...]) -> None:
    if event.startswith(_FORBIDDEN_EVENT_PREFIXES):
        _AUDIT_EVENTS.append(event)


sys.addaudithook(_record_audit_event)


@dataclass
class _ActivityWindow:
    """Scoped view over the process audit log for one CLI invocation."""

    seen: list[str] = field(default_factory=list)
    _mark: int = field(default=0, init=False, repr=False)

    def __enter__(self) -> _ActivityWindow:
        self._mark = len(_AUDIT_EVENTS)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.seen = list(_AUDIT_EVENTS[self._mark :])


# ---------------------------------------------------------------------------
# CLI harness and configuration fixtures.
# ---------------------------------------------------------------------------


@dataclass
class Result:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return self.stdout + self.stderr

    def report(self) -> str:
        return (
            f"argv={self.argv!r} exit_code={self.exit_code!r} "
            f"stdout={self.stdout!r} stderr={self.stderr!r}"
        )


@pytest.fixture
def main_module() -> Any:
    return importlib.import_module("energypod.main")


def run_cli(main_module: Any, argv: list[str], capsys: pytest.CaptureFixture[str]) -> Result:
    """Invoke main in-process; SystemExit escapes fail the test loudly."""
    capsys.readouterr()
    try:
        exit_code = main_module.main(argv)
    except SystemExit as exc:  # pragma: no cover - contract breach
        pytest.fail(f"db commands must return an exit code, not raise SystemExit: {exc!r}")
    captured = capsys.readouterr()
    return Result(list(argv), exit_code, captured.out, captured.err)


def _timing_payload() -> dict[str, Any]:
    return {
        "device_command_expiry_s": 2.35,
        "device_command_expiry_evidence": "commissioning://watchdog-trial-2026-08/rev-1",
        "control_period_s": 0.40,
        "essential_read_timeout_s": 0.10,
        "kernel_timeout_s": 0.05,
        "audit_timeout_s": 0.05,
        "write_timeout_s": 0.10,
        "acknowledgement_timeout_s": 0.10,
        "maximum_jitter_s": 0.10,
        "renewal_margin_s": 0.50,
    }


def _unit_payload(unit_id: str, port: int) -> dict[str, Any]:
    return {
        "unit_id": unit_id,
        "display_name": unit_id.capitalize(),
        "endpoint": {"host": "127.0.0.1", "port": port},
        "transport_profile": "waveshare_rtu_over_tcp",
        "protocol_profile": "iot",
        "device_id": 4,
        "expected_identity": f"BEP-{unit_id.upper()}",
        "expected_cell_count": 59,
    }


def _valid_payload(database: Path, *, with_storage: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "revision": 7,
        "mode": "observe_only",
        "site": {
            "site_id": "db-lifecycle-site",
            "timezone": "Australia/Brisbane",
            "expected_unit_count": 2,
        },
        "units": [
            _unit_payload("db-mid", 4196),
            _unit_payload("db-rhs", 4197),
        ],
        "timing": _timing_payload(),
    }
    if with_storage:
        payload["storage"] = {"database_path": str(database), "busy_timeout_ms": 250}
    return payload


def _write_config(path: Path, database: Path, *, with_storage: bool = True) -> None:
    path.write_text(
        yaml.safe_dump(_valid_payload(database, with_storage=with_storage)),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Database helpers built from raw sqlite3 so the tests never depend on the
# implementation under test to construct their fixtures.
# ---------------------------------------------------------------------------

_LEGACY_AUDIT_TABLE = """CREATE TABLE audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    unit_id TEXT,
    monotonic_offset_s REAL NOT NULL,
    payload TEXT NOT NULL
)"""
_LEGACY_SCHEDULE_TABLE = """CREATE TABLE active_schedule (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    payload TEXT NOT NULL
)"""


def _create_legacy_database(path: Path, *, event_ids: tuple[str, ...] = ("legacy-1",)) -> None:
    """A database as the pre-schema builds wrote it: tables, no version."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(_LEGACY_AUDIT_TABLE)
        connection.execute(_LEGACY_SCHEDULE_TABLE)
        for event_id in event_ids:
            connection.execute(
                "INSERT INTO audit_events(event_id, unit_id, monotonic_offset_s, payload)"
                " VALUES (?, 'db-mid', 0.0, '{}')",
                (event_id,),
            )
        connection.execute(
            "INSERT INTO active_schedule(singleton, version, payload) VALUES (1, 4, '{}')"
        )
        connection.commit()
    finally:
        connection.close()


def _forge_version(path: Path, version: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1),"
            "version INTEGER NOT NULL UNIQUE)"
        )
        connection.execute(
            "INSERT INTO schema_version(singleton, version) VALUES (1, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET version = excluded.version",
            (version,),
        )
        connection.commit()
    finally:
        connection.close()


def _open_raw(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _stored_version(path: Path) -> int | None:
    """The stamped schema version, or None when the table is absent."""
    connection = _open_raw(path)
    try:
        row = connection.execute(
            "SELECT version FROM schema_version WHERE singleton = 1"
        ).fetchone()
    finally:
        connection.close()
    return None if row is None else int(row[0])


def _audit_rows(path: Path) -> int:
    connection = _open_raw(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
    finally:
        connection.close()


def _has_table(path: Path, table: str) -> bool:
    connection = _open_raw(path)
    try:
        row = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    finally:
        connection.close()
    return row is not None


def _integrity_is_ok(path: Path) -> bool:
    connection = _open_raw(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    return bool(row) and row[0] == "ok"


def _append_audit_row(database: SQLiteDatabase, event_id: str) -> None:
    """Commit one audit row through the live store connection (WAL path)."""
    with database.lock:
        database.connection.execute(
            "INSERT INTO audit_events(event_id, unit_id, monotonic_offset_s, payload)"
            " VALUES (?, 'db-mid', 1.0, '{}')",
            (event_id,),
        )


def _no_temporary_files(directory: Path) -> list[str]:
    """Hidden scratch files a failed lifecycle command must never leave."""
    return sorted(child.name for child in directory.iterdir() if child.name.startswith("."))


# ---------------------------------------------------------------------------
# schema_version from day one on the durable stores.
# ---------------------------------------------------------------------------


def test_schema_version_is_stamped_from_day_one(tmp_path: Path) -> None:
    """Opening a fresh database stamps the latest known version before
    anything else runs (version 5 added the night-trust day table)."""
    _require_contract()
    assert SCHEMA_VERSION == 6
    database = SQLiteDatabase(tmp_path / "controller.sqlite3")
    database.open()
    try:
        connection = database.connection
        row = connection.execute("SELECT singleton, version FROM schema_version").fetchone()
        assert row == (1, SCHEMA_VERSION)
        for table in (
            "energy_day",
            "energy_baseline",
            "telemetry_sample",
            "telemetry_rollup_hourly",
            "park_leases",
            "night_trust_day",
        ):
            present = connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            assert present is not None, table
    finally:
        database.close()


def test_audit_and_schedule_stores_share_one_version_row(tmp_path: Path) -> None:
    """Both durable stores live in one database with exactly one version row."""
    _require_contract()
    database = SQLiteDatabase(tmp_path / "controller.sqlite3")
    database.open()
    try:
        SQLiteAuditRepository(database)
        SQLiteScheduleRepository(database)
        rows = database.connection.execute(
            "SELECT singleton, version FROM schema_version"
        ).fetchall()
        assert rows == [(1, SCHEMA_VERSION)]
        assert _stored_version(tmp_path / "controller.sqlite3") == SCHEMA_VERSION
    finally:
        database.close()


def test_existing_store_gains_the_version_table_without_losing_data(
    tmp_path: Path,
) -> None:
    """A pre-schema database is upgraded in place via the migration path."""
    _require_contract()
    path = tmp_path / "controller.sqlite3"
    _create_legacy_database(path)
    database = SQLiteDatabase(path)
    database.open()
    try:
        assert _stored_version(path) == SCHEMA_VERSION
        assert _audit_rows(path) == 1
        assert database.pragma("journal_mode").lower() == "wal"
    finally:
        database.close()


def test_open_refuses_a_newer_schema_version(tmp_path: Path) -> None:
    _require_contract()
    path = tmp_path / "controller.sqlite3"
    _forge_version(path, SCHEMA_VERSION + 1)
    database = SQLiteDatabase(path)
    with pytest.raises(NewerSchemaVersionError):
        database.open()


def test_open_refuses_an_unknown_schema_version(tmp_path: Path) -> None:
    _require_contract()
    path = tmp_path / "controller.sqlite3"
    _forge_version(path, -1)
    database = SQLiteDatabase(path)
    with pytest.raises(UnknownSchemaVersionError):
        database.open()


# ---------------------------------------------------------------------------
# energypod db migrate
# ---------------------------------------------------------------------------


def test_db_migrate_applies_pending_migrations_to_a_legacy_database(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _require_contract()
    database = tmp_path / "controller.sqlite3"
    config = tmp_path / "controller.yaml"
    _create_legacy_database(database)
    _write_config(config, database)

    first = run_cli(main_module, ["db", "migrate", str(config)], capsys)
    assert first.exit_code == 0, first.report()
    assert str(SCHEMA_VERSION) in first.stdout, first.report()
    assert _stored_version(database) == SCHEMA_VERSION
    assert _audit_rows(database) == 1

    second = run_cli(main_module, ["db", "migrate", "--config", str(config)], capsys)
    assert second.exit_code == 0, second.report()
    assert "already" in second.stdout, second.report()
    assert _stored_version(database) == SCHEMA_VERSION
    assert _audit_rows(database) == 1


@pytest.mark.parametrize("kind", ["newer", "unknown"])
def test_db_migrate_refuses_unknown_and_newer_versions(
    main_module: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    kind: str,
) -> None:
    _require_contract()
    forged = SCHEMA_VERSION + 1 if kind == "newer" else -1
    database = tmp_path / "controller.sqlite3"
    config = tmp_path / "controller.yaml"
    _create_legacy_database(database)
    _forge_version(database, forged)
    _write_config(config, database)

    result = run_cli(main_module, ["db", "migrate", str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert kind in result.stderr, result.report()
    assert _stored_version(database) == forged
    assert _audit_rows(database) == 1


def test_db_migration_failure_rolls_back_transactionally(
    main_module: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing migration leaves the stamped version and schema untouched."""
    _require_contract()
    schema = importlib.import_module("energypod.db.schema")
    database = tmp_path / "controller.sqlite3"
    config = tmp_path / "controller.yaml"
    seeded = SQLiteDatabase(database)
    seeded.open()
    seeded.close()
    _write_config(config, database)

    last_real = MIGRATIONS[-1].version
    broken = (
        *MIGRATIONS,
        Migration(
            version=last_real + 1,
            statements=("CREATE TABLE rollback_probe (id INTEGER)",),
        ),
        Migration(
            version=last_real + 2,
            statements=(
                "CREATE TABLE partial_probe (id INTEGER)",
                "DROP TABLE must_not_exist_anywhere",
            ),
        ),
    )
    monkeypatch.setattr(schema, "MIGRATIONS", broken)

    result = run_cli(main_module, ["db", "migrate", str(config)], capsys)

    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert _stored_version(database) == last_real + 1, (
        f"migration {last_real + 1} committed before {last_real + 2} failed"
    )
    assert not _has_table(database, "partial_probe"), "the failed migration rolled back"
    assert _no_temporary_files(tmp_path) == []


def test_db_migrate_refuses_a_missing_database_and_creates_nothing(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "absent.sqlite3"
    config = tmp_path / "controller.yaml"
    _write_config(config, database)
    result = run_cli(main_module, ["db", "migrate", str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert "not found" in result.stderr, result.report()
    assert not database.exists()
    assert {child.name for child in tmp_path.iterdir()} == {"controller.yaml"}


def test_db_migrate_never_composes_serves_or_touches_the_network(
    main_module: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "controller.sqlite3"
    config = tmp_path / "controller.yaml"
    _create_legacy_database(database)
    _write_config(config, database)
    calls: list[Any] = []

    def exploding_build(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))
        raise AssertionError("db migrate must never compose a runtime")

    composition = importlib.import_module("energypod.runtime.composition")
    monkeypatch.setattr(composition, "build_runtime", exploding_build)

    import uvicorn

    def exploding_serve(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("db migrate must never start a server")

    monkeypatch.setattr(uvicorn, "run", exploding_serve)
    monkeypatch.setattr(uvicorn.Server, "serve", exploding_serve)

    with _ActivityWindow() as window:
        result = run_cli(main_module, ["db", "migrate", str(config)], capsys)
    assert result.exit_code == 0, result.report()
    assert calls == []
    assert window.seen == []


# ---------------------------------------------------------------------------
# energypod db backup --out FILE
# ---------------------------------------------------------------------------


def test_db_backup_writes_a_consistent_snapshot_through_the_backup_api(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The snapshot includes WAL-resident commits a raw file copy would miss."""
    _require_contract()
    database_path = tmp_path / "controller.sqlite3"
    target = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    database = SQLiteDatabase(database_path)
    database.open()
    try:
        SQLiteAuditRepository(database)
        SQLiteScheduleRepository(database)
        _append_audit_row(database, "wal-only-1")
        _append_audit_row(database, "wal-only-2")

        _write_config(config, database_path)
        with _ActivityWindow() as window:
            result = run_cli(
                main_module,
                ["db", "backup", "--out", str(target), "--config", str(config)],
                capsys,
            )
        assert result.exit_code == 0, result.report()
        assert str(target) in result.stdout, result.report()
        assert window.seen == []

        assert target.exists()
        assert _stored_version(target) == SCHEMA_VERSION
        assert _audit_rows(target) == 2
        assert _integrity_is_ok(target)

        raw_copy = tmp_path / "raw-copy-probe.sqlite3"
        shutil.copyfile(database_path, raw_copy)
        assert not _has_table(raw_copy, "audit_events"), (
            "the raw main file must not contain the WAL-resident rows; "
            "only a backup-API snapshot can see them"
        )
    finally:
        database.close()
    assert _no_temporary_files(tmp_path) == []


def test_db_backup_refuses_to_overwrite_an_existing_file(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "controller.sqlite3"
    target = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    seeded = SQLiteDatabase(database)
    seeded.open()
    seeded.close()
    target.write_bytes(b"precious existing content")
    _write_config(config, database)

    result = run_cli(main_module, ["db", "backup", "--out", str(target), str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert "exists" in result.stderr, result.report()
    assert target.read_bytes() == b"precious existing content"
    assert _no_temporary_files(tmp_path) == []


def test_db_backup_refuses_a_missing_database_and_writes_nothing(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "absent.sqlite3"
    target = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _write_config(config, database)
    result = run_cli(main_module, ["db", "backup", "--out", str(target), str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert "not found" in result.stderr, result.report()
    assert not target.exists()
    assert not database.exists()
    assert {child.name for child in tmp_path.iterdir()} == {"controller.yaml"}


# ---------------------------------------------------------------------------
# energypod db restore --in FILE
# ---------------------------------------------------------------------------


def _seeded_database(path: Path, *, rows: int) -> None:
    database = SQLiteDatabase(path)
    database.open()
    try:
        SQLiteAuditRepository(database)
        for index in range(rows):
            _append_audit_row(database, f"seed-{index}")
    finally:
        database.close()


def _add_row(path: Path, event_id: str) -> None:
    connection = _open_raw(path)
    try:
        connection.execute(
            "INSERT INTO audit_events(event_id, unit_id, monotonic_offset_s, payload)"
            " VALUES (?, 'db-mid', 2.0, '{}')",
            (event_id,),
        )
    finally:
        connection.close()


def _make_backup(source: Path, backup: Path) -> None:
    connection = _open_raw(source)
    try:
        destination = sqlite3.connect(backup, isolation_level=None)
        try:
            connection.backup(destination)
        finally:
            destination.close()
    finally:
        connection.close()


def test_db_restore_validates_and_swaps_atomically(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _require_contract()
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    _make_backup(database, backup)
    _add_row(database, "post-backup-3")
    assert _audit_rows(database) == 3
    _write_config(config, database)

    with _ActivityWindow() as window:
        result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 0, result.report()
    assert "restored" in result.stdout, result.report()
    assert str(SCHEMA_VERSION) in result.stdout, result.report()
    assert window.seen == []

    assert _audit_rows(database) == 2, "the backup content replaced the live database"
    assert _stored_version(database) == SCHEMA_VERSION
    assert _integrity_is_ok(database)
    assert {child.name for child in tmp_path.iterdir()} == {
        "controller.yaml",
        "controller.sqlite3",
        "snapshot.sqlite3",
    }, "the swap must leave neither temporary files nor stale WAL siblings"


def test_db_restore_provisions_a_missing_target(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _require_contract()
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    _make_backup(database, backup)
    database.unlink()
    _write_config(config, database)

    result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 0, result.report()
    assert _audit_rows(database) == 2
    assert _stored_version(database) == SCHEMA_VERSION


@pytest.mark.parametrize("lock_kind", ["write_transaction", "open_read_transaction"])
def test_db_restore_refuses_while_another_connection_holds_the_database(
    main_module: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    lock_kind: str,
) -> None:
    _require_contract()
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    _make_backup(database, backup)
    _add_row(database, "post-backup-3")
    _write_config(config, database)

    holder = _open_raw(database)
    if lock_kind == "write_transaction":
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO audit_events(event_id, unit_id, monotonic_offset_s, payload)"
            " VALUES ('held-4', 'db-mid', 3.0, '{}')"
        )
    else:
        holder.execute("BEGIN")
        holder.execute("SELECT COUNT(*) FROM audit_events").fetchone()

    result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert "in use" in result.stderr, result.report()
    assert _audit_rows(database) == 3, "a refused restore must not touch the database"
    assert _stored_version(database) == SCHEMA_VERSION

    holder.execute("ROLLBACK")
    holder.close()
    recovered = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert recovered.exit_code == 0, recovered.report()
    assert _audit_rows(database) == 2
    assert _no_temporary_files(tmp_path) == []


def test_db_restore_refuses_a_backup_with_a_newer_schema_version(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _require_contract()
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    _make_backup(database, backup)
    _forge_version(backup, SCHEMA_VERSION + 1)
    _write_config(config, database)

    result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert "newer" in result.stderr, result.report()
    assert _audit_rows(database) == 2, "the live database must remain untouched"
    assert _no_temporary_files(tmp_path) == []


def test_db_restore_refuses_a_backup_without_a_schema_version(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _require_contract()
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    _create_legacy_database(backup)
    _write_config(config, database)

    result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert "schema_version" in result.stderr, result.report()
    assert _audit_rows(database) == 2
    assert _no_temporary_files(tmp_path) == []


def test_db_restore_refuses_a_non_sqlite_backup_file(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    backup.write_bytes(b"this is definitely not a sqlite database")
    _write_config(config, database)

    result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert _audit_rows(database) == 2
    assert _no_temporary_files(tmp_path) == []


def test_db_restore_refuses_when_the_integrity_check_fails(
    main_module: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A readable but corrupt backup is refused before the swap."""
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    _make_backup(database, backup)
    _add_row(database, "post-backup-3")
    _write_config(config, database)

    backup_module = importlib.import_module("energypod.db.backup")
    monkeypatch.setattr(
        backup_module,
        "_integrity_check",
        lambda connection: "database disk image is malformed",
    )

    result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert "integrity" in result.stderr, result.report()
    assert _audit_rows(database) == 3, "the live database must remain untouched"
    assert _no_temporary_files(tmp_path) == []


def test_db_restore_refuses_a_missing_backup_file(
    main_module: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "absent-snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    _write_config(config, database)

    result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 1, result.report()
    assert "database error" in result.stderr, result.report()
    assert "not found" in result.stderr, result.report()
    assert _audit_rows(database) == 2
    assert not backup.exists()


def test_db_restore_never_composes_serves_or_touches_the_network(
    main_module: Any,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "controller.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    config = tmp_path / "controller.yaml"
    _seeded_database(database, rows=2)
    _make_backup(database, backup)
    _write_config(config, database)
    calls: list[Any] = []

    def exploding_build(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))
        raise AssertionError("db restore must never compose a runtime")

    composition = importlib.import_module("energypod.runtime.composition")
    monkeypatch.setattr(composition, "build_runtime", exploding_build)

    import uvicorn

    def exploding_serve(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("db restore must never start a server")

    monkeypatch.setattr(uvicorn, "run", exploding_serve)
    monkeypatch.setattr(uvicorn.Server, "serve", exploding_serve)

    with _ActivityWindow() as window:
        result = run_cli(main_module, ["db", "restore", "--in", str(backup), str(config)], capsys)
    assert result.exit_code == 0, result.report()
    assert calls == []
    assert window.seen == []
