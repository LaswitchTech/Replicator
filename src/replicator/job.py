#!/usr/bin/env python3
# src/replicator/job.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Union
import json
import sqlite3

import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import os
import re

try:
    # corePY SQLite wrapper (preferred)
    from core.database.sqlite import SQLite  # type: ignore
except Exception:  # pragma: no cover
    SQLite = None  # type: ignore

try:
    # corePY Share mount helper
    from core.filesystem.share import Share, ShareAuth, ShareError  # type: ignore
except Exception:  # pragma: no cover
    Share = None  # type: ignore
    ShareAuth = None  # type: ignore
    ShareError = Exception  # type: ignore



JsonDict = Dict[str, Any]


# ---------------------------------------------------------------------------
# Logging helpers (Share adapter + secret redaction)
# ---------------------------------------------------------------------------

def _redact_secrets(s: str) -> str:
    """Redact obvious credentials from command strings/log lines."""
    if not s:
        return s
    s = re.sub(r"(pass=)([^,\s]+)", r"\1***", s, flags=re.IGNORECASE)
    s = re.sub(r"(password=)([^,\s]+)", r"\1***", s, flags=re.IGNORECASE)
    # common flags
    s = re.sub(r"(--password\s+)(\S+)", r"\1***", s, flags=re.IGNORECASE)
    s = re.sub(r"(--pass\s+)(\S+)", r"\1***", s, flags=re.IGNORECASE)
    return s


class _ShareLogger:
    """Adapter that exposes debug/info/warning/error and forwards to Job.run(logger=...)."""

    def __init__(self, fn: Optional[Callable[[str, str], None]]):
        self._fn = fn

    def debug(self, msg: str) -> None:
        if self._fn:
            self._fn(_redact_secrets(msg), "debug")

    def info(self, msg: str) -> None:
        if self._fn:
            self._fn(_redact_secrets(msg), "info")

    def warning(self, msg: str) -> None:
        if self._fn:
            self._fn(_redact_secrets(msg), "warning")

    def error(self, msg: str) -> None:
        if self._fn:
            self._fn(_redact_secrets(msg), "error")



# ---------------------------------------------------------------------------
# Remote endpoints (Share mount helpers)
# ---------------------------------------------------------------------------

class RemoteMountError(RuntimeError):
    pass

def _parse_smb_location(location: str) -> Tuple[str, str, str]:
    """Return (host, share, subpath) from location.

    Accepts forms:
      - //host/share/subpath
      - \\host\\share\\subpath
      - host/share/subpath
    """
    loc = (location or "").strip()
    loc = loc.replace("\\", "/")
    loc = loc.lstrip("/")
    parts = [p for p in loc.split("/") if p]
    if len(parts) < 2:
        raise RemoteMountError("SMB location must include host and share (e.g. //server/Share[/path]).")
    host = parts[0]
    share = parts[1]
    subpath = "/".join(parts[2:]) if len(parts) > 2 else ""
    return host, share, subpath


@dataclass
class _MountedEndpoint:
    local_path: str
    mount_point: Optional[str] = None
    share: Any = None  # Share instance when mounted via core.filesystem.share

    def cleanup(self) -> None:
        if self.share is not None and self.mount_point:
            try:
                self.share.umount(self.mount_point, elevate=False)
            except Exception:
                pass


def _mount_endpoint_if_remote(
    endpoint: "Endpoint",
    job_id: Union[int, None],
    role: str,
    *,
    logger: Optional[Callable[[str, str], None]] = None,
    timeout: int = 15,
) -> _MountedEndpoint:
    """If endpoint is remote (smb), mount it and return local path; otherwise return local location.

    Uses core.filesystem.share.Share (rclone-backed on macOS when only rclone is bundled).
    """
    t = (endpoint.type or "local").lower()
    if t == "local":
        return _MountedEndpoint(local_path=endpoint.location)

    if t != "smb":
        raise RemoteMountError(f"Unsupported endpoint type: {t}")

    if Share is None:
        raise RemoteMountError("Share support not available (failed to import core.filesystem.share.Share)")

    # Mount under a stable temp folder for the job
    base = Path(tempfile.gettempdir()) / "replicator" / "mounts" / (str(job_id or "new")) / role
    base.mkdir(parents=True, exist_ok=True)
    mount_point = str(base)

    # Parse endpoint location into host + remote path
    host = ""
    remote = ""
    auth_in = dict(endpoint.auth or {}) if isinstance(endpoint.auth, dict) else {}

    if t == "smb":
        host, share, subpath = _parse_smb_location(endpoint.location)
        remote = share + (f"/{subpath}" if subpath else "")

    # Build ShareAuth
    try:
        share_auth = ShareAuth(
            username=str(auth_in.get("username") or ""),
            password=str(auth_in.get("password") or ""),
            domain=str(auth_in.get("domain") or ""),
            port=int(auth_in.get("port") or 445) or None,
            guest=bool(auth_in.get("guest", True)),
        )
    except Exception:
        # Fallback if ShareAuth signature changes
        share_auth = None

    share_logger = _ShareLogger(logger)
    share = Share(logger=share_logger)

    # Allow passing through extra rclone args (e.g. VFS cache mode) via auth['rcloneArgs']
    extra_args = auth_in.get("rcloneArgs") if isinstance(auth_in.get("rcloneArgs"), list) else None
    if extra_args:
        share_logger.debug(f"[Share] Extra mount args provided: {extra_args}")

    # Mount
    try:
        share_logger.debug(f"[Share] Mount request: protocol={t} host={host} remote={remote} mount_point={mount_point}")
        # Share.mount supports a generic signature; pass known fields.
        share.mount(
            protocol=t,
            host=host,
            remote=remote,
            mount_point=mount_point,
            auth=share_auth or auth_in,
            timeout=timeout,
            read_only=False,
            elevate=False,
        )
    except Exception as e:
        raise RemoteMountError(f"Failed to mount {t} endpoint via Share: {e}")

    # Share performs its own probe logging; return the mounted endpoint.
    return _MountedEndpoint(local_path=mount_point, mount_point=mount_point, share=share)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Endpoint:
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

        if t == "smb":
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
        elif t == "smb":
            guest = 1 if bool(auth.get("guest", True)) else 0
            username = auth.get("username") or None
            password = auth.get("password") or None
            # Optional SMB domain
            options["domain"] = auth.get("domain")
            # Optional rclone args (applies to all remote types)
            if isinstance(auth.get("rcloneArgs"), list):
                options["rcloneArgs"] = auth.get("rcloneArgs")

        known_keys = {"guest", "username", "password", "port", "domain"}
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
        t = (row.get("type") or "local").lower()
        auth: JsonDict = {}

        if t == "local":
            auth = {}
        elif t == "smb":
            auth = {
                "guest": bool(row.get("guest", 1)),
                "username": row.get("username") or "",
                "password": row.get("password") or "",
            }
            auth["port"] = row.get("port") or 445
            # domain is stored in options JSON when present

        opt = row.get("options")
        if opt:
            try:
                extra = json.loads(opt)
                if isinstance(extra, dict):
                    auth.update(extra)
            except Exception:
                pass

        return Endpoint(type=t, location=row.get("location") or "", auth=auth)


@dataclass
class Schedule:
    enabled: bool = True
    intervalSeconds: int = 3600
    windows: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "intervalSeconds": int(self.intervalSeconds or 0),
            "windows": self.windows if isinstance(self.windows, dict) else {},
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Schedule":
        d = d or {}
        enabled = bool(d.get("enabled", True))
        # intervalSeconds can be on the schedule itself, or in per-day window objects
        try:
            interval_s = int(d.get("intervalSeconds") or 0)
        except Exception:
            interval_s = 0
        windows = d.get("windows") if isinstance(d.get("windows"), dict) else {}
        if interval_s <= 0:
            # best-effort: derive from first window on any day
            try:
                for _k, _v in windows.items():
                    if isinstance(_v, list) and _v and isinstance(_v[0], dict):
                        v = _v[0].get("intervalSeconds")
                        if v is not None:
                            interval_s = int(v or 0)
                            break
            except Exception:
                pass
        if interval_s <= 0:
            interval_s = 3600
        return Schedule(enabled=enabled, intervalSeconds=interval_s, windows=windows)

    def validate(self) -> List[str]:
        errs: List[str] = []
        if self.enabled:
            try:
                iv = int(self.intervalSeconds or 0)
            except Exception:
                iv = 0
            if iv <= 0:
                errs.append("Schedule intervalSeconds must be > 0 when schedule is enabled.")
        # windows is optional; if provided, validate structure best-effort
        if self.windows and not isinstance(self.windows, dict):
            errs.append("Schedule windows must be an object/dict.")
            return errs

        def _parse_hhmm(val: Any) -> Optional[Tuple[int, int]]:
            try:
                parts = str(val).strip().split(":")
                if len(parts) != 2:
                    return None
                hh = int(parts[0]); mm = int(parts[1])
                if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                    return None
                return (hh, mm)
            except Exception:
                return None

        try:
            for _day, arr in (self.windows or {}).items():
                if not isinstance(arr, list):
                    errs.append("Schedule windows entries must be lists.")
                    continue
                for w in arr:
                    if not isinstance(w, dict):
                        errs.append("Schedule window must be an object.")
                        continue
                    if _parse_hhmm(w.get("start")) is None:
                        errs.append("Schedule window start must be HH:MM.")
                    if _parse_hhmm(w.get("end")) is None:
                        errs.append("Schedule window end must be HH:MM.")
                    if "intervalSeconds" in w:
                        try:
                            if int(w.get("intervalSeconds") or 0) <= 0:
                                errs.append("Schedule window intervalSeconds must be > 0 when provided.")
                        except Exception:
                            errs.append("Schedule window intervalSeconds must be an integer.")
        except Exception:
            # best-effort validation only
            pass

        return errs

    def _window_allows_now_local(self, now_local: datetime) -> bool:
        # If windows is empty/missing => allowed.
        if not self.windows or not isinstance(self.windows, dict):
            return True

        wd = now_local.weekday()  # 0..6 (Mon..Sun)
        day_windows = self.windows.get(str(wd)) or self.windows.get(wd)
        if not isinstance(day_windows, list) or not day_windows:
            return False

        tnow = now_local.time().replace(tzinfo=None)

        def _parse_hhmm(val: Any) -> Optional[Tuple[int, int]]:
            try:
                parts = str(val).strip().split(":")
                if len(parts) != 2:
                    return None
                hh = int(parts[0]); mm = int(parts[1])
                if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                    return None
                return (hh, mm)
            except Exception:
                return None

        for w in day_windows:
            if not isinstance(w, dict):
                continue
            ps = _parse_hhmm(w.get("start"))
            pe = _parse_hhmm(w.get("end"))
            if ps is None or pe is None:
                continue
            sh, sm = ps; eh, em = pe
            ts = datetime(2000, 1, 1, sh, sm).time()
            te = datetime(2000, 1, 1, eh, em).time()

            if ts <= te:
                if ts <= tnow <= te:
                    return True
            else:
                # overnight window (e.g. 22:00-06:00)
                if tnow >= ts or tnow <= te:
                    return True

        return False

    def interval_seconds_for_now(self, now: Optional[datetime] = None) -> int:
        now_local = (now or datetime.now(timezone.utc)).astimezone()
        # Priority:
        #   1) per-day window intervalSeconds (first window entry)
        #   2) schedule.intervalSeconds
        try:
            wd = now_local.weekday()
            day = None
            if isinstance(self.windows, dict):
                day = self.windows.get(str(wd)) or self.windows.get(wd)
            if isinstance(day, list) and day and isinstance(day[0], dict):
                v = day[0].get("intervalSeconds")
                if v is not None:
                    iv = int(v or 0)
                    if iv > 0:
                        return iv
        except Exception:
            pass

        try:
            iv = int(self.intervalSeconds or 0)
            return iv if iv > 0 else 3600
        except Exception:
            return 3600

    def should_run_now(self, now: Optional[datetime] = None) -> bool:
        if not self.enabled:
            return False
        now_local = (now or datetime.now(timezone.utc)).astimezone()
        return self._window_allows_now_local(now_local)

    def next_run_at(self, now: Optional[datetime] = None) -> Optional[datetime]:
        # This domain object doesn't store lastScheduledRunAt; caller should decide cadence.
        # We return "now + interval" if windows allow now; otherwise the next allowed window start.
        if not self.enabled:
            return None

        now_utc = now or datetime.now(timezone.utc)
        now_local = now_utc.astimezone()

        if self._window_allows_now_local(now_local):
            return now_utc + timedelta(seconds=int(self.interval_seconds_for_now(now_utc)))

        # Find next allowed window start within the next 7 days (best-effort).
        if not self.windows or not isinstance(self.windows, dict):
            return now_utc + timedelta(seconds=int(self.interval_seconds_for_now(now_utc)))

        def _parse_hhmm(val: Any) -> Optional[Tuple[int, int]]:
            try:
                parts = str(val).strip().split(":")
                if len(parts) != 2:
                    return None
                hh = int(parts[0]); mm = int(parts[1])
                if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                    return None
                return (hh, mm)
            except Exception:
                return None

        base_local = now_local.replace(second=0, microsecond=0)
        for add_days in range(0, 8):
            day_local = base_local + timedelta(days=add_days)
            wd = day_local.weekday()
            day_windows = self.windows.get(str(wd)) or self.windows.get(wd)
            if not isinstance(day_windows, list) or not day_windows:
                continue
            # use the first window as the start candidate (UI currently writes a single window per day)
            w0 = day_windows[0] if isinstance(day_windows[0], dict) else None
            if not w0:
                continue
            ps = _parse_hhmm(w0.get("start"))
            if ps is None:
                continue
            sh, sm = ps
            candidate_local = day_local.replace(hour=sh, minute=sm)
            candidate_utc = candidate_local.astimezone(timezone.utc)
            if candidate_utc > now_utc:
                return candidate_utc

        return None


@dataclass
class JobRunResult:
    ok: bool
    started_at: str
    ended_at: str
    result: str
    message: Optional[str] = None
    stats: JsonDict = field(default_factory=dict)


@dataclass
class Job:
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

    def run(
        self,
        *,
        now: Optional[datetime] = None,
        copy_func: Optional[Callable[..., bool]] = None,
        bidirectional_func: Optional[Callable[..., Tuple[bool, JsonDict]]] = None,
        logger: Optional[Callable[[str, str], None]] = None,
    ) -> JobRunResult:
        now_dt = now or datetime.now(timezone.utc)
        started_at = now_dt.isoformat()

        def _log(msg: str, level: str = "info") -> None:
            if logger:
                try:
                    logger(msg, level)
                except Exception:
                    pass

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

        preserve = bool(self.preserveMetadata)
        allow_del = bool(self.allowDeletion)

        # Resolve endpoints (mount SMB endpoints to local paths for the duration of the run)
        mounted: List[_MountedEndpoint] = []
        src_m = _mount_endpoint_if_remote(self.sourceEndpoint, self.id, "source", logger=logger)
        mounted.append(src_m)
        dst_m = _mount_endpoint_if_remote(self.targetEndpoint, self.id, "target", logger=logger)
        mounted.append(dst_m)

        src = src_m.local_path
        dst = dst_m.local_path

        ok: bool = False
        stats: JsonDict = {}

        try:
            if (self.direction or "").lower() == "bidirectional":
                if not bidirectional_func:
                    raise NotImplementedError("Bidirectional engine not provided.")

                # IMPORTANT:
                # The bidirectional engine historically enforced local endpoints only by checking
                # endpoint types. Since remote endpoints are mounted to local paths for the duration
                # of the run, we provide a local-view of this job to the engine.
                job_local_view = Job(
                    id=self.id,
                    name=self.name,
                    enabled=self.enabled,
                    mode=self.mode,
                    direction=self.direction,
                    allowDeletion=self.allowDeletion,
                    preserveMetadata=self.preserveMetadata,
                    conflictPolicy=self.conflictPolicy,
                    pairId=self.pairId,
                    sourceEndpoint=Endpoint(type="local", location=src, auth={}),
                    targetEndpoint=Endpoint(type="local", location=dst, auth={}),
                    schedule=self.schedule,
                    lastRun=self.lastRun,
                    lastResult=self.lastResult,
                    lastError=self.lastError,
                )

                _log(f"[Job] Running bidirectional job '{self.name}': {src} <-> {dst}", "info")
                ok, stats = bidirectional_func(job_local_view, None)
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
        finally:
            # Always unmount remote endpoints
            for m in reversed(mounted):
                try:
                    m.cleanup()
                except Exception:
                    pass

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

    def to_row_dicts(self) -> Dict[str, Any]:
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
            "enabled": 1 if self.schedule and self.schedule.enabled else 0,
            "intervalSeconds": int(self.schedule.intervalSeconds if self.schedule else 3600),
            "windows": json.dumps(self.schedule.windows if self.schedule and isinstance(self.schedule.windows, dict) else {}),
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

        if schedule_row:
            # Read intervalSeconds and windows as new model, fallback as needed
            try:
                interval_s = int(schedule_row.get("intervalSeconds") or 0)
            except Exception:
                interval_s = 0
            # Remove legacy everyMinutes fallback
            if interval_s <= 0:
                interval_s = 3600
            windows = {}
            try:
                raw = schedule_row.get("windows")
                if raw:
                    windows = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
            except Exception:
                windows = {}
            j.schedule = Schedule(
                enabled=bool(schedule_row.get("enabled", 1)),
                intervalSeconds=int(interval_s),
                windows=windows,
            )
        else:
            j.schedule = Schedule()

        return j

    def to_legacy_dict(self) -> JsonDict:
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
            "schedule": self.schedule.to_dict() if self.schedule else {"enabled": True, "intervalSeconds": 3600, "windows": {}},
            "lastRun": self.lastRun,
            "lastResult": self.lastResult,
            "lastError": self.lastError,
        }


# ---------------------------------------------------------------------------
# JobStore (DB persistence)
# ---------------------------------------------------------------------------

class JobStore:
    """SQLite persistence for Job domain objects (UI-agnostic)."""

    def __init__(self, db: Any):
        self._db = db

    def _is_core_sqlite(self) -> bool:
        return SQLite is not None and isinstance(self._db, SQLite)

    def _conn(self) -> sqlite3.Connection:
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

            # --- endpoints (unique: jobId+role) ---
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
                self._upsert(
                    "endpoints",
                    ep_data,
                    ["jobId", "role"],
                    update_columns=["type", "location", "port", "guest", "username", "password", "useKey", "sshKey", "options"],
                )

            # --- schedule (unique: jobId) ---
            s_data = {
                "jobId": job_id,
                "enabled": int(schedule_row.get("enabled") or 0),
                "intervalSeconds": int(schedule_row.get("intervalSeconds") or 0),
                "windows": schedule_row.get("windows"),
            }
            self._upsert(
                "schedule",
                s_data,
                ["jobId"],
                update_columns=["enabled", "intervalSeconds", "windows"],
            )

        return int(job.id or 0)

    def delete(self, job_id: int) -> None:
        with self._transaction():
            self._delete("jobs", "id = ?", (int(job_id),))
