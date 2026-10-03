"""Apple-inspired design tokens + stylesheet. 8pt spacing system; radii 8 / 12 / 20."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

SP = 8  # base spacing unit
R_SM, R_MD, R_LG = 8, 12, 20
MIN_TARGET = 44  # minimum interactive size
ANIM_MS = 300
FONT_STACK = ["Segoe UI Variable", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "DejaVu Sans"]


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    surface: str
    sidebar: str
    text: str
    subtext: str
    border: str
    accent: str
    accent_text: str
    danger: str
    ok: str
    warn: str
    hover: str


LIGHT = Palette("light", "#F5F5F7", "#FFFFFF", "#ECECF0", "#1D1D1F", "#6E6E73", "#D2D2D7", "#0A84FF", "#FFFFFF", "#FF3B30", "#30B350", "#FF9F0A", "#E8E8ED")
DARK = Palette("dark", "#1C1C1E", "#2C2C2E", "#242426", "#F5F5F7", "#A1A1A6", "#3A3A3C", "#0A84FF", "#FFFFFF", "#FF453A", "#32D74B", "#FF9F0A", "#3A3A3C")


def resolve_palette(theme: str, app: QApplication | None = None) -> Palette:
    if theme == "dark":
        return DARK
    if theme == "light":
        return LIGHT
    app = app or QApplication.instance()  # type: ignore[assignment]
    try:
        from PySide6.QtCore import Qt

        return DARK if app is not None and app.styleHints().colorScheme() == Qt.ColorScheme.Dark else LIGHT
    except Exception:
        return LIGHT


def qss(p: Palette) -> str:
    return f"""
* {{ font-size: 13px; color: {p.text}; }}
QMainWindow, QWidget#Root, QWidget#Page {{ background: {p.bg}; }}
QScrollArea#PageScroll {{ background: {p.bg}; border: none; }}
QScrollArea#PageScroll > QWidget > QWidget {{ background: {p.bg}; }}
QWidget#Sidebar {{ background: {p.sidebar}; border-right: 1px solid {p.border}; }}
QPushButton#NavButton {{ text-align: left; padding: 0 {2*SP}px; min-height: {MIN_TARGET}px; border: none;
    border-radius: {R_SM}px; background: transparent; font-weight: 500; }}
QPushButton#NavButton:hover {{ background: {p.hover}; }}
QPushButton#NavButton:checked {{ background: {p.accent}; color: {p.accent_text}; }}
QLabel#Title {{ font-size: 26px; font-weight: 700; }}
QLabel#Subtitle {{ color: {p.subtext}; }}
QLabel#Hint {{ color: {p.subtext}; font-size: 12px; }}
QLabel#Error {{ color: {p.danger}; }}
QLabel#StatValue {{ font-size: 24px; font-weight: 700; }}
QFrame#Card, QFrame#Panel {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: {R_MD}px; }}
QPushButton {{ min-height: {MIN_TARGET}px; padding: 0 {2*SP}px; border-radius: {R_SM}px; border: 1px solid {p.border};
    background: {p.surface}; font-weight: 500; }}
QPushButton:hover {{ background: {p.hover}; }}
QPushButton:disabled {{ color: {p.subtext}; background: {p.bg}; }}
QPushButton#Primary {{ background: {p.accent}; color: {p.accent_text}; border: none; }}
QPushButton#Primary:hover {{ background: #3395FF; }}
QPushButton#Primary:disabled {{ background: {p.border}; color: {p.subtext}; }}
QPushButton#Danger {{ color: {p.danger}; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ min-height: {MIN_TARGET - 8}px; padding: 0 {SP + 4}px;
    border: 1px solid {p.border}; border-radius: {R_SM}px; background: {p.surface}; selection-background-color: {p.accent}; }}
QPlainTextEdit {{ padding: {SP}px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 2px solid {p.accent}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QTableWidget, QTreeWidget, QListWidget {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: {R_MD}px;
    gridline-color: transparent; alternate-background-color: {p.bg}; outline: 0; }}
QTableWidget::item, QTreeWidget::item, QListWidget::item {{ padding: 6px; }}
QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected {{ background: {p.accent}; color: {p.accent_text}; }}
QHeaderView::section {{ background: {p.surface}; border: none; border-bottom: 1px solid {p.border}; padding: 8px; color: {p.subtext}; font-weight: 600; }}
QProgressBar {{ border: none; border-radius: 4px; background: {p.border}; max-height: 8px; min-height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 4px; background: {p.accent}; }}
QCheckBox {{ spacing: {SP}px; min-height: {MIN_TARGET - 12}px; }}
QScrollBar:vertical {{ width: 10px; background: transparent; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolTip {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; padding: 6px; }}
QDialog {{ background: {p.bg}; }}
"""


def apply_theme(app: QApplication, theme: str) -> Palette:
    p = resolve_palette(theme, app)
    f = QFont()
    f.setFamilies(FONT_STACK)
    f.setPixelSize(13)
    app.setFont(f)
    app.setStyleSheet(qss(p))
    return p


def qcolor(hex_: str, alpha: int = 255) -> QColor:
    c = QColor(hex_)
    c.setAlpha(alpha)
    return c
