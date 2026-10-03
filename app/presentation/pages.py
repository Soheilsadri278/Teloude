"""Main navigation pages: Home, Storages, Backups, Restore, Search, Settings."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..application.notifications import human_size
from ..application.search import TYPE_GROUPS
from ..application.settings import AppSettings
from ..domain.models import MIB, SPEED_PRESETS, RunOutcome
from .controller import AppController
from .dialogs import confirm_cloud_delete
from .theme import SP


def header(title: str, subtitle: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    t = QLabel(title)
    t.setObjectName("Title")
    lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("Subtitle")
        s.setWordWrap(True)
        lay.addWidget(s)
    return w


def card() -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("Card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(2 * SP, 2 * SP, 2 * SP, 2 * SP)
    lay.setSpacing(SP)
    return f, lay


def empty_state(title: str, body: str) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    a = QLabel(title)
    a.setObjectName("StatValue")
    a.setAlignment(Qt.AlignmentFlag.AlignCenter)
    b = QLabel(body)
    b.setObjectName("Subtitle")
    b.setAlignment(Qt.AlignmentFlag.AlignCenter)
    b.setWordWrap(True)
    lay.addWidget(a)
    lay.addWidget(b)
    return w


class Page(QWidget):
    def __init__(self, ctl: AppController):
        super().__init__()
        self.setObjectName("Page")
        self.ctl = ctl
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(4 * SP, 4 * SP, 4 * SP, 3 * SP)
        self.root.setSpacing(3 * SP)

    def refresh(self) -> None:  # overridden
        pass


def storage_combo(ctl: AppController, include_all: bool = False) -> QComboBox:
    cb = QComboBox()
    cb.setMinimumWidth(220)
    fill_storages(cb, ctl, include_all)
    return cb


def fill_storages(cb: QComboBox, ctl: AppController, include_all: bool = False) -> None:
    cur = cb.currentData()
    cb.clear()
    if include_all:
        cb.addItem("All storages", None)
    for r in ctl.repos.storages.list():
        cb.addItem(r["title"].removeprefix("Teloude - "), int(r["id"]))
    i = cb.findData(cur)
    if i >= 0:
        cb.setCurrentIndex(i)


# =============================================================================== Home
class HomePage(Page):
    def __init__(self, ctl: AppController):
        super().__init__(ctl)
        self.greet = header("Welcome", "Back up your folders to your own private Telegram storage.")
        self.root.addWidget(self.greet)
        row = QHBoxLayout()
        row.setSpacing(2 * SP)
        self.stat_files, self.stat_size, self.stat_storages, self.stat_pending = (QLabel("0") for _ in range(4))
        for lab, w in (("Files backed up", self.stat_files), ("Cloud size", self.stat_size), ("Storages", self.stat_storages), ("Waiting / failed", self.stat_pending)):
            c, lay = card()
            w.setObjectName("StatValue")
            cap = QLabel(lab)
            cap.setObjectName("Hint")
            lay.addWidget(w)
            lay.addWidget(cap)
            row.addWidget(c)
        self.root.addLayout(row)
        self.backup_btn = QPushButton("Back up a folder…")
        self.backup_btn.setObjectName("Primary")
        self.backup_btn.setMaximumWidth(260)
        self.backup_btn.clicked.connect(self.choose_backup)
        self.root.addWidget(self.backup_btn)
        self.root.addWidget(QLabel("Recent activity"))
        self.recent = QListWidget()
        self.root.addWidget(self.recent, 1)
        ctl.storages_changed.connect(self.refresh)
        ctl.op_finished.connect(lambda _s: self.refresh())
        ctl.account_changed.connect(lambda n: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        st = self.ctl.repos.files.stats()
        self.stat_files.setText(f"{st['files']:,}")
        self.stat_size.setText(human_size(st["bytes"]))
        self.stat_storages.setText(str(len(self.ctl.repos.storages.list())))
        self.stat_pending.setText(str(st["pending"]))
        name = self.ctl.account_name
        self.greet.findChild(QLabel).setText(f"Welcome{', ' + name if name else ''}")
        self.recent.clear()
        for r in self.ctl.repos.runs.recent(12):
            if r["outcome"] == RunOutcome.RUNNING:
                continue
            txt = f"{r['kind'].capitalize()} — {r['outcome'].replace('_', ' ')} · {r['succeeded']} file(s), {human_size(r['bytes_done'])}"
            if r["failed"]:
                txt += f" · {r['failed']} failed"
            self.recent.addItem(txt)
        if self.recent.count() == 0:
            self.recent.addItem("Nothing yet. Choose a folder to back up.")

    def choose_backup(self) -> None:
        start_backup_flow(self, self.ctl)


def start_backup_flow(parent: QWidget, ctl: AppController, storage_id: int | None = None) -> None:
    folder = QFileDialog.getExistingDirectory(parent, "Choose a folder to back up")
    if not folder:
        return
    default_name = Path(folder).name or "Backup"
    if storage_id is None:
        storages = ctl.repos.storages.list()
        names = [r["title"].removeprefix("Teloude - ") for r in storages] + ["＋ New storage…"]
        pick, ok = QInputDialog.getItem(parent, "Choose storage", "Back up into:", names, len(names) - 1 if not storages else 0, False)
        if not ok:
            return
        if pick == names[-1]:
            name, ok = QInputDialog.getText(parent, "New storage", "Storage name:", text=default_name)
            if not ok or not name.strip():
                return
            ctl.create_storage(name.strip(), lambda sid: ctl.start_backup(sid, folder))
            return
        storage_id = int(storages[names.index(pick)]["id"])
    ctl.start_backup(storage_id, folder)


# =============================================================================== Storages
class StoragesPage(Page):
    def __init__(self, ctl: AppController):
        super().__init__(ctl)
        self.root.addWidget(header("Storages", "Each storage is a private Telegram group with Forum Topics. Folders map to topics."))
        bar = QHBoxLayout()
        bar.setSpacing(SP)
        self.new_btn, self.discover_btn, self.backup_btn, self.forget_btn = (QPushButton(t) for t in ("New storage", "Find on Telegram", "Back up folder…", "Forget on this PC"))
        self.new_btn.setObjectName("Primary")
        for b in (self.new_btn, self.discover_btn, self.backup_btn, self.forget_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        self.root.addLayout(bar)
        self.list = QListWidget()
        self.root.addWidget(self.list, 1)
        self.empty = empty_state("No storages yet", "Create one, or press “Find on Telegram” to discover storages made on another PC.")
        self.root.addWidget(self.empty)
        self.new_btn.clicked.connect(self._new)
        self.discover_btn.clicked.connect(ctl.discover)
        self.backup_btn.clicked.connect(self._backup)
        self.forget_btn.clicked.connect(self._forget)
        ctl.storages_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for r in self.ctl.repos.storages.list():
            n, size = self.ctl.repos.storages.usage(r["id"])
            it = QListWidgetItem(f"{r['title']}\n{n:,} files · {human_size(size)}" + (f" · last folder: {r['local_root']}" if r["local_root"] else ""))
            it.setData(Qt.ItemDataRole.UserRole, int(r["id"]))
            it.setSizeHint(QSize(0, 60))
            self.list.addItem(it)
        self.empty.setVisible(self.list.count() == 0)
        self.list.setVisible(self.list.count() > 0)

    def _selected(self) -> int | None:
        it = self.list.currentItem()
        return int(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def _new(self) -> None:
        name, ok = QInputDialog.getText(self, "New storage", "Storage name (a private Telegram group will be created):")
        if ok and name.strip():
            self.ctl.create_storage(name.strip())

    def _backup(self) -> None:
        sid = self._selected()
        if sid is None:
            self.ctl.message.emit("Select a storage first.")
            return
        start_backup_flow(self, self.ctl, sid)

    def _forget(self) -> None:
        sid = self._selected()
        if sid is None:
            return
        if QMessageBox.question(self, "Forget storage", "Remove this storage from this PC only? The Telegram group and its files stay untouched; you can re-discover it any time.") == QMessageBox.StandardButton.Yes:
            self.ctl.forget_storage(sid)


# =============================================================================== Backups
STATE_LABEL = {
    "queued": "Queued", "hashing": "Preparing", "uploading": "Uploading", "downloading": "Downloading", "paused": "Paused",
    "reconnecting": "Reconnecting", "retrying": "Retrying", "completed": "Completed", "failed": "Failed",
    "recoverable_failed": "Failed — can resume", "permanent_failed": "Failed — needs attention",
    "cancelled": "Cancelled", "skipped": "Skipped (duplicate)", "interrupted": "Interrupted",
}  # fmt: skip


def fmt_eta(eta: float) -> str:
    if eta < 0:
        return ""
    s = int(eta)
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


# States that report a change of situation, not new bytes: they must never lower a progress bar.
TRANSIENT_STATES = frozenset({"hashing", "queued", "paused", "reconnecting", "retrying"})


class BackupsPage(Page):
    def __init__(self, ctl: AppController):
        super().__init__(ctl)
        self.root.addWidget(header("Transfers", "Live progress of backups and restores."))
        top, tl = card()
        self.op_label = QLabel("No operation running")
        self.op_state = QLabel("")
        self.op_state.setObjectName("Hint")
        self.op_state.setWordWrap(True)
        self.overall = QProgressBar()
        self.overall.setRange(0, 1000)
        self.op_bytes = QLabel("")
        self.op_bytes.setObjectName("Hint")
        bar = QHBoxLayout()
        bar.setSpacing(SP)
        self.pause_btn, self.resume_btn, self.cancel_btn, self.details_btn = (
            QPushButton("Pause"), QPushButton("Resume"), QPushButton("Cancel"), QPushButton("Details"),
        )
        self.cancel_btn.setObjectName("Danger")
        for b in (self.pause_btn, self.resume_btn, self.cancel_btn, self.details_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        tl.addWidget(self.op_label)
        tl.addWidget(self.op_state)
        tl.addWidget(self.overall)
        tl.addWidget(self.op_bytes)
        tl.addLayout(bar)
        self.root.addWidget(top)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File", "Status", "Progress", "Speed / ETA"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 340)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 200)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.root.addWidget(self.table, 1)
        self.rows: dict[int, int] = {}
        self._resumable = False
        self._last_summary = None
        self.pause_btn.clicked.connect(ctl.pause)
        self.resume_btn.clicked.connect(self._resume)
        self.cancel_btn.clicked.connect(self._cancel)
        self.details_btn.clicked.connect(self._details)
        ctl.op_started.connect(self._started)
        ctl.op_progress.connect(self._op_progress)
        ctl.op_phase.connect(lambda p: self.op_state.setText({"scanning": "Scanning folder…", "comparing": "Comparing with backup…", "uploading": "Transferring", "restoring": "Restoring", "done": ""}.get(p, p)))
        ctl.file_event.connect(self._file_event)
        ctl.op_finished.connect(self._finished)
        ctl.resumable_changed.connect(self._resumable_changed)
        ctl.paused_changed.connect(self._paused)
        self._set_buttons(False)

    def _set_buttons(self, active: bool, paused: bool = False) -> None:
        self.pause_btn.setEnabled(active and not paused)
        # "Resume" is both the un-pause of a running operation and the resume of a failed one.
        self.resume_btn.setEnabled((active and paused) or (not active and self._resumable))
        self.cancel_btn.setEnabled(active)
        self.details_btn.setEnabled(bool(self._last_summary and self._last_summary.errors))

    def _resumable_changed(self, summary) -> None:  # type: ignore[no-untyped-def]
        self._resumable = bool(getattr(summary, "resumable", False))
        self._set_buttons(self.ctl.busy)
        self._describe_resumable(summary)

    def _describe_resumable(self, summary) -> None:  # type: ignore[no-untyped-def]
        """'Backup failed - Reason: Connection lost - 542 MB / 1.00 GB' (only real checkpoint bytes)."""
        if not self._resumable or getattr(summary, "count", 0) == 0:
            return
        s = self._last_summary
        reason = (getattr(s, "reason", "") or getattr(summary, "reason", "") or "").strip()
        head = "Backup failed" if (s is None or s.kind == "backup") else "Restore failed"
        text = head
        if reason:
            text += f" — Reason: {reason.rstrip('.')}"
        text += f" — {human_size(summary.done_bytes)} / {human_size(summary.total_bytes)}"
        if getattr(summary, "blocked", 0):
            text += f" ({summary.blocked} file(s) need your attention)"
        self.op_state.setText(text)

    def _started(self, kind: str, label: str) -> None:
        self.table.setRowCount(0)
        self.rows.clear()
        self.overall.setValue(0)
        self.op_bytes.setText("")
        self._resumable = False
        self.op_label.setText(f"{'Backing up' if kind == 'backup' else 'Restoring to'} {label}")
        self._set_buttons(True)

    def _paused(self, paused: bool) -> None:
        self._set_buttons(self.ctl.busy, paused)
        if self.ctl.busy:
            self.op_state.setText("Paused — in-flight parts finish, no new ones start" if paused else "Transferring")

    def _resume(self) -> None:
        if self.ctl.busy:
            self.ctl.resume()  # un-pause the running operation
            return
        if not self._resumable:
            return
        self.resume_btn.setEnabled(False)  # a second click must not start a second upload
        if not self.ctl.resume_last():
            self._set_buttons(False)

    def _details(self) -> None:
        s = self._last_summary
        if not s:
            return
        lines = [f"{s.kind.capitalize()} run #{s.run_id} — {s.outcome}", ""]
        if getattr(s, "reason", ""):
            lines.append(f"Reason: {s.reason}")
        lines.append(f"{s.succeeded} succeeded, {s.failed} failed, {s.skipped} skipped, {s.cancelled} cancelled")
        if getattr(s, "recoverable", 0):
            lines.append(f"{s.recoverable} can be resumed from the last checkpoint")
        if getattr(s, "permanent", 0):
            lines.append(f"{s.permanent} need your attention and will not be retried automatically")
        if getattr(s, "unattempted", 0):
            lines.append(f"{s.unattempted} were not started")
        if s.errors:
            lines += ["", "Details:"] + [f"• {e}" for e in s.errors[:40]]
        QMessageBox.information(self, "Backup details", "\n".join(lines))

    def _op_progress(self, done: float, total: float, speed: float, eta: float) -> None:
        """Live, real progress: driven only by bytes Telegram acknowledged."""
        value = int(done * 1000 / total) if total else 0
        if value != self.overall.value():
            self.overall.setValue(value)
        bits = [f"{human_size(done)} / {human_size(total)}"]
        if speed > 0:
            bits.append(f"{human_size(speed)}/s")
        if eta >= 0:
            bits.append(fmt_eta(eta))
        self.op_bytes.setText(" · ".join(bits))

    def _cancel(self) -> None:
        if QMessageBox.question(self, "Cancel operation", "Cancel the current operation? Uploaded parts are kept so you can resume later.") == QMessageBox.StandardButton.Yes:
            self.ctl.cancel()

    def _file_event(self, fid: int, rel: str, state: str, done: float, total: float, speed: float, eta: float) -> None:
        row = self.rows.get(fid)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.rows[fid] = row
            self.table.setItem(row, 0, QTableWidgetItem(rel))
            self.table.setItem(row, 1, QTableWidgetItem(""))
            pb = QProgressBar()
            pb.setRange(0, 1000)
            self.table.setCellWidget(row, 2, pb)
            self.table.setItem(row, 3, QTableWidgetItem(""))
            self.table.setRowHeight(row, 40)
        if rel:
            self.table.item(row, 0).setText(rel)
        self.table.item(row, 1).setText(STATE_LABEL.get(state, state))
        pb: QProgressBar = self.table.cellWidget(row, 2)  # type: ignore[assignment]
        if total:
            value = int(done * 1000 / total)
        elif state == "completed":
            value = pb.maximum()
        else:
            value = pb.value()  # no new numbers in this event: keep what was acknowledged
        if state in TRANSIENT_STATES:
            # A transient state is not a progress report, so it may never rewind the bar - bytes
            # Telegram acknowledged stay acknowledged (Windows paints these events more often).
            value = max(value, pb.value())
        pb.setValue(value)
        txt = f"{human_size(done)} / {human_size(total)}"
        if state in ("uploading", "downloading") and speed > 0:
            txt += f" · {human_size(speed)}/s · {fmt_eta(eta)}"
        self.table.item(row, 3).setText(txt)
        self.table.scrollToItem(self.table.item(row, 0))
        if state in ("uploading", "downloading", "reconnecting", "retrying"):
            self.op_state.setText(STATE_LABEL[state] if state in ("reconnecting", "retrying") else "Transferring")

    def _finished(self, s) -> None:  # type: ignore[no-untyped-def]
        self._last_summary = s
        self._set_buttons(False)
        labels = {
            RunOutcome.COMPLETED: "Completed", RunOutcome.PARTIAL: "Partially completed", RunOutcome.FAILED: "Failed",
            RunOutcome.CANCELLED: "Cancelled", RunOutcome.NOTHING_TO_DO: "Everything is already backed up",
        }  # fmt: skip
        extra = f" — {s.succeeded} done, {s.failed} failed, {s.skipped} skipped" if s.outcome != RunOutcome.NOTHING_TO_DO else ""
        self.op_state.setText(labels.get(s.outcome, str(s.outcome)) + extra)
        if s.outcome in (RunOutcome.COMPLETED, RunOutcome.NOTHING_TO_DO):
            self.overall.setValue(1000)
        # ``resumable_changed`` arrives right after this and replaces the headline with
        # "Backup failed - Reason: ... - X / Y" when there is real work left to continue.


# =============================================================================== Restore
class RestorePage(Page):
    def __init__(self, ctl: AppController):
        super().__init__(ctl)
        self.root.addWidget(header("Restore", "Choose files or folders to download. Existing files are never overwritten without your decision."))
        bar = QHBoxLayout()
        bar.setSpacing(SP)
        self.storage = storage_combo(ctl)
        self.restore_btn = QPushButton("Restore selected…")
        self.restore_btn.setObjectName("Primary")
        self.delete_btn = QPushButton("Delete from Telegram…")
        self.delete_btn.setObjectName("Danger")
        bar.addWidget(self.storage)
        bar.addStretch(1)
        bar.addWidget(self.delete_btn)
        bar.addWidget(self.restore_btn)
        self.root.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Size"])
        self.tree.setColumnWidth(0, 480)
        self.root.addWidget(self.tree, 1)
        self.empty = empty_state("Nothing to restore", "Back up a folder first, or sync a storage from Telegram.")
        self.root.addWidget(self.empty)
        self.storage.currentIndexChanged.connect(self.refresh)
        self.restore_btn.clicked.connect(self._restore)
        self.delete_btn.clicked.connect(self._delete)
        self.tree.itemChanged.connect(self._propagate)
        ctl.storages_changed.connect(self._storages_changed)
        self._guard = False
        self.refresh()

    def _storages_changed(self) -> None:
        fill_storages(self.storage, self.ctl)
        self.refresh()

    def refresh(self) -> None:
        self._guard = True
        self.tree.clear()
        sid = self.storage.currentData()
        folders: dict[str, QTreeWidgetItem] = {}
        rows = self.ctl.repos.files.list_backed_up(sid) if sid is not None else []
        for r in rows:
            parts = r["rel_path"].split("/")
            parent: QTreeWidgetItem | None = None
            for i in range(len(parts) - 1):
                key = "/".join(parts[: i + 1])
                if key not in folders:
                    it = QTreeWidgetItem([parts[i], ""])
                    it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                    it.setCheckState(0, Qt.CheckState.Unchecked)
                    (parent.addChild(it) if parent else self.tree.addTopLevelItem(it))
                    folders[key] = it
                parent = folders[key]
            leaf = QTreeWidgetItem([parts[-1], human_size(r["size"])])
            leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            leaf.setCheckState(0, Qt.CheckState.Unchecked)
            leaf.setData(0, Qt.ItemDataRole.UserRole, int(r["id"]))
            (parent.addChild(leaf) if parent else self.tree.addTopLevelItem(leaf))
        self.tree.expandToDepth(1)
        self._guard = False
        self.empty.setVisible(not rows)
        self.tree.setVisible(bool(rows))

    def _propagate(self, item: QTreeWidgetItem, col: int) -> None:
        if self._guard or col != 0:
            return
        # Qt's auto-tristate handles folders; nothing else to do (guard avoids recursion)

    def checked_ids(self) -> list[int]:
        out: list[int] = []

        def walk(it: QTreeWidgetItem) -> None:
            fid = it.data(0, Qt.ItemDataRole.UserRole)
            if fid is not None and it.checkState(0) == Qt.CheckState.Checked:
                out.append(int(fid))
            for i in range(it.childCount()):
                walk(it.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        return out

    def _restore(self) -> None:
        ids = self.checked_ids()
        if not ids:
            self.ctl.message.emit("Tick the files or folders you want to restore.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Restore to…")
        if dest:
            self.ctl.start_restore(ids, dest)

    def _delete(self) -> None:
        ids = self.checked_ids()
        if ids and confirm_cloud_delete(self, len(ids)):
            self.ctl.delete_cloud_files(ids)


# =============================================================================== Search
class SearchPage(Page):
    def __init__(self, ctl: AppController):
        super().__init__(ctl)
        self.root.addWidget(header("Search", "Search file names, folders, storages and types across all storages."))
        bar = QHBoxLayout()
        bar.setSpacing(SP)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Search…")
        self.storage = storage_combo(ctl, include_all=True)
        self.kind = QComboBox()
        self.kind.addItem("All types", None)
        for k in TYPE_GROUPS:
            self.kind.addItem(k, k)
        bar.addWidget(self.query, 1)
        bar.addWidget(self.storage)
        bar.addWidget(self.kind)
        self.root.addLayout(bar)
        split = QSplitter()
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Folder", "Storage", "Size"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        right = QFrame()
        right.setObjectName("Panel")
        rl = QVBoxLayout(right)
        self.preview_img = QLabel("Select a file to preview")
        self.preview_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_img.setMinimumSize(280, 220)
        self.preview_txt = QPlainTextEdit()
        self.preview_txt.setReadOnly(True)
        self.preview_txt.hide()
        self.note = QLabel("")
        self.note.setObjectName("Hint")
        self.restore_btn = QPushButton("Restore selected…")
        self.restore_btn.setObjectName("Primary")
        rl.addWidget(self.preview_img, 1)
        rl.addWidget(self.preview_txt, 1)
        rl.addWidget(self.note)
        rl.addWidget(self.restore_btn)
        split.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([620, 340])
        self.root.addWidget(split, 1)
        self.hits: list = []
        self._timer = QTimer(self, singleShot=True, interval=200)
        self._timer.timeout.connect(self.run_search)
        self.query.textChanged.connect(lambda _t: self._timer.start())
        self.storage.currentIndexChanged.connect(self.run_search)
        self.kind.currentIndexChanged.connect(self.run_search)
        self.table.itemSelectionChanged.connect(self._selected)
        self.restore_btn.clicked.connect(self._restore)
        ctl.storages_changed.connect(lambda: (fill_storages(self.storage, ctl, True), self.run_search()))
        self.run_search()

    def run_search(self) -> None:
        self.hits = self.ctl.search.search(self.query.text(), self.storage.currentData(), self.kind.currentData())
        self.table.setRowCount(len(self.hits))
        for i, h in enumerate(self.hits):
            for c, v in enumerate((h.name, h.folder, h.storage_title.removeprefix("Teloude - "), human_size(h.size))):
                self.table.setItem(i, c, QTableWidgetItem(v))

    def _selected(self) -> None:
        rows = {i.row() for i in self.table.selectedItems()}
        if len(rows) != 1:
            return
        hit = self.hits[rows.pop()]
        self.note.setText("Loading preview…")
        self.ctl.run_async(self.ctl.preview.get(hit.file_id), ok=self._show_preview, err=lambda e: self.note.setText("Preview unavailable"))

    def _show_preview(self, r) -> None:  # type: ignore[no-untyped-def]
        self.note.setText(r.note)
        if r.kind == "image" and r.png:
            pm = QPixmap()
            pm.loadFromData(r.png)
            self.preview_txt.hide()
            self.preview_img.show()
            self.preview_img.setPixmap(pm.scaled(self.preview_img.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        elif r.kind == "text":
            self.preview_img.hide()
            self.preview_txt.show()
            self.preview_txt.setPlainText(r.text or "")
        else:
            self.preview_txt.hide()
            self.preview_img.show()
            self.preview_img.setPixmap(QPixmap())
            self.preview_img.setText("No preview available")

    def _restore(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        ids = [self.hits[r].file_id for r in rows]
        if not ids:
            self.ctl.message.emit("Select one or more results first.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Restore to…")
        if dest:
            self.ctl.start_restore(ids, dest)


# =============================================================================== Settings
LABEL_W = 190  # one shared label column: every control in every settings card lines up
ACTION_W = 200  # one shared width for the card action buttons


def form_row(label: str, *widgets: QWidget, label_w: int = LABEL_W, grow: bool = False) -> QHBoxLayout:
    """``label  [controls...]  <stretch>`` with the project's spacing system.

    The label column has a fixed width so controls in different cards start at the same x, and the
    row never squeezes its controls below their size hint. ``grow`` lets the last control (a text
    field) take the remaining width instead of being left at its minimum.
    """
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2 * SP)
    lab = QLabel(label)
    lab.setMinimumWidth(label_w)
    lab.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
    lab.setProperty("form_label", True)  # align_form_labels() gives the whole column one width
    row.addWidget(lab)
    for i, w in enumerate(widgets):
        row.addWidget(w, 1 if (grow and i == len(widgets) - 1) else 0, Qt.AlignmentFlag.AlignVCenter)
    if not grow:
        row.addStretch(1)
    return row


def align_form_labels(page: QWidget, pad: int = 8) -> None:
    """Give every ``form_row`` label the same width - the width of the widest one.

    ``setMinimumWidth(LABEL_W)`` alone is not enough: on a machine whose font is wider than
    LABEL_W (a larger UI font, a high-DPI screen, Segoe UI instead of the Linux default) the labels
    grow to their own text width and the controls of each card start at a different x, so the form
    looks ragged. Measuring the widest label with the *actual* font keeps the column aligned - and
    never elides text - on every platform.
    """
    labels = [w for w in page.findChildren(QLabel) if w.property("form_label")]
    if not labels:
        return
    width = max(lab.sizeHint().width() for lab in labels) + pad
    for lab in labels:
        lab.setFixedWidth(width)


def section_title(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    return lab


class SettingsPage(Page):
    def __init__(self, ctl: AppController, open_proxy):  # type: ignore[no-untyped-def]
        super().__init__(ctl)
        self.root.addWidget(header("Settings", "Preferences for this PC. Nothing here changes the files already in Telegram."))
        s = ctl.settings.load()
        # ---- account --------------------------------------------------------------------
        acc, al = card()
        self.account = QLabel("")
        self.account.setWordWrap(True)
        self.sign_out = QPushButton("Sign out")
        self.sign_out.setFixedWidth(ACTION_W)
        al.addWidget(section_title("Telegram account"))
        al.addWidget(self.account)
        al.addWidget(self.sign_out, 0, Qt.AlignmentFlag.AlignLeft)
        self.root.addWidget(acc)
        # ---- network --------------------------------------------------------------------
        net, nl = card()
        self.proxy_btn = QPushButton("Proxy settings…")
        self.proxy_btn.setFixedWidth(ACTION_W)
        self.speed = QComboBox()
        for k in SPEED_PRESETS:
            self.speed.addItem(k, SPEED_PRESETS[k])
        self.speed.addItem("Custom", "custom")
        self.speed.setMinimumWidth(150)
        self.custom = QDoubleSpinBox()
        self.custom.setRange(0.1, 1000)
        self.custom.setSuffix(" MB/s")
        self.custom.setMinimumWidth(130)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 8)
        self.concurrency.setMinimumWidth(90)
        nl.addWidget(section_title("Network"))
        nl.addWidget(self.proxy_btn, 0, Qt.AlignmentFlag.AlignLeft)
        nl.addLayout(form_row("Transfer speed limit", self.speed, self.custom))
        nl.addLayout(form_row("Parallel upload parts", self.concurrency))
        self.root.addWidget(net)
        # ---- preferences ----------------------------------------------------------------
        pref, pl = card()
        self.notifications = QCheckBox("Show notifications when backups and restores finish")
        self.close_tray = QCheckBox("Keep running in the system tray when the window is closed")
        self.theme = QComboBox()
        for t in ("system", "light", "dark"):
            self.theme.addItem(t.capitalize(), t)
        self.theme.setMinimumWidth(150)
        self.ignore = QLineEdit()
        self.ignore.setPlaceholderText("Ignore patterns, comma separated (e.g. Thumbs.db, *.tmp)")
        pl.addWidget(section_title("Preferences"))
        for w in (self.notifications, self.close_tray):
            pl.addWidget(w)
        pl.addLayout(form_row("Appearance", self.theme))
        pl.addLayout(form_row("Ignore these files", self.ignore, grow=True))
        self.root.addWidget(pref)
        # ---- data -----------------------------------------------------------------------
        data, dl = card()
        self.data_label = QLabel(f"Data folder: {ctl.paths.root}")
        self.data_label.setObjectName("Hint")
        self.data_label.setWordWrap(True)
        self.data_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        hint = QLabel("Manage storages on the Storages page. Credentials are stored with Windows DPAPI and never shown or logged.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        self.logs_btn = QPushButton("Open logs folder")
        self.logs_btn.setFixedWidth(ACTION_W)
        dl.addWidget(section_title("Storage & data"))
        dl.addWidget(hint)
        dl.addWidget(self.data_label)
        dl.addWidget(self.logs_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self.root.addWidget(data)
        self.root.addStretch(1)
        align_form_labels(self)  # one shared label column, whatever the font measures
        self._preview_max_mb = s.preview_max_mb
        self.load(s)
        self.proxy_btn.clicked.connect(open_proxy)
        self.sign_out.clicked.connect(self._sign_out)
        self.logs_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctl.paths.logs))))
        for sig in (self.speed.currentIndexChanged, self.custom.valueChanged, self.concurrency.valueChanged, self.notifications.toggled, self.close_tray.toggled, self.theme.currentIndexChanged):
            sig.connect(lambda *_: self.save())
        self.ignore.editingFinished.connect(self.save)
        ctl.account_changed.connect(lambda n: self.account.setText(n or "Not signed in"))

    def load(self, s: AppSettings) -> None:
        for w in (self.speed, self.custom, self.concurrency, self.notifications, self.close_tray, self.theme, self.ignore):
            w.blockSignals(True)
        i = self.speed.findData(s.speed_bytes_per_second)
        if i >= 0:
            self.speed.setCurrentIndex(i)
        else:
            self.speed.setCurrentIndex(self.speed.findData("custom"))
            self.custom.setValue((s.speed_bytes_per_second or MIB) / MIB)
        self.custom.setEnabled(self.speed.currentData() == "custom")
        self.concurrency.setValue(max(1, min(8, s.concurrency)))
        self.notifications.setChecked(s.notifications)
        self.close_tray.setChecked(s.close_to_tray)
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))
        self.ignore.setText(", ".join(s.ignore_patterns))
        self.account.setText(self.ctl.account_name or "Not signed in")
        self._preview_max_mb = s.preview_max_mb  # not editable here: keep it for the next save()
        for w in (self.speed, self.custom, self.concurrency, self.notifications, self.close_tray, self.theme, self.ignore):
            w.blockSignals(False)

    def current(self) -> AppSettings:
        d = self.speed.currentData()
        self.custom.setEnabled(d == "custom")
        bps = self.custom.value() * MIB if d == "custom" else d
        pats = tuple(p.strip() for p in self.ignore.text().split(",") if p.strip())
        # preview_max_mb is not editable here: carry the stored value through instead of resetting it.
        return AppSettings(bps, self.concurrency.value(), self.notifications.isChecked(), self.theme.currentData(),
                           self.close_tray.isChecked(), pats, preview_max_mb=self._preview_max_mb)  # fmt: skip

    def save(self) -> None:
        self.ctl.apply_settings(self.current())

    def _sign_out(self) -> None:
        if QMessageBox.question(self, "Sign out", "Sign out of Telegram on this PC? Your storages and files in Telegram are not affected.") == QMessageBox.StandardButton.Yes:
            self.ctl.sign_out()


__all__ = ["HomePage", "StoragesPage", "BackupsPage", "RestorePage", "SearchPage", "SettingsPage", "os"]
