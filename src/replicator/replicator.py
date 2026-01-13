#!/usr/bin/env python3
# src/replicator/replicator.py

from __future__ import annotations

from typing import Optional, Any, Dict, List

import os
import sqlite3
import json
import shutil
import hashlib

# Add datetime import for lastRun/lastResult
from datetime import datetime, timezone

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QDialog,
    QFormLayout,
    QLineEdit,
    QCheckBox,
    QComboBox,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QFrame,
    QSizePolicy,
    QLayout,
)

try:
    from core.helper import Helper
    from core.configuration import Configuration
    from core.log import Log
    from core.ui import MsgBox, Form
    from core.filesystem.filesystem import FileSystem
except ImportError:
    from helper import Helper
    from configuration import Configuration
    from log import Log
    from ui import MsgBox, Form
    from filesystem.filesystem import FileSystem


# ------------------------------------------------------------------
# ReplicatorDB: SQLite storage/migrations
# ------------------------------------------------------------------
class ReplicatorDB:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self):
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self._conn = conn
        return conn

    def execute(self, sql: str, params: tuple = ()):
        conn = self.connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur

    def query_all(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        conn = self.connect()
        cur = conn.execute(sql, params)
        return cur.fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        conn = self.connect()
        cur = conn.execute(sql, params)
        return cur.fetchone()

    def ensure_created_and_migrated(self):
        # Ensure parent dir exists
        db_dir = os.path.dirname(self._db_path)
        os.makedirs(db_dir, exist_ok=True)
        conn = self.connect()
        # Schema migrations table
        conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created DATETIME DEFAULT CURRENT_TIMESTAMP,
            modified DATETIME DEFAULT CURRENT_TIMESTAMP,
            name TEXT NOT NULL UNIQUE
        );
        """)
        # Try to create modified trigger for schema_migrations (best effort)
        try:
            conn.execute("""
            CREATE TRIGGER trg_schema_migrations_modified
            BEFORE UPDATE ON schema_migrations
            FOR EACH ROW
            BEGIN
                UPDATE schema_migrations SET modified = CURRENT_TIMESTAMP WHERE id = OLD.id;
            END;
            """)
        except Exception:
            pass

        # List of migrations (name, list-of-sql)
        migrations = [
            ("0001_init", [
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
            ]),
            ("0002_file_state_last_seen", [
                "ALTER TABLE file_state ADD COLUMN lastSeenAt TEXT NULL;",
                "ALTER TABLE file_state ADD COLUMN lastSeenRunId INTEGER NULL;",
            ]),
        ]
        # Apply migrations in order
        for name, stmts in migrations:
            row = conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,)).fetchone()
            if not row:
                try:
                    with conn:
                        for stmt in stmts:
                            conn.execute(stmt)
                        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
                except Exception as e:
                    raise RuntimeError(f"Failed to apply migration {name}: {e}")
        # Create modified triggers for all tables (best effort)
        for tbl in ["jobs", "endpoints", "schedule", "runs", "file_state", "conflicts"]:
            try:
                self._ensure_modified_trigger(tbl)
            except Exception:
                pass

    def _ensure_modified_trigger(self, table_name: str):
        # Try to create a BEFORE UPDATE trigger to set modified = CURRENT_TIMESTAMP
        conn = self.connect()
        trig_name = f"trg_{table_name}_modified"
        # Try to drop if exists (to avoid duplicate triggers on repeated runs)
        try:
            conn.execute(f"DROP TRIGGER IF EXISTS {trig_name};")
        except Exception:
            pass
        conn.execute(f"""
        CREATE TRIGGER {trig_name}
        BEFORE UPDATE ON {table_name}
        FOR EACH ROW
        BEGIN
            UPDATE {table_name} SET modified = CURRENT_TIMESTAMP WHERE id = OLD.id;
        END;
        """)



class JobDialog(QDialog):
    def __init__(self, parent=None, job: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("Replication Job")
        self.setModal(True)
        self.setFixedHeight(400)
        self.setMinimumHeight(400)
        self.setMinimumWidth(1200)

        job = job or {}
        self._original_job = job

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        layout.setSizeConstraint(QLayout.SetMinimumSize)

        # ------------------------------
        # Name row (full width)
        # ------------------------------
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self.name = QLineEdit(job.get("name", ""))
        name_row.addWidget(self.name, 1)
        layout.addLayout(name_row)

        # ------------------------------
        # Enabled row
        # ------------------------------
        enabled_row = QHBoxLayout()
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(bool(job.get("enabled", True)))
        enabled_row.addWidget(self.enabled)
        enabled_row.addStretch(1)
        layout.addLayout(enabled_row)

        # Helper to get endpoint dict from job or fallback
        def _get_endpoint(key_endpoint: str, key_str: str, default_type: str = "local") -> Dict[str, Any]:
            ep = job.get(key_endpoint)
            if isinstance(ep, dict):
                return dict(ep)
            return {
                "type": default_type,
                "location": job.get(key_str, ""),
                "auth": {},
            }

        self._source_ep = _get_endpoint("sourceEndpoint", "source", "local")
        self._target_ep = _get_endpoint("targetEndpoint", "target", "local")

        # ------------------------------
        # Two-column area: Source / Destination
        # ------------------------------
        cols = QHBoxLayout()
        cols.setSpacing(20)
        # Keep endpoint panels compact (avoid vertical stretching)
        cols.setAlignment(Qt.AlignTop)

        # Bordered containers for better visual separation
        src_frame = QFrame()
        src_frame.setObjectName("EndpointFrame")
        src_frame.setFrameShape(QFrame.NoFrame)
        src_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        src_frame.setMinimumHeight(0)
        src_frame.setStyleSheet(
            "#EndpointFrame { border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; padding: 10px; }"
        )
        src_col = QVBoxLayout(src_frame)
        src_col.setContentsMargins(10, 10, 10, 10)
        src_col.setSpacing(8)
        src_col.setAlignment(Qt.AlignTop)

        dst_frame = QFrame()
        dst_frame.setObjectName("EndpointFrame")
        dst_frame.setFrameShape(QFrame.NoFrame)
        dst_frame.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        dst_frame.setMinimumHeight(0)
        dst_frame.setStyleSheet(
            "#EndpointFrame { border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; padding: 10px; }"
        )
        dst_col = QVBoxLayout(dst_frame)
        dst_col.setContentsMargins(10, 10, 10, 10)
        dst_col.setSpacing(8)
        dst_col.setAlignment(Qt.AlignTop)

        src_title = QLabel("Source")
        src_title.setStyleSheet("font-weight: 600;")
        dst_title = QLabel("Destination")
        dst_title.setStyleSheet("font-weight: 600;")

        src_col.addWidget(src_title)
        dst_col.addWidget(dst_title)

        (
            self._source_type_combo,
            self._source_location_edit,
            self._source_port_spin,
            self._source_endpoint_row,
            self._source_auth_widget,
            self._source_auth_widgets,
        ) = self._build_endpoint(existing=self._source_ep)

        (
            self._target_type_combo,
            self._target_location_edit,
            self._target_port_spin,
            self._target_endpoint_row,
            self._target_auth_widget,
            self._target_auth_widgets,
        ) = self._build_endpoint(existing=self._target_ep)

        # Endpoint row: Type + Location (+ Port for FTP/SSH)
        src_col.addWidget(self._source_endpoint_row)
        src_col.addWidget(self._source_auth_widget)

        dst_col.addWidget(self._target_endpoint_row)
        dst_col.addWidget(self._target_auth_widget)

        cols.addWidget(src_frame, 1)
        cols.addWidget(dst_frame, 1)
        layout.addLayout(cols)

        # ------------------------------
        # Mode + Direction on same line
        # ------------------------------
        self.mode = QComboBox()
        self.mode.addItems(["mirror"])
        mode_val = job.get("mode", "mirror")
        idx = self.mode.findText(mode_val)
        if idx >= 0:
            self.mode.setCurrentIndex(idx)

        self.direction = QComboBox()
        self.direction.addItems(["unidirectional", "bidirectional"])
        direction_val = job.get("direction", "unidirectional")
        idx = self.direction.findText(direction_val)
        if idx >= 0:
            self.direction.setCurrentIndex(idx)

        # Mode + Direction on same line, each taking half width
        mode_dir_row = QHBoxLayout()

        mode_wrap = QWidget()
        mode_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        mode_lay = QHBoxLayout(mode_wrap)
        mode_lay.setContentsMargins(0, 0, 0, 0)
        mode_lay.setSpacing(8)
        mode_lay.addWidget(QLabel("Mode"))
        mode_lay.addWidget(self.mode, 1)

        dir_wrap = QWidget()
        dir_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        dir_lay = QHBoxLayout(dir_wrap)
        dir_lay.setContentsMargins(0, 0, 0, 0)
        dir_lay.setSpacing(8)
        dir_lay.addWidget(QLabel("Direction"))
        dir_lay.addWidget(self.direction, 1)

        self.mode.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.direction.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        mode_dir_row.addWidget(mode_wrap, 1)
        mode_dir_row.addWidget(dir_wrap, 1)
        layout.addLayout(mode_dir_row)

        # ------------------------------
        # Other options (kept simple)
        # ------------------------------
        opts_row = QHBoxLayout()
        self.allow_deletion = QCheckBox("Allow deletion")
        self.allow_deletion.setChecked(bool(job.get("allowDeletion", False)))
        self.preserve_metadata = QCheckBox("Preserve metadata")
        self.preserve_metadata.setChecked(bool(job.get("preserveMetadata", True)))
        opts_row.addWidget(self.allow_deletion)
        opts_row.addSpacing(16)
        opts_row.addWidget(self.preserve_metadata)
        opts_row.addStretch(1)
        layout.addLayout(opts_row)

        # ------------------------------
        # Buttons
        # ------------------------------
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        ok_btn = QPushButton("Save")
        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        # Wire type changes
        self._source_type_combo.currentIndexChanged.connect(lambda _i: self._on_type_changed(
            self._source_type_combo,
            self._source_location_edit,
            self._source_port_spin,
            self._source_auth_widget,
            self._source_auth_widgets,
        ))
        self._target_type_combo.currentIndexChanged.connect(lambda _i: self._on_type_changed(
            self._target_type_combo,
            self._target_location_edit,
            self._target_port_spin,
            self._target_auth_widget,
            self._target_auth_widgets,
        ))

        # Trigger initial state
        self._on_type_changed(
            self._source_type_combo,
            self._source_location_edit,
            self._source_port_spin,
            self._source_auth_widget,
            self._source_auth_widgets,
            initial=True,
        )
        self._on_type_changed(
            self._target_type_combo,
            self._target_location_edit,
            self._target_port_spin,
            self._target_auth_widget,
            self._target_auth_widgets,
            initial=True,
        )

    # ------------------------------------------------------------------
    # Endpoint UI
    # ------------------------------------------------------------------

    def _build_endpoint(self, existing: Dict[str, Any]):
        """Build endpoint widgets.

        Returns:
            (type_combo, location_edit, port_spin, endpoint_row_widget, auth_widget, widgets_dict)
        """
        # Top row: [Type] [Location] [Port (FTP/SSH)]
        type_combo = QComboBox()
        type_combo.addItems(["local", "smb", "ftp", "ssh"])
        idx = type_combo.findText(existing.get("type", "local"))
        if idx >= 0:
            type_combo.setCurrentIndex(idx)
        type_combo.setFixedWidth(110)
        # Allow _on_type_changed to access original config
        type_combo.setProperty("existing_type", existing.get("type", "local"))
        type_combo.setProperty("existing_auth", existing.get("auth", {}) or {})

        location_edit = QLineEdit(existing.get("location", ""))

        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setFixedWidth(110)
        # Default ports (used when the type is FTP/SSH)
        existing_type = existing.get("type", "local")
        existing_auth = existing.get("auth", {}) or {}
        if existing_type == "ftp":
            port_spin.setValue(int(existing_auth.get("port", 21) or 21))
        elif existing_type == "ssh":
            port_spin.setValue(int(existing_auth.get("port", 22) or 22))
        else:
            # Keep a sane default value even when hidden
            port_spin.setValue(21)

        # Endpoint row widget with label: [Label] [Type] [Location] [Port]
        endpoint_fields = QWidget()
        fields_lay = QHBoxLayout(endpoint_fields)
        fields_lay.setContentsMargins(0, 0, 0, 0)
        fields_lay.setSpacing(8)
        fields_lay.addWidget(type_combo)
        fields_lay.addWidget(location_edit, 1)
        fields_lay.addWidget(port_spin)

        endpoint_row = QWidget()
        endpoint_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        endpoint_form = QFormLayout(endpoint_row)
        endpoint_form.setContentsMargins(0, 0, 0, 0)
        endpoint_form.setSpacing(6)
        endpoint_form.addRow(endpoint_fields)

        # Auth widget (shown only when non-local)
        # Use a dedicated sub-container that shrinks/grows with its visible children.
        auth_widget = QFrame()
        auth_widget.setFrameShape(QFrame.NoFrame)
        auth_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        auth_widget.setMinimumHeight(0)

        auth_lay = QVBoxLayout(auth_widget)
        auth_lay.setContentsMargins(0, 0, 0, 0)
        auth_lay.setSpacing(6)
        auth_lay.setAlignment(Qt.AlignTop)

        auth_title = QLabel("Authentication")
        auth_title.setStyleSheet("font-weight: 600;")

        widgets: Dict[str, Any] = {}

        # SMB auth
        smb_wrap = QWidget()
        smb_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        smb_form = QFormLayout(smb_wrap)
        smb_form.setContentsMargins(0, 0, 0, 0)
        smb_guest = QCheckBox("Login as Guest")
        smb_user_lbl = QLabel("Username")
        smb_username = QLineEdit()
        smb_pass_lbl = QLabel("Password")
        smb_password = QLineEdit()
        smb_password.setEchoMode(QLineEdit.Password)

        smb_auth = existing.get("auth", {}) if existing.get("type") == "smb" else {}
        smb_guest.setChecked(bool(smb_auth.get("guest", True)))  # default guest ON
        smb_username.setText(smb_auth.get("username", ""))
        smb_password.setText(smb_auth.get("password", ""))

        smb_form.addRow(smb_guest)
        smb_form.addRow(smb_user_lbl, smb_username)
        smb_form.addRow(smb_pass_lbl, smb_password)

        def _smb_guest_update():
            guest = smb_guest.isChecked()
            smb_user_lbl.setVisible(not guest)
            smb_username.setVisible(not guest)
            smb_pass_lbl.setVisible(not guest)
            smb_password.setVisible(not guest)

        smb_guest.stateChanged.connect(_smb_guest_update)
        _smb_guest_update()

        # FTP auth
        ftp_wrap = QWidget()
        ftp_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        ftp_form = QFormLayout(ftp_wrap)
        ftp_form.setContentsMargins(0, 0, 0, 0)
        ftp_guest = QCheckBox("Login as Guest")
        ftp_user_lbl = QLabel("Username")
        ftp_username = QLineEdit()
        ftp_pass_lbl = QLabel("Password")
        ftp_password = QLineEdit()
        ftp_password.setEchoMode(QLineEdit.Password)

        ftp_auth = existing.get("auth", {}) if existing.get("type") == "ftp" else {}
        ftp_guest.setChecked(bool(ftp_auth.get("guest", True)))  # default guest ON
        ftp_username.setText(ftp_auth.get("username", ""))
        ftp_password.setText(ftp_auth.get("password", ""))

        ftp_form.addRow(ftp_guest)
        ftp_form.addRow(ftp_user_lbl, ftp_username)
        ftp_form.addRow(ftp_pass_lbl, ftp_password)

        def _ftp_guest_update():
            guest = ftp_guest.isChecked()
            ftp_user_lbl.setVisible(not guest)
            ftp_username.setVisible(not guest)
            ftp_pass_lbl.setVisible(not guest)
            ftp_password.setVisible(not guest)

        ftp_guest.stateChanged.connect(_ftp_guest_update)
        _ftp_guest_update()

        # SSH auth
        ssh_wrap = QWidget()
        ssh_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        ssh_form = QFormLayout(ssh_wrap)
        ssh_form.setContentsMargins(0, 0, 0, 0)
        ssh_use_key = QCheckBox("Use SSH Key")
        ssh_user_lbl = QLabel("Username")
        ssh_username = QLineEdit()
        ssh_pass_lbl = QLabel("Password")
        ssh_password = QLineEdit()
        ssh_password.setEchoMode(QLineEdit.Password)
        ssh_key_lbl = QLabel("SSH Key")
        ssh_key_text = QTextEdit()
        ssh_key_text.setPlaceholderText("Paste SSH private key here...")
        ssh_key_text.setFixedHeight(110)

        ssh_auth = existing.get("auth", {}) if existing.get("type") == "ssh" else {}
        ssh_username.setText(ssh_auth.get("username", ""))
        ssh_password.setText(ssh_auth.get("password", ""))
        ssh_key_text.setPlainText(ssh_auth.get("key", ""))
        # Default: use key if no password provided
        ssh_use_key.setChecked(bool(ssh_auth.get("useKey", True if not ssh_password.text().strip() else False)))

        ssh_form.addRow(ssh_use_key)
        ssh_form.addRow(ssh_user_lbl, ssh_username)
        ssh_form.addRow(ssh_pass_lbl, ssh_password)
        ssh_form.addRow(ssh_key_lbl, ssh_key_text)

        def _ssh_use_key_update():
            use_key = ssh_use_key.isChecked()
            # Username always visible
            ssh_user_lbl.setVisible(True)
            ssh_username.setVisible(True)
            # Toggle password vs key
            ssh_pass_lbl.setVisible(not use_key)
            ssh_password.setVisible(not use_key)
            ssh_key_lbl.setVisible(use_key)
            ssh_key_text.setVisible(use_key)

        ssh_use_key.stateChanged.connect(_ssh_use_key_update)
        _ssh_use_key_update()

        # Stacked display controlled by _on_type_changed
        # We keep all wraps created and toggle visibility.
        auth_lay.addWidget(auth_title)
        auth_lay.addWidget(smb_wrap)
        auth_lay.addWidget(ftp_wrap)
        auth_lay.addWidget(ssh_wrap)

        widgets["smb"] = {
            "wrap": smb_wrap,
            "guest": smb_guest,
            "user_lbl": smb_user_lbl,
            "username": smb_username,
            "pass_lbl": smb_pass_lbl,
            "password": smb_password,
        }
        widgets["ftp"] = {
            "wrap": ftp_wrap,
            "guest": ftp_guest,
            "user_lbl": ftp_user_lbl,
            "username": ftp_username,
            "pass_lbl": ftp_pass_lbl,
            "password": ftp_password,
        }
        widgets["ssh"] = {
            "wrap": ssh_wrap,
            "useKey": ssh_use_key,
            "user_lbl": ssh_user_lbl,
            "username": ssh_username,
            "pass_lbl": ssh_pass_lbl,
            "password": ssh_password,
            "key_lbl": ssh_key_lbl,
            "key": ssh_key_text,
        }

        return type_combo, location_edit, port_spin, endpoint_row, auth_widget, widgets

    def _on_type_changed(
        self,
        type_combo: QComboBox,
        location_edit: QLineEdit,
        port_spin: QSpinBox,
        auth_widget: QWidget,
        widgets: Dict[str, Any],
        initial: bool = False,
    ):
        typ = type_combo.currentText()

        # Placeholders
        placeholder = {
            "local": "Local path (e.g. /data or C:\\Data)",
            "smb": "SMB path (e.g. \\\\SERVER\\Share\\Folder)",
            "ftp": "FTP host/path (e.g. ftp.example.com:/folder)",
            "ssh": "SSH host/path (e.g. example.com:/folder)",
        }.get(typ, "")
        location_edit.setPlaceholderText(placeholder)

        # Port visibility + defaults
        if typ in ("ftp", "ssh"):
            port_spin.setVisible(True)

            existing_type = type_combo.property("existing_type") or "local"
            existing_auth = type_combo.property("existing_auth") or {}

            # If initial load and the saved endpoint type matches, prefer saved port
            if initial and existing_type == typ:
                if typ == "ftp":
                    port_spin.setValue(int(existing_auth.get("port", 21) or 21))
                else:  # ssh
                    port_spin.setValue(int(existing_auth.get("port", 22) or 22))
            else:
                # When switching types, fix common wrong/default values
                cur = int(port_spin.value())
                if typ == "ftp" and cur in (0, 22):
                    port_spin.setValue(21)
                elif typ == "ssh" and cur in (0, 21):
                    port_spin.setValue(22)
        else:
            port_spin.setVisible(False)

        # Auth visibility: collapse container when hidden, resize to content when shown
        if typ == "local":
            auth_widget.setVisible(False)
            auth_widget.setMaximumHeight(0)
        else:
            auth_widget.setVisible(True)
            auth_widget.setMaximumHeight(16777215)

        # Toggle which auth panel is visible
        for key in ("smb", "ftp", "ssh"):
            if key in widgets and "wrap" in widgets[key]:
                widgets[key]["wrap"].setVisible(False)

        if typ in ("smb", "ftp", "ssh"):
            widgets[typ]["wrap"].setVisible(True)

        # Defaults: guest ON for non-local SMB/FTP when not configured
        if typ in ("smb", "ftp"):
            w = widgets[typ]
            if w["guest"].isChecked() is False:
                # Only auto-enable guest if user/pass are empty
                if not w["username"].text().strip() and not w["password"].text().strip():
                    w["guest"].setChecked(True)

        # Force re-layout so the auth container shrinks/grows immediately
        auth_widget.adjustSize()
        if auth_widget.parentWidget() is not None:
            auth_widget.parentWidget().adjustSize()
        self.adjustSize()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _on_ok(self):
        if not self.name.text().strip():
            MsgBox.show(self, "Job", "Name is required.", icon="warning")
            return

        src_type = self._source_type_combo.currentText()
        tgt_type = self._target_type_combo.currentText()

        src_loc = self._source_location_edit.text().strip()
        tgt_loc = self._target_location_edit.text().strip()

        if not src_loc:
            MsgBox.show(self, "Job", "Source location is required.", icon="warning")
            return
        if not tgt_loc:
            MsgBox.show(self, "Job", "Target location is required.", icon="warning")
            return

        # Source auth validation
        if src_type in ("smb", "ftp"):
            w = self._source_auth_widgets[src_type]
            if not w["guest"].isChecked():
                if not w["username"].text().strip() or not w["password"].text().strip():
                    MsgBox.show(self, "Job", "Source username and password are required.", icon="warning")
                    return
        elif src_type == "ssh":
            w = self._source_auth_widgets["ssh"]
            if not w["username"].text().strip():
                MsgBox.show(self, "Job", "Source SSH username is required.", icon="warning")
                return
            use_key = w["useKey"].isChecked()
            if use_key:
                if not w["key"].toPlainText().strip():
                    MsgBox.show(self, "Job", "Source SSH key is required when 'Use SSH Key' is enabled.", icon="warning")
                    return
            else:
                if not w["password"].text().strip():
                    MsgBox.show(self, "Job", "Source SSH password is required when not using a key.", icon="warning")
                    return

        # Target auth validation
        if tgt_type in ("smb", "ftp"):
            w = self._target_auth_widgets[tgt_type]
            if not w["guest"].isChecked():
                if not w["username"].text().strip() or not w["password"].text().strip():
                    MsgBox.show(self, "Job", "Target username and password are required.", icon="warning")
                    return
        elif tgt_type == "ssh":
            w = self._target_auth_widgets["ssh"]
            if not w["username"].text().strip():
                MsgBox.show(self, "Job", "Target SSH username is required.", icon="warning")
                return
            use_key = w["useKey"].isChecked()
            if use_key:
                if not w["key"].toPlainText().strip():
                    MsgBox.show(self, "Job", "Target SSH key is required when 'Use SSH Key' is enabled.", icon="warning")
                    return
            else:
                if not w["password"].text().strip():
                    MsgBox.show(self, "Job", "Target SSH password is required when not using a key.", icon="warning")
                    return

        self.accept()

    # ------------------------------------------------------------------
    # Value extraction
    # ------------------------------------------------------------------

    def value(self) -> Dict[str, Any]:
        def _extract(
            type_combo: QComboBox,
            location_edit: QLineEdit,
            port_spin: QSpinBox,
            widgets: Dict[str, Any],
        ) -> Dict[str, Any]:
            typ = type_combo.currentText()
            location = location_edit.text().strip()
            auth: Dict[str, Any] = {}

            if typ == "local":
                auth = {}
            elif typ == "smb":
                w = widgets["smb"]
                auth = {
                    "guest": bool(w["guest"].isChecked()),
                    "username": w["username"].text().strip(),
                    "password": w["password"].text(),
                }
            elif typ == "ftp":
                w = widgets["ftp"]
                auth = {
                    "guest": bool(w["guest"].isChecked()),
                    "username": w["username"].text().strip(),
                    "password": w["password"].text(),
                    "port": int(port_spin.value()),
                }
            elif typ == "ssh":
                w = widgets["ssh"]
                auth = {
                    "useKey": bool(w["useKey"].isChecked()),
                    "username": w["username"].text().strip(),
                    "password": w["password"].text(),
                    "port": int(port_spin.value()),
                    "key": w["key"].toPlainText(),
                }

            return {
                "type": typ,
                "location": location,
                "auth": auth,
            }

        source_ep = _extract(
            self._source_type_combo,
            self._source_location_edit,
            self._source_port_spin,
            self._source_auth_widgets,
        )
        target_ep = _extract(
            self._target_type_combo,
            self._target_location_edit,
            self._target_port_spin,
            self._target_auth_widgets,
        )

        val: Dict[str, Any] = {
            "name": self.name.text().strip(),
            "sourceEndpoint": source_ep,
            "targetEndpoint": target_ep,
            # Backward compatibility
            "source": source_ep["location"],
            "target": target_ep["location"],
            "allowDeletion": bool(self.allow_deletion.isChecked()),
            "preserveMetadata": bool(self.preserve_metadata.isChecked()),
            "mode": self.mode.currentText(),
            "direction": self.direction.currentText(),
            "enabled": bool(self.enabled.isChecked()),
        }

        # Preserve schedule fields if they existed previously
        if self._original_job and isinstance(self._original_job, dict):
            sched = self._original_job.get("schedule")
            if sched is not None:
                val["schedule"] = sched

        return val


class ScheduleDialog(QDialog):
    def __init__(self, parent=None, job: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("Schedule")
        self.setModal(True)

        job = job or {}
        schedule = job.get("schedule", {})

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        form = QFormLayout()
        layout.addLayout(form)

        self.enabled = QCheckBox()
        self.enabled.setChecked(bool(schedule.get("enabled", False)))
        self.every_minutes = QSpinBox()
        self.every_minutes.setRange(1, 10080)
        self.every_minutes.setValue(int(schedule.get("everyMinutes", 60)))

        form.addRow("Enabled", self.enabled)
        form.addRow("Every minutes", self.every_minutes)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        ok_btn = QPushButton("Save")
        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self._on_ok)

        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

    def _on_ok(self):
        if self.enabled.isChecked() and self.every_minutes.value() < 1:
            MsgBox.show(self, "Schedule", "Every minutes must be at least 1 if enabled.", icon="warning")
            return
        self.accept()

    def value(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled.isChecked()),
            "everyMinutes": int(self.every_minutes.value()),
        }


class Replicator(QMainWindow):

    # ------------------------------------------------------------------
    # Bidirectional sync helpers (local-only for now)
    # ------------------------------------------------------------------

    def _endpoint_local_root(self, ep: Dict[str, Any]) -> str:
        """Return the local root path for an endpoint or raise."""
        if not isinstance(ep, dict):
            raise ValueError("Endpoint is missing")
        typ = (ep.get("type") or "local").lower()
        if typ != "local":
            raise NotImplementedError(f"Bidirectional sync is currently implemented for local endpoints only (got '{typ}').")
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
        rows = self._db.query_all(
            "SELECT relPath, size, mtime, isDir, deleted, deletedAt, lastSeenAt, lastSeenRunId FROM file_state WHERE jobId=? AND side=?",
            (job_id, side),
        )
        prev: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            prev[str(r["relPath"])] = {
                "size": int(r["size"] or 0),
                "mtime": int(r["mtime"] or 0),
                "isDir": bool(r["isDir"]),
                "deleted": bool(r["deleted"]),
                "deletedAt": r["deletedAt"],
                "lastSeenAt": r["lastSeenAt"],
                "lastSeenRunId": r["lastSeenRunId"],
            }
        return prev

    def _persist_file_state(self, job_id: int, side: str, cur: Dict[str, Dict[str, Any]], run_id: Optional[int]) -> None:
        """Upsert current snapshot into file_state and mark missing as deleted, using lastSeenAt/lastSeenRunId."""
        conn = self._db.connect()
        now_iso = datetime.now(timezone.utc).isoformat()
        with conn:
            # Upsert all current entries
            for rel, meta in cur.items():
                is_dir = 1 if meta.get("isDir") else 0
                size = int(meta.get("size", 0) or 0)
                mtime = int(meta.get("mtime", 0) or 0)
                row = conn.execute(
                    "SELECT id FROM file_state WHERE jobId=? AND side=? AND relPath=?",
                    (job_id, side, rel),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE file_state SET size=?, mtime=?, isDir=?, deleted=0, deletedAt=NULL, lastSeenAt=?, lastSeenRunId=? WHERE jobId=? AND side=? AND relPath=?",
                        (size, mtime, is_dir, now_iso, run_id, job_id, side, rel),
                    )
                else:
                    conn.execute(
                        "INSERT INTO file_state (jobId, side, relPath, size, mtime, isDir, deleted, deletedAt, lastSeenAt, lastSeenRunId) VALUES (?,?,?,?,?,?,0,NULL,?,?)",
                        (job_id, side, rel, size, mtime, is_dir, now_iso, run_id),
                    )
            # Mark missing as deleted (single UPDATE)
            rels = list(cur.keys())
            if rels:
                placeholders = ",".join(["?"] * len(rels))
                conn.execute(
                    f"UPDATE file_state SET deleted=1, deletedAt=COALESCE(deletedAt, ?) WHERE jobId=? AND side=? AND deleted=0 AND relPath NOT IN ({placeholders})",
                    (now_iso, job_id, side, *rels),
                )
            else:
                # cur is empty: mark all as deleted for this job/side
                conn.execute(
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

    def _run_job_bidirectional(self, job: Dict[str, Any], run_id: Optional[int]) -> bool:
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
            return False

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

        return True
    """
    Replicator UI + CLI entrypoint.
    Jobs are stored in SQLite database (see ReplicatorDB).
    """
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

        # Legacy configuration jobs bootstrap removed.

        # --- Database path setup ---
        # Use Helper.get_cwd() if present, else os.getcwd()
        if hasattr(self._helper, "get_cwd") and callable(getattr(self._helper, "get_cwd", None)):
            base_dir = self._helper.get_cwd()
        else:
            base_dir = os.getcwd()
        data_dir = os.path.join(base_dir, "data")
        db_path = os.path.join(data_dir, "replicator.db")

        self._db = ReplicatorDB(db_path)
        self._db.ensure_created_and_migrated()
        self._log(f"[Replicator] Database: {db_path}", level="debug")
        self._log(f"[Replicator] Database migrations applied.", level="info")

        self._jobs: List[Dict[str, Any]] = []
        self._table: Optional[QTableWidget] = None

        # After DB is ready, log a snapshot of DB layout/counts (non-verbose)
        self._db_debug_snapshot(verbose=False)

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
        if candidate and self._helper.file_exists(candidate) and candidate.lower().endswith(".png"):
            pm = QPixmap(candidate)
            if not pm.isNull():
                logo.setPixmap(pm.scaled(96, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            logo.setText("Replicator")
            logo.setStyleSheet("font-size: 22px; font-weight: 600;")

        root.addWidget(logo)

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
        self._table = QTableWidget(0, 5, self)
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
        self._jobs = self._db_fetch_jobs()
        self._refresh_table()

    def _save_jobs(self):
        # No longer persists to config; upsert all jobs to DB (used by legacy code)
        for job in self._jobs:
            self._db_upsert_job(job)

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

            self._table.setItem(r, 0, _it(job.get("name", "")))
            self._table.setItem(r, 1, _it("On" if job.get("enabled", True) else "Off"))
            # Source column
            src_str = ""
            src_ep = job.get("sourceEndpoint")
            if isinstance(src_ep, dict):
                src_str = f"{src_ep.get('type', 'local')}:{src_ep.get('location', '')}"
            else:
                src_str = job.get("source", "")
            self._table.setItem(r, 2, _it(src_str))
            # Target column
            tgt_str = ""
            tgt_ep = job.get("targetEndpoint")
            if isinstance(tgt_ep, dict):
                tgt_str = f"{tgt_ep.get('type', 'local')}:{tgt_ep.get('location', '')}"
            else:
                tgt_str = job.get("target", "")
            self._table.setItem(r, 3, _it(tgt_str))
            # Last run (column 5)
            last_run = job.get("lastRun")
            if last_run:
                last_run_str = last_run
            else:
                last_run_str = "Never"
            self._table.setItem(r, 4, _it(last_run_str))
            # Last result (column 6)
            last_result = job.get("lastResult", "")
            self._table.setItem(r, 5, _it(last_result))

    def _add_job(self):
        dlg = JobDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            new_job = dlg.value()
            job_id = self._db_upsert_job(new_job)
            new_job["id"] = job_id
            self._jobs.append(new_job)
            self._reload_jobs()

    def _edit_job(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to edit.", icon="info")
            return
        dlg = JobDialog(self, self._jobs[idx])
        if dlg.exec_() == QDialog.Accepted:
            updated_job = dlg.value()
            # preserve id
            updated_job["id"] = self._jobs[idx].get("id")
            self._db_upsert_job(updated_job)
            self._reload_jobs()

    def _duplicate_job(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to duplicate.", icon="info")
            return
        orig_job = self._jobs[idx]
        new_job = dict(orig_job)
        # Deep copy schedule if present
        if "schedule" in orig_job and isinstance(orig_job["schedule"], dict):
            new_job["schedule"] = dict(orig_job["schedule"])
        name = new_job.get("name")
        if name:
            new_job["name"] = f"{name} (copy)"
        else:
            new_job["name"] = "Copy"
        if "id" in new_job:
            del new_job["id"]
        job_id = self._db_upsert_job(new_job)
        new_job["id"] = job_id
        self._reload_jobs()
        # Select the new row
        if self._table:
            new_row = self._table.rowCount() - 1
            self._table.selectRow(new_row)

    def _delete_job(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to delete.", icon="info")
            return
        choice = MsgBox.show(
            self,
            title="Delete",
            message=f"Delete job '{self._jobs[idx].get('name', '')}'?",
            icon="question",
            buttons=("Cancel", "Delete"),
            default="Cancel",
        )
        if choice == "Delete":
            job_id = self._jobs[idx].get("id")
            if job_id:
                self._db_delete_job(job_id)
            self._reload_jobs()

    def _edit_schedule(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to edit schedule.", icon="info")
            return
        dlg = ScheduleDialog(self, job=self._jobs[idx])
        if dlg.exec_() == QDialog.Accepted:
            job = self._jobs[idx]
            job["schedule"] = dlg.value()
            self._db_upsert_job(job)
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
        jobs = self._jobs
        if not jobs:
            self._log("[Replicator] No jobs configured.", level="warning")
            return False

        all_ok = True
        for job in jobs:
            if not bool(job.get("enabled", True)):
                self._log(f"[Replicator] Skipping disabled job '{job.get('name') or 'Unnamed'}'.", level="info")
                continue
            ok = self._run_job(job)
            all_ok = all_ok and ok

        return all_ok

    def _run_job(self, job: Dict[str, Any]) -> bool:
        name = job.get("name") or "Unnamed"
        if not bool(job.get("enabled", True)):
            self._log(f"[Replicator] Job '{name}' is disabled; skipping.", level="info")
            return True
        job_id = job.get("id")
        # Determine src/dst and types
        src_ep = job.get("sourceEndpoint")
        if isinstance(src_ep, dict):
            src = src_ep.get("location", "")
            src_type = src_ep.get("type", "local")
        else:
            src = job.get("source") or ""
            src_type = "local"
        dst_ep = job.get("targetEndpoint")
        if isinstance(dst_ep, dict):
            dst = dst_ep.get("location", "")
            dst_type = dst_ep.get("type", "local")
        else:
            dst = job.get("target") or ""
            dst_type = "local"
        allow_deletion = bool(job.get("allowDeletion", False))
        preserve_metadata = bool(job.get("preserveMetadata", True))
        mode = job.get("mode", "mirror")
        direction = job.get("direction", "unidirectional")
        schedule = job.get("schedule", {})

        if not src or not dst:
            self._log(f"[Replicator] Job '{name}' invalid: missing source/target.", level="error")
            return False

        sched_str = ""
        if schedule.get("enabled", False):
            sched_str = f", schedule=Every {schedule.get('everyMinutes', 60)} min"
        logline = f"[Replicator] Running job '{name}': {src} -> {dst} (mode={mode}, direction={direction}, delete={allow_deletion}, meta={preserve_metadata}{sched_str}"
        if isinstance(src_ep, dict):
            logline += f", srcType={src_type}"
        if isinstance(dst_ep, dict):
            logline += f", dstType={dst_type}"
        logline += ")"
        self._log(logline)

        # Insert a run record at start
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = None
        try:
            cur = self._db.execute(
                "INSERT INTO runs (jobId, startedAt, result) VALUES (?, ?, ?)",
                (job_id, started_at, "running"),
            )
            run_id = cur.lastrowid
        except Exception as e:
            self._log(f"[Replicator] Failed to insert run record: {e}", level="warning")

        ok = False
        try:
            if str(direction).lower() == "bidirectional":
                ok = self._run_job_bidirectional(job, run_id)
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
        except Exception as e:
            self._log(f"[Replicator] Job '{name}' failed: {e}", level="error")
            ok = False

        # Persist lastRun and lastResult, refresh UI, save to DB
        now_str = datetime.now(timezone.utc).isoformat()
        job["lastRun"] = now_str
        job["lastResult"] = "ok" if ok else "fail"
        job["lastError"] = None if ok else (job.get("lastError") or "Failed")
        self._db_upsert_job(job)
        if run_id:
            try:
                self._db.execute(
                    "UPDATE runs SET endedAt = ?, result = ?, message = ? WHERE id = ?",
                    (now_str, "ok" if ok else "fail", None if ok else (job.get("lastError") or "Failed"), run_id),
                )
            except Exception as e:
                self._log(f"[Replicator] Failed to update run record: {e}", level="warning")
        if self._table:
            self._reload_jobs()

        self._log(f"[Replicator] Job '{name}' result: {'OK' if ok else 'FAIL'} (lastResult={job['lastResult']})", level="info" if ok else "warning")
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
            conn = self._db.connect()

            # List tables
            tables = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()]

            self._log(f"[Replicator][DB] Tables: {', '.join(tables) if tables else '(none)'}", level="debug")

            # Layout + counts
            for t in tables:
                try:
                    cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
                    col_names = [c[1] for c in cols]  # (cid, name, type, notnull, dflt_value, pk)
                    count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    self._log(f"[Replicator][DB] {t}: columns={len(col_names)} rows={count}", level="debug")
                    if verbose:
                        self._log(f"[Replicator][DB] {t}: {', '.join(col_names)}", level="debug")
                except Exception as e:
                    self._log(f"[Replicator][DB] Failed introspecting {t}: {e}", level="warning")

            if not verbose:
                return

            # Recent rows (lightweight, avoid dumping secrets)
            def _safe_row(row: sqlite3.Row) -> Dict[str, Any]:
                d = dict(row)
                # redact sensitive fields if present
                for k in ("password", "sshKey"):
                    if k in d and d[k]:
                        d[k] = "***"
                return d

            for t, order_col in (("jobs", "id"), ("runs", "id"), ("schedule", "id"), ("conflicts", "id")):
                if t in tables:
                    try:
                        rows = conn.execute(f"SELECT * FROM {t} ORDER BY {order_col} DESC LIMIT 3").fetchall()
                        self._log(f"[Replicator][DB] {t}: latest {len(rows)} row(s)", level="debug")
                        for r in rows:
                            self._log(f"[Replicator][DB] {t}: {_safe_row(r)}", level="debug")
                    except Exception as e:
                        self._log(f"[Replicator][DB] Failed reading latest rows from {t}: {e}", level="warning")

        except Exception as e:
            self._log(f"[Replicator][DB] Snapshot failed: {e}", level="warning")


# ------------------------------------------------------------------
# DB-backed job persistence methods
# ------------------------------------------------------------------
    def _db_fetch_jobs(self) -> List[Dict[str, Any]]:
        # Query jobs ordered by id
        rows = self._db.query_all("SELECT * FROM jobs ORDER BY id ASC")
        jobs: List[Dict[str, Any]] = []
        for row in rows:
            job = dict(row)
            job["id"] = row["id"]
            enabled_val = row["enabled"] if "enabled" in row.keys() else 1
            job["enabled"] = bool(enabled_val)
            # Endpoints
            eps = self._db.query_all("SELECT * FROM endpoints WHERE jobId = ?", (row["id"],))
            for ep in eps:
                ep_d = dict(ep)
                auth = {}
                # Compose auth dict
                if ep_d["type"] == "local":
                    auth = {}
                elif ep_d["type"] in ("smb", "ftp"):
                    auth = {
                        "guest": bool(ep_d.get("guest", 1)),
                        "username": ep_d.get("username") or "",
                        "password": ep_d.get("password") or "",
                    }
                    if ep_d["type"] == "ftp":
                        auth["port"] = ep_d.get("port") or 21
                elif ep_d["type"] == "ssh":
                    auth = {
                        "useKey": bool(ep_d.get("useKey", 0)),
                        "username": ep_d.get("username") or "",
                        "password": ep_d.get("password") or "",
                        "port": ep_d.get("port") or 22,
                        "key": ep_d.get("sshKey") or "",
                    }
                # Add any JSON options
                if ep_d.get("options"):
                    try:
                        auth.update(json.loads(ep_d["options"]))
                    except Exception:
                        pass
                ep_obj = {
                    "type": ep_d["type"],
                    "location": ep_d["location"],
                    "auth": auth,
                }
                if ep_d["role"] == "source":
                    job["sourceEndpoint"] = ep_obj
                    job["source"] = ep_d["location"]
                elif ep_d["role"] == "target":
                    job["targetEndpoint"] = ep_obj
                    job["target"] = ep_d["location"]
            # Schedule
            sched_row = self._db.query_one("SELECT * FROM schedule WHERE jobId = ?", (row["id"],))
            if sched_row:
                job["schedule"] = {
                    "enabled": bool(sched_row["enabled"]),
                    "everyMinutes": sched_row["everyMinutes"],
                }
            else:
                job["schedule"] = {"enabled": False, "everyMinutes": 60}
            # lastRun/lastResult/lastError
            job["lastRun"] = row["lastRun"]
            job["lastResult"] = row["lastResult"]
            job["lastError"] = row["lastError"]
            jobs.append(job)
        return jobs

    def _db_upsert_job(self, job: Dict[str, Any]) -> int:

        # Normalize enabled
        enabled = 1 if bool(job.get("enabled", True)) else 0

        # Insert or update jobs row, endpoints, schedule (transaction)
        conn = self._db.connect()
        with conn:
            # Upsert job
            fields = [
                "name", "enabled", "mode", "direction", "allowDeletion", "preserveMetadata",
                "pairId", "conflictPolicy", "lastRun", "lastResult", "lastError"
            ]
            values = [
                job.get("name"),
                enabled,
                job.get("mode", "mirror"),
                job.get("direction", "unidirectional"),
                1 if job.get("allowDeletion", False) else 0,
                1 if job.get("preserveMetadata", True) else 0,
                job.get("pairId"),
                job.get("conflictPolicy", "newest"),
                job.get("lastRun"),
                job.get("lastResult"),
                job.get("lastError"),
            ]
            if job.get("id"):
                # UPDATE
                set_clause = ", ".join(f"{f}=?" for f in fields)
                conn.execute(
                    f"UPDATE jobs SET {set_clause} WHERE id = ?",
                    tuple(values) + (job["id"],)
                )
                job_id = job["id"]
            else:
                # INSERT
                placeholders = ", ".join("?" for _ in fields)
                cur = conn.execute(
                    f"INSERT INTO jobs ({', '.join(fields)}) VALUES ({placeholders})",
                    tuple(values)
                )
                job_id = cur.lastrowid
                job["id"] = job_id

            # Endpoints (upsert by (jobId, role))
            for role in ("source", "target"):
                ep = job.get("sourceEndpoint") if role == "source" else job.get("targetEndpoint")
                if not isinstance(ep, dict):
                    continue
                ep_type = ep.get("type", "local")
                location = ep.get("location", "")
                auth = ep.get("auth", {}) or {}
                # Compose columns
                port = None
                guest = 1
                username = None
                password = None
                useKey = 0
                sshKey = None
                options = {}
                if ep_type == "local":
                    pass
                elif ep_type in ("smb", "ftp"):
                    guest = 1 if auth.get("guest", True) else 0
                    username = auth.get("username")
                    password = auth.get("password")
                    if ep_type == "ftp":
                        port = int(auth.get("port", 21))
                elif ep_type == "ssh":
                    useKey = 1 if auth.get("useKey", False) else 0
                    username = auth.get("username")
                    password = auth.get("password")
                    port = int(auth.get("port", 22))
                    sshKey = auth.get("key")
                # Store any extra keys in options
                known_keys = {"guest", "username", "password", "port", "useKey", "key"}
                for k, v in auth.items():
                    if k not in known_keys:
                        options[k] = v
                # Upsert: try update first, else insert
                ep_row = conn.execute(
                    "SELECT id FROM endpoints WHERE jobId = ? AND role = ?",
                    (job_id, role)
                ).fetchone()
                ep_fields = [
                    "jobId", "role", "type", "location", "port", "guest", "username", "password", "useKey", "sshKey", "options"
                ]
                ep_values = [
                    job_id, role, ep_type, location, port, guest, username, password, useKey, sshKey,
                    json.dumps(options) if options else None
                ]
                if ep_row:
                    set_clause = ", ".join(f"{f}=?" for f in ep_fields[2:])  # skip jobId, role
                    conn.execute(
                        f"UPDATE endpoints SET {set_clause} WHERE jobId=? AND role=?",
                        tuple(ep_values[2:]) + (job_id, role)
                    )
                else:
                    placeholders = ", ".join("?" for _ in ep_fields)
                    conn.execute(
                        f"INSERT INTO endpoints ({', '.join(ep_fields)}) VALUES ({placeholders})",
                        tuple(ep_values)
                    )
            # Schedule
            sched = job.get("schedule")
            if sched:
                sched_row = conn.execute(
                    "SELECT id FROM schedule WHERE jobId = ?", (job_id,)
                ).fetchone()
                enabled = 1 if sched.get("enabled", False) else 0
                every = int(sched.get("everyMinutes", 60))
                if sched_row:
                    conn.execute(
                        "UPDATE schedule SET enabled=?, everyMinutes=? WHERE jobId=?",
                        (enabled, every, job_id)
                    )
                else:
                    conn.execute(
                        "INSERT INTO schedule (jobId, enabled, everyMinutes) VALUES (?, ?, ?)",
                        (job_id, enabled, every)
                    )
        return job.get("id")

    def _db_delete_job(self, job_id: int) -> None:
        self._db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

