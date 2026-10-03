"""System tray: Open / Pause / Resume / View Progress / Exit, plus the close-to-tray policy."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


def should_hide_on_close(busy: bool, close_to_tray: bool, tray_available: bool) -> bool:
    """Closing the window keeps Teloude alive in the tray while an operation is running (or if the user
    asked for it). Otherwise closing the window exits."""
    return tray_available and (busy or close_to_tray)


class TrayController(QObject):
    open_requested = Signal()
    progress_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()
    exit_requested = Signal()

    def __init__(self, icon: QIcon, parent: QObject | None = None):
        super().__init__(parent)
        self.icon = QSystemTrayIcon(icon, parent)
        self.icon.setToolTip("Teloude")
        self.menu = QMenu()
        self.act_open = QAction("Open", self.menu)
        self.act_pause = QAction("Pause", self.menu)
        self.act_resume = QAction("Resume", self.menu)
        self.act_progress = QAction("View Progress", self.menu)
        self.act_exit = QAction("Exit", self.menu)
        for a in (self.act_open, self.act_pause, self.act_resume, self.act_progress):
            self.menu.addAction(a)
        self.menu.addSeparator()
        self.menu.addAction(self.act_exit)
        self.act_open.triggered.connect(self.open_requested)
        self.act_pause.triggered.connect(self.pause_requested)
        self.act_resume.triggered.connect(self.resume_requested)
        self.act_progress.triggered.connect(self.progress_requested)
        self.act_exit.triggered.connect(self.exit_requested)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.set_busy(False, False)

    @staticmethod
    def available() -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open_requested.emit()

    def set_busy(self, busy: bool, paused: bool) -> None:
        self.act_pause.setEnabled(busy and not paused)
        self.act_resume.setEnabled(busy and paused)
        self.act_progress.setEnabled(busy)
        self.icon.setToolTip("Teloude — " + ("paused" if paused else "working…") if busy else "Teloude")

    def notify(self, title: str, message: str, kind: str) -> None:
        icon = {"success": QSystemTrayIcon.MessageIcon.Information, "warning": QSystemTrayIcon.MessageIcon.Warning, "error": QSystemTrayIcon.MessageIcon.Critical}.get(kind, QSystemTrayIcon.MessageIcon.Information)
        self.icon.showMessage(title, message, icon, 8000)
