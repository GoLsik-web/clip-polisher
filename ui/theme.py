"""theme.py — палитра и QSS по UX-эталону (docs/ui-reference.html).

В Qt нет CSS-переменных, поэтому:
  - PALETTE хранит цвета тем (dark/light);
  - build_qss(theme) собирает глобальный стиль с подставленными цветами;
  - step_qss(color) — небольшой стиль для контролов активного окна в его цвете
    (каждому шагу мастера свой цвет — см. STEP_COLORS).

Цвета зон (ZONE_COLORS) постоянны во всём приложении.
"""
from __future__ import annotations

# Цвета окон 1–6 мастера (мягкая разноцветность).
STEP_COLORS = ["#7c5cff", "#5590ff", "#37c9c2", "#57d081", "#f0a93a", "#ff6ba6"]

# Постоянные цвета зон (вебка/геймплей/субтитры/ник/платформа).
ZONE_COLORS = {
    "cam": "#ff5aa0",
    "game": "#5590ff",
    "sub": "#f4b13f",
    "brand": "#57d081",
    "plat": "#ff8149",
}

PALETTE = {
    "dark": {
        "bg": "#08080d", "panel": "#111019", "panel2": "#191826", "line": "#2a2740",
        "text": "#f9f8ff", "muted": "#c2bde0", "accent": "#7c5cff", "accent_ink": "#ffffff",
        "dot": "rgba(124,92,255,0.10)", "glow": (124, 92, 255),
    },
    "light": {
        "bg": "#edeaf6", "panel": "#ffffff", "panel2": "#f3f1fb", "line": "#e2ddf1",
        "text": "#0d0b14", "muted": "#443f58", "accent": "#6a3cff", "accent_ink": "#ffffff",
        "dot": "rgba(106,60,255,0.08)", "glow": (124, 92, 255),
    },
}


def p(theme: str) -> dict:
    return PALETTE.get(theme, PALETTE["dark"])


def build_qss(theme: str) -> str:
    """Глобальный QSS с подставленными цветами темы."""
    c = p(theme)
    return f"""
    QWidget {{
        color: {c['text']};
        font-family: "Segoe UI", "Inter", sans-serif;
        font-size: 14px;
    }}
    QMainWindow, #rootBg {{ background: {c['bg']}; }}

    /* Панели-карточки */
    QFrame#card, QFrame#panel {{
        background: {c['panel']};
        border: 1px solid {c['line']};
        border-radius: 12px;
    }}
    QFrame#panel2 {{ background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 10px; }}

    /* Топбар */
    #topbar {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 12px; }}
    #logo {{ font-weight: 800; letter-spacing: 1.5px; }}
    #tag {{ color: {c['muted']}; border: 1px solid {c['line']}; border-radius: 999px;
            padding: 2px 8px; font-size: 11px; font-weight: 600; }}

    /* Рейка режимов */
    #rail {{ background: {c['panel2']}; border: 1px solid {c['line']};
             border-top: 3px solid {c['accent']}; border-radius: 16px; }}
    #railHead {{ color: {c['text']}; font-weight: 800; font-size: 12px; }}

    QPushButton.mode {{
        text-align: left; background: {c['panel']}; border: 1px solid {c['line']};
        border-radius: 12px; padding: 10px; color: {c['text']};
    }}
    QPushButton.mode:hover {{ border-color: {c['accent']}; }}
    QPushButton.mode[on="true"] {{
        border-color: {c['accent']};
        background: {c['panel2']};
    }}

    /* Общие кнопки */
    QPushButton {{
        background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 8px;
        padding: 9px 15px; color: {c['text']}; font-weight: 700; font-size: 13px;
    }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton.primary {{ background: {c['accent']}; color: {c['accent_ink']}; border-color: {c['accent']}; }}

    /* Поля ввода */
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 8px;
        padding: 7px 10px; color: {c['text']};
    }}
    QComboBox::drop-down {{ border: 0; }}
    QComboBox QAbstractItemView {{
        background: {c['panel']}; color: {c['text']};
        selection-background-color: {c['accent']}; border: 1px solid {c['line']};
    }}

    /* Шаги мастера */
    QFrame.wstep {{ background: {c['panel']}; border: 1px solid {c['line']}; border-radius: 14px; }}
    QLabel.wtitle {{ font-weight: 700; font-size: 14px; }}
    QLabel.wsub {{ color: {c['muted']}; font-size: 11px; }}
    QLabel.wstate {{ color: {c['muted']}; font-size: 11px; }}
    QLabel.wnum {{
        background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 8px;
        font-weight: 800; font-size: 12px; color: {c['muted']};
        min-width: 26px; max-width: 26px; min-height: 26px; max-height: 26px;
        qproperty-alignment: AlignCenter;
    }}
    QLabel.lab {{ font-weight: 600; font-size: 12px; }}
    QLabel.hint {{ color: {c['muted']}; font-size: 11px; }}

    /* Слайдеры */
    QSlider::groove:horizontal {{ height: 6px; background: {c['panel2']};
        border: 1px solid {c['line']}; border-radius: 3px; }}
    QSlider::handle:horizontal {{ width: 16px; margin: -6px 0; border-radius: 8px;
        background: {c['accent']}; }}

    /* Легенда/чипы базово */
    QFrame.leg {{ background: {c['panel2']}; border: 1px solid {c['line']}; border-radius: 8px; }}

    /* Превью-экраны */
    #albumScreen, #tiktokScreen {{ background: #1a1d25; border: 1px solid {c['line']}; border-radius: 12px; }}

    QScrollArea {{ border: 0; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['line']}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}

    QToolTip {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['line']};
                padding: 6px 9px; border-radius: 8px; }}
    """


def step_qss(color: str, accent_ink: str = "#ffffff") -> str:
    """Стиль контролов активного окна в ЕГО цвете (номер, кнопка «Далее», рамка).

    Применяется к контейнеру шага: цветит номер-бейдж, primary-кнопку, рамку.
    """
    return f"""
    QFrame.wstep[active="true"] {{ border: 2px solid {color}; }}
    QLabel.wnum[active="true"] {{ background: {color}; color: {accent_ink}; border-color: {color}; }}
    QLabel.wnum[done="true"] {{ color: {color}; border-color: {color}; }}
    QPushButton.primary {{ background: {color}; border-color: {color}; color: {accent_ink}; }}
    QSlider::handle:horizontal {{ background: {color}; }}
    """
