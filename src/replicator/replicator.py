#!/usr/bin/env python3
# src/replicator/replicator.py

from __future__ import annotations

from typing import Optional, Any, Dict, List

import os
import json
import shutil
import tempfile
import time
from pathlib import Path

# Add datetime import for lastRun/lastResult
from datetime import datetime, timezone, timedelta

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QDialog,
)

from .ui import JobDialog, ScheduleDialog
from .migration import Migration
from .job import Job, Endpoint, Schedule, JobStore


try:
    from core.helper import Helper
    from core.configuration import Configuration
    from core.log import Log
    from core.ui import MsgBox, Form
    from core.filesystem.filesystem import FileSystem
    from core.filesystem.share import Share, ShareAuth
    from core.database.sqlite import SQLite
except ImportError:
    from helper import Helper
    from configuration import Configuration
    from log import Log
    from ui import MsgBox, Form
    from filesystem.filesystem import FileSystem
    from filesystem.share import Share, ShareAuth
    from database.sqlite import SQLite



# ---------------------------------------------------------------------------
# Remote endpoints (Share mount)
# ---------------------------------------------------------------------------

class RemoteMountError(RuntimeError):
    pass


class _ShareLogger:
    """Adapter that forwards Share logs into Replicator's logger."""

    def __init__(self, append_fn):
        self._append = append_fn

    def debug(self, msg: str) -> None:
        self._append(f"[Share] {msg}", level="debug")

    def info(self, msg: str) -> None:
        self._append(f"[Share] {msg}", level="info")

    def warning(self, msg: str) -> None:
        self._append(f"[Share] {msg}", level="warning")

    def error(self, msg: str) -> None:
        self._append(f"[Share] {msg}", level="error")


class _MountedEndpoint:
    def __init__(self, local_path: str, mount_point: str | None = None, share: Share | None = None):
        self.local_path = local_path
        self.mount_point = mount_point
        self.share = share

    def cleanup(self) -> None:
        if self.share is not None and self.mount_point:
            try:
                self.share.umount(self.mount_point, elevate=False)
            except Exception:
                pass



# --- Location parsing helpers for remote endpoints ---

def _parse_smb_location(loc: str) -> tuple[str, str]:
    """Parse SMB location into (host, remote).

    Accepts forms:
      - \\host\Share
      - \\host\Share\dir\sub
      - //host/Share/dir
      - host/Share/dir
    Returns:
      host, remote where remote is "Share" or "Share/dir/sub".
    """
    s = (loc or "").strip()
    s = s.replace("/", "\\")
    while s.startswith("\\"):
        s = s[1:]
    parts = [p for p in s.split("\\") if p]
    if len(parts) < 2:
        raise RemoteMountError(f"Invalid SMB location: {loc}")
    host = parts[0]
    remote = "/".join(parts[1:])
    return host, remote




def _mount_endpoint_if_remote(
    endpoint: dict[str, Any],
    job_id: int | None,
    role: str,
    *,
    share: Share,
    log_append,
) -> _MountedEndpoint:
    """Mount SMB endpoints using Share and return a local path usable by the sync engine."""

    t = (endpoint.get("type") or "local").lower()
    loc = str(endpoint.get("location") or "").strip()

    if t == "local":
        return _MountedEndpoint(local_path=loc)

    if t != "smb":
        raise RemoteMountError(f"Unsupported endpoint type: {t}")

    base = Path(tempfile.gettempdir()) / "replicator" / "mounts" / (str(job_id or "new")) / role
    base.mkdir(parents=True, exist_ok=True)
    mount_point = str(base)

    # Build auth/options payload (do NOT log secrets)
    auth: dict[str, Any] = {}
    if isinstance(endpoint.get("auth"), dict):
        auth.update(endpoint.get("auth") or {})

    # Backward-compatible: endpoints table columns may be stored at top-level
    for k in ("port", "guest", "username", "password", "useKey", "sshKey", "options"):
        if k in endpoint and endpoint.get(k) is not None and k not in auth:
            auth[k] = endpoint.get(k)

    # Build ShareAuth object
    share_auth = ShareAuth(
        username=auth.get("username"),
        password=auth.get("password"),
        domain=auth.get("domain") or auth.get("workgroup"),
        private_key=auth.get("sshKey") or auth.get("private_key"),
    )

    # Parse host and remote from location (SMB only)
    host, remote = _parse_smb_location(loc)

    # Determine port and options
    port = None
    try:
        if auth.get("port") is not None:
            port = int(auth.get("port"))
    except Exception:
        port = None
    if port is None:
        port = 445

    opts = {}
    if isinstance(auth.get("options"), dict):
        opts.update(auth.get("options") or {})

    # Emit a safe debug line (mask secrets, include host/remote)
    safe_auth = dict(auth)
    for k in ("password", "pass"):
        if k in safe_auth and safe_auth[k]:
            safe_auth[k] = "***"
    log_append(
        f"[Replicator][Share] mount protocol={t} host={host} remote={remote} mount_point={mount_point} auth={safe_auth}",
        level="debug",
    )

    # Call Share.mount with correct signature
    share.mount(
        t,
        host,
        remote,
        mount_point,
        auth=share_auth,
        port=port,
        options=opts,
        read_only=bool(auth.get("read_only") or auth.get("ro") or False),
        elevate=False,
        timeout=60,
    )

    return _MountedEndpoint(local_path=mount_point, mount_point=mount_point, share=share)


class Replicator(QMainWindow):

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def __init__(
        self,
        helper: Optional[Helper] = None,
        configuration: Optional[Configuration] = None,
        logger: Optional[Log] = None,
    ):
        super().__init__()

        self._app = QApplication.instance()
        if self._app is None:
            raise RuntimeError("Replicator must be created after QApplication/Application.")

        helper = helper or getattr(self._app, "helper", None)
        configuration = configuration or getattr(self._app, "configuration", None)
        logger = logger or getattr(self._app, "logger", None)

        if helper is None or configuration is None:
            raise RuntimeError("Replicator requires corePY Helper + Configuration.")

        self._helper: Helper = helper
        self._configuration: Configuration = configuration
        self._logger: Optional[Log] = logger

        self._fs = FileSystem(helper=self._helper, logger=self._logger)

        # --- Database path setup ---
        # Use Helper.get_cwd() if present, else os.getcwd()
        if hasattr(self._helper, "get_cwd") and callable(getattr(self._helper, "get_cwd", None)):
            base_dir = self._helper.get_cwd()
        else:
            base_dir = os.getcwd()
        data_dir = os.path.join(base_dir, "data")
        db_path = os.path.join(data_dir, "replicator.db")

        # --- Database (corePY SQLite wrapper) + migrations ---
        self._db = SQLite(db_path=db_path)
        self._migration = Migration(self._db, logger=self._logger)
        self._migration.ensure()

        self._store = JobStore(self._db)

        self._log(f"[Replicator] Database: {db_path}", level="debug")
        self._log("[Replicator] Database migrations applied.", level="info")

        # Domain jobs (Job objects)
        self._jobs: List[Job] = []
        self._table: Optional[QTableWidget] = None

        # After DB is ready, log a snapshot of DB layout/counts (non-verbose)
        self._db_debug_snapshot(verbose=False)

    # ------------------------------------------------------------------
    # Scheduler (service mode)
    # ------------------------------------------------------------------

    def _parse_iso_dt(self, s: Any) -> Optional[datetime]:
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(str(s))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def _window_allows_now_local(self, windows: Dict[str, Any], now_local: datetime) -> bool:
        """
        Determine if 'now_local' falls inside an allowed window for its weekday.
        Windows are interpreted as LOCAL times (America/Montreal via system tz).
        Supports overnight windows like 22:00-06:00.
        If windows is empty/missing => allowed.
        """
        if not windows or not isinstance(windows, dict):
            return True

        wd = now_local.weekday()  # 0..6 (Mon..Sun)
        day_windows = windows.get(str(wd)) or windows.get(wd)
        if not isinstance(day_windows, list) or not day_windows:
            return False

        tnow = now_local.time().replace(tzinfo=None)

        def _parse_hhmm(val: Any) -> Optional[tuple[int, int]]:
            try:
                parts = str(val).strip().split(":")
                if len(parts) != 2:
                    return None
                hh = int(parts[0]); mm = int(parts[1])
                if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                    return None
                return hh, mm
            except Exception:
                return None

        for w in day_windows:
            if not isinstance(w, dict):
                continue
            s = w.get("start"); e = w.get("end")
            if not s or not e:
                continue
            ps = _parse_hhmm(s); pe = _parse_hhmm(e)
            if ps is None or pe is None:
                continue
            sh, sm = ps; eh, em = pe
            ts = datetime(2000, 1, 1, sh, sm).time()
            te = datetime(2000, 1, 1, eh, em).time()

            if ts <= te:
                if ts <= tnow <= te:
                    return True
            else:
                # overnight
                if tnow >= ts or tnow <= te:
                    return True

        return False

    def _interval_seconds_for_schedule_row(self, sched_row: Dict[str, Any], now_local: datetime) -> int:
        """
        Return interval seconds for this schedule row.
        Priority:
          1) schedule.intervalSeconds column
          2) windows[weekday][0].intervalSeconds
          3) service.defaultInterval
        """
        default_iv = self._default_interval_seconds()

        try:
            col = sched_row.get("intervalSeconds")
            if col is not None:
                iv = int(col or 0)
                if iv > 0:
                    return iv
        except Exception:
            pass

        try:
            windows = sched_row.get("windows") or {}
            if isinstance(windows, str):
                windows = json.loads(windows) if windows else {}
            if isinstance(windows, dict):
                wd = now_local.weekday()
                day = windows.get(str(wd)) or windows.get(wd)
                if isinstance(day, list) and day and isinstance(day[0], dict):
                    v = day[0].get("intervalSeconds")
                    if v is not None:
                        iv = int(v or 0)
                        if iv > 0:
                            return iv
        except Exception:
            pass

        return default_iv

    def _should_run_scheduled(self, job: Job, *, now_utc: datetime, now_local: datetime) -> bool:
        """
        Decide if a job should run in SERVICE/SCHEDULED mode.
        Manual 'Run now' intentionally bypasses this.
        """
        if not job.id:
            return False

        sched = self._db.one(
            "SELECT enabled, nextRunAt, lastScheduledRunAt, windows, intervalSeconds FROM schedule WHERE jobId=?",
            (int(job.id),),
        )
        if not sched:
            # No schedule row => do not auto-run (manual run still works)
            return False

        if not bool(sched.get("enabled", 0)):
            return False

        # windows are local-time based
        windows: Dict[str, Any] = {}
        try:
            raw = sched.get("windows")
            if raw:
                windows = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
        except Exception:
            windows = {}

        if not self._window_allows_now_local(windows, now_local):
            return False

        # Respect nextRunAt if present
        next_dt = self._parse_iso_dt(sched.get("nextRunAt"))
        if next_dt and now_utc < next_dt:
            return False

        interval_s = self._interval_seconds_for_schedule_row(sched, now_local)
        last_dt = self._parse_iso_dt(sched.get("lastScheduledRunAt"))

        if last_dt is None:
            # first scheduled run in an allowed window
            return True

        elapsed = (now_utc - last_dt).total_seconds()
        return elapsed >= float(interval_s)
    # Bidirectional sync helpers (local-only for now)
    # ------------------------------------------------------------------

    def _endpoint_local_root(self, ep: Dict[str, Any]) -> str:
        """Return the local root path for an endpoint or raise.
        This is a simple extractor for the already-mounted path."""
        if not isinstance(ep, dict):
            raise ValueError("Endpoint is missing")
        loc = (ep.get("location") or "").strip()
        if not loc:
            raise ValueError("Endpoint location is required")
        return loc

    def _scan_local_tree(self, root: str) -> Dict[str, Dict[str, Any]]:
        """Return a map relPath -> {isDir,size,mtime} for a local filesystem root."""
        root = os.path.abspath(root)
        out: Dict[str, Dict[str, Any]] = {}
        if not os.path.exists(root):
            return out

        # Walk directories; include dirs as entries so deletions can be propagated.
        for dirpath, dirnames, filenames in os.walk(root):
            # Normalize and skip hidden special entries if needed (keep simple for now)
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir == ".":
                rel_dir = ""

            # Record directory itself (except root)
            if rel_dir:
                try:
                    st = os.stat(dirpath)
                    out[rel_dir] = {
                        "isDir": True,
                        "size": 0,
                        "mtime": int(st.st_mtime),
                    }
                except Exception:
                    # If stat fails, still record directory
                    out[rel_dir] = {"isDir": True, "size": 0, "mtime": 0}

            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                try:
                    st = os.stat(full)
                    out[rel] = {
                        "isDir": False,
                        "size": int(st.st_size),
                        "mtime": int(st.st_mtime),
                    }
                except Exception:
                    out[rel] = {"isDir": False, "size": 0, "mtime": 0}

        return out

    def _load_prev_file_state(self, job_id: int, side: str) -> Dict[str, Dict[str, Any]]:
        rows = self._db.query(
            "SELECT relPath, size, mtime, isDir, deleted, deletedAt, lastSeenAt, lastSeenRunId FROM file_state WHERE jobId=? AND side=?",
            (job_id, side),
        )
        prev: Dict[str, Dict[str, Any]] = {}
        for r in rows or []:
            prev[str(r.get("relPath"))] = {
                "size": int(r.get("size") or 0),
                "mtime": int(r.get("mtime") or 0),
                "isDir": bool(r.get("isDir")),
                "deleted": bool(r.get("deleted")),
                "deletedAt": r.get("deletedAt"),
                "lastSeenAt": r.get("lastSeenAt"),
                "lastSeenRunId": r.get("lastSeenRunId"),
            }
        return prev

    def _persist_file_state(self, job_id: int, side: str, cur: Dict[str, Dict[str, Any]], run_id: Optional[int]) -> None:
        # Use wrapper transaction; no raw connection needed.
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._db.transaction():
            # Upsert all current entries
            for rel, meta in cur.items():
                is_dir = 1 if meta.get("isDir") else 0
                size = int(meta.get("size", 0) or 0)
                mtime = int(meta.get("mtime", 0) or 0)

                exists = self._db.scalar(
                    "SELECT id FROM file_state WHERE jobId=? AND side=? AND relPath=?",
                    (job_id, side, rel),
                )
                if exists:
                    self._db.execute(
                        "UPDATE file_state SET size=?, mtime=?, isDir=?, deleted=0, deletedAt=NULL, lastSeenAt=?, lastSeenRunId=? WHERE jobId=? AND side=? AND relPath=?",
                        (size, mtime, is_dir, now_iso, run_id, job_id, side, rel),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO file_state (jobId, side, relPath, size, mtime, isDir, deleted, deletedAt, lastSeenAt, lastSeenRunId) VALUES (?,?,?,?,?,?,0,NULL,?,?)",
                        (job_id, side, rel, size, mtime, is_dir, now_iso, run_id),
                    )

            # Mark missing as deleted (single UPDATE)
            rels = list(cur.keys())
            if rels:
                placeholders = ",".join(["?"] * len(rels))
                self._db.execute(
                    f"UPDATE file_state SET deleted=1, deletedAt=COALESCE(deletedAt, ?) WHERE jobId=? AND side=? AND deleted=0 AND relPath NOT IN ({placeholders})",
                    (now_iso, job_id, side, *rels),
                )
            else:
                # cur is empty: mark all as deleted for this job/side
                self._db.execute(
                    "UPDATE file_state SET deleted=1, deletedAt=COALESCE(deletedAt, ?) WHERE jobId=? AND side=? AND deleted=0",
                    (now_iso, job_id, side),
                )

    def _ensure_parent_dir(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

    def _copy_local_path(self, src_root: str, dst_root: str, rel: str, is_dir: bool, preserve_metadata: bool) -> None:
        src_full = os.path.join(src_root, rel)
        dst_full = os.path.join(dst_root, rel)

        if is_dir:
            os.makedirs(dst_full, exist_ok=True)
            return

        self._ensure_parent_dir(dst_full)
        if preserve_metadata:
            shutil.copy2(src_full, dst_full)
        else:
            shutil.copy(src_full, dst_full)

    def _delete_local_path(self, root: str, rel: str, is_dir: bool) -> None:
        full = os.path.join(root, rel)
        if not os.path.exists(full):
            return
        if is_dir:
            # Only remove if empty; never rmtree blindly in sync engine.
            try:
                os.rmdir(full)
            except OSError:
                pass
        else:
            try:
                os.remove(full)
            except Exception:
                pass

    def _record_conflict(
        self,
        job_id: int,
        run_id: Optional[int],
        rel: str,
        a: Dict[str, Any],
        b: Dict[str, Any],
        note: str = "",
    ) -> None:
        try:
            # Avoid inserting duplicate open conflicts for the same path/note.
            existing = self._db.one(
                "SELECT id FROM conflicts WHERE jobId=? AND relPath=? AND status='open' AND COALESCE(note,'')=COALESCE(?, '') ORDER BY id DESC LIMIT 1",
                (job_id, rel, note or ""),
            )
            if existing:
                return
            self._db.execute(
                """
                INSERT INTO conflicts (jobId, runId, relPath, a_size, a_mtime, a_hash, b_size, b_mtime, b_hash, status, note)
                VALUES (?,?,?,?,?,?,?,?,?,'open',?)
                """,
                (
                    job_id,
                    run_id,
                    rel,
                    int(a.get("size", 0) or 0),
                    int(a.get("mtime", 0) or 0),
                    a.get("hash"),
                    int(b.get("size", 0) or 0),
                    int(b.get("mtime", 0) or 0),
                    b.get("hash"),
                    note or None,
                ),
            )
        except Exception as e:
            self._log(f"[Replicator][DB] Failed to record conflict for '{rel}': {e}", level="warning")

    def _run_job_bidirectional(self, job: Dict[str, Any], run_id: Optional[int]) -> tuple[bool, Dict[str, Any]]:
        """Bidirectional sync using `file_state` as the persistent baseline.

        Currently implemented for local<->local only.
        """
        job_id = int(job.get("id") or 0)
        if job_id <= 0:
            raise ValueError("Job must have an id to run bidirectional sync")

        src_ep = job.get("sourceEndpoint")
        dst_ep = job.get("targetEndpoint")
        a_root = self._endpoint_local_root(src_ep)
        b_root = self._endpoint_local_root(dst_ep)

        allow_deletion = bool(job.get("allowDeletion", False))
        preserve_metadata = bool(job.get("preserveMetadata", True))
        conflict_policy = (job.get("conflictPolicy") or "newest").lower()

        # Load previous baseline before scanning/persisting.
        prev_a = self._load_prev_file_state(job_id, "A")
        prev_b = self._load_prev_file_state(job_id, "B")

        # Scan current trees
        cur_a = self._scan_local_tree(a_root)
        cur_b = self._scan_local_tree(b_root)

        def _meta_changed(cur: Dict[str, Any], prev: Dict[str, Any]) -> bool:
            return (
                bool(cur.get("isDir")) != bool(prev.get("isDir"))
                or int(cur.get("size", 0) or 0) != int(prev.get("size", 0) or 0)
                or int(cur.get("mtime", 0) or 0) != int(prev.get("mtime", 0) or 0)
            )

        def _changed_set(cur: Dict[str, Dict[str, Any]], prev: Dict[str, Dict[str, Any]]) -> set[str]:
            out = set()
            for rel, meta in cur.items():
                p = prev.get(rel)
                if p is None or p.get("deleted", False):
                    out.add(rel)
                else:
                    if _meta_changed(meta, p):
                        out.add(rel)
            return out

        def _deleted_set(cur: Dict[str, Dict[str, Any]], prev: Dict[str, Dict[str, Any]]) -> set[str]:
            # deleted since last baseline means it existed (not deleted) and now missing
            out = set()
            for rel, p in prev.items():
                if p.get("deleted", False):
                    continue
                if rel not in cur:
                    out.add(rel)
            return out

        changed_a = _changed_set(cur_a, prev_a)
        changed_b = _changed_set(cur_b, prev_b)
        deleted_a = _deleted_set(cur_a, prev_a)
        deleted_b = _deleted_set(cur_b, prev_b)

        self._log(
            f"[Replicator][BiDi] Snapshot A={len(cur_a)} entries, B={len(cur_b)} entries; changedA={len(changed_a)} changedB={len(changed_b)} deletedA={len(deleted_a)} deletedB={len(deleted_b)}",
            level="debug",
        )

        # Build unified rel set
        all_paths = set(cur_a.keys()) | set(cur_b.keys()) | set(prev_a.keys()) | set(prev_b.keys())

        actions_copy_a_to_b: list[tuple[str, bool]] = []
        actions_copy_b_to_a: list[tuple[str, bool]] = []
        actions_del_a: list[tuple[str, bool]] = []
        actions_del_b: list[tuple[str, bool]] = []

        # Helper for newest
        def _winner_newest(a: Dict[str, Any], b: Dict[str, Any]) -> str:
            am = int(a.get("mtime", 0) or 0)
            bm = int(b.get("mtime", 0) or 0)
            if am == bm:
                # tie-breaker: larger size wins
                return "A" if int(a.get("size", 0) or 0) >= int(b.get("size", 0) or 0) else "B"
            return "A" if am > bm else "B"

        for rel in sorted(all_paths):
            a_cur = cur_a.get(rel)
            b_cur = cur_b.get(rel)

            a_exists = a_cur is not None
            b_exists = b_cur is not None

            a_changed = rel in changed_a
            b_changed = rel in changed_b

            a_deleted = rel in deleted_a
            b_deleted = rel in deleted_b

            # If exists only on one side.
            # When deletions are enabled and the missing side deleted it since the last baseline,
            # deletion should win unless the existing side also changed (conflict).
            if a_exists and not b_exists:
                if b_deleted and allow_deletion:
                    if a_changed:
                        # conflict: B deleted while A changed
                        self._record_conflict(job_id, run_id, rel, a_cur, {"deleted": True}, note="B deleted, A changed")
                        winner = "A" if conflict_policy == "newest" else "A"
                        if winner == "A":
                            actions_copy_a_to_b.append((rel, bool(a_cur.get("isDir"))))
                        else:
                            actions_del_a.append((rel, bool(a_cur.get("isDir"))))
                    else:
                        # propagate deletion (keep B deleted, delete A)
                        actions_del_a.append((rel, bool(a_cur.get("isDir"))))
                elif b_deleted and not allow_deletion:
                    # If B was deleted and deletions are not allowed, do NOT copy A->B (do nothing)
                    pass
                else:
                    # no deletion involved: treat as create on A, copy to B
                    actions_copy_a_to_b.append((rel, bool(a_cur.get("isDir"))))
                continue

            if b_exists and not a_exists:
                if a_deleted and allow_deletion:
                    if b_changed:
                        # conflict: A deleted while B changed
                        self._record_conflict(job_id, run_id, rel, {"deleted": True}, b_cur, note="A deleted, B changed")
                        winner = "B" if conflict_policy == "newest" else "B"
                        if winner == "B":
                            actions_copy_b_to_a.append((rel, bool(b_cur.get("isDir"))))
                        else:
                            actions_del_b.append((rel, bool(b_cur.get("isDir"))))
                    else:
                        # propagate deletion (keep A deleted, delete B)
                        actions_del_b.append((rel, bool(b_cur.get("isDir"))))
                elif a_deleted and not allow_deletion:
                    # If A was deleted and deletions are not allowed, do NOT copy B->A (do nothing)
                    pass
                else:
                    actions_copy_b_to_a.append((rel, bool(b_cur.get("isDir"))))
                continue

            # Missing on both sides: maybe deletion propagation; nothing to do.
            if not a_exists and not b_exists:
                continue

            # Exists on both: check if identical
            if a_exists and b_exists:
                same = (
                    bool(a_cur.get("isDir")) == bool(b_cur.get("isDir"))
                    and int(a_cur.get("size", 0) or 0) == int(b_cur.get("size", 0) or 0)
                    and int(a_cur.get("mtime", 0) or 0) == int(b_cur.get("mtime", 0) or 0)
                )

                if same:
                    # Maybe propagate deletions if one side deleted previously (should not happen if same exists)
                    continue

                # Not same: detect changes since baseline
                if a_changed and b_changed:
                    # true conflict
                    self._record_conflict(job_id, run_id, rel, a_cur, b_cur, note="Changed on both sides")
                    if conflict_policy == "newest":
                        winner = _winner_newest(a_cur, b_cur)
                    elif conflict_policy in ("keepa", "a"):
                        winner = "A"
                    elif conflict_policy in ("keepb", "b"):
                        winner = "B"
                    else:
                        winner = _winner_newest(a_cur, b_cur)

                    if winner == "A":
                        actions_copy_a_to_b.append((rel, bool(a_cur.get("isDir"))))
                    else:
                        actions_copy_b_to_a.append((rel, bool(b_cur.get("isDir"))))
                elif a_changed and not b_changed:
                    actions_copy_a_to_b.append((rel, bool(a_cur.get("isDir"))))
                elif b_changed and not a_changed:
                    actions_copy_b_to_a.append((rel, bool(b_cur.get("isDir"))))
                else:
                    # differs but we can't prove which changed vs baseline (e.g., first run with baseline empty)
                    # Choose newest as default.
                    winner = _winner_newest(a_cur, b_cur)
                    if winner == "A":
                        actions_copy_a_to_b.append((rel, bool(a_cur.get("isDir"))))
                    else:
                        actions_copy_b_to_a.append((rel, bool(b_cur.get("isDir"))))

        # Deletions propagation (safe rules)
        if allow_deletion:
            # If A deleted and B did not change since baseline, delete on B
            for rel in sorted(deleted_a):
                if rel in changed_b:
                    # conflict: deleted on A but changed on B
                    self._record_conflict(job_id, run_id, rel, {"deleted": True}, cur_b.get(rel, {}), note="A deleted, B changed")
                    continue
                pb = prev_b.get(rel)
                if pb and not pb.get("deleted", False) and rel in cur_b:
                    actions_del_b.append((rel, bool(cur_b[rel].get("isDir"))))

            # If B deleted and A did not change since baseline, delete on A
            for rel in sorted(deleted_b):
                if rel in changed_a:
                    self._record_conflict(job_id, run_id, rel, cur_a.get(rel, {}), {"deleted": True}, note="B deleted, A changed")
                    continue
                pa = prev_a.get(rel)
                if pa and not pa.get("deleted", False) and rel in cur_a:
                    actions_del_a.append((rel, bool(cur_a[rel].get("isDir"))))

        # Execute actions
        try:
            # Copy directories first to ensure parents exist
            for rel, is_dir in actions_copy_a_to_b:
                self._copy_local_path(a_root, b_root, rel, is_dir, preserve_metadata)
            for rel, is_dir in actions_copy_b_to_a:
                self._copy_local_path(b_root, a_root, rel, is_dir, preserve_metadata)

            # Then copy files (copy method handles both, but ordering helps for deep paths)
            # (Already handled in _copy_local_path)

            # Deletions last
            for rel, is_dir in actions_del_a:
                self._delete_local_path(a_root, rel, is_dir)
            for rel, is_dir in actions_del_b:
                self._delete_local_path(b_root, rel, is_dir)

        except Exception as e:
            self._log(f"[Replicator][BiDi] Execution failed: {e}", level="error")
            return False, {}

        # After actions, scan again and persist final file state as baseline
        final_a = self._scan_local_tree(a_root)
        final_b = self._scan_local_tree(b_root)
        self._persist_file_state(job_id, "A", final_a, run_id)
        self._persist_file_state(job_id, "B", final_b, run_id)

        stats = {
            "copyAtoB": len(actions_copy_a_to_b),
            "copyBtoA": len(actions_copy_b_to_a),
            "delA": len(actions_del_a),
            "delB": len(actions_del_b),
            "changedA": len(changed_a),
            "changedB": len(changed_b),
            "deletedA": len(deleted_a),
            "deletedB": len(deleted_b),
        }
        self._log(f"[Replicator][BiDi] Actions: {stats}", level="debug")

        # Update run stats if possible
        if run_id:
            try:
                self._db.execute(
                    "UPDATE runs SET stats=? WHERE id=?",
                    (json.dumps(stats), run_id),
                )
            except Exception:
                pass

        return True, stats

    # ------------------------------------------------------------------
    # CLI integration
    # ------------------------------------------------------------------

    def cli(self, cli: QApplication = None) -> None:
        """
        Called by corePY CommandLine when running in CLI mode.
        """
        if cli is None:
            return
        cli.add("run", "Run all replication jobs.", self.run)
        cli.service.add("run", "Run all replication jobs", self.run_scheduled, interval=1)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def show(self):
        self.init()
        super().show()

    def init(self):
        self.setWindowTitle(getattr(self._app, "name", "Replicator"))
        self.setObjectName("Replicator")
        # Ensure minimum window width for table visibility
        self.setMinimumWidth(800)

        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Logo (top, centered)
        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)

        # Use app icon/logo if you have one; otherwise harmless
        # Adjust path to wherever you store icons in Replicator
        candidate = self._helper.get_path("icons/icon.png") or self._helper.get_path("core/icons/info.svg")
        name = getattr(self._app, "name", None) or self._app.applicationName()
        if candidate and self._helper.file_exists(candidate) and candidate.lower().endswith(".png"):
            pm = QPixmap(candidate)
            if not pm.isNull():
                logo.setPixmap(pm.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            logo.setText(name)
            logo.setStyleSheet("font-size: 32px; font-weight: 200; opacity: 0.8; padding: 0px; margin: 0px;")

        root.addWidget(logo)

        # Application name (under logo)
        app_name = QLabel()
        app_name.setText(str(name) if name else "")
        app_name.setAlignment(Qt.AlignCenter)
        app_name.setStyleSheet("font-size: 32px; font-weight: 200; opacity: 0.8; padding: 0px; margin: 0px;")
        root.addWidget(app_name)

        # --- Actions row (top, after logo) ---
        actions_row = QHBoxLayout()
        # No stretch at start; left-aligned by default
        self._add_btn = Form.button(label="", icon="plus-circle", action=self._add_job)
        self._add_btn.setToolTip("Add job")
        self._edit_btn = Form.button(label="", icon="pencil-square", action=self._edit_job)
        self._edit_btn.setToolTip("Edit job")
        self._edit_btn.setVisible(False)
        self._dup_btn = Form.button(label="", icon="files", action=self._duplicate_job)
        self._dup_btn.setToolTip("Duplicate job")
        self._dup_btn.setVisible(False)
        self._del_btn = Form.button(label="", icon="trash", action=self._delete_job)
        self._del_btn.setToolTip("Delete job")
        self._del_btn.setVisible(False)
        self._schedule_btn = Form.button(label="", icon="calendar-event", action=self._edit_schedule)
        self._schedule_btn.setToolTip("Schedule")
        self._schedule_btn.setVisible(False)
        actions_row.addWidget(self._add_btn)
        actions_row.addWidget(self._edit_btn)
        actions_row.addWidget(self._dup_btn)
        actions_row.addWidget(self._del_btn)
        actions_row.addWidget(self._schedule_btn)
        actions_row.addStretch(1)
        root.addLayout(actions_row)

        # Table
        self._table = QTableWidget(0, 6, self)
        self._table.setHorizontalHeaderLabels([
            "Name", "Enabled", "Source", "Target", "Last run", "Result"
        ])
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)

        # Hide row numbers
        self._table.verticalHeader().setVisible(False)

        # Connect selection change to update visibility of action buttons
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        # Ensure columns never shrink below their content (use scrollbar for overflow)
        header = self._table.horizontalHeader()
        for i in range(self._table.columnCount()):
            header.setSectionResizeMode(i, QHeaderView.ResizeToContents)

        # Keep horizontal scrollbar available for overflow
        header.setStretchLastSection(False)

        root.addWidget(self._table)

        # --- Bottom row: Configurator and Run now (right aligned) ---
        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        config_btn = Form.button(label="", icon="gear-fill", action=self._configuration.show)
        config_btn.setToolTip("Configurator")
        run_btn = Form.button(label="", icon="play-fill", action=lambda: self._run_with_ui_feedback())
        run_btn.setToolTip("Run now")
        bottom_row.addWidget(config_btn)
        bottom_row.addWidget(run_btn)
        root.addLayout(bottom_row)

        self._reload_jobs()
        self._on_selection_changed()


    def _on_selection_changed(self, *_args):
        has = self._selected_index() >= 0
        if hasattr(self, "_edit_btn"):
            self._edit_btn.setVisible(has)
        if hasattr(self, "_dup_btn"):
            self._dup_btn.setVisible(has)
        if hasattr(self, "_del_btn"):
            self._del_btn.setVisible(has)
        if hasattr(self, "_schedule_btn"):
            self._schedule_btn.setVisible(has)

    def _selected_index(self) -> int:
        if not self._table:
            return -1
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return -1
        return rows[0].row()

    def _reload_jobs(self):
        self._jobs = self._store.fetch_all()
        self._refresh_table()

    def _save_jobs(self):
        return

    def _refresh_table(self):
        if not self._table:
            return
        self._table.setRowCount(0)

        for job in self._jobs:
            r = self._table.rowCount()
            self._table.insertRow(r)

            def _it(v: Any) -> QTableWidgetItem:
                item = QTableWidgetItem(str(v) if v is not None else "")
                item.setFlags(item.flags() ^ Qt.ItemIsEditable)
                return item

            self._table.setItem(r, 0, _it(job.name))
            self._table.setItem(r, 1, _it("On" if job.enabled else "Off"))

            src_str = f"{job.sourceEndpoint.type}:{job.sourceEndpoint.location}"
            tgt_str = f"{job.targetEndpoint.type}:{job.targetEndpoint.location}"
            self._table.setItem(r, 2, _it(src_str))
            self._table.setItem(r, 3, _it(tgt_str))

            last_run = job.lastRun
            self._table.setItem(r, 4, _it(last_run if last_run else "Never"))

            self._table.setItem(r, 5, _it(job.lastResult or ""))

    def _default_interval_seconds(self) -> int:
        """Return the configured default interval in seconds (service.defaultInterval), fallback to 3600."""
        try:
            v = self._configuration.get("service.defaultInterval", 3600)
            iv = int(v)
            return iv if iv > 0 else 3600
        except Exception:
            return 3600

    def _default_schedule_dict(self) -> Dict[str, Any]:
        interval = self._default_interval_seconds()
        windows: Dict[str, Any] = {}
        for wd in range(7):
            windows[str(wd)] = [{"start": "00:00", "end": "23:59", "intervalSeconds": interval}]
        return {
            "enabled": True,
            "intervalSeconds": interval,
            "windows": windows,
        }

    def _schedule_interval_seconds_for_now(self, sched: Dict[str, Any]) -> int:
        """Best-effort interval seconds for logging (supports per-day interval stored in window dicts)."""
        try:
            windows = sched.get("windows") if isinstance(sched.get("windows"), dict) else {}
            wd = datetime.now().astimezone().weekday()
            day = windows.get(str(wd)) or windows.get(wd)  # type: ignore[index]
            if isinstance(day, list) and day and isinstance(day[0], dict):
                v = day[0].get("intervalSeconds")
                if v is not None:
                    return int(v)
        except Exception:
            pass

        default_interval = self._default_interval_seconds()

        try:
            if "intervalSeconds" in sched:
                v = int(sched.get("intervalSeconds") or 0)
                return v if v > 0 else default_interval
        except Exception:
            pass

        return default_interval

    def _job_from_legacy_dict(self, d: Dict[str, Any], *, existing_id: Optional[int] = None) -> Job:
        src = d.get("sourceEndpoint") or {"type": "local", "location": d.get("source", ""), "auth": {}}
        tgt = d.get("targetEndpoint") or {"type": "local", "location": d.get("target", ""), "auth": {}}

        # Schedule defaults:
        #   - enabled
        #   - every day, all day
        #   - interval 3600 seconds (1h)
        sched = d.get("schedule")
        if not isinstance(sched, dict):
            sched = self._default_schedule_dict()

        default_interval = self._default_interval_seconds()

        # Normalize intervalSeconds (no everyMinutes support)
        try:
            iv = int(sched.get("intervalSeconds") or 0)
        except Exception:
            iv = 0
        if iv <= 0:
            iv = default_interval
        sched["intervalSeconds"] = iv

        # Ensure windows exist; if missing, make it "every day, all day" with per-day interval
        windows = sched.get("windows")
        if not isinstance(windows, dict) or not windows:
            windows = {}
            for wd in range(7):
                windows[str(wd)] = [{"start": "00:00", "end": "23:59", "intervalSeconds": iv}]
            sched["windows"] = windows
        else:
            # Ensure each enabled day window includes intervalSeconds (per-day) for the upcoming scheduler logic
            for k, v in list(windows.items()):
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    if "intervalSeconds" not in v[0]:
                        v[0]["intervalSeconds"] = iv

        j = Job(
            id=existing_id,
            name=str(d.get("name") or ""),
            enabled=bool(d.get("enabled", True)),
            mode=str(d.get("mode") or "mirror"),
            direction=str(d.get("direction") or "unidirectional"),
            allowDeletion=bool(d.get("allowDeletion", False)),
            preserveMetadata=bool(d.get("preserveMetadata", True)),
            conflictPolicy=str(d.get("conflictPolicy") or "newest"),
            pairId=d.get("pairId"),
            lastRun=d.get("lastRun"),
            lastResult=d.get("lastResult"),
            lastError=d.get("lastError"),
        )

        j.sourceEndpoint = Endpoint(
            type=str(src.get("type") or "local"),
            location=str(src.get("location") or ""),
            auth=dict(src.get("auth") or {}),
        )
        j.targetEndpoint = Endpoint(
            type=str(tgt.get("type") or "local"),
            location=str(tgt.get("location") or ""),
            auth=dict(tgt.get("auth") or {}),
        )

        windows = sched.get("windows") if isinstance(sched.get("windows"), dict) else {}

        j.schedule = Schedule(
            enabled=bool(sched.get("enabled", True)),
            intervalSeconds=int(iv),
            windows=windows,
        )

        return j

    def _add_job(self):
        # Pass configured service.defaultInterval into the dialog so Schedule defaults match user config
        dlg = JobDialog(self, default_interval_seconds=self._default_interval_seconds())
        if dlg.exec_() == QDialog.Accepted:
            new_job_dict = dlg.value()
            if "schedule" not in new_job_dict or not isinstance(new_job_dict.get("schedule"), dict):
                new_job_dict["schedule"] = self._default_schedule_dict()
            job_obj = self._job_from_legacy_dict(new_job_dict)
            job_id = self._store.upsert(job_obj)
            job_obj.id = job_id
            self._reload_jobs()

    def _edit_job(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to edit.", icon="info")
            return

        current = self._jobs[idx]
        # Pass configured service.defaultInterval into the dialog so Schedule defaults remain consistent
        dlg = JobDialog(self, current.to_legacy_dict(), default_interval_seconds=self._default_interval_seconds())
        if dlg.exec_() == QDialog.Accepted:
            updated_dict = dlg.value()
            job_obj = self._job_from_legacy_dict(updated_dict, existing_id=current.id)
            self._store.upsert(job_obj)
            self._reload_jobs()

    def _duplicate_job(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to duplicate.", icon="info")
            return

        orig = self._jobs[idx]
        d = orig.to_legacy_dict()
        d.pop("id", None)
        d["name"] = f"{orig.name} (copy)" if orig.name else "Copy"

        job_obj = self._job_from_legacy_dict(d)
        self._store.upsert(job_obj)
        self._reload_jobs()

        if self._table:
            # Select the newest row (best-effort)
            self._table.selectRow(self._table.rowCount() - 1)

    def _delete_job(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to delete.", icon="info")
            return

        job = self._jobs[idx]
        choice = MsgBox.show(
            self,
            title="Delete",
            message=f"Delete job '{job.name}'?",
            icon="question",
            buttons=("Cancel", "Delete"),
            default="Cancel",
        )
        if choice == "Delete":
            if job.id:
                self._store.delete(int(job.id))
            self._reload_jobs()

    def _edit_schedule(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to edit schedule.", icon="info")
            return

        current = self._jobs[idx]
        # Pass configured service.defaultInterval into the schedule editor so defaults match user config
        dlg = ScheduleDialog(self, job=current.to_legacy_dict(), default_interval_seconds=self._default_interval_seconds())
        if dlg.exec_() == QDialog.Accepted:
            d = current.to_legacy_dict()
            d["schedule"] = dlg.value()
            job_obj = self._job_from_legacy_dict(d, existing_id=current.id)
            self._store.upsert(job_obj)
            self._reload_jobs()

    def _run_with_ui_feedback(self):
        ok = self.run()
        MsgBox.show(
            self,
            title="Replicator",
            message="Replication completed successfully." if ok else "Replication finished with errors.",
            icon="info" if ok else "warning",
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_scheduled(self) -> bool:
        """
        Scheduled/service execution entrypoint.
        Respects per-job schedule (enabled/windows/interval).
        Returns True if all eligible jobs succeeded.
        """
        self._reload_jobs()

        verbose_db = False
        try:
            verbose_db = bool(self._configuration.get("log.verbose", False))
        except Exception:
            verbose_db = False
        self._db_debug_snapshot(verbose=verbose_db)

        if not self._jobs:
            self._log("[Replicator] No jobs configured.", level="warning")
            return False

        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone()  # interpret windows as local times

        any_ran = False
        all_ok = True

        for job in self._jobs:
            if not bool(job.enabled):
                continue

            if not self._should_run_scheduled(job, now_utc=now_utc, now_local=now_local):
                continue

            any_ran = True
            ok = self._run_job(job)
            all_ok = all_ok and ok

            # Update schedule bookkeeping for this job
            try:
                sched_row = self._db.one(
                    "SELECT enabled, nextRunAt, lastScheduledRunAt, windows, intervalSeconds FROM schedule WHERE jobId=?",
                    (int(job.id),),
                ) or {}
                interval_s = self._interval_seconds_for_schedule_row(sched_row, now_local)
                next_run = (datetime.now(timezone.utc) + timedelta(seconds=int(interval_s))).isoformat()
                self._db.update(
                    "schedule",
                    {
                        "lastScheduledRunAt": datetime.now(timezone.utc).isoformat(),
                        "nextRunAt": next_run,
                    },
                    "jobId = :jobId",
                    {"jobId": int(job.id)},
                )
            except Exception:
                pass

        if not any_ran:
            self._log("[Replicator] No jobs due per schedule.", level="debug")

        return all_ok

    def run(self) -> bool:
        """
        Run all configured jobs (from DB).
        Returns True if all jobs succeeded.
        """
        self._reload_jobs()
        # After reloading jobs, log a DB snapshot for debugging.
        # Only log verbose DB details when log.verbose is enabled.
        verbose_db = False
        try:
            verbose_db = bool(self._configuration.get("log.verbose", False))
        except Exception:
            verbose_db = False

        self._db_debug_snapshot(verbose=verbose_db)
        jobs: List[Job] = self._jobs
        if not jobs:
            self._log("[Replicator] No jobs configured.", level="warning")
            return False

        all_ok = True
        for job in jobs:
            if not bool(job.enabled):
                self._log(f"[Replicator] Skipping disabled job '{job.name or 'Unnamed'}'.", level="info")
                continue
            ok = self._run_job(job)
            all_ok = all_ok and ok

        # Run DB maintenance at most once per day.
        try:
            last = self._migration.get_meta("maintenance.lastRunAt")
            should = True
            if last:
                try:
                    last_dt = datetime.fromisoformat(str(last))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    should = (datetime.now(timezone.utc) - last_dt).total_seconds() >= 86400
                except Exception:
                    should = True
            if should:
                self._db_maintenance()
        except Exception:
            pass

        return all_ok

    def _run_job(self, job: Job) -> bool:
        name = job.name or "Unnamed"
        if not bool(job.enabled):
            self._log(f"[Replicator] Job '{name}' is disabled; skipping.", level="info")
            return True
        job_id = job.id
        src = job.sourceEndpoint.location
        src_type = job.sourceEndpoint.type
        dst = job.targetEndpoint.location
        dst_type = job.targetEndpoint.type
        allow_deletion = bool(job.allowDeletion)
        preserve_metadata = bool(job.preserveMetadata)
        mode = job.mode
        direction = job.direction
        schedule = job.to_legacy_dict().get("schedule") or {}

        if not src or not dst:
            self._log(f"[Replicator] Job '{name}' invalid: missing source/target.", level="error")
            return False

        sched_str = ""
        if isinstance(schedule, dict) and bool(schedule.get("enabled", True)):
            interval_s = self._schedule_interval_seconds_for_now(schedule)
            sched_str = f", schedule=Interval {interval_s}s"
        logline = f"[Replicator] Running job '{name}': {src} -> {dst} (mode={mode}, direction={direction}, delete={allow_deletion}, meta={preserve_metadata}{sched_str}"
        logline += f", srcType={src_type}, dstType={dst_type})"
        self._log(logline)

        # Insert a run record at start
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = None
        bidi_stats = None
        try:
            run_id = None
            if job_id:
                try:
                    run_id = int(self._db.insert("runs", {"jobId": int(job_id), "startedAt": started_at, "result": "running"}))
                except Exception as e:
                    self._log(f"[Replicator] Failed to insert run record: {e}", level="warning")
        except Exception as e:
            self._log(f"[Replicator] Failed to insert run record: {e}", level="warning")

        record_noop_runs = bool(self._configuration.get("db.recordNoopRuns", False))

        ok = False
        try:
            if str(direction).lower() == "bidirectional":
                # Mount remote endpoints (SMB only) via Share so the bidirectional engine can
                # operate on local filesystem paths AND we get proper connectivity debug logs.
                mounted: list[_MountedEndpoint] = []
                job_dict = job.to_legacy_dict()

                src_ep = job_dict.get("sourceEndpoint") or {"type": "local", "location": src, "auth": {}}
                dst_ep = job_dict.get("targetEndpoint") or {"type": "local", "location": dst, "auth": {}}

                share_logger = _ShareLogger(self._log)
                share = Share(logger=share_logger)

                src_m = _mount_endpoint_if_remote(src_ep, job_id, "source", share=share, log_append=self._log)
                mounted.append(src_m)
                dst_m = _mount_endpoint_if_remote(dst_ep, job_id, "target", share=share, log_append=self._log)
                mounted.append(dst_m)

                # Local view for the bidirectional sync engine
                job_dict["sourceEndpoint"] = {"type": "local", "location": src_m.local_path, "auth": {}}
                job_dict["targetEndpoint"] = {"type": "local", "location": dst_m.local_path, "auth": {}}

                try:
                    ok, bidi_stats = self._run_job_bidirectional(job_dict, run_id)
                finally:
                    for m in reversed(mounted):
                        try:
                            m.cleanup()
                        except Exception:
                            pass
            else:
                ok = self._fs.copy(
                    src,
                    dst,
                    preserve_metadata=preserve_metadata,
                    allow_deletion=allow_deletion,
                )
        except NotImplementedError as e:
            self._log(f"[Replicator] Job '{name}' not supported: {e}", level="error")
            ok = False
        except RemoteMountError as e:
            self._log(f"[Replicator] Job '{name}' failed to mount remote endpoint: {e}", level="error")
            ok = False
        except Exception as e:
            self._log(f"[Replicator] Job '{name}' failed: {e}", level="error")
            ok = False

        # If this was a bidirectional run with no actions/changes, optionally drop the run row.
        if run_id and bidi_stats is not None and not record_noop_runs:
            try:
                noop = (
                    int(bidi_stats.get("copyAtoB", 0) or 0) == 0
                    and int(bidi_stats.get("copyBtoA", 0) or 0) == 0
                    and int(bidi_stats.get("delA", 0) or 0) == 0
                    and int(bidi_stats.get("delB", 0) or 0) == 0
                    and int(bidi_stats.get("changedA", 0) or 0) == 0
                    and int(bidi_stats.get("changedB", 0) or 0) == 0
                    and int(bidi_stats.get("deletedA", 0) or 0) == 0
                    and int(bidi_stats.get("deletedB", 0) or 0) == 0
                )
                if noop:
                    self._db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
                    run_id = None
            except Exception:
                pass

        # Persist lastRun and lastResult, refresh UI, save to DB
        now_str = datetime.now(timezone.utc).isoformat()
        job.lastRun = now_str
        job.lastResult = "ok" if ok else "fail"
        job.lastError = None if ok else (job.lastError or "Failed")
        self._store.upsert(job)
        if run_id:
            try:
                self._db.update(
                    "runs",
                    {"endedAt": now_str, "result": ("ok" if ok else "fail"), "message": (None if ok else (job.lastError or "Failed"))},
                    "id = :id",
                    {"id": int(run_id)},
                )
            except Exception as e:
                self._log(f"[Replicator] Failed to update run record: {e}", level="warning")
        if self._table:
            self._reload_jobs()

        self._log(f"[Replicator] Job '{job.name}' result: {'OK' if ok else 'FAIL'} (lastResult={job.lastResult})", level="info" if ok else "warning")
        return ok

    def _log(self, msg: str, level: str = "info", channel: str = "replicator") -> None:
        if self._logger is not None and hasattr(self._logger, "append"):
            self._logger.append(msg, level=level, channel=channel)  # type: ignore[call-arg]
        else:
            print(msg)

    def _db_debug_snapshot(self, verbose: bool = False) -> None:
        """
        Log a compact snapshot of the current DB layout + row counts.
        If verbose=True, also logs the most recent rows for key tables.
        """
        try:
            # List tables
            tables = [r["name"] for r in self._db.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]

            self._log(f"[Replicator][DB] Tables: {', '.join(tables) if tables else '(none)'}", level="debug")

            # Layout + counts
            for t in tables:
                try:
                    cols = self._db.query(f"PRAGMA table_info({t})")
                    col_names = [c.get("name") for c in cols]
                    count = int(self._db.scalar(f"SELECT COUNT(*) FROM {t}") or 0)
                    self._log(f"[Replicator][DB] {t}: columns={len(col_names)} rows={count}", level="debug")
                    if verbose:
                        self._log(f"[Replicator][DB] {t}: {', '.join(col_names)}", level="debug")
                except Exception as e:
                    self._log(f"[Replicator][DB] Failed introspecting {t}: {e}", level="warning")

            if not verbose:
                return

            for t, order_col in (("jobs", "id"), ("runs", "id"), ("schedule", "id"), ("conflicts", "id")):
                if t in tables:
                    try:
                        rows = self._db.query(f"SELECT * FROM {t} ORDER BY {order_col} DESC LIMIT 3")
                        self._log(f"[Replicator][DB] {t}: latest {len(rows)} row(s)", level="debug")
                        for r in rows:
                            dr = dict(r)
                            for k in ("password", "pass", "sshKey", "key_file"):
                                if k in dr and dr[k]:
                                    dr[k] = "***"
                            self._log(f"[Replicator][DB] {t}: {dr}", level="debug")
                    except Exception as e:
                        self._log(f"[Replicator][DB] Failed reading latest rows from {t}: {e}", level="warning")

        except Exception as e:
            self._log(f"[Replicator][DB] Snapshot failed: {e}", level="warning")




    def _db_maintenance(self) -> None:
        """Prune old history and run lightweight SQLite maintenance.

        Safe defaults:
          - Keep last N runs per job (db.retention.runs.keepLast)
          - Also prune runs older than X days (db.retention.runs.keepDays)
          - Keep open conflicts; prune resolved conflicts older than X days
          - Optionally VACUUM (off by default)

        This is designed to be called periodically (e.g. daily) by the service loop.
        """
        try:
            keep_last = int(self._configuration.get("db.retention.runs.keepLast", 500))
            keep_days = int(self._configuration.get("db.retention.runs.keepDays", 30))
            prune_conflict_days = int(self._configuration.get("db.retention.conflicts.keepDays", 90))
            prune_deleted_state_days = int(self._configuration.get("db.retention.fileState.deletedKeepDays", 30))
            do_vacuum = bool(self._configuration.get("db.maintenance.vacuum", False))
        except Exception:
            keep_last, keep_days = 500, 30
            prune_conflict_days, prune_deleted_state_days = 90, 30
            do_vacuum = False

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        # Runs: keep last N per job + prune anything older than keep_days
        try:
            job_ids = [int(r.get("id")) for r in (self._db.query("SELECT id FROM jobs") or [])]
            with self._db.transaction():
                for jid in job_ids:
                    if keep_last > 0:
                        self._db.execute(
                            """
                            DELETE FROM runs
                            WHERE jobId = ?
                              AND id NOT IN (
                                SELECT id FROM runs WHERE jobId = ? ORDER BY id DESC LIMIT ?
                              )
                            """,
                            (jid, jid, keep_last),
                        )

                if keep_days > 0:
                    self._db.execute(
                        "DELETE FROM runs WHERE julianday(created) < julianday('now', ?) ",
                        (f'-{keep_days} days',),
                    )
        except Exception as e:
            self._log(f"[Replicator][DB] Maintenance: runs prune failed: {e}", level="warning")

        # Conflicts: keep all open; prune non-open older than prune_conflict_days
        try:
            if prune_conflict_days > 0:
                with self._db.transaction():
                    self._db.execute(
                        "DELETE FROM conflicts WHERE status <> 'open' AND julianday(created) < julianday('now', ?) ",
                        (f'-{prune_conflict_days} days',),
                    )
        except Exception as e:
            self._log(f"[Replicator][DB] Maintenance: conflicts prune failed: {e}", level="warning")

        # file_state: prune deleted entries that have been deleted for a long time
        try:
            if prune_deleted_state_days > 0:
                with self._db.transaction():
                    self._db.execute(
                        "DELETE FROM file_state WHERE deleted = 1 AND deletedAt IS NOT NULL AND julianday(deletedAt) < julianday('now', ?) ",
                        (f'-{prune_deleted_state_days} days',),
                    )
        except Exception as e:
            self._log(f"[Replicator][DB] Maintenance: file_state prune failed: {e}", level="warning")

        # SQLite maintenance: optimize; optionally vacuum
        try:
            self._db.execute("PRAGMA optimize;")
            if do_vacuum:
                self._db.execute("VACUUM;")
            self._migration.set_meta("maintenance.lastRunAt", now_iso)
            self._log("[Replicator][DB] Maintenance completed.", level="debug")
        except Exception as e:
            self._log(f"[Replicator][DB] Maintenance failed: {e}", level="warning")
