#!/usr/bin/env python3
# src/replicator/ui.py

from __future__ import annotations

from typing import Optional, Any, Dict, List, Tuple

from PyQt5.QtCore import Qt, QTime, pyqtSignal
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
    QTimeEdit,
    QFileDialog,
)

try:
    from core.ui import MsgBox
except ImportError:
    from ui import MsgBox

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
class BrowseLineEdit(QLineEdit):
    """QLineEdit that emits a signal when clicked (used to open browse dialogs)."""
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        try:
            self.clicked.emit()
        except Exception:
            pass
        super().mousePressEvent(event)

# ------------------------------------------------------------------
# Job Dialog
# ------------------------------------------------------------------
class JobDialog(QDialog):
    def __init__(self, parent=None, job: Optional[Dict[str, Any]] = None, *, default_interval_seconds: Optional[int] = None):
        super().__init__(parent)
        self.setWindowTitle("Replication Job")
        self.setModal(True)
        self.setFixedHeight(400)
        self.setMinimumHeight(400)
        self.setMinimumWidth(1200)

        job = job or {}
        self._original_job = job

        # Default interval for new schedules (seconds). Provided by the app (e.g. Configuration service.defaultInterval).
        try:
            di = int(default_interval_seconds) if default_interval_seconds is not None else 3600
        except Exception:
            di = 3600
        if di <= 0:
            di = 3600
        self._default_interval_seconds = di

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        layout.setSizeConstraint(QLayout.SetMinimumSize)

        # ------------------------------
        # Name row
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
            return {"type": default_type, "location": job.get(key_str, ""), "auth": {}}

        self._source_ep = _get_endpoint("sourceEndpoint", "source", "local")
        self._target_ep = _get_endpoint("targetEndpoint", "target", "local")

        # ------------------------------
        # Two-column Source / Destination
        # ------------------------------
        cols = QHBoxLayout()
        cols.setSpacing(20)
        cols.setAlignment(Qt.AlignTop)

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
        # Mode is now a first-class control.
        # mirror      => allowDeletion=True
        # incremental => allowDeletion=False
        self.mode.addItems(["mirror", "incremental"])

        # Backward compatibility:
        # - Older jobs may only have allowDeletion without a mode.
        # - Some jobs may still have mode="mirror".
        mode_val = str(job.get("mode") or "").strip().lower()
        if mode_val not in ("mirror", "incremental"):
            # Infer from allowDeletion when mode is missing/legacy.
            mode_val = "mirror" if bool(job.get("allowDeletion", False)) else "incremental"

        idx = self.mode.findText(mode_val)
        if idx >= 0:
            self.mode.setCurrentIndex(idx)

        self.direction = QComboBox()
        self.direction.addItems(["unidirectional", "bidirectional"])
        direction_val = job.get("direction", "unidirectional")
        idx = self.direction.findText(direction_val)
        if idx >= 0:
            self.direction.setCurrentIndex(idx)

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

        mode_dir_row.addWidget(mode_wrap, 1)
        mode_dir_row.addWidget(dir_wrap, 1)
        layout.addLayout(mode_dir_row)

        # ------------------------------
        # Other options
        # ------------------------------
        opts_row = QHBoxLayout()

        # Deletion behavior is controlled by mode now.
        # Keep the UI simple: show metadata + enabled toggles here.
        self.preserve_metadata = QCheckBox("Preserve metadata")
        self.preserve_metadata.setChecked(bool(job.get("preserveMetadata", True)))
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(bool(job.get("enabled", True)))

        opts_row.addWidget(self.preserve_metadata)
        opts_row.addSpacing(16)
        opts_row.addWidget(self.enabled)
        opts_row.addStretch(1)

        # Schedule button (UI stays here)
        self._schedule_btn = QPushButton("Schedule…")
        self._schedule_btn.clicked.connect(self._open_schedule)
        opts_row.addWidget(self._schedule_btn)

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

    def _open_schedule(self) -> None:
        job = self._original_job if isinstance(self._original_job, dict) else {}
        dlg = ScheduleDialog(self, job=job, default_interval_seconds=self._default_interval_seconds)
        if dlg.exec_() == QDialog.Accepted:
            # store schedule back into original job dict so value() preserves it
            if not isinstance(self._original_job, dict):
                self._original_job = {}
            self._original_job["schedule"] = dlg.value()

    # ------------------------------------------------------------------
    # Endpoint UI
    # ------------------------------------------------------------------

    def _build_endpoint(self, existing: Dict[str, Any]):
        type_combo = QComboBox()
        type_combo.addItems(["local", "smb"])
        idx = type_combo.findText(existing.get("type", "local"))
        if idx >= 0:
            type_combo.setCurrentIndex(idx)
        if idx < 0:
            type_combo.setCurrentIndex(0)
        type_combo.setFixedWidth(110)
        type_combo.setProperty("existing_type", existing.get("type", "local"))
        type_combo.setProperty("existing_auth", existing.get("auth", {}) or {})

        location_edit = BrowseLineEdit(existing.get("location", ""))

        port_spin = QSpinBox()
        port_spin.setRange(1, 65535)
        port_spin.setFixedWidth(110)
        # Ports are not used for local/SMB endpoints.
        port_spin.setValue(1)
        port_spin.setVisible(False)

        endpoint_fields = QWidget()
        fields_lay = QHBoxLayout(endpoint_fields)
        fields_lay.setContentsMargins(0, 0, 0, 0)
        fields_lay.setSpacing(8)
        fields_lay.addWidget(type_combo)
        fields_lay.addWidget(location_edit, 1)
        fields_lay.addWidget(port_spin)

        def _browse_location():
            try:
                typ = (type_combo.currentText() or "").lower()
                if typ != "local":
                    return
                start_dir = location_edit.text().strip() or ""
                # If the user typed a file path, prefer its directory.
                if start_dir and not start_dir.endswith("/") and not start_dir.endswith("\\"):
                    try:
                        import os
                        if os.path.isfile(start_dir):
                            start_dir = os.path.dirname(start_dir)
                    except Exception:
                        pass
                selected = QFileDialog.getExistingDirectory(self, "Select folder", start_dir)
                if selected:
                    location_edit.setText(selected)
            except Exception:
                return

        # Click-to-browse for local paths (no extra button needed).
        location_edit.clicked.connect(_browse_location)

        endpoint_row = QWidget()
        endpoint_row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        endpoint_form = QFormLayout(endpoint_row)
        endpoint_form.setContentsMargins(0, 0, 0, 0)
        endpoint_form.setSpacing(6)
        endpoint_form.addRow(endpoint_fields)

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
        smb_user_lbl = QLabel("Username (user or user@domain)")
        smb_username = QLineEdit()
        smb_pass_lbl = QLabel("Password")
        smb_password = QLineEdit()
        smb_password.setEchoMode(QLineEdit.Password)

        smb_auth = existing.get("auth", {}) if existing.get("type") == "smb" else {}
        smb_guest.setChecked(bool(smb_auth.get("guest", True)))
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

        auth_lay.addWidget(auth_title)
        auth_lay.addWidget(smb_wrap)

        widgets["smb"] = {
            "wrap": smb_wrap,
            "guest": smb_guest,
            "user_lbl": smb_user_lbl,
            "username": smb_username,
            "pass_lbl": smb_pass_lbl,
            "password": smb_password,
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

        placeholder = {
            "local": "Local path (e.g. /data or C:\\Data)",
            "smb": "SMB path (e.g. \\\\SERVER\\Share\\Folder)",
        }.get(typ, "")
        location_edit.setPlaceholderText(placeholder)
        # For local endpoints, clicking the location field opens a folder chooser.
        # For non-local endpoints, keep normal typing behavior.
        if (typ or "").lower() == "local":
            location_edit.setCursorPosition(len(location_edit.text()))
        # Ports are not used for local/SMB endpoints.
        port_spin.setVisible(False)

        if typ == "local":
            auth_widget.setVisible(False)
            auth_widget.setMaximumHeight(0)
        else:
            auth_widget.setVisible(True)
            auth_widget.setMaximumHeight(16777215)

        # Hide all auth sections
        for key in ("smb",):
            if key in widgets and "wrap" in widgets[key]:
                widgets[key]["wrap"].setVisible(False)

        # Show only the relevant auth section
        if typ == "smb":
            widgets["smb"]["wrap"].setVisible(True)

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

        if src_type == "smb":
            w = self._source_auth_widgets["smb"]
            if not w["guest"].isChecked():
                # Either Guest is enabled OR both username+password are provided.
                # If either field is missing, force Guest and block save.
                if not w["username"].text().strip() or not w["password"].text().strip():
                    w["guest"].setChecked(True)
                    MsgBox.show(
                        self,
                        "Job",
                        "For the Source SMB endpoint, either enable Guest or provide BOTH a username and a password.",
                        icon="warning",
                    )
                    return

        if tgt_type == "smb":
            w = self._target_auth_widgets["smb"]
            if not w["guest"].isChecked():
                # Either Guest is enabled OR both username+password are provided.
                # If either field is missing, force Guest and block save.
                if not w["username"].text().strip() or not w["password"].text().strip():
                    w["guest"].setChecked(True)
                    MsgBox.show(
                        self,
                        "Job",
                        "For the Destination SMB endpoint, either enable Guest or provide BOTH a username and a password.",
                        icon="warning",
                    )
                    return

        self.accept()

    # ------------------------------------------------------------------
    # Value extraction
    # ------------------------------------------------------------------

    def value(self) -> Dict[str, Any]:
        def _extract(type_combo: QComboBox, location_edit: QLineEdit, port_spin: QSpinBox, widgets: Dict[str, Any]) -> Dict[str, Any]:
            typ = type_combo.currentText()
            location = location_edit.text().strip()
            auth: Dict[str, Any] = {}

            if typ == "local":
                auth = {}
            elif typ == "smb":
                w = widgets["smb"]
                guest = bool(w["guest"].isChecked())
                # If Guest is enabled, credentials are irrelevant; clear them on save.
                auth = {
                    "guest": guest,
                    "username": "" if guest else w["username"].text().strip(),
                    "password": "" if guest else w["password"].text(),
                }
            else:
                # Unsupported/legacy types fall back to local
                typ = "local"
                auth = {}

            return {"type": typ, "location": location, "auth": auth}

        source_ep = _extract(self._source_type_combo, self._source_location_edit, self._source_port_spin, self._source_auth_widgets)
        target_ep = _extract(self._target_type_combo, self._target_location_edit, self._target_port_spin, self._target_auth_widgets)

        val: Dict[str, Any] = {
            "name": self.name.text().strip(),
            "sourceEndpoint": source_ep,
            "targetEndpoint": target_ep,
            "source": source_ep["location"],  # backward compatibility
            "target": target_ep["location"],  # backward compatibility
            # Backward compatible field: allowDeletion derives from mode.
            "allowDeletion": bool((self.mode.currentText() or "").lower() == "mirror"),
            "preserveMetadata": bool(self.preserve_metadata.isChecked()),
            "mode": self.mode.currentText(),
            "direction": self.direction.currentText(),
            "enabled": bool(self.enabled.isChecked()),
        }

        # Preserve schedule (and update if ScheduleDialog was used)
        if self._original_job and isinstance(self._original_job, dict):
            sched = self._original_job.get("schedule")
            if sched is not None:
                val["schedule"] = sched

        return val
    # Remove any remaining references to self.allow_deletion in this class.

# ------------------------------------------------------------------
# Schedule Dialog
# ------------------------------------------------------------------
class ScheduleDialog(QDialog):
    """Schedule editor UI.

    Persists as:
      schedule = {
        "enabled": bool,
        "intervalSeconds": int,   # computed fallback (minimum per-day interval if enabled, else default)
        "windows": {
          "0":[{"start":"22:00","end":"06:00","intervalSeconds":3600}],
          ...
        }
      }
    """

    _DAYS: List[Tuple[int, str]] = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    def __init__(self, parent=None, job: Optional[Dict[str, Any]] = None, *, default_interval_seconds: Optional[int] = None):
        super().__init__(parent)
        self.setWindowTitle("Schedule")
        self.setModal(True)

        # Default interval for new schedules (seconds). Provided by the app (e.g. Configuration service.defaultInterval).
        try:
            di = int(default_interval_seconds) if default_interval_seconds is not None else 3600
        except Exception:
            di = 3600
        if di <= 0:
            di = 3600
        self._default_interval_seconds = di

        job = job or {}
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}

        # IMPORTANT semantics:
        # - schedule.enabled == False => job can be run manually but will NOT be executed by the service.
        # - schedule.enabled == True  => job may run in the service, but ONLY within selected day windows.
        # - enabled with no days selected means "run never" (no service runs).

        if not schedule:
            # New schedule default: enabled, every day, all day, default interval
            schedule = {"enabled": True, "intervalSeconds": int(self._default_interval_seconds), "windows": {}}
            for wd in range(7):
                schedule["windows"][str(wd)] = [
                    {"start": "00:00", "end": "23:59", "intervalSeconds": int(self._default_interval_seconds)}
                ]
        else:
            # Existing schedule: preserve intent.
            # If windows is missing/empty, DO NOT auto-populate days (that would change meaning).
            if not isinstance(schedule.get("windows"), dict):
                schedule["windows"] = {}

            # Ensure each existing day window has intervalSeconds
            try:
                for _k, _v in list(schedule["windows"].items()):
                    if isinstance(_v, list) and _v and isinstance(_v[0], dict) and "intervalSeconds" not in _v[0]:
                        _v[0]["intervalSeconds"] = int(schedule.get("intervalSeconds") or self._default_interval_seconds)
            except Exception:
                pass

        self._windows: Dict[str, Any] = schedule.get("windows", {}) if isinstance(schedule.get("windows", {}), dict) else {}

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        layout.setSizeConstraint(QLayout.SetMinimumSize)

        form = QFormLayout()
        layout.addLayout(form)

        self.enabled = QCheckBox()
        self.enabled.setChecked(bool(schedule.get("enabled", False)))

        form.addRow("Enabled", self.enabled)

        hint = QLabel("Tip: Enable the schedule to allow service runs. If enabled and no days are selected, the job will not run in the service.")
        hint.setWordWrap(True)
        hint.setStyleSheet("opacity: 0.75;")
        layout.addWidget(hint)

        # Windows editor
        win_frame = QFrame()
        win_frame.setFrameShape(QFrame.NoFrame)
        win_lay = QVBoxLayout(win_frame)
        win_lay.setContentsMargins(0, 0, 0, 0)
        win_lay.setSpacing(6)

        title = QLabel("Allowed run windows")
        title.setStyleSheet("font-weight: 600;")
        win_lay.addWidget(title)

        self._day_controls: Dict[int, Dict[str, Any]] = {}

        for wd, label in self._DAYS:
            row = QHBoxLayout()
            row.setSpacing(10)

            day_enabled = QCheckBox(label)
            start = QTimeEdit()
            end = QTimeEdit()
            start.setDisplayFormat("HH:mm")
            end.setDisplayFormat("HH:mm")
            start.setTime(QTime(0, 0))
            end.setTime(QTime(23, 59))

            interval = QSpinBox()
            interval.setRange(1, 604800)  # 1s .. 7 days
            interval.setFixedWidth(130)
            interval.setValue(int(schedule.get("intervalSeconds", self._default_interval_seconds) or self._default_interval_seconds))

            # Load from existing windows if present
            key = str(wd)
            day_windows = self._windows.get(key)
            if isinstance(day_windows, list) and len(day_windows) > 0 and isinstance(day_windows[0], dict):
                w0 = day_windows[0]
                s = str(w0.get("start", "00:00"))
                e = str(w0.get("end", "23:59"))
                day_enabled.setChecked(True)
                start.setTime(self._parse_qtime(s, QTime(0, 0)))
                end.setTime(self._parse_qtime(e, QTime(23, 59)))
                try:
                    interval.setValue(int(w0.get("intervalSeconds", schedule.get("intervalSeconds", self._default_interval_seconds)) or self._default_interval_seconds))
                except Exception:
                    interval.setValue(int(schedule.get("intervalSeconds", self._default_interval_seconds) or self._default_interval_seconds))
            else:
                day_enabled.setChecked(False)
                try:
                    interval.setValue(int(schedule.get("intervalSeconds", self._default_interval_seconds) or self._default_interval_seconds))
                except Exception:
                    interval.setValue(int(self._default_interval_seconds))

            # Disable edits when not enabled
            def _apply_enabled_state(_=None, *, _day_enabled=day_enabled, _start=start, _end=end, _interval=interval):
                en = _day_enabled.isChecked()
                _start.setEnabled(en)
                _end.setEnabled(en)
                _interval.setEnabled(en)

            day_enabled.stateChanged.connect(_apply_enabled_state)
            _apply_enabled_state()

            row.addWidget(day_enabled, 1)
            row.addWidget(QLabel("Start"))
            row.addWidget(start)
            row.addWidget(QLabel("End"))
            row.addWidget(end)
            row.addWidget(QLabel("Interval (s)"))
            row.addWidget(interval)

            win_lay.addLayout(row)

            self._day_controls[wd] = {"enabled": day_enabled, "start": start, "end": end, "interval": interval}

        layout.addWidget(win_frame)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        ok_btn = QPushButton("Save")
        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        self._sync_enabled_state()

        self.enabled.stateChanged.connect(lambda _i: self._sync_enabled_state())

    def _sync_enabled_state(self):
        en = self.enabled.isChecked()
        for wd, ctrls in self._day_controls.items():
            ctrls["enabled"].setEnabled(en)
            # if schedule disabled, visually disable time edits too
            if not en:
                ctrls["start"].setEnabled(False)
                ctrls["end"].setEnabled(False)
                ctrls["interval"].setEnabled(False)
            else:
                # restore per-day checkbox logic
                day_en = ctrls["enabled"].isChecked()
                ctrls["start"].setEnabled(day_en)
                ctrls["end"].setEnabled(day_en)
                ctrls["interval"].setEnabled(day_en)

    def _parse_qtime(self, s: str, default: QTime) -> QTime:
        try:
            parts = s.strip().split(":")
            if len(parts) != 2:
                return default
            hh = int(parts[0])
            mm = int(parts[1])
            if hh < 0 or hh > 23 or mm < 0 or mm > 59:
                return default
            return QTime(hh, mm)
        except Exception:
            return default

    def _on_ok(self):
        if self.enabled.isChecked():
            any_day = any(bool(ctrls["enabled"].isChecked()) for _wd, ctrls in self._day_controls.items())
            if not any_day:
                MsgBox.show(self, "Schedule", "Select at least one day when the schedule is enabled (otherwise the job will never run in the service).", icon="warning")
                return
            for _wd, ctrls in self._day_controls.items():
                if not ctrls["enabled"].isChecked():
                    continue
                if int(ctrls["interval"].value() or 0) < 1:
                    MsgBox.show(self, "Schedule", "Per-day interval must be at least 1 second for enabled days.", icon="warning")
                    return

        # Basic validation: ensure enabled days have valid times (QTimeEdit ensures format)
        # Allow overnight windows naturally (start > end) — that's intended.
        self.accept()

    def value(self) -> Dict[str, Any]:
        windows: Dict[str, Any] = {}
        enabled_intervals = []
        if self.enabled.isChecked():
            for wd, ctrls in self._day_controls.items():
                if not ctrls["enabled"].isChecked():
                    continue
                s = ctrls["start"].time().toString("HH:mm")
                e = ctrls["end"].time().toString("HH:mm")
                itv = int(ctrls["interval"].value())
                windows[str(wd)] = [{"start": s, "end": e, "intervalSeconds": itv}]
                enabled_intervals.append(itv)

        # Compute fallback intervalSeconds: min per-day interval if enabled, else default
        if self.enabled.isChecked() and enabled_intervals:
            computed_interval_seconds = min(enabled_intervals)
        else:
            computed_interval_seconds = int(self._default_interval_seconds)

        return {
            "enabled": bool(self.enabled.isChecked()),
            "intervalSeconds": int(computed_interval_seconds),
            "windows": windows,
        }
