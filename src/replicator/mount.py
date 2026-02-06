
#!/usr/bin/env python3
# src/replicator/mount.py

"""Remote mount helpers for Replicator.

Purpose
-------
Centralize SMB mounting logic so it can be reused by both:
  - UI runner (replicator.py) which often works with legacy dict endpoints
  - Domain runner (job.py) which uses Endpoint dataclass objects

Supported
---------
- SMB (CIFS / Windows shares)

This module is intentionally thin and delegates to corePY's Share helper:
  from core.filesystem.share import Share, ShareAuth

Notes
-----
- Remote "location" strings are accepted in common forms:
    * \\host\\Share\\dir
    * //host/Share/dir
    * host/Share/dir
- On macOS, `mount_smbfs` expects URL-style paths; the remote part is
  percent-encoded (spaces, etc.) while keeping '/' separators intact.
- Secrets are never logged; obvious fields are redacted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import os
import re
import tempfile
from urllib.parse import quote


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RemoteMountError(RuntimeError):
    """Raised when a remote endpoint cannot be mounted or parsed."""


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def redact_secrets(s: str) -> str:
    """Best-effort secret redaction for log lines."""
    if not s:
        return s
    # key=value patterns
    s = re.sub(r"(pass=)([^,\s]+)", r"\1***", s, flags=re.IGNORECASE)
    s = re.sub(r"(password=)([^,\s]+)", r"\1***", s, flags=re.IGNORECASE)
    # CLI flags
    s = re.sub(r"(--password\s+)(\S+)", r"\1***", s, flags=re.IGNORECASE)
    s = re.sub(r"(--pass\s+)(\S+)", r"\1***", s, flags=re.IGNORECASE)
    return s


class ShareLoggerAdapter:
    """Adapter to feed Share's logging into Replicator/corePY style append()."""

    def __init__(self, append_fn: Callable[[str, str], None] | Callable[..., None]):
        self._append = append_fn

    def debug(self, msg: str) -> None:
        self._append(redact_secrets(msg), level="debug")  # type: ignore[misc]

    def info(self, msg: str) -> None:
        self._append(redact_secrets(msg), level="info")  # type: ignore[misc]

    def warning(self, msg: str) -> None:
        self._append(redact_secrets(msg), level="warning")  # type: ignore[misc]

    def error(self, msg: str) -> None:
        self._append(redact_secrets(msg), level="error")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class MountedEndpoint:
    """Represents a mounted (or local) endpoint."""

    local_path: str
    mount_point: Optional[str] = None
    share: Any = None  # Share instance (only present when mounted)

    def cleanup(self) -> None:
        """Unmount the share if mounted; best-effort."""
        if self.share is not None and self.mount_point:
            try:
                self.share.umount(self.mount_point, elevate=False)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_smb_location(location: str) -> Tuple[str, str]:
    """Parse SMB location into (host, remote).

    Accepts forms:
      - \\host\\Share
      - \\host\\Share\\dir\\sub
      - //host/Share/dir
      - host/Share/dir

    Returns:
      host, remote where remote is "Share" or "Share/dir/sub".
    """
    s = (location or "").strip()
    if not s:
        raise RemoteMountError("SMB location is empty")

    # Normalize to backslashes then split.
    s = s.replace("/", "\\")
    while s.startswith("\\"):
        s = s[1:]

    parts = [p for p in s.split("\\") if p]
    if len(parts) < 2:
        raise RemoteMountError(
            f"Invalid SMB location '{location}'. Expected //host/Share[/path] or \\\\host\\Share[/path]."
        )

    host = parts[0]
    remote = "/".join(parts[1:])
    return host, remote


# ---------------------------------------------------------------------------
# Mounting
# ---------------------------------------------------------------------------


def _get_mount_point(job_id: Optional[int], role: str, *, unique: bool = False) -> str:
    """Compute the local mount point for a job/role."""
    base = Path(tempfile.gettempdir()) / "replicator" / "mounts" / (str(job_id or "new")) / role
    if unique:
        # Avoid collisions if caller mounts multiple times quickly.
        base = base / str(os.getpid())
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def mount_endpoint_if_remote(
    endpoint: Any,
    job_id: Optional[int],
    role: str,
    *,
    share: Any = None,
    log_append: Optional[Callable[..., None]] = None,
    timeout: int = 60,
) -> MountedEndpoint:
    """Mount an endpoint if it is remote; otherwise return the local path.

    Parameters
    ----------
    endpoint:
        Can be either:
          - dict-like: {type, location, auth?} or legacy endpoint row fields
          - an object with attributes: .type, .location, .auth
    job_id:
        Job id (used for mount dir structure).
    role:
        "source" or "target" (used for mount dir structure).
    share:
        Optional Share instance to reuse.
    log_append:
        Function compatible with core Log.append(msg, level=..., channel=...).
        If provided, used for safe debug messages.
    """

    # Lazy import to keep this module importable without corePY during tests.
    try:
        from core.filesystem.share import Share, ShareAuth  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RemoteMountError(f"Share support not available: {e}")

    def _get(d: Any, k: str, default: Any = None) -> Any:
        if isinstance(d, dict):
            return d.get(k, default)
        return getattr(d, k, default)

    t = str(_get(endpoint, "type", "local") or "local").lower()
    loc = str(_get(endpoint, "location", "") or "").strip()

    if t == "local":
        return MountedEndpoint(local_path=loc)

    if t != "smb":
        raise RemoteMountError(f"Unsupported endpoint type: {t}")

    mount_point = _get_mount_point(job_id, role, unique=False)

    # Build auth dict from endpoint.auth + legacy flat fields
    auth: Dict[str, Any] = {}
    raw_auth = _get(endpoint, "auth", None)
    if isinstance(raw_auth, dict):
        auth.update(raw_auth)

    # Backward compatible: top-level fields (as stored in endpoints table)
    for k in ("port", "guest", "username", "password", "options", "domain", "workgroup", "read_only", "ro"):
        v = _get(endpoint, k, None)
        if v is not None and k not in auth:
            auth[k] = v

    # Parse host/remote
    host, remote = parse_smb_location(loc)

    # Percent-encode remote path (keep '/' separators)
    remote_enc = quote(remote, safe="/")

    # Port
    port = 445
    try:
        if auth.get("port") is not None:
            port = int(auth.get("port") or 445)
    except Exception:
        port = 445

    # Options
    opts: Dict[str, Any] = {}
    if isinstance(auth.get("options"), dict):
        opts.update(auth.get("options") or {})

    # ShareAuth (best effort)
    share_auth = ShareAuth(
        username=auth.get("username"),
        password=auth.get("password"),
        domain=auth.get("domain") or auth.get("workgroup"),
    )

    # Share instance (reuse if provided)
    if share is None:
        if log_append is not None:
            share = Share(logger=ShareLoggerAdapter(log_append))
        else:
            share = Share(logger=None)

    # Emit safe debug line
    if log_append is not None:
        safe_auth = dict(auth)
        for k in ("password", "pass"):
            if k in safe_auth and safe_auth[k]:
                safe_auth[k] = "***"
        log_append(
            f"[Replicator][Mount] protocol={t} host={host} remote={remote_enc} mount_point={mount_point} auth={safe_auth}",
            level="debug",
        )

    # Mount
    try:
        share.mount(
            t,
            host,
            remote_enc,
            mount_point,
            auth=share_auth,
            port=port,
            options=opts,
            read_only=bool(auth.get("read_only") or auth.get("ro") or False),
            elevate=False,
            timeout=int(timeout or 60),
        )
    except Exception as e:
        raise RemoteMountError(f"Failed to mount SMB endpoint '{loc}': {e}")

    return MountedEndpoint(local_path=mount_point, mount_point=mount_point, share=share)
