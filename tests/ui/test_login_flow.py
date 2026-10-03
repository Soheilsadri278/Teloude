"""The login flow: phone -> code -> 2FA, with the API credentials step only when it is needed."""

from __future__ import annotations

import json
import time

import pytest
from PySide6.QtWidgets import QApplication, QStackedWidget

from app.infrastructure import build_config
from app.infrastructure.config import AppPaths
from app.presentation.controller import AppController
from app.presentation.icon import app_icon
from app.presentation.main_window import MainWindow
from app.presentation.theme import LIGHT, apply_theme
from app.testing.fake_gateway import FakeGateway

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    apply_theme(app, "light")
    return app


def _controller(tmp_path, monkeypatch, *, bundled: bool, stored: bool):
    # must be pinned before the controller starts: it is read once at construction
    cfg = tmp_path / build_config.FILE_NAME
    if bundled:
        cfg.write_text(json.dumps({"api_id": 4242, "api_hash": "f" * 32}), encoding="utf-8")
    else:
        # A developer may have injected credentials for a release build; this fixture is about the
        # source-only experience, so it must not see them.
        cfg = tmp_path / "absent" / build_config.FILE_NAME
    monkeypatch.setattr(build_config, "bundled_credentials_path", lambda: cfg)
    paths = AppPaths(tmp_path / "data").ensure()
    ctl = AppController(paths, gateway=FakeGateway())
    if stored:
        ctl.settings.set_api_credentials(99, "9" * 32)
    ctl.start()
    try:
        yield ctl
    finally:
        ctl.shutdown()


@pytest.fixture
def ctl_bundled(tmp_path, monkeypatch):
    yield from _controller(tmp_path, monkeypatch, bundled=True, stored=False)


@pytest.fixture
def ctl_source(tmp_path, monkeypatch):
    yield from _controller(tmp_path, monkeypatch, bundled=False, stored=False)


def _window(qapp, ctl):
    w = MainWindow(ctl, app_icon(), LIGHT)
    w.show()
    states: list[str] = []
    ctl.login_state.connect(lambda state, detail: states.append(state))
    end = time.time() + 10
    while time.time() < end and not states:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return w, states


def test_bundled_credentials_go_straight_to_the_phone_step(qapp, ctl_bundled, monkeypatch):
    win, _ = _window(qapp, ctl_bundled)
    assert win.login.steps.currentIndex() == 2  # 2 == phone (1 is the API credentials step)
    assert win.login.ctl.bundled_credentials is True
    assert "already contains Telegram API credentials" in win.login.cred_hint.text()


def test_without_credentials_the_app_still_asks_for_them(qapp, ctl_source):
    win, _ = _window(qapp, ctl_source)
    assert win.login.steps.currentIndex() == 1
    assert win.login.ctl.bundled_credentials is False
    assert "ships without Telegram API keys" in win.login.cred_hint.text()
    assert not win.login.api_hash.text()  # never pre-filled with a secret


def test_credential_step_is_never_shown_again_once_credentials_exist(qapp, ctl_bundled):
    win, _ = _window(qapp, ctl_bundled)
    steps = win.login.steps
    assert isinstance(steps, QStackedWidget)
    ctl_bundled.submit_phone("+49 170 1234567")
    deadline = time.time() + 5
    while time.time() < deadline and steps.currentIndex() != 3:  # 3 == the login code
        qapp.processEvents()
        time.sleep(0.01)
    assert steps.currentIndex() == 3
    assert win.login.code.placeholderText() == "Login code from Telegram"
    assert not win.login.api_hash.text()  # the credentials step was skipped, nothing was cached
