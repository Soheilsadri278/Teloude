from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsEffect, QWidget  # noqa: E402

from app.domain.models import ProxySettings, ProxyState  # noqa: E402
from app.presentation.proxy_ui import STATE_COLORS, ProxyIndicator, ProxySheet  # noqa: E402
from app.presentation.theme import DARK, LIGHT, MIN_TARGET  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def host(qapp, w=900, h=600) -> QWidget:
    parent = QWidget()
    parent.resize(w, h)
    from PySide6.QtGui import QPalette

    pal = parent.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#F5F5F7"))
    parent.setPalette(pal)
    parent.setAutoFillBackground(True)
    parent.show()
    qapp.processEvents()
    return parent


def diff_fraction(a, b, rect) -> float:
    n = diff = 0
    for x in range(rect.left(), rect.right(), 3):
        for y in range(rect.top(), rect.bottom(), 3):
            n += 1
            diff += a.pixelColor(x, y) != b.pixelColor(x, y)
    return diff / max(n, 1)


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
def test_proxy_sheet_actually_paints_pixels(qapp, palette):
    """Regression for the invisible-proxy-window bug: an open sheet must paint a non-empty area."""
    parent = host(qapp)
    sheet = ProxySheet(palette, parent)
    closed = parent.grab().toImage()
    sheet.open_sheet(animated=False)
    qapp.processEvents()
    opened = parent.grab().toImage()
    card = sheet.card.geometry()
    assert sheet.isVisible() and sheet.card.isVisible()
    assert card.width() >= 280 and card.height() >= 200  # not collapsed to zero
    assert diff_fraction(closed, opened, card) > 0.9  # card area is genuinely painted
    assert diff_fraction(closed, opened, parent.rect()) > 0.5  # scrim darkens the window too
    sheet.close_sheet(animated=False)
    qapp.processEvents()
    assert not sheet.isVisible()
    assert diff_fraction(closed, parent.grab().toImage(), parent.rect()) == 0.0  # closed -> paints nothing


def test_no_graphics_effects_anywhere_in_proxy_ui(qapp):
    """Nested QGraphicsEffects inside translucent parents are what made packaged Windows builds blank."""
    parent = host(qapp)
    sheet = ProxySheet(LIGHT, parent)
    ind = ProxyIndicator(LIGHT, parent)
    for root in (sheet, ind):
        for w in [root, *root.findChildren(QWidget)]:
            assert w.graphicsEffect() is None
        assert root.findChildren(QGraphicsEffect) == []
    assert not sheet.testAttribute(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.WidgetAttribute.WA_TranslucentBackground)
    assert sheet.parentWidget() is parent  # an in-window overlay, not a separate translucent top-level window


def test_sheet_animation_runs_about_300ms_and_ends_open(qapp):
    parent = host(qapp)
    sheet = ProxySheet(LIGHT, parent)
    sheet.open_sheet(animated=True)
    assert sheet._anim.duration() == 300
    loop_done = []
    QTimer.singleShot(450, lambda: loop_done.append(True))
    while not loop_done:
        qapp.processEvents()
    assert sheet.progress == pytest.approx(1.0)


def test_sheet_roundtrips_settings_and_validates(qapp):
    parent = host(qapp)
    sheet = ProxySheet(LIGHT, parent)
    saved = []
    sheet.saved.connect(saved.append)
    sheet.load(ProxySettings("p.example.com", 8443, "ab" * 16, True))
    assert sheet.current() == ProxySettings("p.example.com", 8443, "ab" * 16, True)
    sheet.secret.clear()
    sheet.save_btn.click()
    assert saved == [] and "secret" in sheet.status.text()  # incomplete + enabled is refused
    sheet.secret.setText("cd" * 16)
    sheet.save_btn.click()
    assert saved and saved[0].secret == "cd" * 16
    assert sheet.secret.echoMode().name == "Password"  # secret is masked
    assert "cdcd" not in repr(saved[0])  # and redacted from repr/logs


def test_indicator_states_colors_and_target_size(qapp):
    parent = host(qapp)
    ind = ProxyIndicator(LIGHT, parent)
    assert ind.width() >= MIN_TARGET and ind.height() >= MIN_TARGET
    assert ind.state == ProxyState.DISCONNECTED
    for st in (ProxyState.CONNECTING, ProxyState.CONNECTED, ProxyState.ERROR, ProxyState.DISCONNECTED):
        ind.set_state(st, "boom")
        assert ind.state == st and ind.toolTip()
        if st == ProxyState.CONNECTING:
            assert ind._pulse_anim.state().name == "Running"
        else:
            assert ind._pulse_anim.state().name != "Running"
        ind._color_anim.setCurrentTime(ind._color_anim.duration())
        qapp.processEvents()
        assert ind._color == QColor(STATE_COLORS[st])
    ind.set_state(ProxyState.CONNECTED)
    img = ind.grab().toImage()
    assert any(img.pixelColor(x, y).alpha() > 0 for x in range(0, 44, 4) for y in range(0, 44, 4))  # paints something


def test_indicator_state_transition_is_animated_not_instant(qapp):
    parent = host(qapp)
    ind = ProxyIndicator(LIGHT, parent)
    ind.set_state(ProxyState.CONNECTED)
    assert ind._color_anim.duration() == 300 and ind._color_anim.state().name == "Running"
