"""mode_menu.py — меню режимов с МОРФОМ бургера.

По клику на бургер: фон затемняется, а слои бургера «разъезжаются» из иконки и
превращаются в полоски меню (морф), после чего проявляется реальный контент карточки.
Карточка — слева-сверху (под бургером). Клик по фону — закрыть.
"""
from __future__ import annotations

from PySide6.QtCore import (Qt, Signal, QPropertyAnimation, QEasingCurve, QRect,
                            QRectF, Property)
from PySide6.QtGui import QPainter, QColor, QPen, QBrush
from PySide6.QtWidgets import (QWidget, QLabel, QFrame, QVBoxLayout, QHBoxLayout,
                               QPushButton, QGraphicsOpacityEffect)

from .theme import STEP_COLORS
from .burger import LAYERS, N_LAYERS, FILL, ACCENT, layer_fill

MODES = [
    ("1", "Twitch-клипы", "Готовый клип → вертикаль", ""),
    ("2", "Метки через бота", "Стример метит моменты в чате", "Этап 2 · скоро"),
    ("3", "Автопоиск ИИ", "ИИ сам находит лучшие моменты", "Этап 3 · скоро"),
]

# Порядок слоёв сверху вниз в карточке: bun_top→заголовок, 3 начинки→3 кнопки, bun_bottom→низ.
_ORDER = [4, 3, 2, 1, 0]               # индексы LAYERS сверху вниз
_WEIGHTS = [1.3, 1.5, 1.5, 1.5, 0.9]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_rect(a: QRectF, b: QRectF, t: float) -> QRectF:
    return QRectF(_lerp(a.x(), b.x(), t), _lerp(a.y(), b.y(), t),
                  _lerp(a.width(), b.width(), t), _lerp(a.height(), b.height(), t))


def _burger_layer_rects(bound: QRectF) -> list[QRectF]:
    """Прямоугольники слоёв в маленьком стопка-бургере (индекс = LAYERS, снизу вверх)."""
    total = sum(l[1] for l in LAYERS)
    base_w = bound.width() * 0.82
    cx = bound.center().x()
    cur_bottom = bound.bottom()
    rects: list[QRectF] = []
    for name, th, spread in LAYERS:
        lh = th / total * bound.height()
        lw = base_w * (1 + spread)
        top = cur_bottom - lh
        rects.append(QRectF(cx - lw / 2, top, lw, lh))
        cur_bottom = top
    return rects


def _target_bands(R: QRectF) -> list[QRectF]:
    """Целевые полоски в карточке (индекс = LAYERS)."""
    pad = 12
    inner = R.adjusted(pad, pad, -pad, -pad)
    total = sum(_WEIGHTS)
    rects = [QRectF()] * N_LAYERS
    y = inner.top()
    for k, li in enumerate(_ORDER):
        bh = _WEIGHTS[k] / total * inner.height()
        rects[li] = QRectF(inner.left(), y, inner.width(), bh - 3)
        y += bh
    return rects


class ModeMenuOverlay(QWidget):
    mode_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("modeOverlay")
        self.hide()
        self._t = 0.0
        self._start: list[QRectF] = []
        self._target: list[QRectF] = []
        self._card_rect = QRectF()

        # Карточка с реальным контентом (проявляется в конце морфа).
        self.card = QFrame(self); self.card.setObjectName("card")
        cl = QVBoxLayout(self.card); cl.setContentsMargins(20, 16, 20, 16); cl.setSpacing(10)
        title = QLabel("Режим работы")
        title.setStyleSheet("font-size:17px;font-weight:800;")
        cl.addWidget(title)
        for i, (num, name, desc, lock) in enumerate(MODES):
            color = STEP_COLORS[i % len(STEP_COLORS)]
            b = QPushButton(); b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton{{text-align:left;background:#191826;border:1px solid #2a2740;"
                f"border-radius:12px;padding:12px;}} QPushButton:hover{{border-color:{color};}}")
            bl = QHBoxLayout(b); bl.setContentsMargins(10, 6, 10, 6); bl.setSpacing(12)
            n = QLabel(num); n.setFixedSize(32, 32); n.setAlignment(Qt.AlignCenter)
            n.setStyleSheet(f"background:{color};color:#fff;border-radius:9px;font-weight:800;font-size:15px;")
            lock_html = (f"<br><span style='color:{color};font-size:11px'>{lock}</span>" if lock else "")
            txt = QLabel(f"<b style='font-size:14px'>{name}</b><br>"
                         f"<span style='color:#c2bde0;font-size:12px'>{desc}</span>{lock_html}")
            bl.addWidget(n); bl.addWidget(txt); bl.addStretch(1)
            b.clicked.connect(lambda _=False, idx=i: self._choose(idx))
            cl.addWidget(b)
        hint = QLabel("Клик вне карточки — закрыть")
        hint.setStyleSheet("color:#8f8ab0;font-size:11px;")
        cl.addWidget(hint)

        self._card_fx = QGraphicsOpacityEffect(self.card)
        self._card_fx.setOpacity(0.0)
        self.card.setGraphicsEffect(self._card_fx)

        self._anim = QPropertyAnimation(self, b"morph", self)
        self._anim.setDuration(500)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    # ---- морф-свойство ---------------------------------------------------

    def get_morph(self) -> float:
        return self._t

    def set_morph(self, v: float) -> None:
        self._t = v
        # контент карточки проявляется во второй половине морфа
        self._card_fx.setOpacity(max(0.0, min(1.0, (v - 0.5) / 0.42)))
        self.update()

    morph = Property(float, get_morph, set_morph)

    # ---- открытие --------------------------------------------------------

    def open(self, anchor: QRect) -> None:
        if self.parent():
            self.setGeometry(self.parent().rect())
        self.card.adjustSize()
        cw = max(360, self.card.sizeHint().width())
        ch = self.card.sizeHint().height()
        R = QRectF(anchor.left(), anchor.bottom() + 8, cw, ch)
        self._card_rect = R
        self.card.setGeometry(int(R.x()), int(R.y()), int(R.width()), int(R.height()))

        # старт — маленький бургер в позиции иконки
        bound = QRectF(anchor).adjusted(4, 2, -4, -2)
        self._start = _burger_layer_rects(bound)
        self._target = _target_bands(R)

        self.raise_(); self.show()
        self._anim.stop()
        self._card_fx.setOpacity(0.0)
        self._anim.setStartValue(0.0); self._anim.setEndValue(1.0)
        self._anim.start()

    def _choose(self, idx: int) -> None:
        self.mode_selected.emit(idx)
        self.hide()

    def mousePressEvent(self, e) -> None:
        if not self.card.geometry().contains(e.position().toPoint()):
            self.hide()

    # ---- отрисовка -------------------------------------------------------

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # затемнение фона
        p.fillRect(self.rect(), QColor(8, 8, 13, int(self._t * 0.66 * 255)))
        # морфящиеся слои бургера; растворяются по мере проявления контента (без грязи)
        if self._start and self._t < 0.9:
            e = self._t
            # альфа: полная до 0.5, к 0.85 → 0
            fade = 1.0 if e < 0.5 else max(0.0, 1.0 - (e - 0.5) / 0.35)
            a = int(fade * 255)
            for i in range(N_LAYERS):
                r = _lerp_rect(self._start[i], self._target[i], e)
                name = LAYERS[i][0]
                col = QColor(layer_fill(name)); col.setAlpha(a)
                edge = QColor(ACCENT); edge.setAlpha(a)
                p.setBrush(col); p.setPen(QPen(edge, 2))
                rad = min(12.0, r.height() / 2)
                p.drawRoundedRect(r, rad, rad)
