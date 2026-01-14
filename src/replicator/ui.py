#!/usr/bin/env python3
# src/replicator/ui.py

from __future__ import annotations

from typing import Optional, Any, Dict

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QDialog,
    QFormLayout,
    QLineEdit,
    QCheckBox,
    QComboBox,
    QSpinBox,
    QTextEdit,
    QFrame,
    QSizePolicy,
    QLayout,
)

try:
    from core.ui import MsgBox
except ImportError:
    from ui import MsgBox


# ------------------------------------------------------------------
# Job Dialog
# ------------------------------------------------------------------
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
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(bool(job.get("enabled", True)))
        opts_row.addWidget(self.allow_deletion)
        opts_row.addSpacing(16)
        opts_row.addWidget(self.preserve_metadata)
        opts_row.addWidget(self.enabled)
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

# ------------------------------------------------------------------
# Schedule Dialog
# ------------------------------------------------------------------
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
