"""theme.py - light/dark palettes, stylesheet, system-theme following, app icon."""
from __future__ import annotations

import os
from string import Template

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen,
                           QPixmap)
from PySide6.QtWidgets import QApplication, QWidget

import core

PALETTES = {
    "light": dict(
        bg="#F3F6F4", surface="#FFFFFF", surface2="#EDF1EE", border="#D5DCD7",
        text="#18211C", muted="#5E6A63",
        accent="#1B5E20", accent_hover="#14491A", accent_text="#333333", accent_soft="#E1F0E3",
        danger="#B3261E",
    ),
    "dark": dict(
        bg="#111412", surface="#1A1E1B", surface2="#242925", border="#333A35",
        text="#E7ECE8", muted="#98A39C",
        accent="#6FCF7B", accent_hover="#8BDE95", accent_text="#08200D", accent_soft="#203726",
        danger="#F2827A",
    ),
}

QSS = Template("""
QWidget { font-size: 13px; }
QMainWindow, QDialog, QWidget#root { background: $bg; }
QLabel { background: transparent; }
QLabel#muted { color: $muted; }
QLabel#h1 { font-size: 22px; font-weight: 700; }
QLabel#h2 { font-size: 20px; font-weight: 700; }
QLabel[tone="ok"] { color: $accent; }
QLabel[tone="err"] { color: $danger; }

QFrame#card { background: $surface; border: 1px solid $border; border-radius: 12px; }
QFrame#dropzone { background: $surface; border: 2px dashed $border; border-radius: 16px; }
QFrame#dropzone[drag="true"] { border-color: $accent; background: $accent_soft; }
QLabel#badge { background: $accent_soft; color: $accent; border-radius: 10px; font-weight: 700; font-size: 12px; }

QPushButton { background: $surface2; border: 1px solid $border; border-radius: 8px; padding: 8px 16px; }
QPushButton:hover { border-color: $accent; }
QPushButton:disabled { color: $muted; }
QPushButton#primary { background: $accent; color: $accent_text; border: 1px solid $accent; font-weight: 600; padding: 10px 24px; }
QPushButton#primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#primary:disabled { background: $surface2; color: $muted; border-color: $border; }
QPushButton#pill { background: $surface; border: 1px solid $border; border-radius: 15px; padding: 5px 14px; color: $muted; }
QPushButton#pill:hover { border-color: $accent; color: $text; }

QToolButton { background: transparent; border: 1px solid transparent; border-radius: 8px; padding: 6px 9px; }
QToolButton:hover { background: $surface2; border-color: $border; }
QToolButton::menu-indicator { image: none; }
QToolButton#link { color: $accent; font-weight: 600; }

QComboBox { background: $surface2; border: 1px solid $border; border-radius: 8px; padding: 6px 10px; min-height: 20px; }
QComboBox:hover { border-color: $accent; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow { image: url("$arrow"); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: $surface; border: 1px solid $border; outline: 0; padding: 4px;
    selection-background-color: $accent; selection-color: $accent_text; }

QLineEdit, QPlainTextEdit { background: $surface2; border: 1px solid $border; border-radius: 8px; padding: 6px 10px;
    selection-background-color: $accent; selection-color: $accent_text; }
QLineEdit:focus, QPlainTextEdit:focus { border-color: $accent; }

QProgressBar { background: $surface2; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; }
QProgressBar::chunk { background: $accent; border-radius: 4px; }

QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $border; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: $muted; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QMenu { background: $surface; border: 1px solid $border; padding: 4px; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 4px 8px; }

QTabWidget::pane { border: 1px solid $border; border-radius: 10px; top: -1px; background: $surface; }
QTabBar::tab { background: transparent; padding: 9px 18px; border-bottom: 2px solid transparent; color: $muted; }
QTabBar::tab:selected { color: $text; border-bottom-color: $accent; }
QTabBar::tab:hover { color: $text; }
""")


def make_palette(p: dict) -> QPalette:
    pal = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    pairs = {
        R.Window: p["bg"], R.WindowText: p["text"], R.Base: p["surface"], R.AlternateBase: p["surface2"],
        R.Text: p["text"], R.Button: p["surface2"], R.ButtonText: p["text"], R.ToolTipBase: p["surface"],
        R.ToolTipText: p["text"], R.Highlight: p["accent"], R.HighlightedText: p["accent_text"],
        R.PlaceholderText: p["muted"], R.Link: p["accent"], R.BrightText: p["text"],
    }
    for role, col in pairs.items():
        pal.setColor(role, QColor(col))
    for role in (R.Text, R.WindowText, R.ButtonText):
        pal.setColor(G.Disabled, role, QColor(p["muted"]))
    return pal


def set_titlebar_dark(widget: QWidget, dark: bool):
    """Make the native Windows title bar follow the theme (even if it differs from the OS)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old value)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:  # noqa: BLE001
        pass


class ThemeManager(QObject):
    changed = Signal(str)  # effective theme: "light" | "dark"

    def __init__(self, app: QApplication, mode: str = "system"):
        super().__init__(app)
        self.app, self.mode, self.effective = app, mode, "light"
        app.installEventFilter(self)
        hints = app.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: self.apply() if self.mode == "system" else None)
        self.apply()

    def system_scheme(self) -> str:
        try:
            return "dark" if self.app.styleHints().colorScheme() == Qt.ColorScheme.Dark else "light"
        except Exception:  # noqa: BLE001  (Qt < 6.5)
            return "light"

    def set_mode(self, mode: str):
        self.mode = mode
        self.apply()

    def _chevron(self, color: str, name: str) -> str:
        d = core.app_data_dir() / "icons"
        d.mkdir(exist_ok=True)
        path = d / f"chevron_{name}.png"
        pm = QPixmap(24, 24)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(6, 9), QPointF(12, 15), QPointF(18, 9)])
        p.end()
        pm.save(str(path), "PNG")
        return path.as_posix()

    def apply(self):
        self.effective = self.system_scheme() if self.mode == "system" else self.mode
        p = PALETTES[self.effective]
        self.app.setProperty("danger_color", p["danger"])
        self.app.setPalette(make_palette(p))
        self.app.setStyleSheet(QSS.substitute(p, arrow=self._chevron(p["muted"], self.effective)))
        for w in QApplication.topLevelWidgets():
            if w.isVisible():
                set_titlebar_dark(w, self.effective == "dark")
        self.changed.emit(self.effective)

    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            set_titlebar_dark(obj, self.effective == "dark")
        return False


# --------------------------------------------------------------------------- #
def _logo(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor("#2E7D32"))
    g.setColorAt(1, QColor("#1B5E20"))
    p.setBrush(g)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    tri = QPainterPath()
    tri.moveTo(size * 0.38, size * 0.27)
    tri.lineTo(size * 0.38, size * 0.73)
    tri.lineTo(size * 0.74, size * 0.50)
    tri.closeSubpath()
    p.setBrush(QColor("white"))
    p.drawPath(tri)
    p.end()
    return pm


def make_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_logo(s))
    return icon
