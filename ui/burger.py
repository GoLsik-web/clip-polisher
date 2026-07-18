"""burger.py — отрисовка бургера по слоям (для иконки-кнопки и загрузчика).

Слои снизу вверх (как просил пользователь): нижняя булка, салат, котлета, сыр,
помидор, соус, верхняя булка (с кунжутом). draw_burger рисует первые n_layers —
это даёт анимацию «сборки» в загрузчике.
"""
from __future__ import annotations

import math

from PySide6.QtCore import (Qt, QRectF, QPointF, QPropertyAnimation, QEasingCurve,
                            Property, QTimer)
from PySide6.QtGui import (QPainter, QColor, QBrush, QPen, QPainterPath,
                           QLinearGradient, QRadialGradient)
from PySide6.QtWidgets import QWidget, QPushButton

from .theme import STEP_COLORS

# Упрощённый бургер: 5 слоёв. Две булки (белые) + 3 начинки = 3 режима меню (цвета окон).
# (имя, относительная толщина, расширение по бокам)
LAYERS = [
    ("bun_bottom", 0.22, 0.00),
    ("fill1",      0.18, 0.08),
    ("fill2",      0.16, 0.12),
    ("fill3",      0.18, 0.06),
    ("bun_top",    0.30, 0.00),
]
N_LAYERS = len(LAYERS)

FILL = "#efecfb"          # булки — почти белые
ACCENT = "#7c5cff"        # акцент (кунжут/обводка)

# Начинки — в цвета окон 1/2/3 (сверху вниз fill3→кнопка1, fill2→кнопка2, fill1→кнопка3).
_FILLS = {"fill3": STEP_COLORS[0], "fill2": STEP_COLORS[1], "fill1": STEP_COLORS[2]}


def layer_fill(name: str) -> QColor:
    return QColor(_FILLS.get(name, FILL))


def draw_burger(p: QPainter, rect: QRectF, n_layers: int = N_LAYERS,
                drop: float = 0.0) -> None:
    """Нарисовать бургер (палитра: белый + фиолетовый) в rect, первые n_layers слоёв.

    drop (0..1) — «падение» верхнего показываемого слоя сверху (для сборки-загрузчика).
    """
    p.setRenderHint(QPainter.Antialiasing)
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    base_w = w * 0.82
    cx = x + w / 2
    total = sum(l[1] for l in LAYERS)
    cur_bottom = y + h
    edge = QPen(QColor(ACCENT), max(1.0, h * 0.014))

    for i in range(min(n_layers, N_LAYERS)):
        name, th, spread = LAYERS[i]
        lh = th / total * h
        lw = base_w * (1 + spread)
        top = cur_bottom - lh
        dy = 0.0
        if i == n_layers - 1 and drop > 0:
            dy = -(1 - drop) * h * 0.5
        r = QRectF(cx - lw / 2, top + dy, lw, lh)
        p.setPen(edge)
        p.setBrush(layer_fill(name))

        if name == "bun_top":
            path = QPainterPath()
            path.moveTo(r.left(), r.bottom())
            path.quadTo(r.left(), r.top(), cx, r.top())
            path.quadTo(r.right(), r.top(), r.right(), r.bottom())
            path.closeSubpath()
            p.drawPath(path)
            p.setPen(Qt.NoPen); p.setBrush(QColor(ACCENT))   # кунжут акцентом
            for sx, sy in [(-0.22, 0.55), (0.0, 0.42), (0.22, 0.55),
                           (-0.11, 0.74), (0.12, 0.74)]:
                p.drawEllipse(QPointF(cx + sx * lw, r.top() + sy * lh),
                              lw * 0.028, lh * 0.09)
        elif name == "bun_bottom":
            path = QPainterPath(); path.addRoundedRect(r, lh * 0.5, lh * 0.5)
            p.drawPath(path)
        elif name == "lettuce":
            path = QPainterPath(); path.moveTo(r.left(), r.center().y())
            n = 5
            for k in range(n + 1):
                px = r.left() + r.width() * k / n
                py = r.top() if k % 2 else r.bottom()
                path.lineTo(px, py)
            path.lineTo(r.right(), r.bottom()); path.lineTo(r.left(), r.bottom()); path.closeSubpath()
            p.drawPath(path)
        else:
            p.drawRoundedRect(r, lh * 0.35, lh * 0.35)

        cur_bottom = top


class BurgerButton(QPushButton):
    """Кнопка-иконка бургера (для топбара)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(44, 40)
        self.setToolTip("Выбрать режим работы")
        self._hover = False

    def set_accent(self, color: str) -> None:
        pass

    def enterEvent(self, e): self._hover = True; self.update()
    def leaveEvent(self, e): self._hover = False; self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor("#7c5cff") if self._hover else QColor("#2a2740"), 1))
        p.setBrush(QColor("#201d30") if self._hover else QColor("#191826"))
        p.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 9, 9)
        draw_burger(p, QRectF(7, 6, self.width() - 14, self.height() - 12))


# ==========================================================================
# РЕАЛИСТИЧНЫЙ бургер — ТОЛЬКО для загрузчика (иконка/морф используют draw_burger)
# ==========================================================================

# Слои снизу вверх: (имя, отн. толщина, расширение по бокам).
REAL_LAYERS = [
    ("bun_bottom", 0.20, 0.00),
    ("patty",      0.16, 0.05),
    ("cheese",     0.09, 0.15),
    ("tomato",     0.11, 0.04),
    ("lettuce",    0.12, 0.17),
    ("bun_top",    0.32, 0.02),
]
N_REAL = len(REAL_LAYERS)

# Пищевая палитра (тёплая, аппетитная).
C_BUN      = QColor("#e3a24c")
C_BUN_DK   = QColor("#c9832f")
C_BUN_HI   = QColor("#f3d49a")
C_SESAME   = QColor("#faedcf")
C_PATTY    = QColor("#5c3a22")
C_PATTY_DK = QColor("#3f2717")
C_CHEESE   = QColor("#f7b32b")
C_CHEESE_DK= QColor("#e69412")
C_TOMATO   = QColor("#e0503a")
C_LETTUCE  = QColor("#86c04b")
C_LETTUCE_DK = QColor("#5f9a33")


def _draw_ingredient(p: QPainter, name: str, r: QRectF) -> None:
    """Нарисовать один ингредиент бургера в прямоугольнике r (реалистичный стиль)."""
    p.setPen(Qt.NoPen)
    cx = r.center().x()

    if name == "bun_bottom":
        grad = QLinearGradient(r.left(), r.top(), r.left(), r.bottom())
        grad.setColorAt(0, C_BUN); grad.setColorAt(1, C_BUN_DK)
        p.setBrush(QBrush(grad))
        path = QPainterPath()
        rad = r.height() * 0.55
        path.moveTo(r.left(), r.top())
        path.lineTo(r.right(), r.top())
        path.lineTo(r.right(), r.bottom() - rad)
        path.quadTo(r.right(), r.bottom(), r.right() - rad, r.bottom())
        path.lineTo(r.left() + rad, r.bottom())
        path.quadTo(r.left(), r.bottom(), r.left(), r.bottom() - rad)
        path.closeSubpath()
        p.drawPath(path)

    elif name == "patty":
        grad = QLinearGradient(r.left(), r.top(), r.left(), r.bottom())
        grad.setColorAt(0, C_PATTY); grad.setColorAt(1, C_PATTY_DK)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(r, r.height() * 0.45, r.height() * 0.45)
        # текстура — светлые крапинки
        p.setBrush(QColor(140, 96, 60, 120))
        for fx, fy in [(-0.28, 0.4), (0.05, 0.6), (0.32, 0.35), (-0.05, 0.3), (0.22, 0.7)]:
            p.drawEllipse(QPointF(cx + fx * r.width(), r.top() + fy * r.height()),
                          r.width() * 0.02, r.height() * 0.1)

    elif name == "cheese":
        p.setBrush(C_CHEESE)
        p.drawRoundedRect(r, r.height() * 0.3, r.height() * 0.3)
        # подтёки сыра по краям (треугольнички вниз)
        p.setBrush(C_CHEESE)
        for fx in (-0.34, -0.05, 0.28):
            dx = cx + fx * r.width()
            drip = QPainterPath()
            w = r.width() * 0.09
            dh = r.height() * (0.9 + abs(fx))
            drip.moveTo(dx - w, r.bottom() - 1)
            drip.lineTo(dx + w, r.bottom() - 1)
            drip.lineTo(dx, r.bottom() + dh)
            drip.closeSubpath()
            p.drawPath(drip)

    elif name == "tomato":
        p.setBrush(C_TOMATO)
        p.drawRoundedRect(r, r.height() * 0.5, r.height() * 0.5)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(QRectF(r.left() + r.width()*0.05, r.top() + r.height()*0.18,
                                 r.width() * 0.9, r.height() * 0.28),
                          r.height()*0.3, r.height()*0.3)

    elif name == "lettuce":
        grad = QLinearGradient(r.left(), r.top(), r.left(), r.bottom())
        grad.setColorAt(0, C_LETTUCE); grad.setColorAt(1, C_LETTUCE_DK)
        p.setBrush(QBrush(grad))
        # волнистый верх (рюши салата)
        path = QPainterPath()
        n = 7
        path.moveTo(r.left(), r.center().y())
        for k in range(n + 1):
            px = r.left() + r.width() * k / n
            py = r.top() if k % 2 else r.top() + r.height() * 0.55
            ctrl_x = r.left() + r.width() * (k - 0.5) / n
            path.quadTo(ctrl_x, r.top() - r.height() * 0.15 if k % 2 else r.center().y(),
                        px, py)
        path.lineTo(r.right(), r.bottom())
        path.lineTo(r.left(), r.bottom())
        path.closeSubpath()
        p.drawPath(path)

    elif name == "bun_top":
        grad = QLinearGradient(r.left(), r.top(), r.left(), r.bottom())
        grad.setColorAt(0, C_BUN_HI); grad.setColorAt(0.5, C_BUN); grad.setColorAt(1, C_BUN_DK)
        p.setBrush(QBrush(grad))
        path = QPainterPath()
        path.moveTo(r.left(), r.bottom())
        path.quadTo(r.left(), r.top() + r.height() * 0.1, cx, r.top())
        path.quadTo(r.right(), r.top() + r.height() * 0.1, r.right(), r.bottom())
        path.closeSubpath()
        p.drawPath(path)
        # блик сверху
        hl = QRadialGradient(cx, r.top() + r.height() * 0.35, r.width() * 0.4)
        hl.setColorAt(0, QColor(255, 255, 255, 70)); hl.setColorAt(1, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(hl)); p.drawPath(path)
        # кунжут
        p.setBrush(C_SESAME)
        for sx, sy in [(-0.24, 0.55), (0.0, 0.42), (0.24, 0.55),
                       (-0.12, 0.72), (0.13, 0.72)]:
            p.save()
            p.translate(cx + sx * r.width(), r.top() + sy * r.height())
            p.rotate(sx * 40)
            p.drawEllipse(QPointF(0, 0), r.width() * 0.028, r.height() * 0.16)
            p.restore()


class BurgerLoader(QWidget):
    """Загрузчик: САМОСТОЯТЕЛЬНАЯ зацикленная анимка сборки бургера.

    НЕ привязана к процентам (проценты — на полоске загрузчика). Один цикл (~3.8 c):
      1) СБОРКА — слои влетают сверху по очереди с отскоком, чуть кренясь то влево,
         то вправо, и сплющиваются при ударе;
      2) ПРАЗДНИК — собранный бургер радостно подпрыгивает-джиглит, вокруг вспыхивают
         искорки, сверху поднимается пар;
      3) УПЛЫВАЕТ — бургер «подают»: взмывает вверх, уменьшается и тает;
      4) цикл повторяется.
    """
    LOOP_MS = 3800
    A_END = 0.56       # конец сборки
    C_END = 0.74       # конец праздника
    H_END = 0.86       # конец покачивания
    # (O_END = 1.0 — уплывание)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(150, 150)
        self._t = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._t = 0.0
        self._timer.start()
        self.update()

    def stop(self) -> None:
        self._timer.stop()

    def set_progress(self, frac: float) -> None:
        # Осознанно игнорируем: бургер живёт своей зацикленной жизнью,
        # прогресс показывает полоска. Метод оставлен для совместимости.
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        self._t = (self._t + self._timer.interval() / self.LOOP_MS) % 1.0
        self.update()

    # ---- вспомогательное движение слоя при падении -----------------------

    @staticmethod
    def _layer_motion(raw: float, sign: int, fall_h: float, lw: float):
        """(yoff, squash, xlean, rot) для слоя по его локальному прогрессу 0..1."""
        if raw <= 0.0:
            return -fall_h, 1.0, sign * 0.16 * lw, sign * 7.0
        if raw < 0.55:                       # падение с ускорением + крен
            fp = raw / 0.55
            yoff = -(1.0 - fp * fp) * fall_h
            return yoff, 1.0, sign * 0.16 * lw * (1 - fp), sign * 7.0 * (1 - fp)
        if raw < 0.75:                       # удар — сплющивание
            k = (raw - 0.55) / 0.20
            return 0.0, 1.0 - 0.22 * math.sin(k * math.pi), 0.0, 0.0
        k = (raw - 0.75) / 0.25              # оседание — лёгкий стретч
        return 0.0, 1.0 + 0.07 * math.sin(k * math.pi), 0.0, 0.0

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._t
        W, H = self.width(), self.height()
        cx = W / 2
        area = QRectF(18, 14, W - 36, H - 28)
        base_w = area.width() * 0.9
        total = sum(l[1] for l in REAL_LAYERS)
        fall_h = area.height() * 1.15

        # --- глобальные преобразования по фазам -------------------------------
        g_dy = 0.0; g_scale = 1.0; g_sqy = 1.0; g_alpha = 1.0; g_rot = 0.0
        raws = [1.0] * N_REAL
        if t < self.A_END:                    # СБОРКА
            slot = self.A_END / N_REAL
            for i in range(N_REAL):
                raws[i] = max(0.0, min(1.0, (t - i * slot * 0.82) / (slot * 1.5)))
        elif t < self.C_END:                  # ПРАЗДНИК: подпрыгивание + джигл
            k = (t - self.A_END) / (self.C_END - self.A_END)
            g_dy = -14.0 * math.sin(k * math.pi) * (1 - 0.3 * k)
            g_sqy = 1.0 - 0.14 * math.sin(k * math.pi * 3) * (1 - k)
        elif t < self.H_END:                  # мягкое покачивание
            k = (t - self.C_END) / (self.H_END - self.C_END)
            g_dy = -3.0 * math.sin(k * math.pi * 2)
        else:                                 # УПЛЫВАЕТ (подают)
            k = (t - self.H_END) / (1.0 - self.H_END)
            g_dy = -46.0 * (k * k)
            g_scale = 1.0 - 0.32 * k
            g_alpha = max(0.0, 1.0 - 1.25 * k)
            g_rot = 8.0 * k

        # --- тень под бургером (даёт «землю») --------------------------------
        sh_squash = raws[0]                    # появляется вместе с нижней булкой
        if g_alpha > 0.02:
            p.setOpacity(0.22 * g_alpha * sh_squash)
            p.setBrush(QColor(0, 0, 0)); p.setPen(Qt.NoPen)
            sw = base_w * (0.9 + 0.12 * (1 - g_sqy)) * g_scale
            p.drawEllipse(QPointF(cx, area.bottom() + 6 + g_dy * 0.15),
                          sw / 2, area.height() * 0.05)
            p.setOpacity(1.0)

        # --- сам бургер (в глобальном преобразовании) ------------------------
        p.save()
        pivot_y = area.bottom()
        p.translate(cx, pivot_y + g_dy)
        p.rotate(g_rot)
        p.scale(g_scale, g_scale * g_sqy)
        p.translate(-cx, -pivot_y)
        p.setOpacity(g_alpha)

        cur_bottom = area.bottom()
        for i in range(N_REAL):
            name, th, spread = REAL_LAYERS[i]
            lh = th / total * area.height()
            lw = base_w * (1 + spread)
            seat_top = cur_bottom - lh
            raw = raws[i]
            yoff, squash, xlean, rot = self._layer_motion(
                raw, -1 if i % 2 else 1, fall_h, lw)

            wscale = 1.0 + (1.0 - squash) * 0.6
            draw_h = lh * squash
            draw_w = lw * wscale
            top = seat_top + (lh - draw_h) + yoff
            lx = cx + xlean
            alpha = min(1.0, raw / 0.12) if raw < 0.12 else 1.0

            p.setOpacity(g_alpha * alpha)
            if abs(rot) > 0.1:
                p.save()
                p.translate(lx, top + draw_h)
                p.rotate(rot)
                _draw_ingredient(p, name, QRectF(-draw_w / 2, -draw_h, draw_w, draw_h))
                p.restore()
            else:
                _draw_ingredient(p, name, QRectF(lx - draw_w / 2, top, draw_w, draw_h))
            cur_bottom = seat_top
        p.setOpacity(1.0)
        p.restore()

        # --- искры + пар (в фазе праздника) ----------------------------------
        if self.A_END <= t < self.H_END:
            self._draw_sparkles(p, t, area, cx)
            self._draw_steam(p, t, area, cx)

    # ---- эффекты ----------------------------------------------------------

    def _draw_sparkles(self, p: QPainter, t: float, area: QRectF, cx: float) -> None:
        k = (t - self.A_END) / (self.H_END - self.A_END)
        top = area.top() + area.height() * 0.18
        spots = [(-0.42, 0.15, 0.00), (0.44, 0.10, 0.18),
                 (-0.30, 0.55, 0.36), (0.34, 0.50, 0.10), (0.0, -0.05, 0.28)]
        for fx, fy, delay in spots:
            lk = (k - delay) / 0.34
            if lk <= 0 or lk >= 1:
                continue
            s = math.sin(lk * math.pi)              # 0→1→0
            r = area.width() * 0.10 * s
            x = cx + fx * area.width() * 0.55
            y = top + fy * area.height()
            p.setOpacity(s)
            p.setBrush(QColor("#fff3c4")); p.setPen(Qt.NoPen)
            star = QPainterPath()
            star.moveTo(x, y - r); star.lineTo(x + r * 0.28, y - r * 0.28)
            star.lineTo(x + r, y); star.lineTo(x + r * 0.28, y + r * 0.28)
            star.lineTo(x, y + r); star.lineTo(x - r * 0.28, y + r * 0.28)
            star.lineTo(x - r, y); star.lineTo(x - r * 0.28, y - r * 0.28)
            star.closeSubpath()
            p.drawPath(star)
        p.setOpacity(1.0)

    def _draw_steam(self, p: QPainter, t: float, area: QRectF, cx: float) -> None:
        p.setPen(QPen(QColor("#ffffff"), max(2.0, area.width() * 0.02)))
        p.setBrush(Qt.NoBrush)
        top = area.top() + area.height() * 0.06
        for wi, off in enumerate((-0.14, 0.0, 0.14)):
            phase = (t * 6.0 + wi * 0.7)
            path = QPainterPath()
            x0 = cx + off * area.width()
            path.moveTo(x0, top + area.height() * 0.10)
            for s in range(1, 7):
                fy = s / 6.0
                x = x0 + math.sin(phase + fy * 4.2) * area.width() * 0.05
                y = top + area.height() * 0.10 - fy * area.height() * 0.22
                path.lineTo(x, y)
            # верх пара тает
            fade = 0.35 * (0.6 + 0.4 * math.sin(phase))
            p.setOpacity(max(0.0, fade))
            p.drawPath(path)
        p.setOpacity(1.0)
