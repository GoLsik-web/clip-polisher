"""marks_mode.py — Этап 2, экран «Метки через бота».

Из ОДНОГО стрима + файла меток бота собираем ОДИН вертикальный клип-склейку:
метки (по типам автора) → моменты (схлопывание/пороги/окно) → отрезки → рендер.

Дизайн строго в системе проекта (theme.py: панели-карточки, скругления, палитра,
ChipRow/ToggleSwitch). Центр экрана — MarksTimeline: тепловая дорожка + пины по
авторам + моменты-отрезки. Слева — компактная колонка настроек.

Виджет самодостаточный: держит MarksFile + TimeMap, пересчитывает моменты и по
запросу собирает PipelineConfig. Реальный рендер запускает MainWindow через worker.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRect, QRectF, QPointF
from PySide6.QtGui import (QPainter, QColor, QPen, QBrush, QPainterPath,
                           QLinearGradient, QPolygonF, QFont)
from PySide6.QtWidgets import (QWidget, QLabel, QFrame, QVBoxLayout, QHBoxLayout,
                               QPushButton, QCheckBox, QComboBox, QLineEdit,
                               QDoubleSpinBox, QScrollArea, QSizePolicy, QFileDialog,
                               QButtonGroup, QStackedWidget)

from core.marks import (MarksFile, Mark, Moment, AuthorType, AudienceMode,
                        select_moments, resolve_mode, by_heat, DEFAULT_WINDOW)
from core.timemap import TimeMap
from core.config import Segment
from .theme import PALETTE, ZONE_COLORS
from .widgets import ToggleSwitch, ChipRow, Chip, EyeToggle
from .preview_panel import EditorPanel
from .bot_panel import BotPanel


# Цвета типов авторов — как значки ролей в Twitch: стример красный, модер зелёный,
# вип розовый/маджента, зритель — фирменный фиолетовый Twitch.
AUTHOR_COLORS = {
    AuthorType.STREAMER:  "#e91916",   # broadcaster (красный)
    AuthorType.MODERATOR: "#00ad03",   # moderator (зелёный меч)
    AuthorType.VIP:       "#e005b9",    # VIP (розовый ромб)
    AuthorType.VIEWER:    "#9146ff",   # Twitch purple (обычный зритель)
}
AUTHOR_LABEL = {
    AuthorType.STREAMER:  "Стример",
    AuthorType.MODERATOR: "Модер",
    AuthorType.VIP:       "Вип",
    AuthorType.VIEWER:    "Зритель",
}
AUTHOR_ORDER = [AuthorType.STREAMER, AuthorType.MODERATOR, AuthorType.VIP, AuthorType.VIEWER]

MODE_LABELS = ["Авто", "Камерный", "Средний", "Большой"]
_LABEL_TO_MODE = {
    "Авто": AudienceMode.AUTO, "Камерный": AudienceMode.SMALL,
    "Средний": AudienceMode.MEDIUM, "Большой": AudienceMode.LARGE,
}
_MODE_HINT = {
    AudienceMode.SMALL:  "камерный онлайн — учитывается каждая метка зрителя",
    AudienceMode.MEDIUM: "средний онлайн — момент от 3 разных зрителей",
    AudienceMode.LARGE:  "большой онлайн — момент от 10 разных зрителей",
}


def _flame_pixmap(color: str, h: int = 14):
    """Нарисованная иконка-пламя (для «жара») — без эмодзи, чтобы не было «тофу»
    в offscreen/без системных эмодзи-шрифтов (грабля проекта, см. CLAUDE.md)."""
    from PySide6.QtGui import QPixmap, QPainterPath
    w = max(8, int(h * 0.72))
    pm = QPixmap(w, h); pm.fill(Qt.transparent)
    pt = QPainter(pm); pt.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.moveTo(w * 0.5, 0)
    path.cubicTo(w * 1.05, h * 0.34, w * 0.86, h * 1.0, w * 0.5, h * 1.0)
    path.cubicTo(w * 0.14, h * 1.0, w * -0.05, h * 0.46, w * 0.5, 0)
    pt.fillPath(path, QColor(color)); pt.end()
    return pm


def _fmt(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec) // 3600
    m = (int(sec) % 3600) // 60
    s = int(sec) % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ==========================================================================
# Таймлайн меток — центральный виджет
# ==========================================================================

class MarksTimeline(QWidget):
    """Дорожка всего стрима: тепловая полоса плотности меток + пины по авторам +
    моменты-отрезки (перетаскиваемые границы) + зоны «нет записи» (дыры)."""

    PAD = 14
    HEAT_H = 34
    RULER_H = 18
    moment_edited = Signal(int, float, float)   # (индекс, start, end) в сек стрима
    moment_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(self.HEAT_H + 74 + self.RULER_H)
        self.setMouseTracking(True)
        self._theme = "dark"
        self._dur = 0.0
        self._marks: list[Mark] = []
        self._moments: list[Moment] = []
        self._gaps: list[tuple[float, float]] = []      # дыры записи (сек стрима)
        self._active = -1
        self._dim_types: set[AuthorType] = set(AUTHOR_ORDER)   # какие авторы яркие
        self._drag: Optional[tuple[int, str]] = None    # (индекс момента, 'start'|'end'|'move')
        self._press_x = 0
        self._orig = (0.0, 0.0)

    # ---- API ----
    def set_theme(self, theme: str) -> None:
        self._theme = theme; self.update()

    def set_duration(self, dur: float) -> None:
        self._dur = max(0.0, dur); self.update()

    def set_marks(self, marks: list[Mark]) -> None:
        self._marks = list(marks); self.update()

    def set_moments(self, moments: list[Moment]) -> None:
        self._moments = list(moments)
        if self._active >= len(self._moments):
            self._active = -1
        self.update()

    def set_gaps(self, gaps: list[tuple[float, float]]) -> None:
        self._gaps = list(gaps); self.update()

    def set_active(self, idx: int) -> None:
        self._active = idx; self.update()

    def set_visible_authors(self, types: set[AuthorType]) -> None:
        self._dim_types = set(types) if types else set(AUTHOR_ORDER)
        self.update()

    # ---- геометрия ----
    def _track(self) -> QRect:
        top = 6 + self.HEAT_H + 4
        h = self.height() - top - self.RULER_H - 4
        return QRect(self.PAD, top, self.width() - 2 * self.PAD, h)

    def _heat_rect(self) -> QRect:
        return QRect(self.PAD, 6, self.width() - 2 * self.PAD, self.HEAT_H)

    def _t2x(self, t: float) -> float:
        tr = self._track()
        if self._dur <= 0:
            return tr.left()
        return tr.left() + (t / self._dur) * tr.width()

    def _x2t(self, x: float) -> float:
        tr = self._track()
        if tr.width() <= 0 or self._dur <= 0:
            return 0.0
        return min(max(0.0, (x - tr.left()) / tr.width() * self._dur), self._dur)

    # ---- мышь ----
    def mousePressEvent(self, e) -> None:
        if self._dur <= 0:
            return
        x = e.position().x()
        for i, mo in enumerate(self._moments):
            sx, ex = self._t2x(mo.start), self._t2x(mo.end)
            if abs(x - sx) <= 7:
                self._drag = (i, "start")
            elif abs(x - ex) <= 7:
                self._drag = (i, "end")
            elif sx < x < ex:
                self._drag = (i, "move")
            else:
                continue
            self._active = i
            self._press_x = x
            self._orig = (mo.start, mo.end)
            self.moment_clicked.emit(i)
            self.update()
            return

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        if self._drag is None:
            near = any(abs(x - self._t2x(mo.start)) <= 7 or abs(x - self._t2x(mo.end)) <= 7
                       for mo in self._moments)
            self.setCursor(Qt.SizeHorCursor if near else Qt.ArrowCursor)
            return
        i, part = self._drag
        mo = self._moments[i]
        t = self._x2t(x)
        if part == "start":
            mo.start = min(t, mo.end - 0.5)
        elif part == "end":
            mo.end = max(t, mo.start + 0.5)
        else:
            dt = self._x2t(x) - self._x2t(self._press_x)
            length = self._orig[1] - self._orig[0]
            ns = min(max(0.0, self._orig[0] + dt), self._dur - length)
            mo.start, mo.end = ns, ns + length
        self.update()
        self.moment_edited.emit(i, mo.start, mo.end)

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None

    # ---- отрисовка ----
    def paintEvent(self, _e) -> None:
        c = PALETTE[self._theme]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tr = self._track()

        # фон дорожки
        p.setPen(Qt.NoPen); p.setBrush(QColor(c["panel2"]))
        p.drawRoundedRect(tr, 8, 8)

        if self._dur <= 0:
            p.setPen(QColor(c["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "Загрузите видео стрима и файл меток — здесь появится дорожка")
            return

        self._draw_heat(p, c)

        # дыры записи (нет footage) — штриховка
        for gs, ge in self._gaps:
            gx1, gx2 = self._t2x(gs), self._t2x(ge)
            gr = QRectF(gx1, tr.top(), max(1.0, gx2 - gx1), tr.height())
            p.save(); path = QPainterPath(); path.addRect(gr); p.setClipPath(path)
            p.fillRect(gr, QColor(0, 0, 0, 90))
            pen = QPen(QColor(c["muted"]), 1); pen.setStyle(Qt.DotLine)
            p.setPen(pen)
            step = 7
            xx = gx1 - gr.height()
            while xx < gx2:
                p.drawLine(QPointF(xx, tr.bottom()), QPointF(xx + gr.height(), tr.top()))
                xx += step
            p.restore()

        # моменты-отрезки
        for i, mo in enumerate(self._moments):
            self._draw_moment(p, c, i, mo, tr)

        # пины меток по авторам
        self._draw_pins(p, tr)

        # линейка времени
        self._draw_ruler(p, c, tr)

    def _draw_heat(self, p: QPainter, c: dict) -> None:
        hr = self._heat_rect()
        p.setPen(Qt.NoPen); p.setBrush(QColor(c["panel2"]))
        p.drawRoundedRect(hr, 6, 6)
        if not self._marks:
            return
        # плотность меток по бинам (сглаженная площадная кривая = «тепло»)
        bins = max(24, hr.width() // 6)
        dens = [0.0] * bins
        for m in self._marks:
            b = int((m.t / self._dur) * (bins - 1)) if self._dur else 0
            b = min(max(0, b), bins - 1)
            w = 4 if m.type == AuthorType.STREAMER else (2 if m.type in
                (AuthorType.MODERATOR, AuthorType.VIP) else 1)
            dens[b] += w
            if b > 0: dens[b - 1] += w * 0.5
            if b < bins - 1: dens[b + 1] += w * 0.5
        peak = max(dens) or 1.0
        path = QPainterPath()
        path.moveTo(hr.left(), hr.bottom())
        for k in range(bins):
            x = hr.left() + (k / (bins - 1)) * hr.width()
            y = hr.bottom() - (dens[k] / peak) * (hr.height() - 4)
            path.lineTo(x, y)
        path.lineTo(hr.right(), hr.bottom())
        path.closeSubpath()
        grad = QLinearGradient(0, hr.top(), 0, hr.bottom())
        acc = QColor(c["accent"])
        g1 = QColor(acc); g1.setAlpha(190); g2 = QColor(acc); g2.setAlpha(40)
        grad.setColorAt(0, g1); grad.setColorAt(1, g2)
        p.save(); clip = QPainterPath(); clip.addRoundedRect(QRectF(hr), 6, 6)
        p.setClipPath(clip)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(grad)); p.drawPath(path)
        p.restore()
        p.setPen(QColor(c["muted"]))
        f = p.font(); f.setPointSizeF(8.0); p.setFont(f)
        p.drawText(hr.adjusted(8, 0, -8, 0), Qt.AlignLeft | Qt.AlignVCenter, "Плотность меток")

    def _draw_moment(self, p: QPainter, c: dict, i: int, mo: Moment, tr: QRect) -> None:
        sx, ex = self._t2x(mo.start), self._t2x(mo.end)
        active = (i == self._active)
        acc = QColor(c["accent"])
        fill = QColor(acc); fill.setAlpha(70 if active else 34)
        p.setPen(Qt.NoPen); p.setBrush(fill)
        p.drawRoundedRect(QRectF(sx, tr.top() + 1, ex - sx, tr.height() - 2), 5, 5)
        pen = QPen(acc, 2 if active else 1.2)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(sx, tr.top() + 1, ex - sx, tr.height() - 2), 5, 5)
        # номер момента
        badge = QRectF(sx + 3, tr.top() + 3, 18, 15)
        p.setPen(Qt.NoPen); p.setBrush(acc)
        p.drawRoundedRect(badge, 4, 4)
        p.setPen(QColor("#ffffff"))
        f = p.font(); f.setPointSizeF(8.0); f.setBold(True); p.setFont(f)
        p.drawText(badge, Qt.AlignCenter, str(i + 1))
        # ручки границ у активного
        if active:
            p.setBrush(acc); p.setPen(Qt.NoPen)
            for hx in (sx, ex):
                p.drawRoundedRect(QRectF(hx - 2.5, tr.top() + 1, 5, tr.height() - 2), 2, 2)

    def _draw_pins(self, p: QPainter, tr: QRect) -> None:
        for m in self._marks:
            x = self._t2x(m.t)
            col = QColor(AUTHOR_COLORS.get(m.type, "#5a86d8"))
            if m.type not in self._dim_types:
                col.setAlpha(45)
            tall = m.type == AuthorType.STREAMER
            h = tr.height() * (0.62 if tall else 0.44)
            y0 = tr.bottom() - h
            p.setPen(QPen(col, 2 if tall else 1.5))
            p.drawLine(QPointF(x, tr.bottom() - 2), QPointF(x, y0))
            p.setPen(Qt.NoPen); p.setBrush(col)
            p.drawEllipse(QPointF(x, y0), 2.6 if tall else 2.0, 2.6 if tall else 2.0)

    def _draw_ruler(self, p: QPainter, c: dict, tr: QRect) -> None:
        p.setPen(QColor(c["muted"]))
        f = p.font(); f.setPointSizeF(8.0); f.setBold(False); p.setFont(f)
        y = tr.bottom() + 4
        n = 6
        for k in range(n + 1):
            t = self._dur * k / n
            x = self._t2x(t)
            # Крайние подписи прижимаем ВНУТРЬ дорожки: раньше их рамка вылезала за
            # виджет и «0:00»/«2:00:00» обрезались по краям.
            if k == 0:
                box, al = QRectF(x, y, 60, self.RULER_H), Qt.AlignLeft
            elif k == n:
                box, al = QRectF(x - 60, y, 60, self.RULER_H), Qt.AlignRight
            else:
                box, al = QRectF(x - 30, y, 60, self.RULER_H), Qt.AlignHCenter
            p.drawText(box, al | Qt.AlignVCenter, _fmt(t))


# ==========================================================================
# Карточка момента в списке
# ==========================================================================

class MomentCard(QFrame):
    toggled = Signal(int, bool)
    clicked = Signal(int)

    def __init__(self, idx: int, mo: Moment, theme: str, parent=None):
        super().__init__(parent)
        self.idx = idx
        self._theme = theme
        self.setObjectName("panel2")
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self); lay.setContentsMargins(10, 8, 10, 8); lay.setSpacing(10)

        self.chk = QCheckBox(); self.chk.setChecked(True)
        self.chk.toggled.connect(lambda on: self.toggled.emit(self.idx, on))
        lay.addWidget(self.chk)

        num = QLabel(str(idx + 1)); num.setFixedSize(24, 24); num.setAlignment(Qt.AlignCenter)
        c = PALETTE[theme]
        num.setStyleSheet(f"background:{c['accent']};color:#fff;border-radius:7px;"
                          f"font-weight:800;font-size:12px;")
        lay.addWidget(num)

        mid = QVBoxLayout(); mid.setSpacing(2)
        title = mo.label or f"Момент {idx + 1}"
        t = QLabel(title); t.setStyleSheet("font-weight:700;font-size:13px;")
        rng = QLabel(f"{_fmt(mo.start)} – {_fmt(mo.end)}  ·  {mo.duration:.0f} с")
        rng.setStyleSheet(f"color:{c['muted']};font-size:11px;")
        mid.addWidget(t); mid.addWidget(rng)
        lay.addLayout(mid, 1)

        # авторские точки (кто метил) + «жар»
        dots = QHBoxLayout(); dots.setSpacing(3)
        present = []
        for at in AUTHOR_ORDER:
            if any(m.type == at for m in mo.marks):
                present.append(at)
        for at in present:
            d = QLabel(); d.setFixedSize(9, 9)
            d.setStyleSheet(f"background:{AUTHOR_COLORS[at]};border-radius:4px;")
            d.setToolTip(AUTHOR_LABEL[at])
            dots.addWidget(d)
        wdots = QWidget(); wdots.setLayout(dots)
        lay.addWidget(wdots)

        heat = QWidget(); hb = QHBoxLayout(heat); hb.setContentsMargins(0, 0, 0, 0); hb.setSpacing(3)
        icon = QLabel(); icon.setPixmap(_flame_pixmap(c["accent"]))
        num = QLabel(str(mo.heat)); num.setStyleSheet(f"color:{c['accent']};font-weight:800;font-size:12px;")
        heat.setToolTip("«Жар» момента — суммарный вес по авторам меток")
        hb.addWidget(icon); hb.addWidget(num)
        lay.addWidget(heat)

        self.setStyleSheet(
            f"QFrame#panel2{{background:{c['panel2']};border:1px solid {c['line']};"
            f"border-radius:10px;}}")

    def set_selected(self, on: bool) -> None:
        c = PALETTE[self._theme]
        border = c["accent"] if on else c["line"]
        self.setStyleSheet(
            f"QFrame#panel2{{background:{c['panel2']};border:1px solid {border};"
            f"border-radius:10px;}}")

    def mousePressEvent(self, e) -> None:
        self.clicked.emit(self.idx)
        super().mousePressEvent(e)


# ==========================================================================
# Вспомогательные строки настроек
# ==========================================================================

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


def _plural_marks(n: int) -> str:
    """«1 метка / 2 метки / 5 меток» — иначе интерфейс выглядит машинным."""
    ten, hundred = n % 10, n % 100
    if ten == 1 and hundred != 11:
        return f"{n} метка"
    if 2 <= ten <= 4 and not 12 <= hundred <= 14:
        return f"{n} метки"
    return f"{n} меток"


def _describe_stream(mf: MarksFile, fallback: str = "") -> str:
    """Человеческая подпись эфира: «Эфир 26 июля, 21:04 · Название · 2 ч 15 мин · 12 меток».

    Нужна, чтобы было видно, ОТ КАКОГО стрима эти метки, — безликое имя файла для
    этого не годится, особенно когда за день было два эфира.
    """
    import datetime
    parts = []
    when = ""
    if mf.started_at:
        try:
            d = datetime.datetime.fromisoformat(mf.started_at)
            when = f"Эфир {d.day} {_MONTHS[d.month - 1]}, {d:%H:%M}"
        except (ValueError, TypeError):
            when = ""
    parts.append(when or (fallback or "Метки"))
    if mf.title:
        parts.append(f"«{mf.title}»")
    if mf.duration:
        h, m = int(mf.duration // 3600), int((mf.duration % 3600) // 60)
        parts.append(f"{h} ч {m:02d} мин" if h else f"{m} мин")
    parts.append(_plural_marks(len(mf.marks)))
    if mf.online:
        parts.append(f"онлайн {mf.online}")
    return "  ·  ".join(parts)


def _section(title: str, theme: str) -> tuple[QFrame, QVBoxLayout]:
    c = PALETTE[theme]
    card = QFrame(); card.setObjectName("card")
    card.setStyleSheet(f"QFrame#card{{background:{c['panel']};border:1px solid {c['line']};"
                       f"border-radius:12px;}}")
    v = QVBoxLayout(card); v.setContentsMargins(14, 12, 14, 12); v.setSpacing(9)
    h = QLabel(title); h.setStyleSheet("font-weight:800;font-size:12px;letter-spacing:.5px;")
    v.addWidget(h)
    return card, v


def _toggle_row(text: str, on: bool, theme: str) -> tuple[QWidget, ToggleSwitch]:
    c = PALETTE[theme]
    w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0)
    lab = QLabel(text); lab.setStyleSheet(f"font-size:12px;color:{c['text']};")
    sw = ToggleSwitch(); sw.setChecked(on)
    l.addWidget(lab); l.addStretch(1); l.addWidget(sw)
    return w, sw


def _eye_row(text: str, color: str, on: bool, theme: str) -> tuple[QWidget, EyeToggle]:
    """Строка панели с глаз-переключателем видимости (цвет — под зону)."""
    c = PALETTE[theme]
    w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(8)
    sw = QLabel(); sw.setFixedSize(11, 11)
    sw.setStyleSheet(f"background:{color};border-radius:3px;")
    lab = QLabel(text); lab.setStyleSheet(f"font-size:12px;color:{c['text']};")
    eye = EyeToggle(color); eye.setChecked(on)
    l.addWidget(sw); l.addWidget(lab); l.addStretch(1); l.addWidget(eye)
    return w, eye


# ==========================================================================
# Главная панель режима 2
# ==========================================================================

class MarksModePanel(QWidget):
    """Экран «Метки через бота»: слева настройки, справа таймлайн + список моментов."""

    render_requested = Signal()

    def __init__(self, theme: str = "dark", parent=None):
        super().__init__(parent)
        self._theme = theme
        self._mf: Optional[MarksFile] = None
        self._video: str = ""
        self._timemap: Optional[TimeMap] = None
        self._moments: list[Moment] = []
        self._included: list[bool] = []
        self._cards: list[MomentCard] = []
        self._sort_heat = False
        self._build()
        self._update_empty()

    # ---- сборка UI ----
    def _build(self) -> None:
        root = QHBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(14)
        root.addWidget(self._left_column(), 36)
        root.addWidget(self._right_column(), 64)

    def _left_column(self) -> QWidget:
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget(); col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 4, 0); col.setSpacing(12)

        # --- Источник ---
        s1, v1 = _section("ИСТОЧНИК ТРАНСЛЯЦИИ", self._theme)
        self.video_btn = QPushButton("Выбрать видео стрима")
        self.video_btn.clicked.connect(self._pick_video)
        self.video_lbl = self._muted_label("Видео не выбрано")
        self.marks_btn = QPushButton("Импортировать файл меток")
        self.marks_btn.clicked.connect(self._pick_marks)
        self.marks_lbl = self._muted_label("Файл меток не загружен")
        v1.addWidget(self.video_btn); v1.addWidget(self.video_lbl)
        v1.addWidget(self.marks_btn); v1.addWidget(self.marks_lbl)
        self.warn_lbl = QLabel(""); self.warn_lbl.setWordWrap(True)
        self.warn_lbl.setStyleSheet("color:#f0a93a;font-size:11px;")
        self.warn_lbl.setVisible(False)
        v1.addWidget(self.warn_lbl)
        # неполная запись
        rowp, self.partial_sw = _toggle_row("Запись неполная", False, self._theme)
        self.partial_sw.toggled.connect(self._on_partial)
        v1.addWidget(rowp)
        self.partial_box = QWidget()
        pb = QHBoxLayout(self.partial_box); pb.setContentsMargins(0, 0, 0, 0); pb.setSpacing(6)
        pb.addWidget(self._muted_label("Запись началась позже эфира на"))
        self.offset_spin = QDoubleSpinBox(); self.offset_spin.setRange(0, 100000)
        self.offset_spin.setSuffix(" с"); self.offset_spin.setDecimals(0)
        self.offset_spin.valueChanged.connect(lambda _v: self._recompute())
        pb.addWidget(self.offset_spin); pb.addStretch(1)
        self.partial_box.setVisible(False)
        v1.addWidget(self.partial_box)
        # Откуда взять метки, если их ещё нет: бот живёт во вкладке «Бот» справа.
        self.bot_hint_btn = QPushButton("Нет меток? Включить бота в чате")
        self.bot_hint_btn.setToolTip("Вкладка «Бот» справа: вход через Twitch одной кнопкой, "
                                     "потом зрители метят моменты командой !clip")
        self.bot_hint_btn.clicked.connect(self._show_bot_tab)
        v1.addWidget(self.bot_hint_btn)
        # Путь «по ссылке на трансляцию» — скачивание кусков VOD, следующий шаг.
        link_row = QWidget(); lr = QHBoxLayout(link_row); lr.setContentsMargins(0, 4, 0, 0); lr.setSpacing(6)
        self.link_edit = QLineEdit(); self.link_edit.setEnabled(False)
        self.link_edit.setPlaceholderText("Ссылка на запись стрима — скоро")
        self.link_btn = QPushButton("По ссылке"); self.link_btn.setEnabled(False)
        self.link_btn.setToolTip("Скачивание только нужных кусков записи по ссылке "
                                 "(yt-dlp) — следующий шаг. Пока выбирай файл видео.")
        lr.addWidget(self.link_edit, 1); lr.addWidget(self.link_btn)
        v1.addWidget(link_row)
        col.addWidget(s1)

        # --- Режим отбора ---
        s2, v2 = _section("РЕЖИМ ОТБОРА МОМЕНТОВ", self._theme)
        self.mode_chips = ChipRow(MODE_LABELS)
        self.mode_chips.changed.connect(lambda _t: self._recompute())
        v2.addWidget(self.mode_chips)
        self.mode_hint = self._muted_label("")
        v2.addWidget(self.mode_hint)
        # окно вокруг метки
        wrow = QWidget(); wl = QHBoxLayout(wrow); wl.setContentsMargins(0, 0, 0, 0); wl.setSpacing(6)
        wl.addWidget(self._muted_label("Окно: до"))
        self.before_spin = QDoubleSpinBox(); self.before_spin.setRange(1, 300)
        self.before_spin.setValue(-DEFAULT_WINDOW[0]); self.before_spin.setSuffix(" с")
        self.before_spin.setDecimals(0)
        self.after_spin = QDoubleSpinBox(); self.after_spin.setRange(0, 120)
        self.after_spin.setValue(DEFAULT_WINDOW[1]); self.after_spin.setSuffix(" с")
        self.after_spin.setDecimals(0)
        for sp in (self.before_spin, self.after_spin):
            sp.valueChanged.connect(lambda _v: self._recompute())
        wl.addWidget(self.before_spin); wl.addWidget(self._muted_label("после"))
        wl.addWidget(self.after_spin); wl.addStretch(1)
        v2.addWidget(wrow)
        # фильтр-подсветка по автору
        v2.addWidget(self._muted_label("Подсветка по автору метки:"))
        self.author_filter = _AuthorFilter(self._theme)
        self.author_filter.changed.connect(self._on_author_filter)
        v2.addWidget(self.author_filter)
        col.addWidget(s2)

        # --- Раскладка: подсказка (сам редактор — во вкладке «Раскладка» справа) ---
        s3, v3 = _section("РАСКЛАДКА", self._theme)
        v3.addWidget(self._muted_label(
            "Настрой раскладку и выключи ненужные панели во вкладке «Раскладка» справа "
            "(пресеты A/B/C/D + ручное перетаскивание зон, как в обычном режиме)."))
        col.addWidget(s3)

        # --- Звук ---
        s4, v4 = _section("ЗВУК", self._theme)
        self.audio_switches: dict[str, ToggleSwitch] = {}
        for key, name, on in [("loudnorm", "Нормализация громкости", True),
                              ("denoise", "Шумодав", False),
                              ("clarity", "Чёткость голоса", False),
                              ("gate", "Гейт (тишина в паузах)", False)]:
            row, sw = _toggle_row(name, on, self._theme)
            self.audio_switches[key] = sw
            v4.addWidget(row)
        v4.addWidget(self._muted_label("Мат:"))
        self.prof_chips = ChipRow(["Не трогать", "Бип", "Заглушить"])
        v4.addWidget(self.prof_chips)
        col.addWidget(s4)

        # --- Экспорт ---
        s5, v5 = _section("БРЕНДИНГ И ЭКСПОРТ", self._theme)
        nrow = QWidget(); nl = QHBoxLayout(nrow); nl.setContentsMargins(0, 0, 0, 0); nl.setSpacing(6)
        nl.addWidget(self._muted_label("Ник:"))
        self.nick_edit = QLineEdit(); self.nick_edit.setPlaceholderText("подставится из меток")
        nl.addWidget(self.nick_edit, 1)
        v5.addWidget(nrow)
        prow = QWidget(); pl = QHBoxLayout(prow); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(6)
        pl.addWidget(self._muted_label("Платформа:"))
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["Twitch", "YouTube", "Kick", "Без значка"])
        pl.addWidget(self.platform_combo, 1)
        v5.addWidget(prow)
        # Как сохранять: каждый момент отдельным клипом (дефолт) или всё склейкой в один.
        v5.addWidget(self._muted_label("Сохранять:"))
        self.export_chips = ChipRow(["Отдельными клипами", "Склеить в один"])
        self.export_chips.changed.connect(self._on_export_mode)
        v5.addWidget(self.export_chips)
        self.render_btn = QPushButton("Нарезать клипы")
        self.render_btn.setProperty("class", "primary")
        c = PALETTE[self._theme]
        self.render_btn.setStyleSheet(
            f"background:{c['accent']};border:1px solid {c['accent']};color:#fff;"
            f"border-radius:9px;padding:11px 16px;font-weight:800;font-size:13px;")
        self.render_btn.clicked.connect(lambda: self.render_requested.emit())
        v5.addWidget(self.render_btn)
        col.addWidget(s5)

        col.addStretch(1)
        scroll.setWidget(inner)
        scroll.setStyleSheet("QScrollArea{background:transparent;}")
        return scroll

    def _right_column(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)
        c = PALETTE[self._theme]

        # Сегментный переключатель: Моменты (метки/склейка) / Раскладка (редактор) / Бот.
        seg = QWidget(); sl = QHBoxLayout(seg); sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(6)
        self.tab_moments = QPushButton("Моменты"); self.tab_moments.setCheckable(True); self.tab_moments.setChecked(True)
        self.tab_layout = QPushButton("Раскладка"); self.tab_layout.setCheckable(True)
        self.tab_bot = QPushButton("Бот"); self.tab_bot.setCheckable(True)
        self.tab_bot.setToolTip("Вход через Twitch и бот меток прямо здесь — "
                                "без Python и настроек")
        tabcss = (f"QPushButton{{background:{c['panel2']};border:1px solid {c['line']};"
                  f"border-radius:8px;padding:8px 18px;font-weight:700;}}"
                  f"QPushButton:checked{{background:{c['accent']};border-color:{c['accent']};color:#fff;}}")
        for b in (self.tab_moments, self.tab_layout, self.tab_bot):
            b.setCursor(Qt.PointingHandCursor); b.setStyleSheet(tabcss)
        grp = QButtonGroup(self); grp.setExclusive(True)
        grp.addButton(self.tab_moments); grp.addButton(self.tab_layout); grp.addButton(self.tab_bot)
        self.tab_moments.clicked.connect(lambda: self.right_stack.setCurrentIndex(0))
        self.tab_layout.clicked.connect(lambda: self.right_stack.setCurrentIndex(1))
        self.tab_bot.clicked.connect(lambda: self.right_stack.setCurrentIndex(2))
        sl.addWidget(self.tab_moments); sl.addWidget(self.tab_layout)
        sl.addWidget(self.tab_bot); sl.addStretch(1)
        v.addWidget(seg)

        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self._moments_page())
        # Вкладка «Раскладка» — тот же редактор композиции (drag/resize зон + глаза + пресеты),
        # но без дорожки обрезки (её роль тут играет таймлайн меток).
        self.layout_editor = EditorPanel(show_trim=False)
        self.layout_editor.set_theme(self._theme)
        self.right_stack.addWidget(self.layout_editor)
        # Вкладка «Бот» — вход через Twitch + сбор меток; готовые метки уходят сюда же,
        # в левую колонку (как будто их импортировали файлом).
        self.bot_panel = BotPanel(self._theme)
        self.bot_panel.marks_ready.connect(self._on_bot_marks)
        self.right_stack.addWidget(self.bot_panel)
        v.addWidget(self.right_stack, 1)
        return wrap

    def _show_bot_tab(self) -> None:
        self.tab_bot.setChecked(True)
        self.right_stack.setCurrentIndex(2)

    def _on_bot_marks(self, mf: MarksFile, name: str) -> None:
        """Метки от бота → в режим склейки (и сразу показываем вкладку «Моменты»)."""
        self.set_marks_file(mf, name)
        self.tab_moments.setChecked(True)
        self.right_stack.setCurrentIndex(0)

    def _moments_page(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(12)
        c = PALETTE[self._theme]

        # таймлайн в карточке
        tcard = QFrame(); tcard.setObjectName("card")
        tcard.setStyleSheet(f"QFrame#card{{background:{c['panel']};border:1px solid {c['line']};"
                            f"border-radius:12px;}}")
        tl = QVBoxLayout(tcard); tl.setContentsMargins(12, 10, 12, 10); tl.setSpacing(6)
        head = QLabel("ДОРОЖКА СТРИМА  ·  метки и моменты")
        head.setStyleSheet("font-weight:800;font-size:12px;letter-spacing:.5px;")
        tl.addWidget(head)
        tl.addWidget(self._author_legend())
        self.timeline = MarksTimeline()
        self.timeline.set_theme(self._theme)
        self.timeline.moment_edited.connect(self._on_moment_edited)
        self.timeline.moment_clicked.connect(self._select_moment)
        tl.addWidget(self.timeline)
        v.addWidget(tcard)

        # список моментов в карточке
        mcard = QFrame(); mcard.setObjectName("card")
        mcard.setStyleSheet(f"QFrame#card{{background:{c['panel']};border:1px solid {c['line']};"
                            f"border-radius:12px;}}")
        ml = QVBoxLayout(mcard); ml.setContentsMargins(12, 10, 12, 12); ml.setSpacing(8)
        hrow = QWidget(); hl = QHBoxLayout(hrow); hl.setContentsMargins(0, 0, 0, 0)
        self.moments_head = QLabel("МОМЕНТЫ")
        self.moments_head.setStyleSheet("font-weight:800;font-size:12px;letter-spacing:.5px;")
        hl.addWidget(self.moments_head); hl.addStretch(1)
        self.sort_btn = QPushButton("По времени")
        self.sort_btn.setToolTip("Сортировка: по времени / по «жару»")
        self.sort_btn.clicked.connect(self._toggle_sort)
        self.top_btn = QPushButton("Взять топ-5")
        self.top_btn.setToolTip("Отметить 5 самых «горячих» моментов, снять остальные")
        self.top_btn.clicked.connect(self._take_top)
        hl.addWidget(self.sort_btn); hl.addWidget(self.top_btn)
        ml.addWidget(hrow)

        self.moments_scroll = QScrollArea(); self.moments_scroll.setWidgetResizable(True)
        self.moments_scroll.setFrameShape(QFrame.NoFrame)
        # Прозрачным должен быть и сам QScrollArea, и его viewport, и вложенный виджет:
        # иначе на тёмной теме пустой список светился белым прямоугольником.
        self.moments_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}")
        self.moments_scroll.viewport().setAutoFillBackground(False)
        self.moments_host = QWidget()
        self.moments_host.setAutoFillBackground(False)
        self.moments_vl = QVBoxLayout(self.moments_host)
        self.moments_vl.setContentsMargins(0, 0, 4, 0); self.moments_vl.setSpacing(7)
        self.moments_vl.addStretch(1)
        self.moments_scroll.setWidget(self.moments_host)
        ml.addWidget(self.moments_scroll, 1)
        # Пустое состояние: подсказываем, откуда берутся моменты (было просто пусто).
        self.moments_empty = QLabel(
            "Моментов пока нет.\n\nЗагрузи файл меток слева — или включи бота "
            "во вкладке «Бот»,\nчтобы зрители метили моменты командой !clip в чате.")
        self.moments_empty.setAlignment(Qt.AlignCenter)
        self.moments_empty.setStyleSheet(f"color:{c['muted']};font-size:12px;")
        ml.addWidget(self.moments_empty, 1)
        v.addWidget(mcard, 1)
        return wrap

    def _author_legend(self) -> QWidget:
        c = PALETTE[self._theme]
        w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(14)
        for at in AUTHOR_ORDER:
            item = QWidget(); il = QHBoxLayout(item); il.setContentsMargins(0, 0, 0, 0); il.setSpacing(5)
            d = QLabel(); d.setFixedSize(10, 10)
            d.setStyleSheet(f"background:{AUTHOR_COLORS[at]};border-radius:5px;")
            t = QLabel(AUTHOR_LABEL[at]); t.setStyleSheet(f"color:{c['muted']};font-size:11px;")
            il.addWidget(d); il.addWidget(t); l.addWidget(item)
        l.addStretch(1)
        return w

    def _muted_label(self, text: str) -> QLabel:
        c = PALETTE[self._theme]
        lab = QLabel(text); lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{c['muted']};font-size:11px;")
        return lab

    # ---- загрузка данных ----
    def _pick_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Видео стрима", "",
                                              "Видео (*.mp4 *.mkv *.mov *.webm *.flv)")
        if path:
            self.set_video(path)

    def _pick_marks(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Файл меток бота", "",
                                              "Метки (*.json *.clipmarks);;Все файлы (*.*)")
        if path:
            try:
                mf = MarksFile.from_json(path)
            except Exception as ex:  # noqa: BLE001
                self.warn_lbl.setText(f"Не удалось прочитать файл меток: {ex}")
                self.warn_lbl.setVisible(True)
                return
            self.set_marks_file(mf, os.path.basename(path))

    def set_video(self, path: str) -> None:
        self._video = path
        self.video_lbl.setText(os.path.basename(path))
        # длительность видео (для таймлайна и матчинга)
        try:
            from core import ffmpeg_utils as ff
            self._video_dur = ff.probe_video(path).duration
        except Exception:  # noqa: BLE001
            self._video_dur = 0.0
        self._recompute()

    def set_marks_file(self, mf: MarksFile, name: str = "") -> None:
        self._mf = mf
        self.marks_lbl.setText(_describe_stream(mf, name))
        # автоподстановка ника/платформы
        if mf.streamer and not self.nick_edit.text().strip():
            self.nick_edit.setText(mf.streamer)
        pmap = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}
        if mf.platform in pmap:
            self.platform_combo.setCurrentText(pmap[mf.platform])
        self._recompute()

    # ---- пересчёт моментов ----
    def _current_mode(self) -> AudienceMode:
        return _LABEL_TO_MODE.get(self.mode_chips.current(), AudienceMode.AUTO)

    def _build_timemap(self) -> Optional[TimeMap]:
        dur = getattr(self, "_video_dur", 0.0)
        if dur <= 0:
            # без видео берём длину из меток (для предпросмотра дорожки)
            dur = (self._mf.duration if self._mf and self._mf.duration else 0.0)
            if dur <= 0:
                return None
            return TimeMap.identity(dur)
        if self.partial_sw.isChecked():
            return TimeMap.with_start_offset(dur, self.offset_spin.value())
        return TimeMap.identity(dur)

    def _recompute(self) -> None:
        self._timemap = self._build_timemap()
        # режим-подсказка
        online = self._mf.online if self._mf else None
        eff = resolve_mode(self._current_mode(), online)
        self.mode_hint.setText(_MODE_HINT.get(eff, ""))
        self._check_match()

        if not self._mf:
            self._moments = []
            self._refresh_timeline(); self._rebuild_cards(); self._update_empty()
            return
        window = (-self.before_spin.value(), self.after_spin.value())
        self._moments = select_moments(self._mf, self._current_mode(), window=window)
        self._included = [True] * len(self._moments)
        self._refresh_timeline()
        self._rebuild_cards()
        self._update_empty()

    def _refresh_timeline(self) -> None:
        tm = self._timemap
        span = tm.stream_span if tm else (0.0, 0.0)
        dur = span[1] if span[1] > 0 else (self._mf.duration if self._mf else 0.0)
        self.timeline.set_duration(dur or 0.0)
        self.timeline.set_marks(self._mf.marks if self._mf else [])
        self.timeline.set_moments(self._moments)
        # дыры записи из timemap (между кусками)
        gaps = []
        if tm and len(tm.pieces) > 1:
            for a, b in zip(tm.pieces, tm.pieces[1:]):
                gaps.append((a.stream_end, b.stream_start))
        # сдвиг начала = дыра [0, offset]
        if tm and tm.pieces and tm.pieces[0].stream_start > 0:
            gaps.insert(0, (0.0, tm.pieces[0].stream_start))
        self.timeline.set_gaps(gaps)

    def _rebuild_cards(self) -> None:
        for c in self._cards:
            c.setParent(None)
        self._cards = []
        order = list(range(len(self._moments)))
        if self._sort_heat:
            order.sort(key=lambda i: self._moments[i].heat, reverse=True)
        for i in order:
            card = MomentCard(i, self._moments[i], self._theme)
            card.chk.setChecked(self._included[i] if i < len(self._included) else True)
            card.toggled.connect(self._on_card_toggle)
            card.clicked.connect(self._select_moment)
            self._cards.append(card)
            self.moments_vl.insertWidget(self.moments_vl.count() - 1, card)
        self.moments_head.setText(f"МОМЕНТЫ ({len(self._moments)})")

    def _update_empty(self) -> None:
        has = bool(self._moments)
        self.render_btn.setEnabled(has)
        self.top_btn.setEnabled(has)
        self.sort_btn.setEnabled(has)
        # Пока моментов нет — вместо пустого списка показываем подсказку.
        if hasattr(self, "moments_empty"):
            self.moments_empty.setVisible(not has)
            self.moments_scroll.setVisible(has)

    # ---- матчинг видео↔метки ----
    def _check_match(self) -> None:
        if not self._mf or not self._video:
            self.warn_lbl.setVisible(False)
            return
        problems = []
        vdur = getattr(self, "_video_dur", 0.0)
        mdur = self._mf.duration
        if vdur and mdur and not self.partial_sw.isChecked():
            if abs(vdur - mdur) > max(60.0, 0.1 * mdur):
                problems.append(f"длина видео {_fmt(vdur)} ≠ длина трансляции {_fmt(mdur)}")
        if vdur and self._mf.marks:
            last = max(m.t for m in self._mf.marks)
            reach = vdur + (self.offset_spin.value() if self.partial_sw.isChecked() else 0)
            if last > reach + 5:
                problems.append("метки выходят за конец видео (запись короче?)")
        if problems:
            self.warn_lbl.setText("⚠ Похоже, видео и метки не совпадают: "
                                  + "; ".join(problems) + ". Проверьте файл/сдвиг.")
            self.warn_lbl.setVisible(True)
        else:
            self.warn_lbl.setVisible(False)

    # ---- взаимодействие ----
    def _on_partial(self, on: bool) -> None:
        self.partial_box.setVisible(on)
        self._recompute()

    def _on_author_filter(self, active: set) -> None:
        self.timeline.set_visible_authors(active)

    def _on_moment_edited(self, idx: int, start: float, end: float) -> None:
        if 0 <= idx < len(self._moments):
            self._moments[idx].start = start
            self._moments[idx].end = end
            # обновить подпись диапазона на карточке
            for card in self._cards:
                if card.idx == idx:
                    self._rebuild_cards()
                    break

    def _select_moment(self, idx: int) -> None:
        self.timeline.set_active(idx)
        for card in self._cards:
            card.set_selected(card.idx == idx)

    def _on_card_toggle(self, idx: int, on: bool) -> None:
        if idx < len(self._included):
            self._included[idx] = on

    def _toggle_sort(self) -> None:
        self._sort_heat = not self._sort_heat
        self.sort_btn.setText("По жару" if self._sort_heat else "По времени")
        self._rebuild_cards()

    def _take_top(self, _=False, n: int = 5) -> None:
        top = set(i for i in sorted(range(len(self._moments)),
                                    key=lambda i: self._moments[i].heat, reverse=True)[:n])
        self._included = [i in top for i in range(len(self._moments))]
        for card in self._cards:
            card.chk.setChecked(card.idx in top)

    # ---- сборка конфигурации рендера ----
    def _timemap_or_identity(self):
        return self._timemap or (TimeMap.identity(getattr(self, "_video_dur", 0.0))
                                 if getattr(self, "_video_dur", 0.0) else None)

    def _included_moments(self) -> list[Moment]:
        return [mo for i, mo in enumerate(self._moments)
                if i < len(self._included) and self._included[i]]

    def included_segments(self) -> list[Segment]:
        """Все отрезки включённых моментов в ФАЙЛОВОМ времени (для режима склейки)."""
        tm = self._timemap_or_identity()
        if tm is None:
            return []
        return tm.map_segments([Segment(mo.start, mo.end) for mo in self._included_moments()])

    def _separate(self) -> bool:
        return self.export_chips.current() != "Склеить в один"

    def _on_export_mode(self, _text: str) -> None:
        self.render_btn.setText("Нарезать клипы" if self._separate()
                                else "Склеить в один клип")

    def validate(self) -> Optional[str]:
        if not self._video:
            return "Не выбрано видео стрима."
        if not self._mf:
            return "Не загружен файл меток."
        if not self._included_moments():
            return "Не выбран ни один момент."
        if not self.included_segments():
            return "Выбранные моменты попадают в дыры записи — нечего рендерить."
        return None

    def _make_config(self, out_dir: str, filename: str, segments: list[Segment]):
        """Собрать один PipelineConfig с общими настройками + заданными сегментами/именем.
        Импорты локальные — чтобы UI-модуль не тянул тяжёлый core при импорте."""
        from core.pipeline import PipelineConfig
        from core.config import ExportConfig
        from core.captions import CaptionStyle, CaptionAnimation
        from core.branding import BrandingConfig, Platform

        comp = self.layout_editor.get_composition()   # раскладка из ручного редактора
        pmap = {"Twitch": "twitch", "YouTube": "youtube", "Kick": "kick", "Без значка": "none"}
        prof = {"Не трогать": ("off", "beep"), "Бип": ("beep", "beep"),
                "Заглушить": ("silence", "silence")}[self.prof_chips.current()]
        return PipelineConfig(
            source=self._video,
            segments=segments,
            composition=comp,
            export=ExportConfig(out_dir=out_dir, filename=filename),
            captions_enabled=getattr(comp.subtitles, "visible", True),
            caption_style=CaptionStyle(),
            caption_animation=CaptionAnimation.POP,
            profanity_enabled=(prof[0] != "off"),
            profanity_mode=("beep" if prof[0] == "beep" else "silence"),
            loudnorm=self.audio_switches["loudnorm"].isChecked(),
            denoise=self.audio_switches["denoise"].isChecked(),
            clarity=self.audio_switches["clarity"].isChecked(),
            gate=self.audio_switches["gate"].isChecked(),
            branding=BrandingConfig(nickname=self.nick_edit.text().strip(),
                                    platform=Platform(pmap[self.platform_combo.currentText()])),
        )

    def build_pipeline_configs(self, out_dir: str) -> list:
        """Список PipelineConfig: по одному на момент (раздельно) ИЛИ один со всеми
        сегментами (склейка). Пустой список — если рендерить нечего."""
        tm = self._timemap_or_identity()
        if tm is None:
            return []
        moments = self._included_moments()
        if self._separate():
            cfgs = []
            for idx, mo in enumerate(moments):
                segs = tm.map_range(mo.start, mo.end)
                if not segs:
                    continue                       # момент целиком в дыре записи — пропускаем
                cfgs.append(self._make_config(out_dir, self._clip_name(idx, mo), segs))
            return cfgs
        segs = tm.map_segments([Segment(mo.start, mo.end) for mo in moments])
        if not segs:
            return []
        return [self._make_config(out_dir, self._out_name(), segs)]

    def _stem(self) -> str:
        """Начало имени файла: ник + дата эфира — чтобы клипы разных стримов не путались."""
        s = (self._mf.streamer if self._mf and self._mf.streamer else "stream")
        for ch in '<>:"/\\|?*':
            s = s.replace(ch, "_")
        day = (self._mf.started_at[:10] if self._mf and self._mf.started_at else "")
        return f"{s}_{day}" if day else s

    def _clip_name(self, idx: int, mo: Moment) -> str:
        label = (mo.label or "").strip()
        for ch in '<>:"/\\|?*':
            label = label.replace(ch, "_")
        label = label.replace(" ", "_")[:40]
        tail = f"{idx + 1:02d}_{label}" if label else f"{idx + 1:02d}"
        return f"{self._stem()}_{tail}_vertical.mp4"

    def _out_name(self) -> str:
        return f"{self._stem()}_montage.mp4"

    # ---- тема ----
    def apply_theme(self, theme: str) -> None:
        from . import theme as theme_mod
        theme_mod.set_current(theme)
        self._theme = theme
        self.timeline.set_theme(theme)
        # Тумблеры/чипы рисуют себя сами — просим перекраситься (иначе на светлой
        # теме они оставались тёмными пятнами).
        for cls in (Chip, ToggleSwitch):
            for w in self.findChildren(cls):
                w.set_theme(theme)
        if hasattr(self, "layout_editor"):
            self.layout_editor.set_theme(theme)
        # простая перерисовка (детальный ре-стайл — на полноценной смене темы)
        self.update()


class _AuthorFilter(QWidget):
    """Мультивыбор типов авторов для подсветки: клик по типу — остальные тускнеют."""
    changed = Signal(set)

    def __init__(self, theme: str, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._active: set[AuthorType] = set()
        l = QHBoxLayout(self); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(6)
        self._btns: dict[AuthorType, QPushButton] = {}
        for at in AUTHOR_ORDER:
            b = QPushButton(AUTHOR_LABEL[at]); b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, a=at: self._toggle(a))
            self._restyle(b, at, False)
            self._btns[at] = b
            l.addWidget(b)
        l.addStretch(1)

    def _restyle(self, b: QPushButton, at: AuthorType, on: bool) -> None:
        col = AUTHOR_COLORS[at]
        if on:
            b.setStyleSheet(f"QPushButton{{background:{col};border:1px solid {col};color:#fff;"
                            f"border-radius:999px;padding:4px 10px;font-size:11px;font-weight:700;}}")
        else:
            b.setStyleSheet(f"QPushButton{{background:transparent;border:1px solid {col};"
                            f"color:{col};border-radius:999px;padding:4px 10px;font-size:11px;"
                            f"font-weight:600;}}")

    def _toggle(self, at: AuthorType) -> None:
        if at in self._active:
            self._active.discard(at)
        else:
            self._active.add(at)
        self._restyle(self._btns[at], at, at in self._active)
        # пусто → показываем всех
        self.changed.emit(self._active if self._active else set(AUTHOR_ORDER))
