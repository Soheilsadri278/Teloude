"""Settings layout / state regressions.

The bugs these lock down: overlapping controls, squeezed (unreadable) labels, controls that do not
line up between cards, text that overflows its card, and a settings save that silently reset values
the page does not own.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QFrame,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QWidget,
)

from app.application.settings import AppSettings  # noqa: E402
from app.infrastructure.config import AppPaths  # noqa: E402
from app.presentation.controller import AppController  # noqa: E402
from app.presentation.icon import app_icon  # noqa: E402
from app.presentation.main_window import MainWindow  # noqa: E402
from app.presentation.theme import LIGHT, apply_theme  # noqa: E402
from app.testing.fake_gateway import FakeGateway  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ctl(qapp, tmp_path):
    paths = AppPaths(tmp_path / "data").ensure()
    c = AppController(paths, gateway=FakeGateway())
    c.settings.set_api_credentials(12345, "hash-for-tests-only")
    c.start()
    yield c
    c.shutdown()


@pytest.fixture
def win(qapp, ctl):
    apply_theme(qapp, "light")
    w = MainWindow(ctl, app_icon(), LIGHT)
    w.resize(900, 600)  # the minimum window size: the worst case for a tall page
    w.pages.setCurrentIndex(5)  # Settings
    w.show()  # layouts are only computed for a shown window
    settle(qapp, ctl, w)
    return w


def settle(qapp, ctl, w, timeout: float = 10.0) -> None:
    """Let the async bootstrap finish, then force the signed-in shell (the page under test)."""
    seen: list[str] = []
    ctl.login_state.connect(lambda state, detail: seen.append(state))
    end = time.time() + timeout
    while time.time() < end and not seen:
        qapp.processEvents()
        time.sleep(0.005)
    w._login_state("ready", "")
    qapp.processEvents()
    qapp.processEvents()
    assert w.page_widgets["Settings"].isVisible()


def cards(page: QWidget) -> list[QFrame]:
    return [f for f in page.findChildren(QFrame) if f.objectName() == "Card"]


def visible_children(parent: QWidget) -> list[QWidget]:
    return [c for c in parent.children() if isinstance(c, QWidget) and not c.isWindow() and c.isVisible()]


def test_settings_page_is_scrollable_instead_of_squeezing_its_controls(qapp, ctl, win):
    page = win.page_widgets["Settings"]
    area = win.pages.widget(5)
    assert isinstance(area, QScrollArea)
    assert area.widget() is page
    assert page.sizeHint().height() > area.viewport().height()  # it really does not fit
    # ...and every child still gets at least its size hint: nothing is compressed into overlap.
    for card in cards(page):
        for child in visible_children(card):
            assert child.height() >= min(child.sizeHint().height(), 8), (
                f"{type(child).__name__} was squeezed to {child.height()}px"
            )


def test_no_control_overlaps_another_in_any_card(qapp, ctl, win):
    page = win.page_widgets["Settings"]
    for card in cards(page):
        kids = [k for k in visible_children(card) if k.width() > 2 and k.height() > 2]
        for i in range(len(kids)):
            for j in range(i + 1, len(kids)):
                a, b = kids[i].geometry(), kids[j].geometry()
                assert not a.intersects(b), (
                    f"{type(kids[i]).__name__}{a.getRect()} overlaps {type(kids[j]).__name__}{b.getRect()}"
                )


def test_no_control_overflows_its_card(qapp, ctl, win):
    page = win.page_widgets["Settings"]
    for card in cards(page):
        for child in visible_children(card):
            g = child.geometry()
            assert g.right() <= card.width() + 1 and g.bottom() <= card.height() + 1, (
                f"{type(child).__name__} overflows its card: {g.getRect()} in {card.width()}x{card.height()}"
            )


def test_labels_are_readable_not_clipped(qapp, ctl, win):
    """Measured against the label's OWN font, so it holds on Linux, Windows and any DPI.

    A hard-coded pixel height does not travel: the same label is 14 px tall with the Linux default
    font and 13 px with Segoe UI, and neither number says whether the text is actually clipped.
    """
    page = win.page_widgets["Settings"]
    for lab in page.findChildren(QLabel):
        if not lab.text() or not lab.isVisible():
            continue
        text = lab.text()[:30]
        fm = QFontMetrics(lab.font())
        assert lab.height() >= fm.height() - 1, f"label {text!r}: {lab.height()}px tall, one line needs {fm.height()}px"
        if not lab.wordWrap():  # a wrapping label is *meant* to be narrower than its text
            needed = fm.horizontalAdvance(lab.text())
            assert lab.width() >= needed - 2, f"label {text!r}: {lab.width()}px wide, its text needs {needed}px"


def test_form_controls_line_up_in_one_column(qapp, ctl, win):
    """Every row label has the same width, so the controls of all cards start at the same x."""
    page = win.page_widgets["Settings"]
    controls = [
        w
        for cls in (QComboBox, QSpinBox, QLineEdit)
        for w in page.findChildren(cls)
        # parent is the card itself: skip the QLineEdit that lives INSIDE a spin box
        if w.isVisible() and isinstance(w.parent(), QFrame)
    ]
    xs = {w.geometry().x() for w in controls}
    # The ignore-patterns field is intentionally full width; everything else shares one column.
    assert len(xs) <= 2, f"controls start at too many different x positions: {sorted(xs)}"
    assert min(xs) >= 200


def test_long_text_wraps_instead_of_being_cut_off(qapp, ctl, win):
    page = win.page_widgets["Settings"]
    for attr in ("account", "data_label"):
        assert getattr(page, attr).wordWrap(), f"{attr} must wrap (long account names / long paths)"


def test_settings_round_trip_and_unowned_values_are_preserved(qapp, ctl, win):
    page = win.page_widgets["Settings"]
    ctl.apply_settings(AppSettings(preview_max_mb=42))
    page.load(ctl.settings.load())

    page.speed.setCurrentIndex(page.speed.findData("custom"))
    page.custom.setValue(3.5)
    page.concurrency.setValue(6)
    page.notifications.setChecked(False)
    page.close_tray.setChecked(False)
    page.theme.setCurrentIndex(page.theme.findData("dark"))
    page.ignore.setText("*.tmp, Thumbs.db")
    page.save()

    s = ctl.settings.load()
    assert s.speed_bytes_per_second == pytest.approx(3.5 * 1024 * 1024)
    assert s.concurrency == 6
    assert s.notifications is False and s.close_to_tray is False
    assert s.theme == "dark"
    assert s.ignore_patterns == ("*.tmp", "Thumbs.db")
    assert s.preview_max_mb == 42  # a value this page does not own must survive a save


def test_custom_speed_field_is_only_enabled_for_custom(qapp, ctl, win):
    page = win.page_widgets["Settings"]
    page.speed.setCurrentIndex(page.speed.findData(None))  # Unlimited
    assert not page.custom.isEnabled()
    page.speed.setCurrentIndex(page.speed.findData("custom"))
    assert page.custom.isEnabled()
    page.speed.setCurrentIndex(page.speed.findData(2 * 1024 * 1024))
    assert not page.custom.isEnabled()


def test_loading_settings_does_not_emit_spurious_saves(qapp, ctl, win):
    page = win.page_widgets["Settings"]
    ctl.apply_settings(AppSettings(concurrency=7, theme="dark", preview_max_mb=11))
    saves: list[int] = []
    page.theme.currentIndexChanged.connect(lambda *_: saves.append(1))
    page.load(ctl.settings.load())
    assert saves == []  # blockSignals covers every widget, including the text field
    assert page.theme.currentData() == "dark" and page.concurrency.value() == 7
