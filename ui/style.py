"""Global fonts, palettes, and application style sheets."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

from .theme import shade_color


class UiStyle:
    # ─────────── fonts ───────────
    _BASE_SIZE  = 12
    FONT        = QFont("Roboto", _BASE_SIZE)
    TITLE_FONT  = QFont("Roboto", _BASE_SIZE + 8, QFont.Bold)
    FONT_H1     = TITLE_FONT
    FONT_H2     = QFont("Roboto", _BASE_SIZE + 4, QFont.Medium)
    FONT_BODY   = QFont("Roboto", _BASE_SIZE + 2)

    # ────────── colour helpers ──────────
    @staticmethod
    def _shade(hex_rgb: str, k: float) -> str:
        """Deprecated wrapper; use ``ui.theme.shade_color``."""
        return shade_color(hex_rgb, k)

    # ────────── QSS blocks (shared) ──────────
    _SCROLLBAR_QSS = r"""
        QScrollBar:vertical {{ background:transparent; width:10px; margin:4px 0; }}
        QScrollBar::handle:vertical {{
            background:{ACCENT}; border-radius:5px; min-height:24px;
        }}
        QScrollBar::handle:vertical:hover  {{ background:{ACCENT_DARK}; }}
        QScrollBar::handle:vertical:pressed{{ background:{ACCENT_DARKEST}; }}
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical     {{ height:0px; }}
    """

    _INPUT_QSS = r"""
        QCheckBox::indicator, QRadioButton::indicator {{ width:24px; height:24px; }}
        QCheckBox::indicator:checked,
        QRadioButton::indicator:checked {{
            background:{ACCENT}; border:1px solid {ACCENT_DARK};
        }}
        QSpinBox::up-button, QSpinBox::down-button {{ width:28px; height:20px; }}
    """

    # ───────────── ENHANCED DARK THEME (Blue-Gray Sophisticated) ─────────────
    _CORE_DARK_QSS = r"""
        QWidget {{ 
            background:#1A1D23; 
            color:#E8EAF0; 
            font-family:Roboto; 
        }}
        QPushButton {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #3A404B, stop:1 #2D3238);
            color:#E8EAF0; 
            border:1px solid #4A5568; 
            border-radius:6px; 
            padding:8px 16px;
            font-weight:500;
        }}
        QPushButton:hover {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #4A5568, stop:1 #3A404B);
            border:1px solid #5A6C7D;
        }}
        QPushButton:pressed {{ 
            background:{ACCENT}; 
            color:#FFFFFF; 
            border:1px solid {ACCENT_DARK};
        }}
        QPushButton[role="special"] {{ 
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 {ACCENT}, stop:1 {ACCENT_DARK}); 
            color:#FFFFFF; 
            border:none;
            font-weight:600;
        }}
        QPushButton[role="destructive"] {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #E53935, stop:1 #C62828);
            color:#FFFFFF; 
            border:none;
            font-weight:600;
        }}
        QListWidget, QTableWidget {{
            background:#252A32; 
            border:1px solid #3A404B;
            selection-background-color:{ACCENT}; 
            selection-color:#FFFFFF;
            alternate-background-color:#2A3038;
        }}
        QHeaderView::section {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #3A404B, stop:1 #2D3238);
            color:#E8EAF0;
            border:1px solid #4A5568;
            padding:8px;
            font-weight:400;
        }}
        QLineEdit, QTextEdit, QComboBox {{
            background:#2D3238; 
            border:1px solid #3A404B; 
            border-radius:4px; 
            padding:3px 4px;
            color:#E8EAF0;
        }}
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ 
            border:1px solid {ACCENT}; 
            background:#323842;
        }}
        QTabWidget::pane {{
            border:1px solid #3A404B;
            background:#252A32;
        }}
        QTabBar::tab {{
            background:#2D3238;
            color:#E8EAF0;
            border:1px solid #3A404B;
            padding:4px 8px;
            margin-right:2px;
        }}
        QTabBar::tab:selected {{
            background:{ACCENT};
            color:#FFFFFF;
            border-bottom:none;
        }}
        QGroupBox {{
            color:#E8EAF0;
            border:1px solid #3A404B;
            border-radius:6px;
            margin-top:10px;
            padding-top:10px;
            font-weight:400;
        }}
        QGroupBox::title {{
            subcontrol-origin:margin;
            left:10px;
            padding:0 8px 0 8px;
            background:#1A1D23;
        }}
    """

    # ───────────── ENHANCED LIGHT THEME (Warm Cream Sophisticated) ─────────────
    _CORE_LIGHT_QSS = r"""
        QWidget {{ 
            background:#F8F6F3; 
            color:#2C2A27; 
            font-family:Roboto; 
        }}
        QPushButton {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #FFFFFF, stop:1 #F5F3F0);
            color:#2C2A27; 
            border:1px solid #D4CFC7; 
            border-radius:6px; 
            padding:8px 16px;
            font-weight:500;
        }}
        QPushButton:hover {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #FEFEFE, stop:1 #F0EDE8);
            border:1px solid #C9C3BA;
        }}
        QPushButton:pressed {{ 
            background:{ACCENT}; 
            color:#FFFFFF; 
            border:1px solid {ACCENT_DARK};
        }}
        QPushButton[role="special"] {{ 
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 {ACCENT}, stop:1 {ACCENT_DARK}); 
            color:#FFFFFF; 
            border:none;
            font-weight:600;
        }}
        QPushButton[role="destructive"] {{ 
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #E53935, stop:1 #C62828); 
            color:#FFFFFF; 
            border:none;
            font-weight:600;
        }}
        QListWidget, QTableWidget {{
            background:#FFFFFF; 
            border:1px solid #E1DDD6;
            selection-background-color:{ACCENT}; 
            selection-color:#FFFFFF;
            alternate-background-color:#FDFCFA;
        }}
        QHeaderView::section {{
            background:qlineargradient(spread:pad, x1:0, y1:0, x2:0, y2:1, 
                stop:0 #F5F3F0, stop:1 #E8E4DE);
            color:#2C2A27;
            border:1px solid #D4CFC7;
            padding:8px;
            font-weight:400;
        }}
        QLineEdit, QTextEdit, QComboBox {{
            background:#FFFFFF; 
            border:1px solid #E1DDD6; 
            border-radius:3px; 
            padding:3px 4px;
            color:#2C2A27;
        }}
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{ 
            border:2px solid {ACCENT}; 
            background:#FEFEFE;
        }}
        QTabWidget::pane {{
            border:1px solid #E1DDD6;
            background:#FFFFFF;
        }}
        QTabBar::tab {{
            background:#F5F3F0;
            color:#2C2A27;
            border:1px solid #E1DDD6;
            padding:4px 8px;
            margin-right:2px;
        }}
        QTabBar::tab:selected {{
            background:{ACCENT};
            color:#FFFFFF;
            border-bottom:none;
        }}
        QGroupBox {{
            color:#2C2A27;
            border:2px solid #E1DDD6;
            border-radius:4px;
            margin-top:10px;
            padding-top:10px;
            font-weight:400;
        }}
        QGroupBox::title {{
            subcontrol-origin:margin;
            left:10px;
            padding:0 8px 0 8px;
            background:#F8F6F3;
        }}
    """

    # ───────────── PINK THEME (UNCHANGED except selection property) ─────────────
    _CORE_PINK_QSS = r"""
        QWidget {{ background:#FDEDEE; color:#4A4A4A; font-family:Roboto; }}
        QPushButton {{
            background:#F9D1D9; color:#4A4A4A;
            border:1px solid #E9A9B8; border-radius:4px; padding:6px 12px;
        }}
        QPushButton:hover        {{ background:#F6C3CE; }}
        QPushButton:pressed      {{ background:{ACCENT}; color:#FFF; }}
        QPushButton[role="special"] {{ background:#BFA2FF; color:#FFF; border:none; }}
        QPushButton[role="destructive"] {{ background:#FF4F79; color:#FFF; border:none; }}
        QListWidget, QTableWidget {{
            background:#F7D7DF; border:1px solid #E9A9B8;
            selection-background-color:{ACCENT}; selection-color:#FFF;
        }}
        QLineEdit, QTextEdit {{
            background:#FFFFFF; border:1px solid #E9A9B8; border-radius:3px; padding:4px;
        }}
        QLineEdit:focus, QTextEdit:focus {{ border:1px solid {ACCENT}; }}
    """

    @staticmethod
    def apply(app: QApplication, theme="dark", accent_color="#5C8DBC") -> None:
        """Apply palette + QSS to *app* (now supports enhanced dark/light themes)."""
        accent       = accent_color
        accent_dark  = shade_color(accent, 0.85)
        accent_drkst = shade_color(accent, 0.7)

        pal = QPalette()
        if theme == "light":
            # Enhanced warm cream light theme
            pal.setColor(QPalette.Window, QColor("#F8F6F3"))
            pal.setColor(QPalette.WindowText, QColor("#2C2A27"))
            pal.setColor(QPalette.Base, QColor("#FFFFFF"))
            pal.setColor(QPalette.Text, QColor("#2C2A27"))
            pal.setColor(QPalette.Button, QColor("#F5F3F0"))
            pal.setColor(QPalette.ButtonText, QColor("#2C2A27"))
            pal.setColor(QPalette.Highlight, QColor(accent))
            pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
            qss_core = UiStyle._CORE_LIGHT_QSS
        elif theme == "pink":
            # pastel-pink palette (UNCHANGED)
            pal.setColor(QPalette.Window, QColor("#FDEDEE"))
            pal.setColor(QPalette.WindowText, QColor("#4A4A4A"))
            pal.setColor(QPalette.Base, QColor("#FFFFFF"))
            pal.setColor(QPalette.Text, QColor("#4A4A4A"))
            pal.setColor(QPalette.Button, QColor("#F9D1D9"))
            pal.setColor(QPalette.ButtonText, QColor("#4A4A4A"))
            pal.setColor(QPalette.Highlight, QColor("#FF85A1"))   # rose
            pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
            # use rose family for accent overrides
            accent = "#FF85A1"; accent_dark = shade_color(accent, 0.85); accent_drkst = shade_color(accent, 0.7)
            qss_core = UiStyle._CORE_PINK_QSS
        else:   # dark (enhanced blue-gray theme)
            pal.setColor(QPalette.Window, QColor("#1A1D23"))
            pal.setColor(QPalette.WindowText, QColor("#E8EAF0"))
            pal.setColor(QPalette.Base, QColor("#252A32"))
            pal.setColor(QPalette.Text, QColor("#E8EAF0"))
            pal.setColor(QPalette.Button, QColor("#3A404B"))
            pal.setColor(QPalette.ButtonText, QColor("#E8EAF0"))
            pal.setColor(QPalette.Highlight, QColor(accent))
            pal.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
            qss_core = UiStyle._CORE_DARK_QSS

        app.setPalette(pal)
        app.setStyleSheet(
            (qss_core + UiStyle._SCROLLBAR_QSS + UiStyle._INPUT_QSS).format(
                ACCENT=accent,
                ACCENT_DARK=accent_dark,
                ACCENT_DARKEST=accent_drkst
            )
        )

__all__ = ["UiStyle"]
