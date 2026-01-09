#!/usr/bin/env python3
# src/replicator/replicator.py

from __future__ import annotations

from typing import Optional, Any, Dict, List

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


class JobDialog(QDialog):
    def __init__(self, parent=None, job: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        self.setWindowTitle("Replication Job")
        self.setModal(True)

        job = job or {}
        self._original_job = job

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.name = QLineEdit(job.get("name", ""))
        self.source = QLineEdit(job.get("source", ""))
        self.target = QLineEdit(job.get("target", ""))
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
        self.allow_deletion = QCheckBox()
        self.allow_deletion.setChecked(bool(job.get("allowDeletion", False)))
        self.preserve_metadata = QCheckBox()
        self.preserve_metadata.setChecked(bool(job.get("preserveMetadata", True)))

        form.addRow("Name", self.name)
        form.addRow("Source", self.source)
        form.addRow("Target", self.target)
        form.addRow("Mode", self.mode)
        form.addRow("Direction", self.direction)
        form.addRow("Allow deletion", self.allow_deletion)
        form.addRow("Preserve metadata", self.preserve_metadata)

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
        if not self.name.text().strip():
            MsgBox.show(self, "Job", "Name is required.", icon="warning")
            return
        if not self.source.text().strip():
            MsgBox.show(self, "Job", "Source is required.", icon="warning")
            return
        if not self.target.text().strip():
            MsgBox.show(self, "Job", "Target is required.", icon="warning")
            return
        self.accept()

    def value(self) -> Dict[str, Any]:
        val: Dict[str, Any] = {
            "name": self.name.text().strip(),
            "source": self.source.text().strip(),
            "target": self.target.text().strip(),
            "allowDeletion": bool(self.allow_deletion.isChecked()),
            "preserveMetadata": bool(self.preserve_metadata.isChecked()),
            "mode": self.mode.currentText(),
            "direction": self.direction.currentText(),
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
    """
    Replicator UI + CLI entrypoint.
    Jobs are stored in configuration under: replicator.jobs (list of dicts).
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

        # Ensure defaults exist
        if self._configuration.get("replicator.jobs") is None:
            self._configuration.add("replicator.jobs", [], "json", label="Jobs")
            self._configuration.save()

        self._jobs: List[Dict[str, Any]] = []
        self._table: Optional[QTableWidget] = None

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

        # Table
        self._table = QTableWidget(0, 10, self)
        self._table.setHorizontalHeaderLabels([
            "Name", "Source", "Target", "Mode", "Direction", "Delete", "Metadata", "Schedule", "Last run", "Result"
        ])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(8, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(9, QHeaderView.ResizeToContents)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)

        root.addWidget(self._table)

        # Buttons (icon only using Form.button)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)

        add_btn = Form.button(label="", icon="plus-circle", action=self._add_job)
        add_btn.setToolTip("Add job")
        edit_btn = Form.button(label="", icon="pencil-square", action=self._edit_job)
        edit_btn.setToolTip("Edit job")
        dup_btn = Form.button(label="", icon="files", action=self._duplicate_job)
        dup_btn.setToolTip("Duplicate job")
        del_btn = Form.button(label="", icon="trash", action=self._delete_job)
        del_btn.setToolTip("Delete job")
        schedule_btn = Form.button(label="", icon="calendar-event", action=self._edit_schedule)
        schedule_btn.setToolTip("Schedule")
        config_btn = Form.button(label="", icon="gear-fill", action=self._configuration.show)
        config_btn.setToolTip("Configurator")
        run_btn = Form.button(label="", icon="play-fill", action=lambda: self._run_with_ui_feedback())
        run_btn.setToolTip("Run now")

        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(dup_btn)
        btn_row.addWidget(del_btn)
        btn_row.addWidget(schedule_btn)
        btn_row.addWidget(config_btn)
        btn_row.addWidget(run_btn)

        root.addLayout(btn_row)

        self._reload_jobs()

    def _selected_index(self) -> int:
        if not self._table:
            return -1
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return -1
        return rows[0].row()

    def _reload_jobs(self):
        self._jobs = self._configuration.get("replicator.jobs", []) or []
        self._refresh_table()

    def _save_jobs(self):
        self._configuration.set("replicator.jobs", self._jobs)
        self._configuration.save()

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
            self._table.setItem(r, 1, _it(job.get("source", "")))
            self._table.setItem(r, 2, _it(job.get("target", "")))
            self._table.setItem(r, 3, _it(job.get("mode", "mirror")))
            self._table.setItem(r, 4, _it(job.get("direction", "unidirectional")))
            self._table.setItem(r, 5, _it("Yes" if job.get("allowDeletion") else "No"))
            self._table.setItem(r, 6, _it("Yes" if job.get("preserveMetadata", True) else "No"))
            sched = job.get("schedule")
            if not sched or not sched.get("enabled", False):
                sched_str = "Off"
            else:
                sched_str = f"Every {sched.get('everyMinutes', 60)} min"
            self._table.setItem(r, 7, _it(sched_str))
            # Last run (column 8)
            last_run = job.get("lastRun")
            if last_run:
                last_run_str = last_run
            else:
                last_run_str = "Never"
            self._table.setItem(r, 8, _it(last_run_str))
            # Last result (column 9)
            last_result = job.get("lastResult", "")
            self._table.setItem(r, 9, _it(last_result))

    def _add_job(self):
        dlg = JobDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self._jobs.append(dlg.value())
            self._save_jobs()
            self._refresh_table()

    def _edit_job(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to edit.", icon="info")
            return
        dlg = JobDialog(self, self._jobs[idx])
        if dlg.exec_() == QDialog.Accepted:
            self._jobs[idx] = dlg.value()
            self._save_jobs()
            self._refresh_table()

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
        self._jobs.append(new_job)
        self._save_jobs()
        self._refresh_table()
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
            self._jobs.pop(idx)
            self._save_jobs()
            self._refresh_table()

    def _edit_schedule(self):
        idx = self._selected_index()
        if idx < 0:
            MsgBox.show(self, "Jobs", "Select a job to edit schedule.", icon="info")
            return
        dlg = ScheduleDialog(self, job=self._jobs[idx])
        if dlg.exec_() == QDialog.Accepted:
            self._jobs[idx]["schedule"] = dlg.value()
            self._save_jobs()
            self._refresh_table()

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
        Run all configured jobs.
        Returns True if all jobs succeeded.
        """
        jobs = self._configuration.get("replicator.jobs", []) or []
        if not jobs:
            self._log("[Replicator] No jobs configured.", level="warning")
            return False

        all_ok = True
        for job in jobs:
            ok = self._run_job(job)
            all_ok = all_ok and ok

        return all_ok

    def _run_job(self, job: Dict[str, Any]) -> bool:
        name = job.get("name") or "Unnamed"
        src = job.get("source") or ""
        dst = job.get("target") or ""
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
        self._log(f"[Replicator] Running job '{name}': {src} -> {dst} (mode={mode}, direction={direction}, delete={allow_deletion}, meta={preserve_metadata}{sched_str})")

        ok = self._fs.copy(
            src,
            dst,
            preserve_metadata=preserve_metadata,
            allow_deletion=allow_deletion,
        )

        # Persist lastRun and lastResult, refresh UI, save
        job["lastRun"] = datetime.now(timezone.utc).isoformat()
        job["lastResult"] = "ok" if ok else "fail"
        self._save_jobs()
        if self._table:
            self._refresh_table()

        self._log(f"[Replicator] Job '{name}' result: {'OK' if ok else 'FAIL'} (lastResult={job['lastResult']})", level="info" if ok else "warning")
        return ok

    def _log(self, msg: str, level: str = "info", channel: str = "replicator") -> None:
        if self._logger is not None and hasattr(self._logger, "append"):
            self._logger.append(msg, level=level, channel=channel)  # type: ignore[call-arg]
        else:
            print(msg)
