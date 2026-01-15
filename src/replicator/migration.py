#!/usr/bin/env python3
# src/replicator/migration.py

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

try:
    from core.database.sqlite import SQLite
    from core.log import Log
except ImportError:
    from database.sqlite import SQLite
    from log import Log


class Migration:
    """Database schema creation + migrations for Replicator.

    This class intentionally *does not* re-implement a DB wrapper.
    It relies on corePY's `SQLite` for connections/transactions and only
    owns Replicator's schema + migration history + small meta KV helpers.
    """

    def __init__(self, db: SQLite, logger: Optional[Log] = None):
        self._db = db
        self._logger = logger

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure(self) -> None:
        """Ensure base tables exist and apply all pending migrations."""
        self._ensure_schema_migrations_table()
        self._apply_migrations(self._migrations())

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read a value from the `meta` table. Returns default if missing."""
        try:
            row = self._db.one("SELECT value FROM meta WHERE key = ?", (key,))
            if not row:
                return default
            val = row.get("value")
            return default if val is None else str(val)
        except Exception:
            return default

    def set_meta(self, key: str, value: Optional[str]) -> None:
        """Upsert a value into the `meta` table."""
        try:
            with self._db.transaction():
                exists = self._db.scalar("SELECT 1 FROM meta WHERE key = ? LIMIT 1", (key,))
                if exists:
                    self._db.execute(
                        "UPDATE meta SET value = ?, modified = CURRENT_TIMESTAMP WHERE key = ?",
                        (value, key),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?)",
                        (key, value),
                    )
        except Exception:
            # best effort
            pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _log(self, msg: str, *, level: str = "info", channel: str = "migration") -> None:
        if self._logger is not None and hasattr(self._logger, "append"):
            self._logger.append(msg, level=level, channel=channel)  # type: ignore[call-arg]
        else:
            print(msg)

    def _ensure_schema_migrations_table(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created DATETIME DEFAULT CURRENT_TIMESTAMP,
                modified DATETIME DEFAULT CURRENT_TIMESTAMP,
                name TEXT NOT NULL UNIQUE
            );
            """
        )

    def _is_applied(self, name: str) -> bool:
        r = self._db.scalar(
            "SELECT 1 FROM schema_migrations WHERE name = ? LIMIT 1;",
            (name,),
        )
        return bool(r)

    def _apply_migrations(self, migrations: Sequence[Tuple[str, Sequence[str]]]) -> None:
        for name, stmts in migrations:
            if self._is_applied(name):
                continue

            try:
                with self._db.transaction():
                    for stmt in stmts:
                        self._db.execute(stmt)
                    self._db.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))

                self._log(f"[Migration] applied {name}", level="debug")
            except Exception as e:
                raise RuntimeError(f"Failed to apply migration {name}: {e}")

    def _migrations(self) -> List[Tuple[str, List[str]]]:
        return [
            (
                "0001_init",
                [
                    # jobs
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created DATETIME DEFAULT CURRENT_TIMESTAMP,
                        modified DATETIME DEFAULT CURRENT_TIMESTAMP,
                        name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        mode TEXT NOT NULL DEFAULT 'mirror',
                        direction TEXT NOT NULL DEFAULT 'unidirectional',
                        allowDeletion INTEGER NOT NULL DEFAULT 0,
                        preserveMetadata INTEGER NOT NULL DEFAULT 1,
                        pairId TEXT NULL,
                        conflictPolicy TEXT NOT NULL DEFAULT 'newest',
                        lastRun TEXT NULL,
                        lastResult TEXT NULL,
                        lastError TEXT NULL
                    );
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_jobs_enabled ON jobs(enabled);",

                    # endpoints
                    """
                    CREATE TABLE IF NOT EXISTS endpoints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created DATETIME DEFAULT CURRENT_TIMESTAMP,
                        modified DATETIME DEFAULT CURRENT_TIMESTAMP,
                        jobId INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        type TEXT NOT NULL,
                        location TEXT NOT NULL,
                        port INTEGER NULL,
                        guest INTEGER NOT NULL DEFAULT 1,
                        username TEXT NULL,
                        password TEXT NULL,
                        useKey INTEGER NOT NULL DEFAULT 0,
                        sshKey TEXT NULL,
                        options TEXT NULL,
                        FOREIGN KEY(jobId) REFERENCES jobs(id) ON DELETE CASCADE
                    );
                    """,
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_endpoints_job_role ON endpoints(jobId, role);",

                    # schedule
                    """
                    CREATE TABLE IF NOT EXISTS schedule (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created DATETIME DEFAULT CURRENT_TIMESTAMP,
                        modified DATETIME DEFAULT CURRENT_TIMESTAMP,
                        jobId INTEGER NOT NULL UNIQUE,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        everyMinutes INTEGER NOT NULL DEFAULT 60,
                        nextRunAt TEXT NULL,
                        lastScheduledRunAt TEXT NULL,
                        FOREIGN KEY(jobId) REFERENCES jobs(id) ON DELETE CASCADE
                    );
                    """,

                    # runs
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created DATETIME DEFAULT CURRENT_TIMESTAMP,
                        modified DATETIME DEFAULT CURRENT_TIMESTAMP,
                        jobId INTEGER NOT NULL,
                        startedAt TEXT NOT NULL,
                        endedAt TEXT NULL,
                        result TEXT NOT NULL,
                        message TEXT NULL,
                        stats TEXT NULL,
                        FOREIGN KEY(jobId) REFERENCES jobs(id) ON DELETE CASCADE
                    );
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_runs_job_started ON runs(jobId, startedAt);",

                    # file_state
                    """
                    CREATE TABLE IF NOT EXISTS file_state (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created DATETIME DEFAULT CURRENT_TIMESTAMP,
                        modified DATETIME DEFAULT CURRENT_TIMESTAMP,
                        jobId INTEGER NOT NULL,
                        side TEXT NOT NULL,
                        relPath TEXT NOT NULL,
                        size INTEGER NOT NULL DEFAULT 0,
                        mtime INTEGER NOT NULL DEFAULT 0,
                        hash TEXT NULL,
                        isDir INTEGER NOT NULL DEFAULT 0,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        deletedAt TEXT NULL,
                        meta TEXT NULL,
                        FOREIGN KEY(jobId) REFERENCES jobs(id) ON DELETE CASCADE
                    );
                    """,
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_file_state ON file_state(jobId, side, relPath);",

                    # conflicts
                    """
                    CREATE TABLE IF NOT EXISTS conflicts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created DATETIME DEFAULT CURRENT_TIMESTAMP,
                        modified DATETIME DEFAULT CURRENT_TIMESTAMP,
                        jobId INTEGER NOT NULL,
                        runId INTEGER NULL,
                        relPath TEXT NOT NULL,
                        a_size INTEGER NULL,
                        a_mtime INTEGER NULL,
                        a_hash TEXT NULL,
                        b_size INTEGER NULL,
                        b_mtime INTEGER NULL,
                        b_hash TEXT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        resolution TEXT NULL,
                        note TEXT NULL,
                        FOREIGN KEY(jobId) REFERENCES jobs(id) ON DELETE CASCADE,
                        FOREIGN KEY(runId) REFERENCES runs(id) ON DELETE SET NULL
                    );
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_conflicts_job_status ON conflicts(jobId, status);",
                ],
            ),
            (
                "0002_file_state_last_seen",
                [
                    "ALTER TABLE file_state ADD COLUMN lastSeenAt TEXT NULL;",
                    "ALTER TABLE file_state ADD COLUMN lastSeenRunId INTEGER NULL;",
                ],
            ),
            (
                "0003_meta_kv",
                [
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        created DATETIME DEFAULT CURRENT_TIMESTAMP,
                        modified DATETIME DEFAULT CURRENT_TIMESTAMP,
                        value TEXT NULL
                    );
                    """,
                ],
            ),
            (
                "0004_schedule_windows",
                [
                    # Store schedule windows as JSON (dict weekday -> list[{start,end},...])
                    # This is the minimal schema required to support your UI scheduling window editor.
                    "ALTER TABLE schedule ADD COLUMN windows TEXT NULL;",
                ],
            ),
        ]
