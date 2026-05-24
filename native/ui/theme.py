"""Brand palette + global QSS for the native shell.

Colors mirror ``static/img/app-icon.svg`` so the in-app look stays consistent
with the icon and the README banner. Keep this file the single source of
truth — don't hard-code colors in stage widgets.
"""
from __future__ import annotations


BG_INK = "#0D1521"        # outer navy
BG_PANEL = "#162334"      # raised navy
BG_HOVER = "#1c2c42"      # +1 step
BORDER = "#243349"
INK_BRIGHT = "#F5FAFF"
INK = "#CBD6E1"
INK_MUTED = "#8c99ad"
INK_DIM = "#5a6a82"
ACCENT = "#7BD5E5"        # primary teal
ACCENT_DEEP = "#3FA9BD"
ACCENT_INK = "#0a1a22"    # text on accent buttons
PURPLE = "#B07BFF"
ERROR = "#ff7a8a"


GLOBAL_QSS = f"""
* {{
  font-family: "Segoe UI", "Inter", "SF Pro Text", "Helvetica Neue", sans-serif;
  color: {INK};
}}

QMainWindow,
QWidget#stage,
QStackedWidget,
QStatusBar {{
  background-color: {BG_INK};
}}

QMenuBar {{
  background-color: {BG_PANEL};
  border-bottom: 1px solid {BORDER};
  padding: 4px 8px;
}}
QMenuBar::item {{
  padding: 6px 12px;
  border-radius: 6px;
  color: {INK};
}}
QMenuBar::item:selected {{
  background-color: {BG_HOVER};
  color: {INK_BRIGHT};
}}
QMenu {{
  background-color: {BG_PANEL};
  border: 1px solid {BORDER};
  padding: 4px;
}}
QMenu::item {{
  padding: 6px 16px;
  border-radius: 6px;
}}
QMenu::item:selected {{
  background-color: {BG_HOVER};
  color: {INK_BRIGHT};
}}

QStatusBar {{
  background-color: {BG_PANEL};
  border-top: 1px solid {BORDER};
  color: {INK_MUTED};
  padding: 4px 12px;
}}
QStatusBar QLabel {{
  color: {INK_MUTED};
  background: transparent;
}}

QLabel {{
  background: transparent;
}}

QPushButton {{
  background-color: {BG_PANEL};
  border: 1px solid {BORDER};
  color: {INK_BRIGHT};
  padding: 8px 16px;
  border-radius: 6px;
}}
QPushButton:hover {{
  background-color: {BG_HOVER};
  border-color: {ACCENT_DEEP};
}}
QPushButton:pressed {{
  background-color: {BG_INK};
}}
QPushButton:disabled {{
  color: {INK_DIM};
  border-color: {BORDER};
}}
QPushButton[primary="true"] {{
  background-color: {ACCENT};
  border-color: {ACCENT};
  color: {ACCENT_INK};
  font-weight: 600;
}}
QPushButton[primary="true"]:hover {{
  background-color: {ACCENT_DEEP};
  border-color: {ACCENT_DEEP};
  color: {INK_BRIGHT};
}}

QLineEdit, QPlainTextEdit, QTextEdit {{
  background-color: {BG_PANEL};
  border: 1px solid {BORDER};
  border-radius: 6px;
  padding: 8px 10px;
  color: {INK_BRIGHT};
  selection-background-color: {ACCENT};
  selection-color: {ACCENT_INK};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
  border-color: {ACCENT};
}}

QSlider::groove:horizontal {{
  height: 6px;
  background-color: {BG_PANEL};
  border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
  background-color: {ACCENT_DEEP};
  border-radius: 3px;
}}
QSlider::handle:horizontal {{
  background-color: {ACCENT};
  width: 14px;
  height: 14px;
  margin: -4px 0;
  border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
  background-color: {INK_BRIGHT};
}}

QProgressBar {{
  background-color: {BG_PANEL};
  border: 1px solid {BORDER};
  border-radius: 6px;
  height: 16px;
  text-align: center;
  color: {INK_BRIGHT};
}}
QProgressBar::chunk {{
  background-color: {ACCENT_DEEP};
  border-radius: 5px;
}}

QListWidget {{
  background-color: {BG_PANEL};
  border: 1px solid {BORDER};
  border-radius: 8px;
  padding: 4px;
}}
QListWidget::item {{
  padding: 8px 10px;
  border-radius: 6px;
  color: {INK};
}}
QListWidget::item:selected {{
  background-color: {ACCENT_DEEP};
  color: {INK_BRIGHT};
}}
QListWidget::item:hover {{
  background-color: {BG_HOVER};
}}

QFrame#card {{
  background-color: {BG_PANEL};
  border: 1px solid {BORDER};
  border-radius: 12px;
}}
QFrame#dropzone {{
  background-color: {BG_PANEL};
  border: 2px dashed {BORDER};
  border-radius: 12px;
}}
QFrame#dropzone[hover="true"] {{
  border-color: {ACCENT};
  background-color: {BG_HOVER};
}}

QLabel[kicker="true"] {{
  color: {ACCENT};
  font-weight: 600;
  letter-spacing: 1px;
}}
QLabel[title="true"] {{
  color: {INK_BRIGHT};
  font-size: 20px;
  font-weight: 600;
}}
QLabel[hint="true"] {{
  color: {INK_MUTED};
}}
QLabel[mono="true"] {{
  font-family: "Cascadia Mono", "Consolas", "Menlo", monospace;
  color: {INK};
}}
"""
