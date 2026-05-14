"""Soft UI theme + visual effects helpers."""

from __future__ import annotations

from typing import Dict, Tuple

from PyQt5.QtCore import QEvent, QObject
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDockWidget,
    QFrame,
    QGraphicsDropShadowEffect,
    QGroupBox,
    QLineEdit,
    QListWidget,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)


ThemeTokens = Dict[str, str]


_TOKENS: Dict[str, ThemeTokens] = {
    "light": {
        "window_bg": "#F2F4F7",
        "surface": "#FFFFFF",
        "surface_soft": "#F6F7F9",
        "surface_press": "#EEF1F5",
        "stroke": "rgba(0, 0, 0, 0.06)",
        "stroke_soft": "rgba(0, 0, 0, 0.04)",
        "text_primary": "#1E2430",
        "text_secondary": "#5D6572",
        "text_muted": "#8A93A3",
        "accent": "#3748CA",
        "accent_hover": "#4A59D8",
        "accent_press": "#2D3DB2",
        "accent_text": "#FFFFFF",
        "focus_ring": "rgba(55, 72, 202, 0.20)",
        "list_select": "rgba(55, 72, 202, 0.13)",
        "status_bg": "#F7F8FA",
        "toolbar_bg": "#F7F8FA",
        "danger": "#E44B5F",
        "warning": "#E59E2B",
        "success": "#28B57A",
    },
    "dark": {
        "window_bg": "#0A0F16",
        "surface": "#101925",
        "surface_soft": "#131D2A",
        "surface_press": "#0E1722",
        "stroke": "rgba(255, 255, 255, 0.08)",
        "stroke_soft": "rgba(255, 255, 255, 0.05)",
        "text_primary": "#E7EEF8",
        "text_secondary": "#B5C1D2",
        "text_muted": "#8D9DB3",
        "accent": "#3748CA",
        "accent_hover": "#4A59D8",
        "accent_press": "#2D3DB2",
        "accent_text": "#FFFFFF",
        "focus_ring": "rgba(55, 72, 202, 0.30)",
        "list_select": "rgba(55, 72, 202, 0.26)",
        "status_bg": "#0E1722",
        "toolbar_bg": "#0F1824",
        "danger": "#FF6779",
        "warning": "#F2BC62",
        "success": "#45CF94",
    },
}


_SHADOWS: Dict[str, Dict[str, Tuple[QColor, float, float, float]]] = {
    "light": {
        "card": (QColor(24, 35, 52, 28), 30.0, 0.0, 8.0),
        "panel": (QColor(24, 35, 52, 20), 24.0, 0.0, 6.0),
        "input": (QColor(24, 35, 52, 16), 16.0, 0.0, 4.0),
        "button": (QColor(24, 35, 52, 18), 18.0, 0.0, 5.0),
        "list": (QColor(24, 35, 52, 16), 18.0, 0.0, 4.0),
        "status": (QColor(24, 35, 52, 12), 12.0, 0.0, 2.0),
        "focus": (QColor(55, 72, 202, 88), 28.0, 0.0, 0.0),
    },
    "dark": {
        "card": (QColor(0, 0, 0, 76), 34.0, 0.0, 10.0),
        "panel": (QColor(0, 0, 0, 62), 28.0, 0.0, 8.0),
        "input": (QColor(0, 0, 0, 52), 18.0, 0.0, 4.0),
        "button": (QColor(0, 0, 0, 58), 20.0, 0.0, 5.0),
        "list": (QColor(0, 0, 0, 54), 20.0, 0.0, 4.0),
        "status": (QColor(0, 0, 0, 46), 14.0, 0.0, 2.0),
        "focus": (QColor(55, 72, 202, 118), 30.0, 0.0, 0.0),
    },
}


def _normalize_theme(theme: str) -> str:
    if str(theme).strip().lower() == "dark":
        return "dark"
    return "light"


def _build_stylesheet(t: ThemeTokens) -> str:
    return f"""
QMainWindow {{
    background-color: {t["window_bg"]};
}}

QWidget {{
    background-color: {t["window_bg"]};
    color: {t["text_primary"]};
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 14px;
}}

QFrame, QGroupBox {{
    background-color: {t["surface"]};
}}

QGroupBox {{
    border: 1px solid {t["stroke"]};
    border-radius: 14px;
    margin-top: 14px;
    padding: 14px 14px 12px 14px;
    font-weight: 600;
    color: {t["text_primary"]};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {t["text_secondary"]};
    background: transparent;
}}

QFrame#statusCard {{
    border: 1px solid {t["stroke"]};
    border-radius: 14px;
    background: {t["surface"]};
}}

QFrame#globalStatusBar {{
    border: 1px solid {t["stroke_soft"]};
    border-radius: 12px;
    background: {t["status_bg"]};
}}

QFrame#globalStatusBar QLabel {{
    color: {t["text_secondary"]};
    font-weight: 600;
}}

QLabel#statusCardTitle {{
    color: {t["text_muted"]};
    font-size: 12px;
}}

QLabel#statusCardValue {{
    color: {t["text_primary"]};
    font-size: 20px;
    font-weight: 700;
}}

QLabel#statusCardDetail {{
    color: {t["text_secondary"]};
    font-size: 12px;
}}

QLabel#appTitleLabel {{
    color: {t["text_primary"]};
    font-size: 17px;
    font-weight: 700;
    background: transparent;
}}

QLabel#headerStateLabel {{
    color: {t["text_secondary"]};
    font-size: 14px;
    font-weight: 700;
    background: transparent;
    padding: 0 8px;
}}

QLabel#tcpSummaryLabel {{
    color: {t["text_primary"]};
    font-size: 15px;
    font-weight: 700;
    background: transparent;
}}

QLabel#hintLabel {{
    color: {t["text_muted"]};
    font-size: 12px;
    background: transparent;
}}

QLabel#jointValueLabel, QLabel#poseValueLabel {{
    color: {t["text_primary"]};
    font-weight: 700;
    background: transparent;
}}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    min-height: 44px;
    border-radius: 10px;
    border: 1px solid {t["stroke"]};
    background-color: {t["surface"]};
    color: {t["text_primary"]};
    padding: 0 12px;
}}

QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border: 1px solid {t["stroke_soft"]};
    background-color: {t["surface_soft"]};
}}

QLineEdit[softFocus="true"], QComboBox[softFocus="true"],
QSpinBox[softFocus="true"], QDoubleSpinBox[softFocus="true"],
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {t["focus_ring"]};
    background-color: {t["surface"]};
    padding: 0 11px;
}}

QComboBox::drop-down, QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    border: none;
    width: 24px;
    background: transparent;
}}

QComboBox QAbstractItemView {{
    background-color: {t["surface"]};
    border: 1px solid {t["stroke"]};
    border-radius: 10px;
    color: {t["text_primary"]};
    selection-background-color: {t["list_select"]};
}}

QPushButton {{
    min-height: 42px;
    border-radius: 10px;
    border: 1px solid {t["stroke"]};
    background: {t["surface_soft"]};
    color: {t["text_primary"]};
    font-weight: 600;
    padding: 0 14px;
}}

QPushButton:hover {{
    background: {t["surface"]};
    border: 1px solid {t["stroke_soft"]};
}}

QPushButton:pressed {{
    background: {t["surface_press"]};
    padding-top: 1px;
}}

QPushButton:disabled {{
    color: {t["text_muted"]};
    background: {t["surface_soft"]};
    border-color: {t["stroke_soft"]};
}}

QFrame#jogPadFrame {{
    background: transparent;
    border: none;
}}

QFrame#jogPadCenter {{
    background: {t["surface_soft"]};
    border: 1px solid {t["stroke"]};
    border-radius: 18px;
}}

QFrame#jogPadArc {{
    background: {t["surface_soft"]};
    border: 1px solid {t["stroke"]};
    border-radius: 20px;
}}

QPushButton#jogKeyBtn {{
    min-width: 58px;
    min-height: 58px;
    max-width: 58px;
    max-height: 58px;
    border-radius: 14px;
    font-size: 22px;
    font-weight: 700;
    padding: 0px;
}}

QPushButton#jogKeyBtn[jogStyle="line"] {{
    border: 1px solid {t["stroke"]};
    background: {t["surface"]};
    color: {t["accent"]};
}}

QPushButton#jogKeyBtn[jogStyle="line"]:hover {{
    border: 1px solid {t["stroke_soft"]};
    background: {t["surface_soft"]};
}}

QPushButton#jogKeyBtn[jogStyle="line"]:pressed {{
    border: 1px solid {t["stroke"]};
    background: {t["surface_press"]};
    padding-top: 1px;
}}

QPushButton#jogKeyBtn[jogStyle="line"]:disabled {{
    background: {t["surface_press"]};
    border-color: {t["stroke"]};
    color: {t["text_muted"]};
}}

QPushButton#jogKeyBtn[jogStyle="soft"] {{
    border: 1px solid {t["accent"]};
    background: {t["accent"]};
    color: {t["accent_text"]};
}}

QPushButton#jogKeyBtn[jogStyle="soft"]:hover {{
    background: {t["accent_hover"]};
    border-color: {t["accent_hover"]};
}}

QPushButton#jogKeyBtn[jogStyle="soft"]:pressed {{
    background: {t["accent_press"]};
    border-color: {t["accent_press"]};
    padding-top: 1px;
}}

QPushButton#jogKeyBtn[jogStyle="soft"]:disabled {{
    background: {t["surface_press"]};
    border-color: {t["stroke"]};
    color: {t["text_muted"]};
}}

QLabel#jogHintLabel {{
    background: transparent;
    color: {t["text_secondary"]};
    font-size: 12px;
    font-weight: 600;
}}

QPushButton#primaryBtn, QPushButton#navBtn:checked {{
    background: {t["accent"]};
    border: 1px solid {t["accent"]};
    color: {t["accent_text"]};
}}

QPushButton#primaryBtn:hover, QPushButton#navBtn:checked:hover {{
    background: {t["accent_hover"]};
    border-color: {t["accent_hover"]};
}}

QPushButton#primaryBtn:pressed, QPushButton#navBtn:checked:pressed {{
    background: {t["accent_press"]};
    border-color: {t["accent_press"]};
}}

QFrame#globalStatusBar QPushButton {{
    min-height: 30px;
    max-height: 32px;
    border-radius: 8px;
    padding: 0 12px;
    font-size: 13px;
}}

QFrame#globalStatusBar QPushButton#statusActionBtn {{
    background: {t["surface"]};
    border: 1px solid {t["stroke"]};
    color: {t["text_secondary"]};
}}

QFrame#globalStatusBar QPushButton#statusActionBtn:hover {{
    background: {t["surface_soft"]};
    color: {t["text_primary"]};
}}

QPushButton#navBtn {{
    min-width: 112px;
}}

QPushButton#connectBtn {{
    background: {t["success"]};
    border-color: {t["success"]};
    color: #FFFFFF;
}}

QPushButton#disconnectBtn, QPushButton#recordBtn {{
    background: {t["danger"]};
    border-color: {t["danger"]};
    color: #FFFFFF;
}}

QPushButton#homeBtn {{
    background: {t["warning"]};
    border-color: {t["warning"]};
    color: #FFFFFF;
}}

QPushButton#tileBtn {{
    border-radius: 14px;
    border: 1px solid {t["stroke"]};
    background: {t["surface"]};
    text-align: left;
    padding: 16px;
    font-size: 15px;
    min-height: 88px;
}}

QPushButton#tileBtn:hover {{
    background: {t["surface_soft"]};
}}

QTabWidget::pane {{
    border: 1px solid {t["stroke"]};
    border-radius: 14px;
    background: {t["surface"]};
    top: -1px;
}}

QTabBar::tab {{
    min-height: 36px;
    border-radius: 10px;
    border: 1px solid transparent;
    background: transparent;
    color: {t["text_secondary"]};
    padding: 0 14px;
    margin: 6px 6px 4px 0;
}}

QTabBar::tab:hover {{
    background: {t["surface_soft"]};
}}

QTabBar::tab:selected {{
    background: {t["surface"]};
    border-color: {t["stroke"]};
    color: {t["text_primary"]};
}}

QListWidget {{
    border: 1px solid {t["stroke"]};
    border-radius: 10px;
    background: {t["surface"]};
    padding: 6px;
}}

QListWidget::item {{
    border-radius: 8px;
    padding: 8px 10px;
}}

QListWidget::item:hover {{
    background: {t["surface_soft"]};
}}

QListWidget::item:selected {{
    background: {t["list_select"]};
    color: {t["text_primary"]};
}}

QTextEdit {{
    border: 1px solid {t["stroke"]};
    border-radius: 10px;
    background: {t["surface"]};
    color: {t["text_primary"]};
    padding: 10px;
}}

QTextEdit#logView {{
    font-family: "SF Mono", "Consolas", "Menlo", monospace;
    font-size: 12px;
    padding: 8px;
}}

QLabel#cameraLabel {{
    border: 1px solid {t["stroke"]};
    border-radius: 14px;
    background: {t["surface"]};
}}

QLabel#recLabel {{
    color: {t["danger"]};
    font-weight: 700;
}}

QToolBar {{
    border: 1px solid {t["stroke_soft"]};
    border-radius: 10px;
    background: {t["toolbar_bg"]};
    spacing: 6px;
    padding: 4px;
}}

QToolBar QToolButton {{
    border: 1px solid {t["stroke"]};
    border-radius: 8px;
    background: {t["surface"]};
    color: {t["text_primary"]};
    min-height: 30px;
    padding: 2px 10px;
}}

QToolBar QToolButton:hover {{
    background: {t["surface_soft"]};
}}

QDockWidget {{
    color: {t["text_primary"]};
}}

QDockWidget::title {{
    background: {t["surface_soft"]};
    color: {t["text_secondary"]};
    border: 1px solid {t["stroke"]};
    border-radius: 8px;
    padding: 6px 10px;
    text-align: left;
}}

QStatusBar {{
    border: 1px solid {t["stroke_soft"]};
    border-radius: 10px;
    background: {t["status_bg"]};
    color: {t["text_secondary"]};
}}

QStatusBar::item {{
    border: none;
}}

QMessageBox, QInputDialog {{
    background-color: {t["window_bg"]};
}}
"""


STYLE_SHEET_LIGHT = _build_stylesheet(_TOKENS["light"])
STYLE_SHEET_DARK = _build_stylesheet(_TOKENS["dark"])

# Backward compatible default.
STYLE_SHEET = STYLE_SHEET_LIGHT


def get_stylesheet(theme: str) -> str:
    if _normalize_theme(theme) == "dark":
        return STYLE_SHEET_DARK
    return STYLE_SHEET_LIGHT


def _repolish(widget: QWidget):
    style = widget.style()
    if style is None:
        return
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def apply_soft_shadow(widget: QWidget, level: str = "panel", theme: str = "light"):
    theme_key = _normalize_theme(theme)
    table = _SHADOWS[theme_key]
    if level not in table:
        level = "panel"

    color, blur, dx, dy = table[level]
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsDropShadowEffect):
        effect = QGraphicsDropShadowEffect(widget)
        widget.setGraphicsEffect(effect)

    effect.setBlurRadius(float(blur))
    effect.setOffset(float(dx), float(dy))
    effect.setColor(color)


class _FocusGlowFilter(QObject):
    def __init__(self, theme: str):
        super().__init__()
        self.theme = _normalize_theme(theme)

    def set_theme(self, theme: str):
        self.theme = _normalize_theme(theme)

    def _set_focus_state(self, widget: QWidget, focused: bool):
        widget.setProperty("softFocus", bool(focused))
        apply_soft_shadow(widget, "focus" if focused else "input", self.theme)
        _repolish(widget)

    def eventFilter(self, obj, event):
        if not isinstance(obj, QWidget):
            return False
        if event.type() == QEvent.FocusIn:
            self._set_focus_state(obj, True)
        elif event.type() == QEvent.FocusOut:
            self._set_focus_state(obj, False)
        return False


def _install_focus_glow(widget: QWidget, theme: str):
    filt = getattr(widget, "_soft_focus_filter", None)
    if not isinstance(filt, _FocusGlowFilter):
        filt = _FocusGlowFilter(theme)
        widget._soft_focus_filter = filt
        widget.installEventFilter(filt)
    else:
        filt.set_theme(theme)

    widget.setProperty("softFocus", bool(widget.hasFocus()))
    apply_soft_shadow(widget, "focus" if widget.hasFocus() else "input", theme)
    _repolish(widget)


def apply_soft_effects(root: QWidget, theme: str = "light"):
    """Apply soft shadows + focus glow behaviors on top of QSS."""
    theme_key = _normalize_theme(theme)

    for widget in root.findChildren(QGroupBox):
        apply_soft_shadow(widget, "card", theme_key)
    for widget in root.findChildren(QFrame):
        if widget.objectName() == "statusCard":
            apply_soft_shadow(widget, "card", theme_key)
        elif widget.objectName() == "globalStatusBar":
            apply_soft_shadow(widget, "status", theme_key)
    for widget in root.findChildren(QTabWidget):
        apply_soft_shadow(widget, "panel", theme_key)
    for widget in root.findChildren(QListWidget):
        apply_soft_shadow(widget, "list", theme_key)
    for widget in root.findChildren(QPushButton):
        apply_soft_shadow(widget, "button", theme_key)
    for widget in root.findChildren(QDockWidget):
        apply_soft_shadow(widget, "panel", theme_key)
    for widget in root.findChildren(QToolBar):
        apply_soft_shadow(widget, "panel", theme_key)
    for widget in root.findChildren(QStatusBar):
        apply_soft_shadow(widget, "status", theme_key)

    for widget in root.findChildren(QLineEdit):
        _install_focus_glow(widget, theme_key)
    for widget in root.findChildren(QComboBox):
        _install_focus_glow(widget, theme_key)
    for widget in root.findChildren(QAbstractSpinBox):
        _install_focus_glow(widget, theme_key)


__all__ = [
    "STYLE_SHEET",
    "STYLE_SHEET_LIGHT",
    "STYLE_SHEET_DARK",
    "apply_soft_effects",
    "apply_soft_shadow",
    "get_stylesheet",
]
