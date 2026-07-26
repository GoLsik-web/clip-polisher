"""clip_strip.py — лента клипов мульти-редактора (слева от редактора).

Мульти-редактор: список клипов, у каждого свои зоны/обрезка (правки под клип),
поверх общего шаблона (стиль субтитров/платформа/экспорт — из мастера). Клик по
карточке грузит клип в основной редактор; обработка идёт очередью, статус каждого
клипа виден на его карточке (ожидает / идёт / готово / ошибка).

Только виджеты; вся логика (загрузка, рендер) — в MainWindow.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen
from PySide6.QtWidgets import (QWidget, QLabel, QFrame, QVBoxLayout, QHBoxLayout,
                               QPushButton, QScrollArea, QSizePolicy)


class ClipItem:
    """Один клип в мульти-редакторе: источник + индивидуальные правки + статус."""
    def __init__(self, source: str, duration: float = 0.0, thumb: str = ""):
        self.source = source
        self.name = os.path.basename(source) or source
        self.duration = duration
        self.start = 0.0
        self.end = duration
        self.comp = None            # core.config.Composition (ставит MainWindow)
        self.nick: Optional[str] = None   # переопределение ника (None → из шаблона)
        self.thumb = thumb
        self.status = "pending"     # pending | processing | done | error
        self.frac = 0.0
        self.out_path = ""


_STATUS = {
    "pending":    ("Ожидает", "#8a86ad"),
    "processing": ("Идёт…",   "#37c9c2"),
    "done":       ("Готово",  "#57d081"),
    "error":      ("Ошибка",  "#f0a93a"),
}


class ClipCard(QFrame):
    """Карточка клипа: миниатюра + имя + длительность + статус/прогресс + удалить."""
    clicked = Signal()
    remove = Signal()

    def __init__(self, item: ClipItem, index: int, parent=None):
        super().__init__(parent)
        self.item = item
        self.index = index
        self._active = False
        self._pal = {"panel": "#191826", "panel2": "#141320", "line": "#2a2740",
                     "text": "#f9f8ff", "muted": "#c2bde0", "accent": "#7c5cff"}
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(126)

        root = QVBoxLayout(self); root.setContentsMargins(8, 8, 8, 8); root.setSpacing(4)
        top = QHBoxLayout(); top.setContentsMargins(0, 0, 0, 0)
        self.num = QLabel(str(index + 1)); self.num.setFixedSize(18, 18)
        self.num.setAlignment(Qt.AlignCenter)
        top.addWidget(self.num); top.addStretch(1)
        self.del_btn = QPushButton("✕"); self.del_btn.setFixedSize(18, 18)
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.clicked.connect(lambda: self.remove.emit())
        top.addWidget(self.del_btn)
        root.addLayout(top)

        self.thumb = QLabel(); self.thumb.setFixedHeight(58)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("background:#0d0f14;border-radius:6px;")
        self._set_thumb()
        root.addWidget(self.thumb)

        self.name = QLabel(); self.name.setWordWrap(False)
        self.name.setText(self._elide(item.name))
        root.addWidget(self.name)

        self.status = QLabel()
        root.addWidget(self.status)

        self._restyle()
        self.refresh()

    # ---- содержимое ------------------------------------------------------

    def _elide(self, text: str) -> str:
        return text if len(text) <= 18 else text[:16] + "…"

    def _set_thumb(self) -> None:
        if self.item.thumb and os.path.isfile(self.item.thumb):
            pix = QPixmap(self.item.thumb)
            if not pix.isNull():
                self.thumb.setPixmap(pix.scaled(QSize(200, 58), Qt.KeepAspectRatio,
                                                Qt.SmoothTransformation))
                return
        self.thumb.setText("кадр…")

    def refresh(self) -> None:
        self.item.name = self.item.name
        self.name.setText(self._elide(self.item.name))
        self._set_thumb()
        label, color = _STATUS.get(self.item.status, _STATUS["pending"])
        dur = self.item.duration
        extra = f" · {int(dur//60):d}:{int(dur%60):02d}" if dur > 0 else ""
        if self.item.status == "processing" and self.item.frac > 0:
            label = f"Идёт… {int(self.item.frac * 100)}%"
        self.status.setText(f"● {label}{extra}")
        self.status.setStyleSheet(f"color:{color};font-size:10px;font-weight:600;")

    def set_active(self, on: bool) -> None:
        self._active = on
        self._restyle()

    def set_palette(self, pal: dict) -> None:
        self._pal = pal
        self._restyle()

    def _restyle(self) -> None:
        p = self._pal
        border = p["accent"] if self._active else p["line"]
        bw = 2 if self._active else 1
        self.setStyleSheet(
            f"ClipCard{{background:{p['panel'] if self._active else p['panel2']};"
            f"border:{bw}px solid {border};border-radius:10px;}}")
        self.num.setStyleSheet(
            f"background:{p['accent'] if self._active else p['line']};"
            f"color:#fff;border-radius:9px;font-size:10px;font-weight:800;")
        self.name.setStyleSheet(f"color:{p['text']};font-size:11px;font-weight:700;")
        self.del_btn.setStyleSheet(
            f"QPushButton{{background:transparent;border:none;color:{p['muted']};"
            f"font-size:11px;font-weight:800;}} QPushButton:hover{{color:#ff6b6b;}}")

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit()


class ClipStrip(QWidget):
    """Колонка-лента миниатюр клипов слева от редактора + кнопка «Добавить»."""
    add_requested = Signal()
    selected = Signal(int)
    removed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(164)
        self._pal = {"panel": "#191826", "panel2": "#141320", "line": "#2a2740",
                     "text": "#f9f8ff", "muted": "#c2bde0", "accent": "#7c5cff"}
        self._cards: list[ClipCard] = []
        self._active = -1

        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(8)
        self.head = QLabel("Клипы")
        self.head.setStyleSheet("font-weight:800;font-size:12px;")
        root.addWidget(self.head)

        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget(); holder.setAttribute(Qt.WA_TranslucentBackground, True)
        self._list = QVBoxLayout(holder); self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8); self._list.addStretch(1)
        self.scroll.setWidget(holder)
        self.scroll.setStyleSheet("QScrollArea{background:transparent;border:0;}")
        root.addWidget(self.scroll, 1)

        self.add_btn = QPushButton("+ Добавить клипы")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(lambda: self.add_requested.emit())
        root.addWidget(self.add_btn)

        self.empty = QLabel("Пусто. Жми «+ Добавить клипы» — они появятся здесь.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        root.insertWidget(1, self.empty)
        self._restyle()

    def set_palette(self, pal: dict) -> None:
        self._pal = pal
        for c in self._cards:
            c.set_palette(pal)
        self._restyle()

    def _restyle(self) -> None:
        p = self._pal
        self.head.setStyleSheet(f"color:{p['text']};font-weight:800;font-size:12px;")
        self.empty.setStyleSheet(f"color:{p['muted']};font-size:11px;")
        self.add_btn.setStyleSheet(
            f"QPushButton{{background:{p['panel2']};border:1px dashed {p['accent']};"
            f"border-radius:9px;padding:9px;color:{p['text']};font-weight:700;font-size:12px;}}"
            f"QPushButton:hover{{background:{p['panel']};}}")

    def rebuild(self, items: list) -> None:
        """Пересобрать карточки из списка ClipItem."""
        for c in self._cards:
            c.setParent(None); c.deleteLater()
        self._cards = []
        # убрать все виджеты кроме финального stretch
        for i, item in enumerate(items):
            card = ClipCard(item, i)
            card.set_palette(self._pal)
            card.clicked.connect(lambda idx=i: self.selected.emit(idx))
            card.remove.connect(lambda idx=i: self.removed.emit(idx))
            self._list.insertWidget(self._list.count() - 1, card)
            self._cards.append(card)
        self.empty.setVisible(len(items) == 0)
        self.set_active(self._active)

    def set_active(self, index: int) -> None:
        self._active = index
        for i, c in enumerate(self._cards):
            c.set_active(i == index)

    def update_card(self, index: int) -> None:
        if 0 <= index < len(self._cards):
            self._cards[index].refresh()

    def refresh_all(self) -> None:
        for c in self._cards:
            c.refresh()

    def set_header(self, text: str, busy: bool = False) -> None:
        """Шапка ленты: «Клипы» в покое или «Обработка N/M · P%» во время очереди."""
        self.head.setText(text)
        p = self._pal
        color = p["accent"] if busy else p["text"]
        self.head.setStyleSheet(f"color:{color};font-weight:800;font-size:12px;")

    def set_add_enabled(self, on: bool) -> None:
        self.add_btn.setEnabled(on)
