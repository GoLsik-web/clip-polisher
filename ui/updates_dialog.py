"""updates_dialog.py — окно «Обновления».

Показывает состояние: проверяю / у вас последняя версия / доступно обновление
(с «Что нового» из описания релиза) / нет сети. Кнопки «Обновить» и «Проверить
снова». Логику (проверка/скачивание) ведёт MainWindow через сигналы.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QTextEdit)


class UpdatesDialog(QDialog):
    recheck = Signal()
    do_update = Signal()

    def __init__(self, current_version: str, accent: str = "#37c9c2", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Обновления")
        self.setMinimumWidth(460)
        self._cur = current_version
        self._accent = accent

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 18)
        root.setSpacing(10)

        self.title = QLabel()
        self.title.setStyleSheet("font-size:17px;font-weight:800;")
        self.sub = QLabel()
        self.sub.setWordWrap(True)
        self.sub.setStyleSheet("color:#c2bde0;font-size:12px;")

        self.notes_label = QLabel("Что нового в этой версии:")
        self.notes_label.setStyleSheet("font-weight:700;margin-top:4px;")
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(180)
        self.notes.setStyleSheet(
            "QTextEdit{background:#151425;color:#ece9fb;border:1px solid #2a2740;"
            "border-radius:8px;padding:8px;}")

        row = QHBoxLayout()
        self.recheck_btn = QPushButton("Проверить снова")
        self.recheck_btn.clicked.connect(self.recheck.emit)
        self.update_btn = QPushButton("Обновить сейчас")
        self.update_btn.setStyleSheet(
            f"background:{accent};border:none;border-radius:8px;color:#06231f;"
            "padding:8px 16px;font-weight:800;")
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

        self.set_state("checking")

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
        self.recheck_btn.setEnabled(recheck_on)

    def set_downloading(self, text: str) -> None:
        """Пока идёт скачивание обновления — блокируем кнопки и показываем статус."""
        self.title.setText("Скачиваю обновление…")
        self.sub.setText(text)
        self.update_btn.setEnabled(False)
        self.recheck_btn.setEnabled(False)
