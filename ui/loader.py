"""loader.py — оверлей-«прикол» вместо консоли (эталон: docs/ui-reference.html).

Анимированная хлопушка + процент + подменяющиеся шутливые русские статусы,
привязанные к реальным этапам пайплайна. Неблокирующий (работа в QThread),
сворачиваемый (рендер продолжается в фоне).
"""
from __future__ import annotations

from PySide6.QtCore import (Qt, QPropertyAnimation, QEasingCurve, Property,
                            QRectF, Signal, QTimer)
from PySide6.QtGui import QPainter, QColor, QLinearGradient, QBrush
from PySide6.QtWidgets import (QWidget, QLabel, QFrame, QVBoxLayout, QPushButton,
                               QProgressBar)

# Шутливые статусы по ключевым словам реального этапа.
STAGE_JOKES = [
    ("Получени", "Тащу клип с Твича…"),
    ("качив", "Тащу клип с Твича…"),
    ("Распознав", "Слушаю, где ты орал громче всех…"),
    ("Whisper", "Слушаю, где ты орал громче всех…"),
    ("слов", "Разбираю по словечкам…"),
    ("Мат", "Пикаю маты, чтобы ютуб не плакал…"),
    ("Субтитры", "Прибиваю субтитры гвоздями…"),
    ("Рендер", "Собираю бургер… то есть клип"),
    ("Кодек", "Кормлю видеокарту…"),
    ("Готово", "Готово! Пальчики оближешь"),
]


class ClapBoard(QWidget):
    """Хлопушка: нижняя часть + хлопающая верхняя планка (анимация угла)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(96, 96)
        self._angle = 0.0
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setDuration(1000)
        self._anim.setLoopCount(-1)
        self._anim.setKeyValueAt(0.0, 0.0)
        self._anim.setKeyValueAt(0.3, -32.0)
        self._anim.setKeyValueAt(0.6, 2.0)
        self._anim.setKeyValueAt(1.0, 0.0)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)

    def start(self): self._anim.start()
    def stop(self): self._anim.stop()

    def get_angle(self) -> float: return self._angle
    def set_angle(self, v: float): self._angle = v; self.update()
    angle = Property(float, get_angle, set_angle)

    def paintEvent(self, _e) -> None:
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        # Низ (доска).
        grad = QLinearGradient(0, 30, 88, 88)
        grad.setColorAt(0, QColor("#7c5cff"))
        grad.setColorAt(1, QColor("#2a9c96"))
        pt.setPen(Qt.NoPen)
        pt.setBrush(QBrush(grad))
        pt.drawRoundedRect(QRectF(4, 34, 88, 58), 8, 8)
        # Верхняя планка (хлопок) — поворот вокруг левого края.
        pt.save()
        pt.translate(4, 26)
        pt.rotate(self._angle)
        pt.setBrush(QColor("#20242e"))
        pt.drawRoundedRect(QRectF(0, 0, 88, 22), 6, 6)
        # полоски хлопушки
        pt.setBrush(QColor("#f9f8ff"))
        for i in range(6):
            pt.drawRect(QRectF(4 + i * 15, 3, 7, 16))
        pt.restore()


class LoaderOverlay(QWidget):
    """Полупрозрачный оверлей с карточкой загрузчика."""
    minimized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("loaderOverlay")
        self.setAttribute(Qt.WA_StyledBackground, True)  # чтобы красился фон затемнения
        self.hide()

        # --- Сглаженный МОНОТОННЫЙ прогресс ---------------------------------
        # _target — реальная доля от пайплайна; _display — то, что видит глаз.
        # Таймер плавно подтягивает _display к _target и НИКОГДА не идёт назад
        # (убирает и «скачки», и откат 70→50). Пока цель не двигается (долгий
        # Whisper/старт ffmpeg) — лёгкий «дыхательный» дополз, чтобы не выглядело
        # зависшим, но с потолком чуть выше цели (не врём сильно).
        self._target = 0.0
        self._display = 0.0
        self._idle_ms = 0
        self._timer = QTimer(self)
        self._timer.setInterval(16)   # ~60 к/с
        self._timer.timeout.connect(self._tick)
        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignCenter)

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(360)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(30, 28, 30, 24)
        cl.setSpacing(6)
        cl.setAlignment(Qt.AlignCenter)

        from .burger import BurgerLoader
        self.clap = BurgerLoader()
        cl.addWidget(self.clap, alignment=Qt.AlignCenter)
        self.title = QLabel("Собираю клип…")
        self.title.setStyleSheet("font-size:16px;font-weight:800;")
        self.title.setAlignment(Qt.AlignCenter)
        self.msg = QLabel("Готовлюсь…")
        self.msg.setStyleSheet("color:#c2bde0;font-size:13px;")
        self.msg.setAlignment(Qt.AlignCenter)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setRange(0, 100)
        self.pct = QLabel("0%")
        self.pct.setStyleSheet("color:#c2bde0;font-size:12px;")
        self.pct.setAlignment(Qt.AlignCenter)
        self.close_btn = QPushButton("Свернуть")
        self.close_btn.clicked.connect(self._minimize)

        cl.addWidget(self.title)
        cl.addWidget(self.msg)
        cl.addSpacing(8)
        cl.addWidget(self.bar)
        cl.addWidget(self.pct)
        cl.addSpacing(6)
        cl.addWidget(self.close_btn, alignment=Qt.AlignCenter)
        root.addWidget(card)

        self.setStyleSheet("""
            #loaderOverlay { background: rgba(8,8,13,0.82); }
            QProgressBar { background:#191826; border:1px solid #2a2740; border-radius:6px; }
            QProgressBar::chunk { background:#7c5cff; border-radius:6px; }
        """)

    def start(self) -> None:
        if self.parent():
            self.setGeometry(self.parent().rect())
        self.raise_()
        self.show()
        self.clap.start()
        self._target = 0.0
        self._display = 0.0
        self._idle_ms = 0
        self.bar.setValue(0)
        self.pct.setText("0%")
        self.msg.setText("Готовлюсь…")
        self._timer.start()

    def set_progress(self, frac: float, stage: str) -> None:
        """Принять РЕАЛЬНУЮ долю из пайплайна. Монотонно (назад не откатываем)
        и без резких скачков — сам показ доводит таймер _tick."""
        frac = max(0.0, min(1.0, frac))
        if frac > self._target:
            self._target = frac
            self._idle_ms = 0        # цель сдвинулась — сбрасываем «дозаполнение»
        for key, joke in STAGE_JOKES:
            if key.lower() in stage.lower():
                self.msg.setText(joke)
                break

    def _tick(self) -> None:
        # Мягкое подтягивание к цели (экспон. сглаживание) — плавно и без рывков.
        gap = self._target - self._display
        if gap > 0.0005:
            self._display += gap * 0.14 + 0.0006
            self._idle_ms = 0
        else:
            # Цель стоит: лёгкий дополз к потолку чуть выше цели (не более +4%),
            # чтобы во время долгого этапа бар «дышал», а не висел мёртво.
            self._idle_ms += self._timer.interval()
            ceil = min(0.99, self._target + 0.04)
            if self._idle_ms > 300 and self._display < ceil:
                self._display += (ceil - self._display) * 0.02
        self._display = min(self._display, 1.0)
        pct = int(self._display * 100 + 0.5)
        self.bar.setValue(pct)
        self.pct.setText(f"{pct}%")
        self.clap.set_progress(self._display)

    def stop(self) -> None:
        self._timer.stop()
        self.clap.stop()
        self.hide()

    def finish(self) -> None:
        """Мгновенно долить бар до 100% перед закрытием (аккуратный финал)."""
        self._target = 1.0
        self._display = 1.0
        self.bar.setValue(100)
        self.pct.setText("100%")
        self.clap.set_progress(1.0)

    def _minimize(self) -> None:
        self.hide()
        self.minimized.emit()

    def resizeEvent(self, e):
        pass
