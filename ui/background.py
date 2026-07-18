"""background.py — анимированный фон под контентом (эталон: docs/ui-reference.html).

Рисует:
  - фон-заливку темы;
  - еле заметную точечную сетку (кэш-тайл, дёшево);
  - ОДНУ размытую сферу, которая:
      * гуляет ТОЛЬКО по свободным боковым полям (гуттерам), не под интерфейсом;
      * плавно летит вертикально (time-based, ~110 px/с), уходит за край → респавн;
      * плавно ПЕРЕКРАШИВАЕТСЯ в цвет активного окна.
Сфера — кэшированный radial-gradient пиксмап (мягкий → выглядит размытым), без
пересчёта блёра каждый кадр. Позиция/цвет обновляются по таймеру.

Виджет лежит ПОД контентом (отдельный слой), не перехватывает мышь.
"""
from __future__ import annotations

import random

from PySide6.QtCore import Qt, QTimer, QElapsedTimer, QRect, QPointF
from PySide6.QtGui import QPainter, QColor, QRadialGradient, QPixmap, QBrush
from PySide6.QtWidgets import QWidget

SPEED = 110.0        # px/сек
GUTTER_MIN = 90      # минимальная ширина свободного поля


class AnimatedBackground(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._bg = QColor("#08080d")
        self._dot = QColor(124, 92, 255, 26)
        self._content = QRect(0, 0, 0, 0)
        self.reduced = False

        # Сфера.
        self._cur = [124.0, 92.0, 255.0]
        self._tgt = [124.0, 92.0, 255.0]
        self._x = 0.0
        self._y = 0.0
        self._vy = SPEED
        self._size = 360
        self._margin = 220
        self._sphere_pm: QPixmap | None = None
        self._sphere_key = None
        self._dot_tile: QPixmap | None = None

        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._last = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)  # ~60 fps
        self._spawn()

    # ---- Публичное API ---------------------------------------------------

    def set_palette(self, bg: str, dot_rgba: tuple[int, int, int, int]) -> None:
        self._bg = QColor(bg)
        self._dot = QColor(*dot_rgba)
        self._dot_tile = None
        self.update()

    def set_content_rect(self, r: QRect) -> None:
        """Область контента (центр), чтобы сфера гуляла только по гуттерам."""
        self._content = r

    def set_target_color(self, hex_color: str) -> None:
        col = QColor(hex_color)
        self._tgt = [float(col.red()), float(col.green()), float(col.blue())]

    # ---- Анимация --------------------------------------------------------

    def _gutters(self) -> list[float]:
        w = self.width()
        r = self._content
        g = []
        if r.left() > GUTTER_MIN:
            g.append(r.left() * 0.5)
        if w - r.right() > GUTTER_MIN:
            g.append((r.right() + w) * 0.5)
        return g

    def _spawn(self) -> None:
        w, h = self.width(), self.height()
        r = self._content
        gW = max(GUTTER_MIN, min(r.left() if r.left() > 0 else GUTTER_MIN,
                                 (w - r.right()) if (w - r.right()) > 0 else GUTTER_MIN))
        self._size = int(max(320, min(gW * 2.4, 700)))
        self._margin = self._size / 2 + 40
        gutters = self._gutters()
        if gutters:
            self._x = random.choice(gutters)
        else:
            self._x = r.left() * 0.5 if random.random() < 0.5 else (r.right() + w) * 0.5
        if random.random() < 0.5:
            self._y = -self._margin
            self._vy = SPEED
        else:
            self._y = h + self._margin
            self._vy = -SPEED

    def _tick(self) -> None:
        now = self._elapsed.elapsed() / 1000.0
        dt = min(now - self._last, 0.05)  # clamp — без рывков при потере фокуса
        self._last = now

        # Плавный перекрас.
        for i in range(3):
            self._cur[i] += (self._tgt[i] - self._cur[i]) * 0.06

        if not self.reduced:
            self._y += self._vy * dt
            if self._y < -self._margin or self._y > self.height() + self._margin:
                self._spawn()
        self.update()

    # ---- Отрисовка -------------------------------------------------------

    def _ensure_dot_tile(self) -> None:
        if self._dot_tile is not None:
            return
        tile = QPixmap(26, 26)
        tile.fill(Qt.transparent)
        pt = QPainter(tile)
        pt.setRenderHint(QPainter.Antialiasing)
        pt.setPen(Qt.NoPen)
        pt.setBrush(self._dot)
        pt.drawEllipse(QPointF(1.2, 1.2), 1.0, 1.0)
        pt.end()
        self._dot_tile = tile

    def _ensure_sphere(self) -> None:
        r, g, b = (int(round(v)) for v in self._cur)
        key = (self._size, r // 8, g // 8, b // 8)  # огрубляем, чтобы не пересоздавать каждый кадр
        if self._sphere_pm is not None and key == self._sphere_key:
            return
        self._sphere_key = key
        d = self._size
        pm = QPixmap(d, d)
        pm.fill(Qt.transparent)
        pt = QPainter(pm)
        pt.setRenderHint(QPainter.Antialiasing)
        grad = QRadialGradient(d / 2, d / 2, d / 2)
        grad.setColorAt(0.0, QColor(r, g, b, 135))
        grad.setColorAt(0.5, QColor(r, g, b, 60))
        grad.setColorAt(1.0, QColor(r, g, b, 0))
        pt.setBrush(QBrush(grad))
        pt.setPen(Qt.NoPen)
        pt.drawEllipse(0, 0, d, d)
        pt.end()
        self._sphere_pm = pm

    def _draw_ambient(self, pt: QPainter) -> None:
        """Мягкие статичные свечения по углам — фон «наполненнее», но не мешает."""
        w, h = self.width(), self.height()
        r, g, b = 124, 92, 255
        spots = [(0, 0, max(w, h) * 0.45, 26),            # верх-лево
                 (w, h, max(w, h) * 0.5, 30),             # низ-право
                 (w, 0, max(w, h) * 0.32, 16)]            # верх-право (слабее)
        for cx, cy, rad, a in spots:
            grad = QRadialGradient(cx, cy, rad)
            grad.setColorAt(0.0, QColor(r, g, b, a))
            grad.setColorAt(1.0, QColor(r, g, b, 0))
            pt.setBrush(QBrush(grad)); pt.setPen(Qt.NoPen)
            pt.drawRect(0, 0, w, h)

    def paintEvent(self, _e) -> None:
        pt = QPainter(self)
        pt.fillRect(self.rect(), self._bg)
        # Мягкие угловые свечения (наполненность).
        self._draw_ambient(pt)
        # Точечная сетка.
        self._ensure_dot_tile()
        pt.fillRect(self.rect(), QBrush(self._dot_tile))
        # Плавающая сфера.
        self._ensure_sphere()
        if self._sphere_pm is not None:
            pt.drawPixmap(int(self._x - self._size / 2),
                          int(self._y - self._size / 2), self._sphere_pm)
