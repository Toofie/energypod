"""Database lifecycle operations: schema versioning, backup, restore.

This package owns the durable store's maintenance surface (API_CONTRACTS
"Operations surface (Milestone C)"). It depends only on the standard
library's ``sqlite3`` — never on the adapter stack, the runtime, or any
hardware-touching module — so the ``energypod db`` commands can run against
a parked controller without composing anything.
"""

from __future__ import annotations

from energypod.db.backup import (
    BackupError,
    BackupTargetExistsError,
    InvalidBackupError,
    RestoreError,
    backup_database,
    restore_database,
)
from energypod.db.schema import (
    BASELINE_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION,
    DatabaseBusyError,
    DatabaseLifecycleError,
    DatabaseNotFoundError,
    Migration,
    MigrationResult,
    NewerSchemaVersionError,
    SchemaVersionError,
    UnknownSchemaVersionError,
    apply_pending_migrations,
    ensure_supported_version,
    migrate_file,
    read_schema_version,
)

__all__ = [
    "BASELINE_VERSION",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "BackupError",
    "BackupTargetExistsError",
    "DatabaseBusyError",
    "DatabaseLifecycleError",
    "DatabaseNotFoundError",
    "InvalidBackupError",
    "Migration",
    "MigrationResult",
    "NewerSchemaVersionError",
    "RestoreError",
    "SchemaVersionError",
    "UnknownSchemaVersionError",
    "apply_pending_migrations",
    "backup_database",
    "ensure_supported_version",
    "migrate_file",
    "read_schema_version",
    "restore_database",
]
