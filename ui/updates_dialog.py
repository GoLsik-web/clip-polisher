"""updates_dialog.py — окно «Обновления».

Показывает состояние: проверяю / у вас последняя версия / доступно обновление
(с «Что нового» из описания релиза) / нет сети. Кнопки «Обновить» и «Проверить
снова». Логику (проверка/скачивание) ведёт MainWindow через сигналы.

Стиль полностью theme-aware: цвета берутся из PALETTE (theme.py), захардкоженных
цветов нет — окно выглядит одинаково «своим» и на тёмной, и на светлой теме.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QTextEdit)

from .theme import p as theme_palette


def _mix(hex_a: str, hex_b: str, t: float) -> str:
    """Смешать два #rrggbb в пропорции t (0→a, 1→b) — для лёгкого фона плашки."""
    a = tuple(int(hex_a[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(hex_b[i:i + 2], 16) for i in (1, 3, 5))
    m = tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))
    return "#{:02x}{:02x}{:02x}".format(*m)


class UpdatesDialog(QDialog):
    recheck = Signal()
    do_update = Signal()

    def __init__(self, current_version: str, theme: str = "dark",
                 accent: str = "#37c9c2", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Обновления")
        self.setMinimumWidth(460)
        self._cur = current_version
        self._accent = accent
        self._theme = theme

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)

        self.title = QLabel()
        self.title.setObjectName("updTitle")
        self.sub = QLabel()
        self.sub.setObjectName("updSub")
        self.sub.setWordWrap(True)

        self.notes_label = QLabel("Что нового в этой версии:")
        self.notes_label.setObjectName("updNotesLabel")
        self.notes = QTextEdit()
        self.notes.setObjectName("updNotes")
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(180)

        row = QHBoxLayout()
        self.recheck_btn = QPushButton("Проверить снова")
        self.recheck_btn.clicked.connect(self.recheck.emit)
        self.update_btn = QPushButton("Обновить сейчас")
        self.update_btn.setObjectName("updPrimary")
        self.update_btn.clicked.connect(self.do_update.emit)
        self.close_btn = QPushButton("Закрыть")
        self.close_btn.clicked.connect(self.reject)
        row.addWidget(self.recheck_btn)
        row.addStretch(1)
        row.addWidget(self.update_btn)
        row.addWidget(self.close_btn)

        root.addWidget(self.title)
        root.addWidget(self.sub)
        root.addWidget(self.notes_label)
        root.addWidget(self.notes, 1)
        root.addLayout(row)

        self.apply_theme(theme, accent)
        self.set_state("checking")

    def apply_theme(self, theme: str, accent: str | None = None) -> None:
        """Пересобрать стиль окна под тему (цвета из палитры, не захардкожено)."""
        self._theme = theme
        if accent:
            self._accent = accent
        c = theme_palette(theme)
        ac = self._accent
        # Тёмная подложка плашки «Что нового» — панель, чуть уведённая от фона.
        notes_bg = _mix(c["panel"], c["bg"], 0.35)
        # Контрастный текст на плашке-кнопке accent (тёмная краска на светлом бирюзовом).
        ink = "#06231f" if theme == "dark" else "#06231f"
        self.setStyleSheet(f"""
            QDialog {{ background: {c['bg']}; }}
            QLabel {{ color: {c['text']}; }}
            QLabel#updTitle {{ font-size: 17px; font-weight: 800; color: {c['text']}; }}
            QLabel#updSub {{ color: {c['muted']}; font-size: 12px; }}
            QLabel#updNotesLabel {{ font-weight: 700; margin-top: 4px; color: {c['text']}; }}
            QTextEdit#updNotes {{
                background: {notes_bg}; color: {c['text']};
                border: 1px solid {c['line']}; border-radius: 8px; padding: 8px;
            }}
            QPushButton {{
                background: {c['panel2']}; border: 1px solid {c['line']};
                border-radius: 8px; padding: 8px 16px; color: {c['text']};
                font-weight: 700;
            }}
            QPushButton:hover {{ border-color: {ac}; }}
            QPushButton:disabled {{ color: {c['muted']}; border-color: {c['line']}; }}
            QPushButton#updPrimary {{
                background: {ac}; border: none; color: {ink}; font-weight: 800;
            }}
            QPushButton#updPrimary:disabled {{ background: {c['line']}; color: {c['muted']}; }}
            QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: {c['line']}; border-radius: 5px;
                min-height: 30px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        """)

    def set_state(self, state: str, info: dict | None = None, error: str = "") -> None:
        show_notes = False
        show_update = False
        recheck_on = True
        if state == "checking":
            self.title.setText("Проверяю обновления…")
            self.sub.setText(f"Текущая версия: v{self._cur}")
            recheck_on = False
        elif state == "uptodate":
            self.title.setText("У вас последняя версия")
            self.sub.setText(f"Установлена v{self._cur} — это актуальная версия. "
                             "Обновлять нечего.")
        elif state == "update":
            ver = (info or {}).get("version", "?")
            self.title.setText(f"Доступна новая версия — v{ver}")
            self.sub.setText(f"У вас v{self._cur}. Нажми «Обновить сейчас» — приложение "
                             "само скачает её и установит поверх, заходить на сайт не нужно.")
            self.notes.setMarkdown((info or {}).get("notes") or "_описание отсутствует_")
            show_notes = True
            show_update = True
        else:  # error
            self.title.setText("Не удалось проверить обновления")
            self.sub.setText("Нет соединения с GitHub. Проверь интернет и нажми "
                             "«Проверить снова».\n" + (error or ""))

        self.notes_label.setVisible(show_notes)
        self.notes.setVisible(show_notes)
        self.update_btn.setVisible(show_update)
        self.update_btn.setEnabled(True)
        self.recheck_btn.setEnabled(recheck_on)

    def set_downloading(self, text: str) -> None:
        """Пока идёт скачивание обновления — блокируем кнопки и показываем статус."""
        self.title.setText("Скачиваю обновление…")
        self.sub.setText(text)
        self.update_btn.setEnabled(False)
        self.recheck_btn.setEnabled(False)
