"""Consistent backup and validated restore for the durable SQLite store.

API_CONTRACTS "Operations surface (Milestone C)": ``db backup --out FILE``
produces a consistent snapshot through the SQLite backup API — never a
mid-write file copy — and refuses to overwrite an existing file. ``db
restore --in FILE`` validates the backup's schema version and integrity
before swapping it in atomically (temp file + rename in the target's own
directory), and refuses while a server holds the database open. Both paths
write only scratch files they own and clean them up on every failure, so a
refused operation leaves the live database byte-for-byte untouched.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Final

from energypod.db.schema import (
    DatabaseBusyError,
    DatabaseLifecycleError,
    DatabaseNotFoundError,
    ensure_supported_version,
    read_schema_version,
)

_TEMP_PURPOSE_BACKUP: Final = "backup"
_TEMP_PURPOSE_RESTORE: Final = "restore"


class BackupError(DatabaseLifecycleError):
    """The snapshot could not be produced or verified."""


class BackupTargetExistsError(BackupError):
    """The requested backup target already exists; overwriting is refused."""


class RestoreError(DatabaseLifecycleError):
    """The restore could not be validated or swapped in."""


class InvalidBackupError(RestoreError):
    """The backup file is not a usable controller database snapshot."""


def _is_busy(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    return connection


def _integrity_check(connection: sqlite3.Connection) -> str:
    """The ``PRAGMA integrity_check`` verdict; ``"ok"`` means healthy."""
    row = connection.execute("PRAGMA integrity_check").fetchone()
    return "" if row is None else str(row[0])


def _temp_path(target: Path, purpose: str) -> Path:
    """A scratch sibling of ``target`` so the final rename is same-volume."""
    return target.parent / (f".{target.name}.{purpose}-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp")


def _remove_quietly(*paths: Path) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


def _siblings(path: Path) -> tuple[Path, ...]:
    return (path, Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm"))


def _copy_by_backup_api(source: Path, temp: Path, busy_timeout_ms: int) -> None:
    """Copy ``source`` into ``temp`` through the SQLite backup API."""
    try:
        source_connection = _connect(source, busy_timeout_ms)
    except sqlite3.Error as exc:
        if _is_busy(exc):
            raise DatabaseBusyError(
                f"the database {source} is busy (is a server holding it open?): {exc}"
            ) from exc
        raise InvalidBackupError(f"the backup source cannot be opened: {exc}") from exc
    try:
        destination = sqlite3.connect(temp, timeout=busy_timeout_ms / 1000, isolation_level=None)
        try:
            source_connection.backup(destination)
        finally:
            destination.close()
    except sqlite3.Error as exc:
        if _is_busy(exc):
            raise DatabaseBusyError(
                f"the database {source} is busy (is a server holding it open?): {exc}"
            ) from exc
        raise InvalidBackupError(
            f"the backup source is not a readable SQLite database: {exc}"
        ) from exc
    finally:
        source_connection.close()


def backup_database(source: Path, target: Path, *, busy_timeout_ms: int = 250) -> None:
    """Write a consistent snapshot of ``source`` to the new file ``target``."""
    if target.exists():
        raise BackupTargetExistsError(
            f"backup target already exists (overwriting is refused): {target}"
        )
    if not source.is_file():
        raise DatabaseNotFoundError(f"database not found: {source}")
    if not target.parent.is_dir():
        raise BackupError(f"backup target directory does not exist: {target.parent}")
    temp = _temp_path(target, _TEMP_PURPOSE_BACKUP)
    try:
        _copy_by_backup_api(source, temp, busy_timeout_ms)
        verification = _connect(temp, busy_timeout_ms)
        try:
            verdict = _integrity_check(verification)
        except sqlite3.Error as exc:
            raise BackupError(f"the snapshot could not be verified: {exc}") from exc
        finally:
            verification.close()
        if verdict != "ok":
            raise BackupError(f"the snapshot failed its integrity check: {verdict}")
        if target.exists():  # pragma: no cover - racing creator
            raise BackupTargetExistsError(
                f"backup target appeared while writing (overwriting is refused): {target}"
            )
        try:
            os.replace(temp, target)
        except OSError as exc:
            raise BackupError(f"cannot move the snapshot into place: {exc}") from exc
    finally:
        _remove_quietly(*_siblings(temp))


def _in_use_message(target: Path, reason: object) -> str:
    return (
        f"the database {target} is in use (a server or another connection holds "
        f"it open; stop the controller before restoring): {reason}"
    )


def _refuse_if_wal_siblings_survive(target: Path) -> None:
    """A clean close deletes WAL siblings; survivors prove an open connection.

    SQLite removes ``-wal``/``-shm`` when the last connection closes cleanly,
    so after our probe connection has closed, any surviving sibling means
    another connection still holds the database open — exactly the server
    a restore must never swap files under.
    """
    for sibling in (Path(f"{target}-wal"), Path(f"{target}-shm")):
        if sibling.exists():
            raise DatabaseBusyError(
                _in_use_message(target, f"{sibling.name} survived a clean close")
            )


def _assert_not_in_use(target: Path, busy_timeout_ms: int) -> None:
    """Refuse when another connection holds the target database open.

    Active transactions are caught directly: a truncating WAL checkpoint
    fails while a writer is active and ``BEGIN IMMEDIATE`` fails while any
    rollback-journal reader or WAL writer is active. Idle open connections
    are caught structurally afterwards: the probe's own clean close deletes
    the WAL siblings it created, so siblings that survive belong to another
    connection. An idle connection holding no file state at all cannot be
    detected portably — the operator contract is "stop the server first".
    """
    try:
        connection = _connect(target, busy_timeout_ms)
    except sqlite3.Error as exc:
        raise DatabaseBusyError(
            f"the database {target} cannot be checked because it is in use "
            f"(stop the controller before restoring): {exc}"
        ) from exc
    try:
        try:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise DatabaseBusyError(_in_use_message(target, exc)) from exc
            raise RestoreError(f"the current database cannot be checked: {exc}") from exc
        if row is None or row[0] != 0:
            raise DatabaseBusyError(_in_use_message(target, "the checkpoint could not complete"))
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise DatabaseBusyError(_in_use_message(target, exc)) from exc
            raise RestoreError(f"the current database cannot be locked: {exc}") from exc
    finally:
        connection.close()
    _refuse_if_wal_siblings_survive(target)


def restore_database(source: Path, target: Path, *, busy_timeout_ms: int = 250) -> int:
    """Validate ``source`` and atomically swap it in as ``target``.

    Returns the validated schema version of the restored database. The swap
    is a rename of a validated copy within the target's own directory, so the
    target is either the old database or the complete new one — never a
    half-written mixture.
    """
    if not source.is_file():
        raise InvalidBackupError(f"backup file not found: {source}")
    if not target.parent.is_dir():
        raise RestoreError(f"restore target directory does not exist: {target.parent}")
    temp = _temp_path(target, _TEMP_PURPOSE_RESTORE)
    try:
        _copy_by_backup_api(source, temp, busy_timeout_ms)
        validation = _connect(temp, busy_timeout_ms)
        try:
            try:
                verdict = _integrity_check(validation)
                version = read_schema_version(validation)
            except sqlite3.Error as exc:
                raise InvalidBackupError(
                    f"the backup is not a readable SQLite database: {exc}"
                ) from exc
        finally:
            validation.close()
        if verdict != "ok":
            raise InvalidBackupError(f"backup integrity check failed: {verdict}")
        if version is None:
            raise InvalidBackupError(
                "the backup carries no schema_version (a pre-schema database "
                "cannot be restored; migrate it first)"
            )
        ensure_supported_version(version)
        if target.exists():
            _assert_not_in_use(target, busy_timeout_ms)
        try:
            os.replace(temp, target)
        except OSError as exc:
            if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
                # Windows sharing violation: another handle still holds the
                # target open even though every probe looked clean.
                raise DatabaseBusyError(_in_use_message(target, exc)) from exc
            raise RestoreError(f"cannot swap the restored database into place: {exc}") from exc
        # Stale WAL siblings of the replaced file must not outlive it.
        _remove_quietly(Path(f"{target}-journal"), Path(f"{target}-wal"), Path(f"{target}-shm"))
        return version
    finally:
        _remove_quietly(*_siblings(temp))
