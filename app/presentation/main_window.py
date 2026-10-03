"""Main window: sidebar navigation, login gate, always-available proxy indicator and sheet."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..domain.models import ProxyState
from .controller import AppController, answer_duplicate
from .dialogs import ConflictDialog, DuplicateDialog
from .login import LoginPage
from .pages import BackupsPage, HomePage, RestorePage, SearchPage, SettingsPage, StoragesPage
from .proxy_ui import ProxyIndicator, ProxySheet
from .theme import MIN_TARGET, SP, Palette, apply_theme
from .tray import TrayController, should_hide_on_close

NAV = ["Home", "Storages", "Backups", "Restore", "Search", "Settings"]


class MainWindow(QMainWindow):
    def __init__(self, ctl: AppController, icon: QIcon, palette: Palette):
        super().__init__()
        self.ctl = ctl
        self.palette_tokens = palette
        self._quitting = False
        self._tray_hint_shown = False
        self.setWindowTitle("Teloude")
        self.setWindowIcon(icon)
        self.resize(1120, 740)
        self.setMinimumSize(900, 600)

        self.root = QWidget()
        self.root.setObjectName("Root")
        self.setCentralWidget(self.root)
        lay = QVBoxLayout(self.root)
        lay.setContentsMargins(0, 0, 0, 0)
        self.gate = QStackedWidget()
        lay.addWidget(self.gate)

        self.login = LoginPage(ctl)
        self.gate.addWidget(self.login)
        shell = QWidget()
        sl = QHBoxLayout(shell)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(220)
        vl = QVBoxLayout(side)
        vl.setContentsMargins(2 * SP, 3 * SP, 2 * SP, 2 * SP)
        vl.setSpacing(4)
        brand = QLabel("Teloude")
        brand.setObjectName("Title")
        vl.addWidget(brand)
        vl.addSpacing(SP)
        self.pages = QStackedWidget()
        self.page_widgets: dict[str, QWidget] = {}
        self.nav = QButtonGroup(self)
        self.settings_page: SettingsPage
        for i, name in enumerate(NAV):
            w: QWidget
            if name == "Home":
                w = HomePage(ctl)
            elif name == "Storages":
                w = StoragesPage(ctl)
            elif name == "Backups":
                w = BackupsPage(ctl)
            elif name == "Restore":
                w = RestorePage(ctl)
            elif name == "Search":
                w = SearchPage(ctl)
            else:
                w = self.settings_page = SettingsPage(ctl, self.open_proxy)
            self.page_widgets[name] = w
            # Pages are taller than a small window: without a scroll area Qt squeezes every child
            # below its size hint, which makes Settings' controls overlap and clip their text.
            self.pages.addWidget(self._scrollable(w))
            b = QPushButton(name)
            b.setObjectName("NavButton")
            b.setCheckable(True)
            b.setMinimumHeight(MIN_TARGET)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.nav.addButton(b, i)
            vl.addWidget(b)
        vl.addStretch(1)
        self.toast = QLabel("")
        self.toast.setObjectName("Hint")
        self.toast.setWordWrap(True)
        vl.addWidget(self.toast)
        self.nav.button(0).setChecked(True)
        self.nav.idClicked.connect(self.pages.setCurrentIndex)
        sl.addWidget(side)
        sl.addWidget(self.pages, 1)
        self.gate.addWidget(shell)

        # overlays: ALWAYS available, before and after sign-in
        self.proxy_btn = ProxyIndicator(palette, self.root)
        self.proxy_btn.clicked.connect(self.open_proxy)
        self.sheet = ProxySheet(palette, self.root)
        self.sheet.saved.connect(self._proxy_saved)
        self.sheet.test_requested.connect(self._proxy_test)
        self.sheet.load(ctl.get_proxy())
        self.proxy_btn.raise_()
        self._place_indicator()
        p = ctl.get_proxy()
        self.proxy_btn.set_state(ProxyState.CONNECTING if p.enabled and p.is_complete() else ProxyState.DISCONNECTED)

        # tray
        self.tray = TrayController(icon, self)
        self.tray_ok = TrayController.available()
        if self.tray_ok:
            self.tray.show()
        self.tray.open_requested.connect(self.bring_to_front)
        self.tray.progress_requested.connect(self.show_progress)
        self.tray.pause_requested.connect(ctl.pause)
        self.tray.resume_requested.connect(ctl.resume)
        self.tray.exit_requested.connect(self.request_exit)

        # wiring
        ctl.login_state.connect(self._login_state)
        ctl.proxy_state.connect(lambda st, d: self.proxy_btn.set_state(ProxyState(st), d))
        ctl.proxy_test_result.connect(lambda ok, msg: self.sheet.set_status(msg, ok))
        ctl.message.connect(self._toast)
        ctl.notify.connect(self.tray.notify)
        ctl.op_started.connect(lambda *_: self.tray.set_busy(True, False))
        ctl.op_finished.connect(lambda *_: self.tray.set_busy(False, False))
        ctl.paused_changed.connect(lambda paused: self.tray.set_busy(ctl.busy, paused))
        ctl.ask_duplicate.connect(self._ask_duplicate)
        ctl.ask_conflict.connect(self._ask_conflict)
        ctl.op_started.connect(lambda *_: self.show_progress())
        self.settings_page.theme.currentIndexChanged.connect(lambda *_: self.retheme(self.settings_page.theme.currentData()))
        self.gate.setCurrentIndex(0)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setObjectName("PageScroll")
        area.setWidgetResizable(True)  # keeps stretch factors working when there IS room
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(page)
        return area

    def _place_indicator(self) -> None:
        self.proxy_btn.move(self.root.width() - self.proxy_btn.width() - 2 * SP, 2 * SP)
        self.proxy_btn.raise_()

    def resizeEvent(self, e) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        super().resizeEvent(e)
        self._place_indicator()

    def showEvent(self, e) -> None:  # type: ignore[no-untyped-def]  # noqa: N802
        super().showEvent(e)
        QTimer.singleShot(0, self._place_indicator)

    def retheme(self, theme: str) -> None:
        p = apply_theme(QApplication.instance(), theme)  # type: ignore[arg-type]
        self.palette_tokens = p
        self.proxy_btn.set_palette_tokens(p)
        self.sheet.set_palette_tokens(p)

    def _login_state(self, state: str, detail: str) -> None:
        self.gate.setCurrentIndex(1 if state == "ready" else 0)
        self._place_indicator()

    def _toast(self, text: str) -> None:
        self.toast.setText(text)
        QTimer.singleShot(9000, lambda: self.toast.setText("") if self.toast.text() == text else None)

    def show_progress(self) -> None:
        if self.gate.currentIndex() == 1:
            self.pages.setCurrentIndex(NAV.index("Backups"))
            self.nav.button(NAV.index("Backups")).setChecked(True)

    def bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------ proxy
    def open_proxy(self) -> None:
        self.sheet.load(self.ctl.get_proxy())
        self.sheet.set_status("")
        self.sheet.open_sheet()

    def _proxy_saved(self, proxy) -> None:  # type: ignore[no-untyped-def]
        self.ctl.save_proxy(proxy)

    def _proxy_test(self, proxy) -> None:  # type: ignore[no-untyped-def]
        self.sheet.set_status("Testing connection…")
        self.ctl.test_proxy(proxy)

    # ------------------------------------------------------------------ decisions
    def _ask_duplicate(self, info, fut) -> None:  # type: ignore[no-untyped-def]
        self.bring_to_front()
        dlg = DuplicateDialog(info, self)
        dlg.exec()
        from ..domain.models import DuplicateAction

        answer_duplicate(fut, dlg.result_action or DuplicateAction.CANCEL, dlg.apply_all.isChecked())  # type: ignore[arg-type]

    def _ask_conflict(self, info, fut) -> None:  # type: ignore[no-untyped-def]
        from ..application.restore import ConflictDecision
        from ..domain.models import ConflictAction

        self.bring_to_front()
        dlg = ConflictDialog(info, self)
        dlg.exec()
        if not fut.done():
            fut.set_result(ConflictDecision(dlg.result_action or ConflictAction.CANCEL, dlg.apply_all.isChecked()))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ close / exit
    def closeEvent(self, e: QCloseEvent) -> None:  # noqa: N802
        if self._quitting:
            e.accept()
            return
        s = self.ctl.settings.load()
        if should_hide_on_close(self.ctl.busy, s.close_to_tray, self.tray_ok):
            e.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.notify("Teloude is still running", "Use the tray icon to open, pause or exit.", "success")
            return
        self.request_exit()
        e.ignore()

    def request_exit(self) -> None:
        if self.ctl.busy:
            self.bring_to_front()
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("Exit Teloude")
            box.setText("A transfer is still running.")
            box.setInformativeText("Exiting stops it safely. Uploaded parts are saved and the transfer can be resumed next time.")
            exit_btn = box.addButton("Stop and exit", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Keep running", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not exit_btn:
                return
            self.ctl.cancel()
            self._wait_then_quit(0)
            return
        self._quit_now()

    def _wait_then_quit(self, waited_ms: int) -> None:
        if self.ctl.busy and waited_ms < 15000:  # let the in-flight parts and the final checkpoint land
            QTimer.singleShot(200, lambda: self._wait_then_quit(waited_ms + 200))
        else:
            self._quit_now()

    def _quit_now(self) -> None:
        self._quitting = True
        self.tray.hide()
        self.ctl.shutdown()
        QApplication.quit()

    def changeEvent(self, e: QEvent) -> None:  # noqa: N802
        super().changeEvent(e)
