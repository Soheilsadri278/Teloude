from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.application.backup import DuplicateInfo  # noqa: E402
from app.application.restore import ConflictInfo  # noqa: E402
from app.domain.models import (  # noqa: E402
    ConflictAction,
    DuplicateAction,
    ProxySettings,
    ProxyState,
    RunOutcome,
)
from app.infrastructure.config import AppPaths  # noqa: E402
from app.presentation.controller import AppController  # noqa: E402
from app.presentation.dialogs import ConflictDialog, DuplicateDialog  # noqa: E402
from app.presentation.icon import app_icon  # noqa: E402
from app.presentation.main_window import MainWindow  # noqa: E402
from app.presentation.theme import LIGHT, apply_theme  # noqa: E402
from app.presentation.tray import TrayController, should_hide_on_close  # noqa: E402
from app.testing.fake_gateway import FakeGateway  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(qapp, cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture
def ctl(qapp, tmp_path):
    paths = AppPaths(tmp_path / "data").ensure()
    gw = FakeGateway(password="pw")
    c = AppController(paths, gateway=gw)
    c.settings.set_api_credentials(12345, "hash-for-tests-only")
    c.start()
    yield c
    c.shutdown()


def test_login_flow_with_2fa_then_ready(qapp, ctl):
    states: list[str] = []
    ctl.login_state.connect(lambda s, d: states.append(s))
    assert wait_for(qapp, lambda: "need_phone" in states)
    ctl.submit_phone("+10000000000")
    assert wait_for(qapp, lambda: "need_code" in states)
    ctl.submit_code("00000")
    assert wait_for(qapp, lambda: states[-1] == "need_code" and states.count("need_code") >= 2)  # wrong code stays on code step
    ctl.submit_code("12345")
    assert wait_for(qapp, lambda: "need_password" in states)
    ctl.submit_password("pw")
    assert wait_for(qapp, lambda: states[-1] == "ready")
    assert ctl.account_name == "Test User"


def test_proxy_saved_applies_globally_and_secret_not_in_database(qapp, ctl):
    seen: list[str] = []
    ctl.proxy_state.connect(lambda s, d: seen.append(s))
    secret = "ab" * 16
    ctl.save_proxy(ProxySettings("proxy.example.com", 443, secret, True))
    assert wait_for(qapp, lambda: ProxyState.CONNECTED.value in seen)
    assert ctl.gateway.proxy.host == "proxy.example.com" and ProxyState.CONNECTED.value in seen
    dump = " ".join(str(v) for r in ctl.db.query("SELECT value FROM settings") for v in r)
    assert secret not in dump  # lives only in the protected SecretStore
    assert ctl.get_proxy().secret == secret
    results: list[tuple[bool, str]] = []
    ctl.proxy_test_result.connect(lambda ok, m: results.append((ok, m)))
    ctl.test_proxy(ProxySettings("bad", 1, secret, True))
    assert wait_for(qapp, lambda: results) and results[0][0] is False and ProxyState.ERROR.value in seen


def test_backup_through_controller_emits_progress_states_and_notification(qapp, ctl, tmp_path):
    src = tmp_path / "Docs"
    src.mkdir()
    (src / "a.bin").write_bytes(os.urandom(40_000))
    got_events: list[tuple[str, str]] = []
    done: list = []
    notes: list[tuple[str, str]] = []
    ctl.file_event.connect(lambda fid, rel, st, d, t, sp, eta: got_events.append((rel, st)))
    ctl.op_finished.connect(done.append)
    ctl.notify.connect(lambda t, m, k: notes.append((t, k)))
    created: list[int] = []
    ctl.create_storage("Docs", created.append)
    assert wait_for(qapp, lambda: created)
    ctl.start_backup(created[0], str(src))
    assert wait_for(qapp, lambda: done)
    assert done[0].outcome == RunOutcome.COMPLETED
    states = [s for r, s in got_events if r.endswith("a.bin")]
    assert "queued" in states and "completed" in states and states[-1] == "completed"
    assert notes == [("Backup completed", "success")]  # exactly one notification
    assert not ctl.busy
    ctl.notifications.notify_run(done[0])
    assert len(notes) == 1  # replaying the same run does not notify again


def test_duplicate_prompt_reaches_ui_via_signal_and_answer_flows_back(qapp, ctl, tmp_path):
    src = tmp_path / "D"
    src.mkdir()
    blob = os.urandom(3000)
    (src / "one.bin").write_bytes(blob)
    created: list[int] = []
    ctl.create_storage("D", created.append)
    assert wait_for(qapp, lambda: created)
    done: list = []
    ctl.op_finished.connect(done.append)
    ctl.start_backup(created[0], str(src))
    assert wait_for(qapp, lambda: done)
    (src / "two.bin").write_bytes(blob)
    asked: list[DuplicateInfo] = []

    def on_ask(info, fut):
        asked.append(info)
        from app.presentation.controller import answer_duplicate

        answer_duplicate(fut, DuplicateAction.SKIP, True)

    ctl.ask_duplicate.connect(on_ask)
    done.clear()
    ctl.start_backup(created[0], str(src))
    assert wait_for(qapp, lambda: done)
    assert len(asked) == 1 and done[0].skipped == 1


def test_main_window_proxy_icon_is_available_before_and_after_login(qapp, ctl):
    win = MainWindow(ctl, app_icon(), LIGHT)
    win.show()
    qapp.processEvents()
    assert win.gate.currentIndex() == 0 and win.proxy_btn.isVisible()  # login screen
    r = win.proxy_btn.geometry()
    assert r.right() <= win.root.width() and r.top() >= 0 and r.left() > win.root.width() // 2  # top-right corner
    win._login_state("ready", "")
    qapp.processEvents()
    assert win.gate.currentIndex() == 1 and win.proxy_btn.isVisible()  # still there after sign-in
    win.proxy_btn.click()
    qapp.processEvents()
    assert win.sheet.isVisible()
    win.sheet.close_sheet(animated=False)
    win.close()


def test_progress_table_shows_each_transfer_state(qapp, ctl):
    win = MainWindow(ctl, app_icon(), LIGHT)
    page = win.page_widgets["Backups"]
    for i, st in enumerate(["uploading", "paused", "reconnecting", "retrying", "completed", "failed", "cancelled"]):
        ctl.file_event.emit(i, f"f{i}.bin", st, 50.0, 100.0, 1000.0, 5.0)
    qapp.processEvents()
    labels = [page.table.item(r, 1).text() for r in range(page.table.rowCount())]
    assert labels == ["Uploading", "Paused", "Reconnecting", "Retrying", "Completed", "Failed", "Cancelled"]
    assert page.table.cellWidget(0, 2).value() == 500
    assert "/s" in page.table.item(0, 3).text()  # speed and ETA shown while uploading


def test_dialogs_return_explicit_choices_and_closing_means_cancel(qapp):
    info = DuplicateInfo("a/b.txt", 10, ["a/c.txt"])
    for action, key in ((DuplicateAction.SKIP, DuplicateAction.SKIP), (DuplicateAction.UPLOAD_AGAIN, DuplicateAction.UPLOAD_AGAIN), (DuplicateAction.CANCEL, DuplicateAction.CANCEL)):
        d = DuplicateDialog(info)
        d.apply_all.setChecked(True)
        d.buttons[key].click()
        assert d.result_action == action and d.apply_all.isChecked()
    assert {b.text() for b in DuplicateDialog(info).buttons.values()} == {"Skip", "Upload again", "Cancel"}
    d = DuplicateDialog(info)
    d.reject()
    assert d.result_action == DuplicateAction.CANCEL  # never a silent default
    c = ConflictDialog(ConflictInfo("x", "/tmp/x", 1, 2))
    c.reject()
    assert c.result_action == ConflictAction.CANCEL


def test_tray_menu_and_close_policy(qapp):
    assert should_hide_on_close(busy=True, close_to_tray=False, tray_available=True)
    assert not should_hide_on_close(busy=False, close_to_tray=False, tray_available=True)
    assert should_hide_on_close(busy=False, close_to_tray=True, tray_available=True)
    assert not should_hide_on_close(busy=True, close_to_tray=True, tray_available=False)  # no tray -> never hide into nothing
    tray = TrayController(app_icon())
    assert [a.text() for a in tray.menu.actions() if a.text()] == ["Open", "Pause", "Resume", "View Progress", "Exit"]
    tray.set_busy(True, False)
    assert tray.act_pause.isEnabled() and not tray.act_resume.isEnabled()
    tray.set_busy(True, True)
    assert tray.act_resume.isEnabled() and not tray.act_pause.isEnabled()
    tray.set_busy(False, False)
    assert not tray.act_pause.isEnabled()
    fired = []
    tray.exit_requested.connect(lambda: fired.append(1))
    tray.act_exit.trigger()
    assert fired


def test_closing_window_while_busy_hides_instead_of_quitting(qapp, ctl):
    win = MainWindow(ctl, app_icon(), LIGHT)
    win.tray_ok = True  # pretend a tray exists (offscreen has none)
    win.show()
    from app.application.control import TransferControl
    from app.presentation.controller import ActiveOp

    ctl.active = ActiveOp("backup", "x", TransferControl())
    win.close()
    qapp.processEvents()
    assert not win.isVisible() and not win._quitting
    ctl.active = None


def test_theme_applies_stylesheet_and_icon_loads(qapp):
    p = apply_theme(qapp, "dark")
    assert p.name == "dark" and "#1C1C1E" in qapp.styleSheet()
    apply_theme(qapp, "light")
    assert not app_icon().isNull()
    assert Path("assets/icon.ico").exists()


def test_window_icon_shows_the_master_artwork(qapp):
    """The window, taskbar and tray must show Logo&icon/'s artwork, not the painted fallback."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import make_icon  # noqa: E402  (scripts/ holds executables, not an importable package)
    from PIL import Image  # noqa: E402
    from PySide6.QtGui import QImage  # noqa: E402

    master, _source = make_icon.master()
    image = app_icon().pixmap(64).toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    assert not image.isNull()
    shown = Image.frombytes(
        "RGBA",
        (image.width(), image.height()),
        bytes(image.constBits()),
        "raw",
        "RGBA",
        image.bytesPerLine(),
    )
    assert make_icon.deviation(shown, master) <= make_icon.TOLERANCE
