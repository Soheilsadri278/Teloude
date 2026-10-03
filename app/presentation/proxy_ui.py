"""Proxy indicator button + proxy settings *sheet*.

Rendering safety (this is what broke on packaged Windows builds before):
* The sheet is an **in-window overlay child widget**, not a separate translucent top-level window,
  so there is no per-pixel-alpha native window to fail to composite.
* **No QGraphicsEffect anywhere** (drop-shadow / blur / opacity effects nested in translucent parents
  are the classic cause of invisible windows). Shadow and translucency are painted manually with
  QPainter in ``paintEvent``.
* ``tests/ui/test_proxy_ui.py`` renders the widget and fails if the painted area is zero.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QEvent,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..domain.models import ProxySettings, ProxyState
from .theme import ANIM_MS, MIN_TARGET, R_LG, R_SM, SP, Palette, qcolor

STATE_COLORS = {
    ProxyState.DISCONNECTED: "#8E8E93",
    ProxyState.CONNECTING: "#FF9F0A",
    ProxyState.CONNECTED: "#30B350",
    ProxyState.ERROR: "#FF3B30",
}
STATE_TEXT = {
    ProxyState.DISCONNECTED: "Proxy off",
    ProxyState.CONNECTING: "Connecting…",
    ProxyState.CONNECTED: "Connected via proxy",
    ProxyState.ERROR: "Proxy error",
}


class ProxyIndicator(QAbstractButton):
    """44x44 shield button; the status ring cross-fades between states and pulses while connecting."""

    def __init__(self, palette: Palette, parent: QWidget | None = None):
        super().__init__(parent)
        self._p = palette
        self._state = ProxyState.DISCONNECTED
        self._color = QColor(STATE_COLORS[self._state])
        self._pulse = 0.0
        self.setFixedSize(MIN_TARGET, MIN_TARGET)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Proxy settings")
        self.setToolTip(STATE_TEXT[self._state])
        self._color_anim = QVariantAnimation(self, duration=ANIM_MS)
        self._color_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._color_anim.valueChanged.connect(self._on_color)
        self._pulse_anim = QVariantAnimation(self, duration=1100, startValue=0.0, endValue=1.0, loopCount=-1)
        self._pulse_anim.valueChanged.connect(self._on_pulse)

    @property
    def state(self) -> ProxyState:
        return self._state

    def set_palette_tokens(self, p: Palette) -> None:
        self._p = p
        self.update()

    def set_state(self, state: ProxyState, detail: str = "") -> None:
        if state == self._state:
            return
        old = self._color
        self._state = state
        self._color_anim.stop()
        self._color_anim.setStartValue(old)
        self._color_anim.setEndValue(QColor(STATE_COLORS[state]))
        self._color_anim.start()
        if state == ProxyState.CONNECTING:
            self._pulse_anim.start()
        else:
            self._pulse_anim.stop()
            self._pulse = 0.0
        self.setToolTip(STATE_TEXT[state] + (f" - {detail}" if detail and state == ProxyState.ERROR else ""))
        self.update()

    def _on_color(self, c: QColor) -> None:
        self._color = QColor(c)
        self.update()

    def _on_pulse(self, v: float) -> None:
        self._pulse = float(v)
        self.update()

    def paintEvent(self, _e: QEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        hover = self.underMouse() or self.hasFocus()
        p.setPen(QPen(qcolor(self._p.border), 1))
        p.setBrush(qcolor(self._p.surface, 235 if hover else 200))
        p.drawEllipse(r)
        if self._state == ProxyState.CONNECTING:  # soft pulsing halo
            halo = QColor(self._color)
            halo.setAlpha(int(120 * (1 - self._pulse)))
            p.setPen(QPen(halo, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            grow = 6 * self._pulse
            p.drawEllipse(r.adjusted(-grow, -grow, grow, grow).intersected(QRectF(self.rect())))
        # shield glyph
        c = r.center()
        path = QPainterPath()
        w, h = 12.0, 14.0
        path.moveTo(c.x(), c.y() - h / 2)
        path.lineTo(c.x() + w / 2, c.y() - h / 2 + 3)
        path.lineTo(c.x() + w / 2, c.y() + 1)
        path.quadTo(c.x() + w / 2, c.y() + h / 2 - 1, c.x(), c.y() + h / 2)
        path.quadTo(c.x() - w / 2, c.y() + h / 2 - 1, c.x() - w / 2, c.y() + 1)
        path.lineTo(c.x() - w / 2, c.y() - h / 2 + 3)
        path.closeSubpath()
        p.setPen(QPen(qcolor(self._p.text, 210), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        # status dot
        p.setPen(QPen(qcolor(self._p.surface), 2))
        p.setBrush(self._color)
        p.drawEllipse(QPointF(r.right() - 4, r.bottom() - 4), 5.0, 5.0)
        p.end()


class _Card(QFrame):
    """Rounded translucent card painted manually (no QGraphicsEffect)."""

    def __init__(self, palette: Palette, parent: QWidget):
        super().__init__(parent)
        self._p = palette
        self.setObjectName("SheetCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_palette_tokens(self, p: Palette) -> None:
        self._p = p
        self.update()

    def paintEvent(self, _e: QEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(qcolor(self._p.border), 1))
        p.setBrush(qcolor(self._p.surface, 250))  # restrained translucency (content behind must not bleed through)
        p.drawRoundedRect(r, R_LG, R_LG)
        p.end()


class ProxySheet(QWidget):
    """Modal-style sheet that slides up over the window. Fields: server, port, secret, enabled."""

    saved = Signal(object)  # ProxySettings
    test_requested = Signal(object)
    closed = Signal()

    CARD_W = 440
    SHADOW = 28

    def __init__(self, palette: Palette, parent: QWidget):
        super().__init__(parent)
        self._p = palette
        self._progress = 0.0
        self.hide()
        self.setObjectName("ProxySheet")
        self.card = _Card(palette, self)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(3 * SP, 3 * SP, 3 * SP, 3 * SP)
        lay.setSpacing(2 * SP)

        title = QLabel("Proxy")
        title.setObjectName("Title")
        sub = QLabel("Route all Telegram traffic through an MTProto proxy. Applies to sign-in, uploads, downloads and search.")
        sub.setObjectName("Subtitle")
        sub.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(sub)

        self.host = QLineEdit()
        self.host.setPlaceholderText("Server (e.g. proxy.example.com)")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(443)
        self.port.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.secret = QLineEdit()
        self.secret.setPlaceholderText("Secret (hex or base64)")
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.enabled = QCheckBox("Use this proxy")
        row = QHBoxLayout()
        row.setSpacing(SP)
        row.addWidget(self.host, 1)
        row.addWidget(self.port)
        lay.addLayout(row)
        lay.addWidget(self.secret)
        lay.addWidget(self.enabled)
        self.status = QLabel("")
        self.status.setObjectName("Hint")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        btns.setSpacing(SP)
        self.test_btn = QPushButton("Test / Connect")
        self.cancel_btn = QPushButton("Close")
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("Primary")
        btns.addWidget(self.test_btn)
        btns.addStretch(1)
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.save_btn)
        lay.addLayout(btns)

        self.save_btn.clicked.connect(self._save)
        self.test_btn.clicked.connect(lambda: self.test_requested.emit(self.current()))
        self.cancel_btn.clicked.connect(self.close_sheet)
        self._anim = QPropertyAnimation(self, b"progress", self)
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._anim_done)
        parent.installEventFilter(self)

    # -- state ------------------------------------------------------------------------------
    def current(self) -> ProxySettings:
        return ProxySettings(self.host.text().strip(), int(self.port.value()), self.secret.text().strip(), self.enabled.isChecked())

    def load(self, p: ProxySettings) -> None:
        self.host.setText(p.host)
        self.port.setValue(p.port or 443)
        self.secret.setText(p.secret)
        self.enabled.setChecked(p.enabled)

    def set_status(self, text: str, ok: bool | None = None) -> None:
        self.status.setText(text)
        self.status.setObjectName("Hint" if ok in (None, True) else "Error")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def set_palette_tokens(self, p: Palette) -> None:
        self._p = p
        self.card.set_palette_tokens(p)
        self.update()

    def _save(self) -> None:
        cur = self.current()
        if cur.enabled and not cur.is_complete():
            self.set_status("Enter a server, port and secret to enable the proxy.", ok=False)
            return
        self.saved.emit(cur)
        self.close_sheet()

    # -- animation (custom property; NO graphics effect) ----------------------------------------
    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, v: float) -> None:
        self._progress = float(v)
        self._layout_card()
        self.update()

    progress = Property(float, _get_progress, _set_progress)

    @property
    def is_open(self) -> bool:
        return self.isVisible() and self._progress > 0

    def open_sheet(self, animated: bool = True) -> None:
        par = self.parentWidget()
        if par is not None:
            self.setGeometry(par.rect())
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setDirection(QPropertyAnimation.Direction.Forward)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        if animated:
            self._anim.start()
        else:
            self._set_progress(1.0)
        self.host.setFocus()

    def close_sheet(self, animated: bool = True) -> None:
        self._anim.stop()
        self._anim.setDirection(QPropertyAnimation.Direction.Forward)
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(0.0)
        if animated and self.isVisible():
            self._anim.start()
        else:
            self._set_progress(0.0)
            self._anim_done()

    def _anim_done(self) -> None:
        if self._progress <= 0.001:
            self.hide()
            self.closed.emit()

    def eventFilter(self, obj, ev):  # type: ignore[no-untyped-def]  # noqa: N802
        if obj is self.parentWidget() and ev.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self._layout_card()
        return False

    def keyPressEvent(self, e):  # type: ignore[no-untyped-def]  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.close_sheet()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):  # type: ignore[no-untyped-def]  # noqa: N802
        if not self.card.geometry().contains(e.position().toPoint()):
            self.close_sheet()  # click on the scrim dismisses

    def _layout_card(self) -> None:
        w = min(self.CARD_W, max(280, self.width() - 4 * SP))
        h = self.card.sizeHint().height()
        x = (self.width() - w) // 2
        y_final = max(2 * SP, (self.height() - h) // 3)
        y = int(y_final + (1 - self._progress) * 3 * SP * 3)
        self.card.setGeometry(x, y, w, h)

    def resizeEvent(self, e):  # type: ignore[no-untyped-def]  # noqa: N802
        super().resizeEvent(e)
        self._layout_card()

    def paintEvent(self, _e: QEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, int(105 * self._progress)))  # scrim
        card = QRectF(self.card.geometry())
        for i in range(self.SHADOW, 0, -4):  # soft shadow: stacked rounded rects, no effect object
            a = int(16 * self._progress * (1 - i / (self.SHADOW + 4)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, max(a, 0)))
            p.drawRoundedRect(card.adjusted(-i, -i + 8, i, i + 8), R_LG + i / 2, R_LG + i / 2)
        p.end()


__all__ = ["ProxyIndicator", "ProxySheet", "STATE_COLORS", "QSizePolicy", "R_SM"]
