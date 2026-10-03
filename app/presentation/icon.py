"""Application icon loading (works from source and from any PyInstaller bundle layout)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap


def resource_path(rel: str) -> Path | None:
    """The bundled file called ``rel``, or ``None`` when this build ships no such file."""
    from ..infrastructure.build_config import bundle_roots, bundled_resource

    return bundled_resource(rel) or bundle_roots()[0] / rel


def painted_icon(size: int = 256) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor("#4DA3FF"))
    g.setColorAt(1, QColor("#5E5CE6"))
    p.setBrush(g)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    p.setBrush(QColor("white"))
    s = size
    cloud = QPainterPath()
    cloud.addEllipse(QRectF(s * 0.22, s * 0.40, s * 0.32, s * 0.30))
    cloud.addEllipse(QRectF(s * 0.38, s * 0.28, s * 0.34, s * 0.38))
    cloud.addEllipse(QRectF(s * 0.55, s * 0.40, s * 0.24, s * 0.30))
    cloud.addRoundedRect(QRectF(s * 0.30, s * 0.50, s * 0.42, s * 0.20), s * 0.08, s * 0.08)
    p.drawPath(cloud)
    p.setBrush(QColor("#4F7DF0"))
    arrow = QPainterPath()
    arrow.moveTo(s * 0.5, s * 0.40)
    arrow.lineTo(s * 0.62, s * 0.54)
    arrow.lineTo(s * 0.54, s * 0.54)
    arrow.lineTo(s * 0.54, s * 0.66)
    arrow.lineTo(s * 0.46, s * 0.66)
    arrow.lineTo(s * 0.46, s * 0.54)
    arrow.lineTo(s * 0.38, s * 0.54)
    arrow.closeSubpath()
    p.drawPath(arrow)
    p.end()
    return pm


def app_icon() -> QIcon:
    for rel in ("assets/icon.ico", "assets/icon.png"):
        path = resource_path(rel)
        if path is not None and path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon(painted_icon())
