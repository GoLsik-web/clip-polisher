"""zone_editor.py — виджет разметки зон мышью на кадре клипа.

Показывает кадр (с сохранением аспекта, letterbox) и позволяет рисовать два
прямоугольника: зону ВЕБКИ (синий) и зону ГЕЙМПЛЕЯ (зелёный). Зоны хранятся и
возвращаются в ДОЛЯХ исходного кадра (0..1) — как ждёт core.layout.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QPixmap, QPainter, QPen, QColor, QBrush
from PySide6.QtWidgets import QWidget

from core.config import Zone


class ZoneEditor(QWidget):
    """Редактор зон. active_zone: 'webcam' | 'gameplay'."""
    zones_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 240)
        self._pixmap: Optional[QPixmap] = None
        self.active_zone = "webcam"
        # Зоны в долях (0..1) или None.
        self._webcam: Optional[Zone] = None
        self._gameplay: Optional[Zone] = None
        self._drag_start: Optional[QPointF] = None
        self._drag_cur: Optional[QPointF] = None

    # ---- Данные ----------------------------------------------------------

    def set_frame(self, path: str) -> None:
        pm = QPixmap(path)
        self._pixmap = pm if not pm.isNull() else None
        self.update()

    def set_zones(self, webcam: Optional[Zone], gameplay: Optional[Zone]) -> None:
        self._webcam = webcam
        self._gameplay = gameplay
        self.update()

    def webcam_zone(self) -> Optional[Zone]:
        return self._webcam

    def gameplay_zone(self) -> Optional[Zone]:
        return self._gameplay

    # ---- Геометрия: область изображения внутри виджета -------------------

    def _image_rect(self) -> QRectF:
        """Прямоугольник, в котором реально нарисован кадр (letterbox)."""
        if self._pixmap is None:
            return QRectF(0, 0, self.width(), self.height())
        iw, ih = self._pixmap.width(), self._pixmap.height()
        ww, wh = self.width(), self.height()
        scale = min(ww / iw, wh / ih)
        dw, dh = iw * scale, ih * scale
        x = (ww - dw) / 2
        y = (wh - dh) / 2
        return QRectF(x, y, dw, dh)

    def _to_ratio(self, p: QPointF) -> QPointF:
        r = self._image_rect()
        rx = (p.x() - r.x()) / r.width() if r.width() else 0
        ry = (p.y() - r.y()) / r.height() if r.height() else 0
        return QPointF(min(max(rx, 0.0), 1.0), min(max(ry, 0.0), 1.0))

    def _zone_to_px(self, z: Zone) -> QRectF:
        r = self._image_rect()
        return QRectF(r.x() + z.x * r.width(), r.y() + z.y * r.height(),
                      z.w * r.width(), z.h * r.height())

    # ---- Мышь ------------------------------------------------------------

    def mousePressEvent(self, e) -> None:
        if self._pixmap is None:
            return
        self._drag_start = e.position()
        self._drag_cur = e.position()
        self.update()

    def mouseMoveEvent(self, e) -> None:
        if self._drag_start is not None:
            self._drag_cur = e.position()
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if self._drag_start is None:
            return
        a = self._to_ratio(self._drag_start)
        b = self._to_ratio(e.position())
        x, y = min(a.x(), b.x()), min(a.y(), b.y())
        w, h = abs(a.x() - b.x()), abs(a.y() - b.y())
        if w > 0.01 and h > 0.01:
            z = Zone(x, y, w, h)
            if self.active_zone == "webcam":
                self._webcam = z
            else:
                self._gameplay = z
            self.zones_changed.emit()
        self._drag_start = None
        self._drag_cur = None
        self.update()

    # ---- Отрисовка -------------------------------------------------------

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(30, 30, 34))
        if self._pixmap is not None:
            p.drawPixmap(self._image_rect().toRect(), self._pixmap)
        else:
            p.setPen(QColor(160, 160, 160))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Загрузите клип и нажмите «Кадр для разметки»")
            return

        # Существующие зоны.
        if self._gameplay is not None:
            self._draw_zone(p, self._gameplay, QColor(60, 220, 120), "геймплей")
        if self._webcam is not None:
            self._draw_zone(p, self._webcam, QColor(80, 150, 255), "вебка")

        # Текущее перетаскивание.
        if self._drag_start is not None and self._drag_cur is not None:
            color = QColor(80, 150, 255) if self.active_zone == "webcam" else QColor(60, 220, 120)
            pen = QPen(color, 2, Qt.DashLine)
            p.setPen(pen)
            p.drawRect(QRectF(self._drag_start, self._drag_cur))

    def _draw_zone(self, p: QPainter, z: Zone, color: QColor, label: str) -> None:
        rect = self._zone_to_px(z)
        p.setPen(QPen(color, 3))
        fill = QColor(color)
        fill.setAlpha(40)
        p.setBrush(QBrush(fill))
        p.drawRect(rect)
        p.setBrush(Qt.NoBrush)
        p.setPen(color)
        p.drawText(rect.adjusted(4, 2, -4, -4), Qt.AlignLeft | Qt.AlignTop, label)
