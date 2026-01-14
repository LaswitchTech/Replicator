#!/usr/bin/env python3
# src/replicator/job.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Union
import json
import sqlite3
try:
    # corePY SQLite wrapper (preferred)
    from core.database.sqlite import SQLite  # type: ignore
except Exception:  # pragma: no cover
    SQLite = None  # type: ignore


JsonDict = Dict[str, Any]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Endpoint:
    """
    UI-agnostic endpoint description.

    Compatible with current DB schema in replicator.py:
      - endpoints table: role, type, location, port, guest, username, password, useKey, sshKey, options
      - Job stores endpoints as:
          {"type": "...", "location": "...", "auth": {...}}
    """
    type: str = "local"
    location: str = ""
    auth: JsonDict = field(default_factory=dict)

    def validate(self, role: str) -> List[str]:
        errs: List[str] = []
        if not self.type:
            errs.append(f"{role} endpoint type is required.")
        if not self.location:
            errs.append(f"{role} endpoint location is required.")
        t = (self.type or "").lower()

        # Basic sanity checks only (no network IO, no UI)
        if t in ("ftp", "ssh"):
            port = self.auth.get("port")
            if port is not None:
                try:
                    p = int(port)
                    if p <= 0 or p > 65535:
                        errs.append(f"{role} endpoint port must be between 1 and 65535.")
                except Exception:
                    errs.append(f"{role} endpoint port must be an integer.")
        return errs

    def to_db_fields(self, role: str) -> JsonDict:
        """
        Convert endpoint into endpoints-table fields.
        Keeps compatibility with your current schema/logic:
          - known auth keys are stored as columns
          - extra auth keys are JSON in 'options'
        """
        t = (self.type or "local").lower()
        auth = dict(self.auth or {})

        port: Optional[int] = None
        guest: int = 1
        username: Optional[str] = None
        password: Optional[str] = None
        useKey: int = 0
        sshKey: Optional[str] = None
        options: JsonDict = {}

        if t == "local":
            pass
        elif t in ("smb", "ftp"):
            guest = 1 if bool(auth.get("guest", True)) else 0
            username = auth.get("username") or None
            password = auth.get("password") or None
            if t == "ftp":
                try:
                    port = int(auth.get("port", 21))
                except Exception:
                    port = 21
        elif t == "ssh":
            useKey = 1 if bool(auth.get("useKey", False)) else 0
            username = auth.get("username") or None
            password = auth.get("password") or None
            try:
                port = int(auth.get("port", 22))
            except Exception:
                port = 22
            sshKey = auth.get("key") or None

        known_keys = {"guest", "username", "password", "port", "useKey", "key"}
        for k, v in auth.items():
            if k not in known_keys:
                options[k] = v

        return {
            "role": role,
            "type": t,
            "location": self.location,
            "port": port,
            "guest": guest,
            "username": username,
            "password": password,
            "useKey": useKey,
            "sshKey": sshKey,
            "options": json.dumps(options) if options else None,
        }

    @staticmethod
    def from_db_row(row: Mapping[str, Any]) -> "Endpoint":
        """
        Parse endpoints table row into Endpoint.
        """
        t = (row.get("type") or "local").lower()
        auth: JsonDict = {}

        if t == "local":
            auth = {}
        elif t in ("smb", "ftp"):
            auth = {
                "guest": bool(row.get("guest", 1)),
                "username": row.get("username") or "",
                "password": row.get("password") or "",
            }
            if t == "ftp":
                auth["port"] = row.get("port") or 21
        elif t == "ssh":
            auth = {
                "useKey": bool(row.get("useKey", 0)),
                "username": row.get("username") or "",
                "password": row.get("password") or "",
                "port": row.get("port") or 22,
                "key": row.get("sshKey") or "",
            }

        opt = row.get("options")
        if opt:
            try:
                extra = json.loads(opt)
                if isinstance(extra, dict):
                    auth.update(extra)
            except Exception:
                pass

        return Endpoint(type=t, location=row.get("location") or "", auth=auth)


@dataclass(frozen=True)
class Schedule:
    """
    Job schedule config.

    Current DB has:
      - enabled
      - everyMinutes
      - nextRunAt
      - lastScheduledRunAt

    Upcoming feature: day/time windows.
      - windows: dict weekday -> list of {start:"HH:MM", end:"HH:MM"}
        weekday: 0=Mon ... 6=Sun (datetime.weekday()).

    Note: windows are not persisted yet by default; but we keep structure ready.
    """
    enabled: bool = False
    everyMinutes: int = 60

    # Optional advanced scheduling windows:
    # {0:[{"start":"22:00","end":"06:00"}], 6:[{"start":"00:00","end":"23:59"}], ...}
    windows: JsonDict = field(default_factory=dict)

    # Optional DB fields (if you want to use them later)
    nextRunAt: Optional[str] = None
    lastScheduledRunAt: Optional[str] = None

    def validate(self) -> List[str]:
        errs: List[str] = []
        if self.enabled:
            if self.everyMinutes <= 0:
                errs.append("Schedule everyMinutes must be > 0 when schedule is enabled.")
        # windows validation is tolerant (future UI can enforce strictness)
        if self.windows and not isinstance(self.windows, dict):
            errs.append("Schedule windows must be a dict of weekday -> list[window].")
        return errs

    def _parse_hhmm(self, s: str) -> Optional[time]:
        try:
            parts = s.strip().split(":")
            if len(parts) != 2:
                return None
            hh = int(parts[0])
            mm = int(parts[1])
            if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                return None
            return time(hour=hh, minute=mm)
        except Exception:
            return None

    def _in_window_for_day(self, dt: datetime) -> bool:
        """
        Returns True if dt is within any allowed window for dt.weekday().
        Supports overnight windows like 22:00-06:00.
        If windows is empty => always allowed.
        """
        if not self.windows:
            return True

        wd = dt.weekday()  # 0..6
        day_windows = self.windows.get(str(wd)) or self.windows.get(wd)
        if not day_windows:
            return False

        tnow = dt.timetz().replace(tzinfo=None)

        for w in day_windows:
            if not isinstance(w, dict):
                continue
            s = w.get("start")
            e = w.get("end")
            if not s or not e:
                continue
            ts = self._parse_hhmm(str(s))
            te = self._parse_hhmm(str(e))
            if ts is None or te is None:
                continue

            if ts <= te:
                # same-day window
                if ts <= tnow <= te:
                    return True
            else:
                # overnight window (e.g., 22:00 -> 06:00)
                if tnow >= ts or tnow <= te:
                    return True

        return False

    def should_run_now(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        now = now or datetime.now(timezone.utc)
        return self._in_window_for_day(now)

    def next_run_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        """
        Returns the next datetime the job is allowed to run based on:
          - enabled
          - everyMinutes
          - windows (if any)

        Strategy:
          - Start from next minute tick.
          - Scan forward in 1-minute steps up to 7 days.
          - Pick first time that is within an allowed window AND aligns to interval.

        This is deterministic and fast enough (<= 10080 iterations).
        """
        if not self.enabled:
            return None
        if self.everyMinutes <= 0:
            # Interval 0 is "continuous" in your earlier convo; for scheduling we treat it as "run now"
            return now or datetime.now(timezone.utc)

        now = now or datetime.now(timezone.utc)
        # Normalize to next minute (zero seconds/micros)
        cur = now.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # Align to interval relative to epoch minute 0 (UTC)
        def aligned(dt: datetime) -> bool:
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            minutes = int((dt - epoch).total_seconds() // 60)
            return (minutes % int(self.everyMinutes)) == 0

        limit = cur + timedelta(days=7)
        while cur <= limit:
            if aligned(cur) and self._in_window_for_day(cur):
                return cur
            cur += timedelta(minutes=1)

        return None


@dataclass
class JobRunResult:
    ok: bool
    started_at: str
    ended_at: str
    result: str  # "ok"|"fail"|"running"
    message: Optional[str] = None
    stats: JsonDict = field(default_factory=dict)


@dataclass
class Job:
    """
    UI-agnostic domain object.

    Owns:
      - id, name, enabled, mode, direction, allowDeletion, preserveMetadata, conflictPolicy, pairId
      - sourceEndpoint, targetEndpoint
      - schedule
      - lastRun, lastResult, lastError

    Does NOT own:
      - dialogs/widgets
      - MsgBox
      - DB connections/cursors
    """

    id: Optional[int] = None
    name: str = ""
    enabled: bool = True
    mode: str = "mirror"
    direction: str = "unidirectional"
    allowDeletion: bool = False
    preserveMetadata: bool = True
    conflictPolicy: str = "newest"
    pairId: Optional[str] = None

    sourceEndpoint: Endpoint = field(default_factory=Endpoint)
    targetEndpoint: Endpoint = field(default_factory=Endpoint)

    schedule: Schedule = field(default_factory=Schedule)

    lastRun: Optional[str] = None
    lastResult: Optional[str] = None
    lastError: Optional[str] = None

    # ------------------------------------------------------------------
    # Validation & scheduling
    # ------------------------------------------------------------------

    def validate(self) -> List[str]:
        errs: List[str] = []
        if not self.name.strip():
            errs.append("Job name is required.")
        if not self.mode:
            errs.append("Job mode is required.")
        if not self.direction:
            errs.append("Job direction is required.")

        errs.extend(self.sourceEndpoint.validate("source"))
        errs.extend(self.targetEndpoint.validate("target"))
        errs.extend(self.schedule.validate())

        # Basic constraints
        d = (self.direction or "").lower()
        if d not in ("unidirectional", "bidirectional"):
            errs.append("Job direction must be 'unidirectional' or 'bidirectional'.")

        cp = (self.conflictPolicy or "newest").lower()
        if cp not in ("newest", "keepa", "a", "keepb", "b"):
            errs.append("conflictPolicy must be one of: newest, keepA/a, keepB/b.")

        return errs

    def should_run_now(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        return self.schedule.should_run_now(now)

    def next_run_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        if not self.enabled:
            return None
        return self.schedule.next_run_at(now)

    # ------------------------------------------------------------------
    # Execution (delegated)
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        now: Optional[datetime] = None,
        copy_func: Optional[Callable[..., bool]] = None,
        bidirectional_func: Optional[Callable[..., Tuple[bool, JsonDict]]] = None,
        logger: Optional[Callable[[str, str], None]] = None,
    ) -> JobRunResult:
        """
        Execute job using injected runtime functions.

        - copy_func: should perform unidirectional copy, signature compatible with:
            copy_func(src, dst, preserve_metadata=bool, allow_deletion=bool) -> bool
          (matches your FileSystem.copy usage)
        - bidirectional_func: should perform bidi sync, signature:
            bidirectional_func(job: Job, run_id: Optional[int]) -> (ok, stats)
          NOTE: run_id is NOT managed here; JobRunner should pass run_id.
        - logger: (message, level) callback; level examples: "debug","info","warning","error"

        Returns JobRunResult with ok/fail + stats + timestamps.
        """
        now_dt = now or datetime.now(timezone.utc)
        started_at = now_dt.isoformat()

        def _log(msg: str, level: str = "info") -> None:
            if logger:
                try:
                    logger(msg, level)
                except Exception:
                    pass

        # Validate first (no UI)
        errs = self.validate()
        if errs:
            msg = "; ".join(errs)
            ended = datetime.now(timezone.utc).isoformat()
            self.lastRun = ended
            self.lastResult = "fail"
            self.lastError = msg
            return JobRunResult(
                ok=False,
                started_at=started_at,
                ended_at=ended,
                result="fail",
                message=msg,
                stats={},
            )

        if not self.enabled:
            ended = datetime.now(timezone.utc).isoformat()
            return JobRunResult(
                ok=True,
                started_at=started_at,
                ended_at=ended,
                result="ok",
                message="Job disabled; skipped.",
                stats={},
            )

        src = self.sourceEndpoint.location
        dst = self.targetEndpoint.location
        preserve = bool(self.preserveMetadata)
        allow_del = bool(self.allowDeletion)

        ok: bool = False
        stats: JsonDict = {}

        try:
            if (self.direction or "").lower() == "bidirectional":
                if not bidirectional_func:
                    raise NotImplementedError("Bidirectional engine not provided.")
                _log(f"[Job] Running bidirectional job '{self.name}': {src} <-> {dst}", "info")
                ok, stats = bidirectional_func(self, None)
            else:
                if not copy_func:
                    raise NotImplementedError("Copy function not provided.")
                _log(f"[Job] Running unidirectional job '{self.name}': {src} -> {dst}", "info")
                ok = bool(copy_func(src, dst, preserve_metadata=preserve, allow_deletion=allow_del))
        except NotImplementedError as e:
            ok = False
            self.lastError = str(e)
            _log(f"[Job] Not supported: {e}", "error")
        except Exception as e:
            ok = False
            self.lastError = str(e)
            _log(f"[Job] Execution failed: {e}", "error")

        ended_at = datetime.now(timezone.utc).isoformat()
        self.lastRun = ended_at
        self.lastResult = "ok" if ok else "fail"
        if ok:
            self.lastError = None
        else:
            self.lastError = self.lastError or "Failed"

        return JobRunResult(
            ok=ok,
            started_at=started_at,
            ended_at=ended_at,
            result="ok" if ok else "fail",
            message=None if ok else self.lastError,
            stats=stats or {},
        )

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_row_dicts(self) -> Dict[str, Any]:
        """
        Serialize into DB-ready row dicts:
          - job_row: fields for jobs table
          - endpoint_rows: list of fields for endpoints table (excluding jobId)
          - schedule_row: fields for schedule table (excluding jobId)

        JobStore/Runner can add jobId and persist.
        """
        job_row = {
            "id": self.id,
            "name": self.name,
            "enabled": 1 if self.enabled else 0,
            "mode": self.mode or "mirror",
            "direction": self.direction or "unidirectional",
            "allowDeletion": 1 if self.allowDeletion else 0,
            "preserveMetadata": 1 if self.preserveMetadata else 0,
            "pairId": self.pairId,
            "conflictPolicy": self.conflictPolicy or "newest",
            "lastRun": self.lastRun,
            "lastResult": self.lastResult,
            "lastError": self.lastError,
        }

        endpoint_rows = [
            self.sourceEndpoint.to_db_fields("source"),
            self.targetEndpoint.to_db_fields("target"),
        ]

        sched_row = {
            "enabled": 1 if self.schedule.enabled else 0,
            "everyMinutes": int(self.schedule.everyMinutes),
            # nextRunAt / lastScheduledRunAt can be managed by JobRunner
            "nextRunAt": self.schedule.nextRunAt,
            "lastScheduledRunAt": self.schedule.lastScheduledRunAt,
            # windows not persisted yet by default
            # "windows": json.dumps(self.schedule.windows) ...
        }

        return {
            "job_row": job_row,
            "endpoint_rows": endpoint_rows,
            "schedule_row": sched_row,
        }

    @staticmethod
    def from_db_rows(
        job_row: Mapping[str, Any],
        endpoint_rows: Iterable[Mapping[str, Any]],
        schedule_row: Optional[Mapping[str, Any]] = None,
    ) -> "Job":
        """
        Build Job from DB rows (jobs + endpoints + schedule).

        This matches your current schema and your current in-memory shape.
        """
        j = Job(
            id=int(job_row.get("id")) if job_row.get("id") is not None else None,
            name=str(job_row.get("name") or ""),
            enabled=bool(job_row.get("enabled", 1)),
            mode=str(job_row.get("mode") or "mirror"),
            direction=str(job_row.get("direction") or "unidirectional"),
            allowDeletion=bool(job_row.get("allowDeletion", 0)),
            preserveMetadata=bool(job_row.get("preserveMetadata", 1)),
            conflictPolicy=str(job_row.get("conflictPolicy") or "newest"),
            pairId=job_row.get("pairId"),
            lastRun=job_row.get("lastRun"),
            lastResult=job_row.get("lastResult"),
            lastError=job_row.get("lastError"),
        )

        # Endpoints by role
        src = None
        tgt = None
        for r in endpoint_rows:
            role = (r.get("role") or "").lower()
            ep = Endpoint.from_db_row(r)
            if role == "source":
                src = ep
            elif role == "target":
                tgt = ep

        j.sourceEndpoint = src or Endpoint()
        j.targetEndpoint = tgt or Endpoint()

        # Schedule
        if schedule_row:
            windows: JsonDict = {}
            # If you later add a windows column, you can parse it here.
            # raw_windows = schedule_row.get("windows")
            # if raw_windows: ...
            j.schedule = Schedule(
                enabled=bool(schedule_row.get("enabled", 0)),
                everyMinutes=int(schedule_row.get("everyMinutes", 60) or 60),
                windows=windows,
                nextRunAt=schedule_row.get("nextRunAt"),
                lastScheduledRunAt=schedule_row.get("lastScheduledRunAt"),
            )
        else:
            j.schedule = Schedule(enabled=False, everyMinutes=60)

        return j

    # Convenience: current in-memory dict shape compatibility
    def to_legacy_dict(self) -> JsonDict:
        """
        Produce the same job dict shape your current Replicator code expects.
        Useful as an interim bridge while refactoring.
        """
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "mode": self.mode,
            "direction": self.direction,
            "allowDeletion": self.allowDeletion,
            "preserveMetadata": self.preserveMetadata,
            "pairId": self.pairId,
            "conflictPolicy": self.conflictPolicy,
            "sourceEndpoint": {"type": self.sourceEndpoint.type, "location": self.sourceEndpoint.location, "auth": dict(self.sourceEndpoint.auth)},
            "targetEndpoint": {"type": self.targetEndpoint.type, "location": self.targetEndpoint.location, "auth": dict(self.targetEndpoint.auth)},
            "schedule": {
                "enabled": self.schedule.enabled,
                "everyMinutes": self.schedule.everyMinutes,
                # windows not exposed yet by current UI
            },
            "lastRun": self.lastRun,
            "lastResult": self.lastResult,
            "lastError": self.lastError,
        }



class JobStore:
    """SQLite persistence for Job domain objects.

    This class is UI-agnostic and contains no Qt dependencies.

    It targets the schema currently defined in `replicator.py`:
      - jobs
      - endpoints
      - schedule

    Notes:
      - This store does not write runs/conflicts/file_state.
      - It upserts endpoints by (jobId, role) and schedule by (jobId).
    """

    def __init__(self, db: Any):
        """Create a JobStore.

        Preferred dependency is `core.database.sqlite.SQLite` (wrapper).

        Accepts either:
          - a `SQLite` instance (preferred)
          - a `sqlite3.Connection`
          - a callable that returns `sqlite3.Connection`

        The fallback connection path exists only for backwards compatibility.
        """
        self._db = db

    # -------------------------------
    # Connection helpers
    # -------------------------------

    def _is_core_sqlite(self) -> bool:
        # Avoid importing Qt here; we only check for the wrapper API.
        return SQLite is not None and isinstance(self._db, SQLite)

    def _conn(self) -> sqlite3.Connection:
        """Fallback: get raw sqlite3.Connection."""
        if isinstance(self._db, sqlite3.Connection):
            return self._db
        if callable(self._db):
            c = self._db()
            if not isinstance(c, sqlite3.Connection):
                raise TypeError("JobStore connection provider must return sqlite3.Connection")
            return c
        raise TypeError("JobStore requires core.database.sqlite.SQLite or sqlite3.Connection")

    def _select(self, table: str, where: Optional[str] = None, params: Any = None, *, order_by: Optional[str] = None) -> List[Dict[str, Any]]:
        if self._is_core_sqlite():
            return self._db.select(table, where=where, params=params, order_by=order_by)  # type: ignore[union-attr]
        conn = self._conn()
        sql = f'SELECT * FROM "{table}"'
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        sql += ";"
        cur = conn.execute(sql, params or ())
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def _one(self, table: str, where: str, params: Any) -> Optional[Dict[str, Any]]:
        if self._is_core_sqlite():
            return self._db.one(f'SELECT * FROM "{table}" WHERE {where} LIMIT 1;', params)  # type: ignore[union-attr]
        conn = self._conn()
        cur = conn.execute(f'SELECT * FROM "{table}" WHERE {where} LIMIT 1;', params)
        r = cur.fetchone()
        return dict(r) if r else None

    def _insert(self, table: str, data: Dict[str, Any]) -> int:
        if self._is_core_sqlite():
            return int(self._db.insert(table, data))  # type: ignore[union-attr]
        conn = self._conn()
        keys = list(data.keys())
        cols = ", ".join([f'"{k}"' for k in keys])
        placeholders = ", ".join(["?" for _ in keys])
        sql = f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders});'
        cur = conn.execute(sql, tuple(data[k] for k in keys))
        return int(cur.lastrowid or 0)

    def _update(self, table: str, data: Dict[str, Any], where: str, params: Any) -> None:
        if self._is_core_sqlite():
            # wrapper uses named binding for update WHERE params
            if not isinstance(params, dict):
                raise ValueError("JobStore._update with core SQLite requires dict params")
            self._db.update(table, data, where, params)  # type: ignore[union-attr]
            return
        conn = self._conn()
        keys = list(data.keys())
        set_clause = ", ".join([f'"{k}"=?' for k in keys])
        sql = f'UPDATE "{table}" SET {set_clause} WHERE {where};'
        conn.execute(sql, tuple(data[k] for k in keys) + tuple(params if isinstance(params, tuple) else ()))

    def _upsert(self, table: str, data: Dict[str, Any], conflict_columns: List[str], update_columns: Optional[List[str]] = None) -> None:
        if self._is_core_sqlite():
            self._db.upsert(table, data, conflict_columns, update_columns)  # type: ignore[union-attr]
            return
        # Fallback path: build an INSERT..ON CONFLICT statement.
        keys = list(data.keys())
        cols = ", ".join([f'"{k}"' for k in keys])
        placeholders = ", ".join(["?" for _ in keys])
        conflict = ", ".join([f'"{c}"' for c in conflict_columns])
        if update_columns is None:
            update_columns = [k for k in keys if k not in conflict_columns]
        if update_columns:
            set_clause = ", ".join([f'"{k}"=excluded."{k}"' for k in update_columns])
            sql = f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders}) ON CONFLICT({conflict}) DO UPDATE SET {set_clause};'
        else:
            sql = f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders}) ON CONFLICT({conflict}) DO NOTHING;'
        conn = self._conn()
        conn.execute(sql, tuple(data[k] for k in keys))

    def _delete(self, table: str, where: str, params: Any) -> None:
        if self._is_core_sqlite():
            self._db.delete(table, where, params)  # type: ignore[union-attr]
            return
        conn = self._conn()
        conn.execute(f'DELETE FROM "{table}" WHERE {where};', params)

    def _transaction(self):
        if self._is_core_sqlite():
            return self._db.transaction()  # type: ignore[union-attr]
        # sqlite3.Connection supports context manager transactions
        return self._conn()

    # -------------------------------
    # Reads
    # -------------------------------

    def fetch_all(self) -> List[Job]:
        job_rows = self._select("jobs", order_by="id ASC")
        jobs: List[Job] = []
        for jr in job_rows:
            jid = int(jr.get("id") or 0)
            ep_rows = self.fetch_endpoints_rows(jid)
            sched_row = self.fetch_schedule_row(jid)
            jobs.append(Job.from_db_rows(jr, ep_rows, sched_row))
        return jobs

    def fetch_by_id(self, job_id: int) -> Optional[Job]:
        jr = self._one("jobs", "id = ?", (int(job_id),))
        if not jr:
            return None
        ep_rows = self.fetch_endpoints_rows(int(job_id))
        sched_row = self.fetch_schedule_row(int(job_id))
        return Job.from_db_rows(jr, ep_rows, sched_row)

    def fetch_endpoints_rows(self, job_id: int) -> List[Dict[str, Any]]:
        return self._select("endpoints", "jobId = ?", (int(job_id),))

    def fetch_schedule_row(self, job_id: int) -> Optional[Dict[str, Any]]:
        return self._one("schedule", "jobId = ?", (int(job_id),))

    # -------------------------------
    # Writes
    # -------------------------------

    def upsert(self, job: Job) -> int:
        """Insert or update a job + its endpoints + schedule. Returns job id."""

        row_dicts = job.to_row_dicts()
        job_row: Dict[str, Any] = row_dicts["job_row"]
        endpoint_rows: List[Dict[str, Any]] = row_dicts["endpoint_rows"]
        schedule_row: Dict[str, Any] = row_dicts["schedule_row"]

        with self._transaction():
            # --- jobs ---
            data = {
                "name": job_row.get("name"),
                "enabled": int(job_row.get("enabled") or 0),
                "mode": job_row.get("mode") or "mirror",
                "direction": job_row.get("direction") or "unidirectional",
                "allowDeletion": int(job_row.get("allowDeletion") or 0),
                "preserveMetadata": int(job_row.get("preserveMetadata") or 0),
                "pairId": job_row.get("pairId"),
                "conflictPolicy": job_row.get("conflictPolicy") or "newest",
                "lastRun": job_row.get("lastRun"),
                "lastResult": job_row.get("lastResult"),
                "lastError": job_row.get("lastError"),
            }

            if job.id:
                self._update("jobs", data, "id = :id", {"id": int(job.id)})
                job_id = int(job.id)
            else:
                job_id = self._insert("jobs", data)
                job.id = job_id

            # --- endpoints ---
            for ep in endpoint_rows:
                role = ep.get("role")
                if role not in ("source", "target"):
                    continue
                ep_data = {
                    "jobId": job_id,
                    "role": role,
                    "type": ep.get("type"),
                    "location": ep.get("location"),
                    "port": ep.get("port"),
                    "guest": ep.get("guest"),
                    "username": ep.get("username"),
                    "password": ep.get("password"),
                    "useKey": ep.get("useKey"),
                    "sshKey": ep.get("sshKey"),
                    "options": ep.get("options"),
                }
                # Unique index on (jobId, role)
                self._upsert("endpoints", ep_data, ["jobId", "role"], update_columns=[
                    "type", "location", "port", "guest", "username", "password", "useKey", "sshKey", "options"
                ])

            # --- schedule ---
            s_data = {
                "jobId": job_id,
                "enabled": int(schedule_row.get("enabled") or 0),
                "everyMinutes": int(schedule_row.get("everyMinutes") or 60),
                "nextRunAt": schedule_row.get("nextRunAt"),
                "lastScheduledRunAt": schedule_row.get("lastScheduledRunAt"),
            }
            # schedule.jobId is UNIQUE
            self._upsert("schedule", s_data, ["jobId"], update_columns=[
                "enabled", "everyMinutes", "nextRunAt", "lastScheduledRunAt"
            ])

        return int(job.id or 0)

    def delete(self, job_id: int) -> None:
        with self._transaction():
            self._delete("jobs", "id = ?", (int(job_id),))
