"""Decision dialogs: duplicates, restore conflicts, destructive confirmation."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..application.backup import DuplicateInfo
from ..application.notifications import human_size
from ..application.restore import ConflictInfo
from ..domain.models import ConflictAction, DuplicateAction
from .theme import SP


class _DecisionDialog(QDialog):
    def __init__(self, title: str, heading: str, body: str, buttons: list[tuple[str, object, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(460)
        self.result_action: object | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3 * SP, 3 * SP, 3 * SP, 3 * SP)
        lay.setSpacing(2 * SP)
        h = QLabel(heading)
        h.setObjectName("Title")
        h.setWordWrap(True)
        b = QLabel(body)
        b.setWordWrap(True)
        b.setObjectName("Subtitle")
        lay.addWidget(h)
        lay.addWidget(b)
        self.apply_all = QCheckBox("Apply to all")
        lay.addWidget(self.apply_all)
        row = QHBoxLayout()
        row.setSpacing(SP)
        row.addStretch(1)
        self.buttons: dict[object, QPushButton] = {}
        for text, action, obj in buttons:
            btn = QPushButton(text)
            if obj:
                btn.setObjectName(obj)
            btn.clicked.connect(lambda _=False, a=action: self._choose(a))
            row.addWidget(btn)
            self.buttons[action] = btn
        lay.addLayout(row)

    def _choose(self, action: object) -> None:
        self.result_action = action
        self.accept()

    def reject(self) -> None:  # closing the window == the safe "Cancel" choice, never a silent default
        self.result_action = self._cancel_action
        super().reject()

    _cancel_action: object = None


class DuplicateDialog(_DecisionDialog):
    _cancel_action = DuplicateAction.CANCEL

    def __init__(self, info: DuplicateInfo, parent: QWidget | None = None):
        where = "\n".join(f"• {p}" for p in info.existing_paths[:5])
        super().__init__(
            "Duplicate file",
            "This file is already backed up",
            f"“{info.rel_path}” ({human_size(info.size)}) has identical content to:\n{where}",
            [("Skip", DuplicateAction.SKIP, ""), ("Upload again", DuplicateAction.UPLOAD_AGAIN, "Primary"), ("Cancel", DuplicateAction.CANCEL, "")],
            parent,
        )


class ConflictDialog(_DecisionDialog):
    _cancel_action = ConflictAction.CANCEL

    def __init__(self, info: ConflictInfo, parent: QWidget | None = None):
        super().__init__(
            "File already exists",
            "A file with this name already exists",
            f"{info.dest_path}\nExisting: {human_size(info.existing_size)}   •   From backup: {human_size(info.incoming_size)}\n"
            "Teloude never overwrites your files without asking.",
            [("Skip", ConflictAction.SKIP, ""), ("Keep both", ConflictAction.KEEP_BOTH, "Primary"), ("Replace", ConflictAction.REPLACE, "Danger"), ("Cancel", ConflictAction.CANCEL, "")],
            parent,
        )


def confirm_cloud_delete(parent: QWidget, count: int) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Delete from Telegram")
    box.setText(f"Delete {count} file(s) from your Telegram storage?")
    box.setInformativeText("This permanently removes the cloud copies. Files on this PC are NOT deleted. This cannot be undone.")
    delete = box.addButton("Delete from Telegram", QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is delete


__all__ = ["DuplicateDialog", "ConflictDialog", "confirm_cloud_delete", "Qt"]
