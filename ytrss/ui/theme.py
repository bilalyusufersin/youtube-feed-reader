"""NewsFlash esintili koyu (antrasit) tema, renk paleti ve rozet stilleri.

İlk açılışta sistem açık temada olsa bile koyu tema uygulanır.
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# Temel yüzey ve metin renkleri
BG_SIDEBAR = "#17191e"
BG_LIST = "#1d2026"
BG_DETAIL = "#15171c"
BG_HEADER = "#17191e"
BG_CARD_HOVER = "#252932"
BG_CARD_SELECTED = "#2b313d"
BG_INPUT = "#121418"
BORDER_SUBTLE = "#282c35"
TEXT_PRIMARY = "#e6e9ef"
TEXT_SECONDARY = "#9da5b4"
TEXT_MUTED = "#6b7280"
ACCENT_PRIMARY = "#e53935"
ACCENT_BLUE = "#4da3ff"
STAR_GOLD = "#f5b041"
WARN_AMBER = "#f59e0b"
ERR_RED = "#ef4444"

# Tür rozeti renkleri: (arka_plan, metin_rengi, kenarlık)
BADGE_COLORS: dict[str, tuple[str, str, str]] = {
    "video": ("#1e2d3d", "#7cc4fa", "#2b4259"),
    "short": ("#381f33", "#f48fb1", "#522d4b"),
    "live_now": ("#471a1c", "#ff7070", "#6e272a"),
    "live_scheduled": ("#382e18", "#ffd166", "#544524"),
    "live_completed": ("#29233d", "#c3a6ff", "#3e355c"),
    "premiere": ("#1b3533", "#64dfdf", "#29524e"),
    "unknown": ("#362c19", "#f59e0b", "#524326"),
    "watched": ("#163029", "#6ee7b7", "#234e42"),
    "watch_later": ("#1e293b", "#93c5fd", "#334155"),
}


DARK_QSS = f"""
QMainWindow, QDialog {{
    background-color: {BG_DETAIL};
    color: {TEXT_PRIMARY};
}}
QWidget {{
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT_BLUE};
    selection-color: #ffffff;
}}
QSplitter::handle {{
    background-color: {BORDER_SUBTLE};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}
QFrame#ColumnHeader {{
    background-color: {BG_HEADER};
    border-bottom: 1px solid {BORDER_SUBTLE};
}}
QFrame#SidebarPane {{
    background-color: {BG_SIDEBAR};
}}
QFrame#MiddlePane {{
    background-color: {BG_LIST};
}}
QFrame#DetailPane {{
    background-color: {BG_DETAIL};
}}
QFrame#InfoBox {{
    background-color: #1c1f26;
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 8px;
}}
QFrame#BannerBox {{
    background-color: #21252e;
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
}}
QTreeWidget#SidebarTree {{
    background-color: {BG_SIDEBAR};
    selection-background-color: {BG_SIDEBAR};
    border: none;
    outline: 0;
    padding: 4px 2px;
}}
QTreeWidget#SidebarTree::item {{
    background: transparent;
    border: none;
    padding: 0px;
    margin: 0px;
}}
QTreeWidget#SidebarTree::item:hover,
QTreeWidget#SidebarTree::item:selected,
QTreeWidget#SidebarTree::item:selected:active,
QTreeWidget#SidebarTree::item:selected:!active,
QTreeWidget#SidebarTree::branch:hover,
QTreeWidget#SidebarTree::branch:selected {{
    background: transparent;
    border: none;
}}
QListView#VideoListView {{
    background-color: {BG_LIST};
    border: none;
    outline: 0;
}}
QLineEdit, QComboBox, QSpinBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    padding: 5px 9px;
    color: {TEXT_PRIMARY};
    min-height: 20px;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 1px solid {ACCENT_BLUE};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: #1e222a;
    border: 1px solid {BORDER_SUBTLE};
    selection-background-color: { BG_CARD_SELECTED };
    color: {TEXT_PRIMARY};
}}
QPushButton {{
    background-color: #232730;
    border: 1px solid #303540;
    border-radius: 6px;
    padding: 5px 11px;
    color: {TEXT_PRIMARY};
}}
QPushButton:hover {{
    background-color: #2c313c;
    border-color: #3c4250;
}}
QPushButton:pressed {{
    background-color: #1c1f26;
}}
QPushButton:checked {{
    background-color: #2e3848;
    border-color: {ACCENT_BLUE};
    color: #ffffff;
}}
QPushButton#PrimaryButton {{
    background-color: #c62828;
    border: 1px solid #e53935;
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background-color: #d32f2f;
}}
QPushButton#FlatIconButton {{
    background-color: transparent;
    border: 1px solid transparent;
    padding: 4px 8px;
}}
QPushButton#FlatIconButton:hover {{
    background-color: #262a33;
    border-color: {BORDER_SUBTLE};
}}
QScrollArea {{
    background-color: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #343946;
    min-height: 24px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: #464d5e;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QMenu {{
    background-color: #1e222a;
    border: 1px solid {BORDER_SUBTLE};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 20px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {BG_CARD_SELECTED};
}}
QToolTip {{
    background-color: #111318;
    color: {TEXT_PRIMARY};
    border: 1px solid #333844;
    padding: 6px;
}}
"""


def apply_dark_theme(app: QtWidgets.QApplication) -> None:
    """Sistem açık temada olsa bile koyu Fusion paleti + QSS uygular."""
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(BG_DETAIL))
    pal.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(TEXT_PRIMARY))
    pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(BG_INPUT))
    pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(BG_LIST))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor("#111318"))
    pal.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(TEXT_PRIMARY))
    pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(TEXT_PRIMARY))
    pal.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor("#232730"))
    pal.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(TEXT_PRIMARY))
    pal.setColor(QtGui.QPalette.ColorRole.BrightText, QtCore.Qt.GlobalColor.white)
    pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(ACCENT_BLUE))
    pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtCore.Qt.GlobalColor.white)
    pal.setColor(QtGui.QPalette.ColorRole.PlaceholderText, QtGui.QColor(TEXT_MUTED))
    app.setPalette(pal)
    app.setStyleSheet(DARK_QSS)
