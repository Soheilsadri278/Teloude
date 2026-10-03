"""Login flow: API credentials -> phone -> code -> optional 2FA password."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .controller import AppController
from .theme import SP


class LoginPage(QWidget):
    def __init__(self, ctl: AppController):
        super().__init__()
        self.setObjectName("Page")
        self.ctl = ctl
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box = QFrame()
        box.setObjectName("Card")
        box.setFixedWidth(440)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(4 * SP, 4 * SP, 4 * SP, 4 * SP)
        lay.setSpacing(2 * SP)
        self.title = QLabel("Teloude")
        self.title.setObjectName("Title")
        self.sub = QLabel("Back up to your own private Telegram storage.")
        self.sub.setObjectName("Subtitle")
        self.sub.setWordWrap(True)
        self.error = QLabel("")
        self.error.setObjectName("Error")
        self.error.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.sub)
        self.steps = QStackedWidget()
        lay.addWidget(self.steps)
        lay.addWidget(self.error)
        outer.addWidget(box)

        # 0: connecting / offline
        self.busy_label = QLabel("Connecting…")
        self.retry_btn = QPushButton("Try again")
        self.retry_btn.clicked.connect(ctl.retry_connect)
        self.steps.addWidget(self._col(self.busy_label, self.retry_btn))
        # 1: API credentials
        self.api_id, self.api_hash = QLineEdit(), QLineEdit()
        self.api_id.setPlaceholderText("API ID")
        self.api_hash.setPlaceholderText("API hash")
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        link = QPushButton("Get API credentials at my.telegram.org")
        link.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://my.telegram.org/apps")))
        go = self._primary("Continue", lambda: ctl.submit_credentials(self.api_id.text(), self.api_hash.text()))
        self.cred_hint = QLabel(
            "This build of Teloude already contains Telegram API credentials. You can sign in straight "
            "away, or paste your own from my.telegram.org below."
            if ctl.bundled_credentials
            else "Teloude is open source and ships without Telegram API keys. Create your own free keys "
            "once; they are stored protected on this PC."
        )
        self.cred_hint.setObjectName("Hint")
        self.cred_hint.setWordWrap(True)
        self.steps.addWidget(self._col(self.cred_hint, self.api_id, self.api_hash, link, go))
        # 2: phone
        self.phone = QLineEdit()
        self.phone.setPlaceholderText("Phone number, e.g. +49 170 1234567")
        self.steps.addWidget(self._col(self.phone, self._primary("Send code", lambda: ctl.submit_phone(self.phone.text()))))
        # 3: code
        self.code = QLineEdit()
        self.code.setPlaceholderText("Login code from Telegram")
        self.steps.addWidget(self._col(self.code, self._primary("Sign in", lambda: ctl.submit_code(self.code.text()))))
        # 4: password
        self.password = QLineEdit()
        self.password.setPlaceholderText("Two-step verification password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.steps.addWidget(self._col(self.password, self._primary("Continue", lambda: ctl.submit_password(self.password.text()))))
        for w, fn in ((self.phone, lambda: ctl.submit_phone(self.phone.text())), (self.code, lambda: ctl.submit_code(self.code.text())), (self.password, lambda: ctl.submit_password(self.password.text())), (self.api_hash, lambda: ctl.submit_credentials(self.api_id.text(), self.api_hash.text()))):
            w.returnPressed.connect(fn)
        ctl.login_state.connect(self.show_state)
        self.show_state("checking", "")

    def _primary(self, text: str, fn) -> QPushButton:  # type: ignore[no-untyped-def]
        b = QPushButton(text)
        b.setObjectName("Primary")
        b.clicked.connect(fn)
        return b

    def _col(self, *widgets: QWidget) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(SP + 4)
        for x in widgets:
            lay.addWidget(x)
        return w

    def show_state(self, state: str, detail: str) -> None:
        # secrets are wiped from the form as soon as they have been used
        index = {"checking": 0, "offline": 0, "need_credentials": 1, "need_phone": 2, "need_code": 3, "need_password": 4}.get(state)
        if index is None:
            return
        if state != "need_code":
            self.code.clear()
        if state != "need_password":
            self.password.clear()
        if state != "need_credentials":
            self.api_hash.clear()
        self.steps.setCurrentIndex(index)
        self.retry_btn.setVisible(state == "offline")
        self.busy_label.setText("Can't connect to Telegram." if state == "offline" else "Connecting…")
        self.error.setText(detail if state not in ("checking",) else "")
        self.sub.setText({"need_code": "Enter the code Telegram sent you.", "need_password": "Your account has two-step verification enabled.", "need_phone": "Sign in with your Telegram account."}.get(state, "Back up to your own private Telegram storage."))


__all__ = ["LoginPage", "QHBoxLayout"]
