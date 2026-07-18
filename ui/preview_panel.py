"""preview_panel.py — РЕДАКТОР композиции (эталон + свободная компоновка на 9:16).

Один активный холст, переключаемый кнопкой формата:
  - «16:9 · Источник» — исходный кадр, где помечаем ЧТО вырезать (вебка/геймплей).
  - «9:16 · Финалка» — чёрная канва, где расставляем ВСЕ элементы (вебка, геймплей,
    субтитры, ник, платформа): позиция, размер, поворот. Вебка всегда поверх других.

Зоны — дети холста, позиция по долям, пересчёт в resizeEvent. Изменения пишутся в
Composition (ядро). Под холстом: легенда + таймлайн.
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, QRectF, QPoint, QPointF, QSize
from PySide6.QtGui import (QPixmap, QColor, QPainter, QPen, QBrush, QTransform,
                           QPainterPath)
from PySide6.QtWidgets import (QWidget, QLabel, QFrame, QVBoxLayout, QHBoxLayout,
                               QGridLayout, QPushButton, QSizePolicy)

from core.config import (Zone, Placement, Composition, LayoutPreset,
                         composition_from_preset)
from .theme import ZONE_COLORS
from .widgets import HelpIcon


class ZoneBox(QWidget):
    """Зона на холсте: перетаскивание, масштаб (угол), поворот (верхняя ручка).

    Хранит доли относительно родителя-холста + rotation(°) + shape. Родитель зовёт
    reposition() при ресайзе. Пишет назад в переданный объект (Zone или Placement).
    """
    HANDLE = 12
    ROT_OFFSET = 20
    changed = Signal()

    def __init__(self, name: str, color: str, target, rotatable: bool, parent=None):
        super().__init__(parent)
        self.name = name
        self.color = QColor(color)
        self.target = target           # Zone | Placement (пишем сюда)
        self.rotatable = rotatable
        self._drag: Optional[str] = None
        self._press = QPoint()
        self._orig = QRect()
        self._orig_rot = 0.0
        self.editable = False          # по умолчанию заблокирована (редактируем только выбранную)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setMouseTracking(True)

    def set_editable(self, on: bool) -> None:
        """Разрешить/запретить редактирование этой зоны (клики проходят мимо, если нет)."""
        self.editable = on
        self.setAttribute(Qt.WA_TransparentForMouseEvents, not on)
        if on:
            self.raise_()
        self.update()

    # ---- геометрия -------------------------------------------------------

    @property
    def rotation(self) -> float:
        return getattr(self.target, "rotation", 0.0)

    @rotation.setter
    def rotation(self, v: float) -> None:
        if hasattr(self.target, "rotation"):
            self.target.rotation = v

    def reposition(self) -> None:
        par = self.parentWidget()
        if par is None or par.width() <= 0 or par.height() <= 0:
            return
        W, H = par.width(), par.height()
        pad = self.ROT_OFFSET + 6
        # Виджет шире зоны сверху — чтобы вместить ручку поворота.
        self.setGeometry(int(self.target.x * W), int(self.target.y * H) - pad,
                         max(30, int(self.target.w * W)),
                         max(24, int(self.target.h * H)) + pad)
        self.raise_()

    def _box_rect(self) -> QRect:
        """Прямоугольник зоны ВНУТРИ виджета (без верхнего поля под ручку)."""
        return QRect(0, self.ROT_OFFSET + 6, self.width(), self.height() - self.ROT_OFFSET - 6)

    def _commit(self) -> None:
        par = self.parentWidget()
        if par is None or par.width() == 0:
            return
        W, H = par.width(), par.height()
        b = self._box_rect().translated(self.x(), self.y())
        self.target.x = min(max(b.x() / W, 0.0), 1.0)
        self.target.y = min(max(b.y() / H, 0.0), 1.0)
        self.target.w = min(b.width() / W, 1.0)
        self.target.h = min(b.height() / H, 1.0)
        self.changed.emit()

    # ---- hit-test --------------------------------------------------------

    def _rot_handle_center(self) -> QPoint:
        return QPoint(self.width() // 2, self.ROT_OFFSET // 2)

    def _in_rot(self, p: QPoint) -> bool:
        return self.rotatable and (p - self._rot_handle_center()).manhattanLength() < 16

    def _in_resize(self, p: QPoint) -> bool:
        b = self._box_rect()
        return p.x() >= b.right() - self.HANDLE and p.y() >= b.bottom() - self.HANDLE

    # ---- мышь ------------------------------------------------------------

    def mousePressEvent(self, e) -> None:
        p = e.position().toPoint()
        self._press = e.globalPosition().toPoint()
        self._orig = self.geometry()
        self._orig_rot = self.rotation
        self._drag = "rot" if self._in_rot(p) else ("resize" if self._in_resize(p) else "move")
        self.raise_()

    def mouseMoveEvent(self, e) -> None:
        p = e.position().toPoint()
        if self._drag is None:
            if self._in_rot(p):
                self.setCursor(Qt.CrossCursor)
            elif self._in_resize(p):
                self.setCursor(Qt.SizeFDiagCursor)
            else:
                self.setCursor(Qt.SizeAllCursor)
            return
        d = e.globalPosition().toPoint() - self._press
        par = self.parentWidget()
        W, H = (par.width(), par.height()) if par else (1, 1)
        if self._drag == "rot":
            center = self.mapToParent(QPoint(self.width() // 2,
                                             self.ROT_OFFSET + 6 + self._box_rect().height() // 2))
            v = e.globalPosition().toPoint() - self.mapToGlobal(QPoint(0, 0)) + self.pos() - center
            ang = math.degrees(math.atan2(v.y(), v.x())) + 90
            self.rotation = round(ang)
            self.update()
            return
        g = QRect(self._orig)
        if self._drag == "move":
            g.moveTo(min(max(0, g.x() + d.x()), W - g.width()),
                     min(max(-self.ROT_OFFSET, g.y() + d.y()), H - g.height() + self.ROT_OFFSET))
        else:
            g.setWidth(max(30, min(W - g.x(), self._orig.width() + d.x())))
            g.setHeight(max(24 + self.ROT_OFFSET + 6, self._orig.height() + d.y()))
        self.setGeometry(g)

    def mouseReleaseEvent(self, e) -> None:
        if self._drag in ("move", "resize"):
            self._commit()
        elif self._drag == "rot":
            self.changed.emit()
        self._drag = None

    # ---- отрисовка -------------------------------------------------------

    def paintEvent(self, _e) -> None:
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        b = self._box_rect()
        cx, cy = b.center().x(), b.center().y()
        ed = self.editable

        pt.save()
        pt.translate(cx, cy)
        pt.rotate(self.rotation)
        rw, rh = b.width(), b.height()
        rrect = QRect(-rw // 2, -rh // 2, rw, rh)
        fill = QColor(self.color); fill.setAlpha(70 if ed else 28)
        pt.setBrush(QBrush(fill))
        pt.setPen(QPen(self.color, 2.6 if ed else 1.4, Qt.SolidLine if ed else Qt.DashLine))
        if getattr(self.target, "shape", "rect") == "circle":
            d = min(rw, rh)
            pt.drawEllipse(QRect(-d // 2, -d // 2, d, d))
        else:
            pt.drawRoundedRect(rrect, 7, 7)
        pt.setPen(QColor("#ffffff") if ed else QColor(255, 255, 255, 150))
        pt.drawText(rrect.adjusted(6, 4, -4, -4), Qt.AlignLeft | Qt.AlignTop, self.name)
        if ed:
            # уголок-ручка масштаба (только у выбранной)
            pt.setBrush(QColor("#ffffff")); pt.setPen(QPen(QColor("#111"), 1))
            pt.drawRect(rw // 2 - self.HANDLE, rh // 2 - self.HANDLE, self.HANDLE - 2, self.HANDLE - 2)
        pt.restore()

        # ручка поворота — только у выбранной
        if ed and self.rotatable:
            c = self._rot_handle_center()
            pt.setPen(QPen(self.color, 1.5))
            pt.drawLine(c.x(), c.y() + 8, cx, b.top())
            pt.setBrush(QColor("#ffffff")); pt.setPen(QPen(QColor("#111"), 1))
            pt.drawEllipse(c, 5, 5)


class AspectCanvas(QFrame):
    """Холст фикс-пропорции (16:9 или 9:16), масштабируется по ширине; держит зоны."""
    def __init__(self, object_name: str, parent=None):
        super().__init__(parent)
        self.setObjectName(object_name)
        self.aspect = 16 / 9
        self._pixmap: Optional[QPixmap] = None
        self.zones: list[ZoneBox] = []
        # Размер холста задаёт CanvasArea (вписывает по доступному месту).

    def set_aspect(self, w: int, h: int) -> None:
        self.aspect = w / h

    def set_frame(self, path: Optional[str]) -> None:
        self._pixmap = QPixmap(path) if path else None
        if self._pixmap is not None and self._pixmap.isNull():
            self._pixmap = None
        self.update()

    def clear_zones(self) -> None:
        for z in self.zones:
            z.setParent(None); z.deleteLater()
        self.zones = []

    def add_zone(self, z: ZoneBox) -> None:
        z.setParent(self)
        self.zones.append(z)
        z.reposition(); z.show()

    def raise_zone(self, name: str) -> None:
        for z in self.zones:
            if z.name == name:
                z.raise_()

    def resizeEvent(self, e) -> None:
        for z in self.zones:
            z.reposition()

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        pt = QPainter(self)
        if self._pixmap:
            sc = self._pixmap.scaled(self.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            pt.drawPixmap((self.width() - sc.width()) // 2, (self.height() - sc.height()) // 2, sc)
        else:
            pt.fillRect(self.rect(), QColor("#0d0f14"))
            pt.setPen(QColor("#4a4668"))
            pt.drawText(self.rect(), Qt.AlignCenter, "9:16 канва")


class CanvasArea(QWidget):
    """Область холста: вписывает AspectCanvas (contain) по доступному месту, центрируя."""
    def __init__(self, canvas: AspectCanvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.canvas.setParent(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(180)

    def refit(self) -> None:
        W, H = self.width(), self.height()
        if W <= 0 or H <= 0:
            return
        a = self.canvas.aspect
        w = min(W, H * a)
        h = w / a
        if h > H:
            h = H; w = h * a
        self.canvas.setGeometry(int((W - w) / 2), int((H - h) / 2), int(w), int(h))

    def resizeEvent(self, e) -> None:
        self.refit()


class TrimTimeline(QWidget):
    """Интерактивная дорожка обрезки: киноленты-стоп-кадры + две ручки (начало/конец).

    Тянешь ручки — меняются границы отрезка; область вне выделения затемнена.
    Клик-тяга внутри выделения двигает отрезок целиком. Двусторонний синхрон с
    числовыми полями делает MainWindow через сигнал changed(start, end)."""
    PAD = 12
    HANDLE = 9
    changed = Signal(float, float)   # (start, end) в секундах
    scrub = Signal(float)            # позиция для стоп-кадра (при отпускании)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(58)
        self.setMouseTracking(True)
        self._dur = 0.0
        self._start = 0.0
        self._end = 0.0
        self._film: Optional[QPixmap] = None
        self._drag: Optional[str] = None
        self._press_x = 0
        self._orig = (0.0, 0.0)

    # ---- API -------------------------------------------------------------

    def set_duration(self, dur: float) -> None:
        self._dur = max(0.0, dur)
        if self._end <= 0 or self._end > self._dur:
            self._end = self._dur
        self._start = min(self._start, self._end)
        self.update()

    def set_range(self, start: float, end: float) -> None:
        """Задать границы извне (из числовых полей) — без эмита changed."""
        if self._dur <= 0:
            self._start, self._end = start, end
            self.update()
            return
        self._start = min(max(0.0, start), self._dur)
        self._end = min(max(self._start, end), self._dur)
        self.update()

    def set_filmstrip(self, path: str) -> None:
        pix = QPixmap(path)
        self._film = None if pix.isNull() else pix
        self.update()

    # ---- геометрия -------------------------------------------------------

    def _track(self) -> QRect:
        return QRect(self.PAD, 4, self.width() - 2 * self.PAD, self.height() - 22)

    def _t2x(self, t: float) -> int:
        tr = self._track()
        if self._dur <= 0:
            return tr.left()
        return int(tr.left() + (t / self._dur) * tr.width())

    def _x2t(self, x: int) -> float:
        tr = self._track()
        if tr.width() <= 0 or self._dur <= 0:
            return 0.0
        return min(max(0.0, (x - tr.left()) / tr.width() * self._dur), self._dur)

    # ---- мышь ------------------------------------------------------------

    def mousePressEvent(self, e) -> None:
        if self._dur <= 0:
            return
        x = int(e.position().x())
        sx, ex = self._t2x(self._start), self._t2x(self._end)
        if abs(x - sx) <= self.HANDLE + 3:
            self._drag = "start"
        elif abs(x - ex) <= self.HANDLE + 3:
            self._drag = "end"
        elif sx < x < ex:
            self._drag = "region"
        else:
            self._drag = "end"       # клик по пустому месту тянет ближнюю границу
            self._set_from_x(x)
        self._press_x = x
        self._orig = (self._start, self._end)

    def mouseMoveEvent(self, e) -> None:
        x = int(e.position().x())
        if self._drag is None:
            sx, ex = self._t2x(self._start), self._t2x(self._end)
            near = abs(x - sx) <= self.HANDLE + 3 or abs(x - ex) <= self.HANDLE + 3
            self.setCursor(Qt.SizeHorCursor if near else Qt.ArrowCursor)
            return
        if self._drag == "region":
            dt = self._x2t(x) - self._x2t(self._press_x)
            length = self._orig[1] - self._orig[0]
            ns = min(max(0.0, self._orig[0] + dt), self._dur - length)
            self._start, self._end = ns, ns + length
        else:
            self._set_from_x(x)
        self.update()
        self.changed.emit(self._start, self._end)

    def _set_from_x(self, x: int) -> None:
        t = self._x2t(x)
        if self._drag == "start":
            self._start = min(t, self._end - 0.1) if self._end > 0.1 else t
            self._start = max(0.0, self._start)
        else:
            self._end = max(t, self._start + 0.1)
            self._end = min(self._end, self._dur)

    def mouseReleaseEvent(self, e) -> None:
        if self._drag == "start":
            self.scrub.emit(self._start)
        elif self._drag in ("end", "region"):
            self.scrub.emit(self._start)
        self._drag = None

    # ---- отрисовка -------------------------------------------------------

    @staticmethod
    def _fmt(sec: float) -> str:
        return f"{int(sec)//60:02d}:{int(sec)%60:02d}"

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tr = self._track()
        # фон дорожки
        p.setPen(Qt.NoPen); p.setBrush(QColor("#191826"))
        p.drawRoundedRect(tr, 6, 6)
        # киноленты-стоп-кадры
        if self._film is not None:
            p.save(); path = QPainterPath(); path.addRoundedRect(QRectF(tr), 6, 6)
            p.setClipPath(path)
            sc = self._film.scaled(tr.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            p.setOpacity(0.9); p.drawPixmap(tr.topLeft(), sc); p.setOpacity(1.0)
            p.restore()
        if self._dur <= 0:
            p.setPen(QColor("#6a6690"))
            p.drawText(tr, Qt.AlignCenter, "Загрузите клип — появятся стоп-кадры")
            return
        sx, ex = self._t2x(self._start), self._t2x(self._end)
        # затемнение вне отрезка
        p.setPen(Qt.NoPen); p.setBrush(QColor(8, 8, 13, 165))
        p.drawRect(QRect(tr.left(), tr.top(), sx - tr.left(), tr.height()))
        p.drawRect(QRect(ex, tr.top(), tr.right() - ex + 1, tr.height()))
        # рамка выделения
        acc = QColor("#7c5cff")
        p.setBrush(Qt.NoBrush); p.setPen(QPen(acc, 2))
        p.drawRect(QRect(sx, tr.top() + 1, ex - sx, tr.height() - 2))
        # ручки
        for hx in (sx, ex):
            p.setPen(Qt.NoPen); p.setBrush(acc)
            p.drawRoundedRect(QRect(hx - self.HANDLE // 2, tr.top() - 2,
                                    self.HANDLE, tr.height() + 4), 3, 3)
            p.setPen(QPen(QColor("#fff"), 1))
            for dx in (-2, 1):
                p.drawLine(hx + dx, tr.center().y() - 5, hx + dx, tr.center().y() + 5)
        # подписи времени
        p.setPen(QColor("#c2bde0"))
        y = self.height() - 4
        p.drawText(QRect(tr.left(), y - 12, 120, 14), Qt.AlignLeft,
                   self._fmt(self._start))
        p.drawText(QRect(tr.right() - 120, y - 12, 120, 14), Qt.AlignRight,
                   self._fmt(self._end))
        dur = max(0.0, self._end - self._start)
        p.setPen(acc)
        p.drawText(QRect(tr.left(), y - 12, tr.width(), 14), Qt.AlignCenter,
                   f"отрезок {self._fmt(dur)} · весь клип {self._fmt(self._dur)}")


class EditorPanel(QFrame):
    """Правый большой редактор: переключатель формата + холст + легенда + таймлайн."""
    change_frame = Signal()
    composition_changed = Signal()
    trim_changed = Signal(float, float)   # начало/конец отрезка (сек) из таймлайна
    trim_scrub = Signal(float)            # позиция для обновления стоп-кадра

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Дефолтная композиция (пресет A) — источник clip-подобный.
        self.comp = composition_from_preset(
            LayoutPreset.A, Zone(0.0, 0.73, 0.235, 0.265), Zone(0.0, 0.13, 1.0, 0.74))
        self._mode = "source"   # 'source' (16:9) | 'final' (9:16)
        self._selected = None   # имя выбранной для редактирования зоны
        self._legend_btns: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Верхняя панель: переключатель формата + изменить кадр.
        top = QHBoxLayout()
        self.btn_src = QPushButton("16:9 · Источник"); self.btn_src.setCheckable(True); self.btn_src.setChecked(True)
        self.btn_fin = QPushButton("9:16 · Финалка"); self.btn_fin.setCheckable(True)
        self.btn_src.clicked.connect(lambda: self.set_mode("source"))
        self.btn_fin.clicked.connect(lambda: self.set_mode("final"))
        top.addWidget(QLabel("Холст:"))
        top.addWidget(self.btn_src); top.addWidget(self.btn_fin)
        top.addWidget(HelpIcon("16:9 — помечаешь, что вырезать (вебка/геймплей). "
                               "9:16 — расставляешь финальную компоновку."))
        top.addStretch(1)
        self.change_btn = QPushButton("Изменить кадр")
        self.change_btn.clicked.connect(self.change_frame.emit)
        top.addWidget(self.change_btn)
        root.addLayout(top)

        # Холст вписывается в среднюю область; легенда/таймлайн всегда видны снизу.
        self.canvas = AspectCanvas("albumScreen")
        self.canvas_area = CanvasArea(self.canvas)
        root.addWidget(self.canvas_area, 1)
        root.addWidget(self._legend())
        root.addWidget(self._timeline())

        self._rebuild_zones()

    # ---- режим/зоны ------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.btn_src.setChecked(mode == "source")
        self.btn_fin.setChecked(mode == "final")
        if mode == "source":
            self.canvas.set_aspect(16, 9)
            self.canvas.set_frame(getattr(self, "_src_frame", None))
        else:
            self.canvas.set_aspect(9, 16)
            self.canvas.set_frame(None)
        self.canvas_area.refit()
        self._rebuild_zones()

    def _rebuild_zones(self) -> None:
        self.canvas.clear_zones()
        if self._mode == "source":
            z1 = ZoneBox("Геймплей", ZONE_COLORS["game"], self.comp.gameplay_source, False)
            z2 = ZoneBox("Вебка", ZONE_COLORS["cam"], self.comp.webcam_source, False)
            for z in (z1, z2):
                z.changed.connect(self.composition_changed.emit)
                self.canvas.add_zone(z)
        else:
            order = [("Геймплей", "game", self.comp.gameplay),
                     ("Субтитры", "sub", self.comp.subtitles),
                     ("Ник", "brand", self.comp.nick),
                     ("Платформа", "plat", self.comp.platform),
                     ("Вебка", "cam", self.comp.webcam)]  # вебка последней → сверху
            for name, ckey, target in order:
                if not getattr(target, "visible", True):
                    continue
                z = ZoneBox(name, ZONE_COLORS[ckey], target, rotatable=True)
                z.changed.connect(self.composition_changed.emit)
                self.canvas.add_zone(z)
            self.canvas.raise_zone("Вебка")   # всегда поверх

        # применить состояние выбора: редактируется только выбранная зона (если есть)
        present = [z.name for z in self.canvas.zones]
        if self._selected not in present:
            self._selected = None
        for z in self.canvas.zones:
            z.set_editable(z.name == self._selected)
        self._refresh_legend_state()

    def apply_preset(self, preset: LayoutPreset) -> None:
        """Сменить пресет — пересобрать композицию, сохранив исходные зоны."""
        self.comp = composition_from_preset(preset, self.comp.webcam_source, self.comp.gameplay_source)
        self._rebuild_zones()
        self.composition_changed.emit()

    # ---- легенда/таймлайн ------------------------------------------------

    def _legend(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(6)
        hint = QLabel("Выбери зону, чтобы её редактировать (чтобы случайно не двигать другую):")
        hint.setStyleSheet("color:#c2bde0;font-size:11px;")
        hint.setWordWrap(True)
        v.addWidget(hint)
        grid = QGridLayout(); grid.setSpacing(7)
        items = [("cam", "Вебка", "Лицо, не обрезается"), ("game", "Геймплей", "Картинка игры"),
                 ("sub", "Субтитры", "Зона текста"), ("brand", "Ник", "Имя на клипе"),
                 ("plat", "Платформа", "Значок площадки")]
        for i, (key, name, desc) in enumerate(items):
            btn = QPushButton(); btn.setCursor(Qt.PointingHandCursor)
            btn._zname = name; btn._zcolor = ZONE_COLORS[key]
            bl = QHBoxLayout(btn); bl.setContentsMargins(9, 6, 9, 6); bl.setSpacing(8)
            sw = QLabel(); sw.setFixedSize(13, 13)
            sw.setStyleSheet(f"background:{ZONE_COLORS[key]};border-radius:4px;")
            txt = QLabel(f"<b>{name}</b><br><span style='color:#c2bde0;font-size:11px'>{desc}</span>")
            bl.addWidget(sw); bl.addWidget(txt); bl.addStretch(1)
            btn.clicked.connect(lambda _=False, n=name: self._select_zone(n))
            self._legend_btns[name] = btn
            grid.addWidget(btn, i // 3, i % 3)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        v.addLayout(grid)
        return w

    def _style_legend_btn(self, btn, selected: bool, enabled: bool) -> None:
        color = btn._zcolor
        if selected:
            css = (f"QPushButton{{text-align:left;background:#201d30;border:2px solid {color};"
                   f"border-radius:8px;}}")
        elif enabled:
            css = (f"QPushButton{{text-align:left;background:#191826;border:1px solid #2a2740;"
                   f"border-radius:8px;}} QPushButton:hover{{border-color:{color};}}")
        else:
            css = ("QPushButton{text-align:left;background:#141320;border:1px solid #201d30;"
                   "border-radius:8px;}")
        btn.setStyleSheet(css)

    def _refresh_legend_state(self) -> None:
        if not self._legend_btns:
            return
        present = [z.name for z in self.canvas.zones]
        for name, btn in self._legend_btns.items():
            en = name in present
            btn.setEnabled(en)
            self._style_legend_btn(btn, self._selected == name and en, en)

    def _select_zone(self, name: str) -> None:
        present = [z.name for z in self.canvas.zones]
        if name not in present:
            return
        self._selected = name
        for z in self.canvas.zones:
            z.set_editable(z.name == name)
        self._refresh_legend_state()

    def _timeline(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)
        self.timeline = TrimTimeline()
        self.timeline.changed.connect(self.trim_changed.emit)
        self.timeline.scrub.connect(self.trim_scrub.emit)
        v.addWidget(self.timeline)
        self.tl_row = QLabel("Перетаскивай ручки начала и конца прямо на дорожке со стоп-кадрами")
        self.tl_row.setStyleSheet("color:#c2bde0;font-size:11px;")
        v.addWidget(self.tl_row)
        return w

    # ---- API -------------------------------------------------------------

    def set_source_frame(self, path: str) -> None:
        self._src_frame = path
        if self._mode == "source":
            self.canvas.set_frame(path)

    def set_result_frame(self, path: str) -> None:
        # В режиме финалки показываем отрендеренный результат под зонами.
        if self._mode == "final":
            self.canvas.set_frame(path)

    def get_composition(self) -> Composition:
        return self.comp

    # ---- таймлайн обрезки ------------------------------------------------

    def set_duration(self, dur: float) -> None:
        self.timeline.set_duration(dur)

    def set_trim(self, start: float, end: float) -> None:
        self.timeline.set_range(start, end)

    def set_filmstrip(self, path: str) -> None:
        self.timeline.set_filmstrip(path)

    def set_timeline_text(self, text: str) -> None:
        self.tl_row.setText(text)
