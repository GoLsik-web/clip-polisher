"""widgets.py — маленькие переиспользуемые виджеты интерфейса.

HelpIcon — кружок «i» с подсказкой по-русски (у каждого контрола).
Chip / ChipRow — переключаемые «пилюли» (анимация выбора — через цвет окна).
ToggleSwitch — тумблер вкл/выкл.
Все окрашиваются в цвет активного окна через set_accent().
"""
from __future__ import annotations

from PySide6.QtCore import (Qt, Signal, QSize, QPoint, QPointF, QPropertyAnimation,
                            QEasingCurve, Property)
from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath
from PySide6.QtWidgets import QLabel, QWidget, QHBoxLayout, QPushButton, QButtonGroup


class HelpIcon(QLabel):
    """Кружок «i» с подсказкой (tooltip). Ховер подсвечивает цветом окна."""
    def __init__(self, tip: str, parent=None):
        super().__init__("i", parent)
        self.setToolTip(tip)
        self.setFixedSize(16, 16)
        self.setAlignment(Qt.AlignCenter)
        self._accent = "#7c5cff"
        self._hover = False
        self.setStyleSheet(self._css())

    def set_accent(self, color: str) -> None:
        self._accent = color
        self.setStyleSheet(self._css())

    def _css(self) -> str:
        border = self._accent if self._hover else "#2a2740"
        col = self._accent if self._hover else "#c2bde0"
        return (f"QLabel{{border:1px solid {border};border-radius:8px;"
                f"color:{col};font-size:10px;font-weight:800;font-style:normal;}}")

    def enterEvent(self, e):
        self._hover = True
        self.setStyleSheet(self._css())

    def leaveEvent(self, e):
        self._hover = False
        self.setStyleSheet(self._css())


class HamburgerButton(QPushButton):
    """Кнопка-гамбургер (три полоски), нарисованная QPainter."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40, 34)
        self._accent = QColor("#7c5cff")
        self.setToolTip("Выбрать режим работы")

    def set_accent(self, color: str) -> None:
        self._accent = QColor(color); self.update()

    def paintEvent(self, _e) -> None:
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        pt.setPen(QPen(QColor("#2a2740"), 1))
        pt.setBrush(QColor("#191826"))
        pt.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 8, 8)
        pt.setPen(QPen(self._accent, 2.4, Qt.SolidLine, Qt.RoundCap))
        cx = self.width() / 2
        for dy in (-6, 0, 6):
            pt.drawLine(int(cx - 8), int(self.height() / 2 + dy),
                        int(cx + 8), int(self.height() / 2 + dy))


class Chip(QPushButton):
    """Переключаемая «пилюля»."""
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._accent = "#7c5cff"
        self._restyle()

    def set_accent(self, color: str) -> None:
        self._accent = color
        self._restyle()

    def set_theme(self, _theme: str = "") -> None:
        """Перекрасить под текущую тему (цвета берём из палитры, не из констант)."""
        self._restyle()

    def _restyle(self) -> None:
        from .theme import p, current
        c = p(current())
        self.setStyleSheet(f"""
            QPushButton {{ background:{c['panel2']}; border:1px solid {c['line']};
                color:{c['muted']}; border-radius:999px; padding:5px 12px;
                font-size:12px; font-weight:600; }}
            QPushButton:checked {{ background:{self._accent}; border-color:{self._accent}; color:#fff; }}
        """)


class ChipRow(QWidget):
    """Ряд взаимоисключающих чипов (как radio)."""
    changed = Signal(str)

    def __init__(self, options: list[str], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._chips: list[Chip] = []
        for i, opt in enumerate(options):
            ch = Chip(opt)
            if i == 0:
                ch.setChecked(True)
            self._group.addButton(ch, i)
            self._chips.append(ch)
            lay.addWidget(ch)
        lay.addStretch(1)
        self._group.idClicked.connect(lambda i: self.changed.emit(self._chips[i].text()))

    def current(self) -> str:
        b = self._group.checkedButton()
        return b.text() if b else ""

    def set_current(self, text: str) -> None:
        for ch in self._chips:
            if ch.text().lower() == text.lower():
                ch.setChecked(True)

    def set_accent(self, color: str) -> None:
        for ch in self._chips:
            ch.set_accent(color)


class VersionPill(QPushButton):
    """Техничная пилюля версии со статус-точкой (как build-индикатор в IDE).

    Показывает установленную версию + цветную точку состояния обновлений:
      серый — проверяю, зелёный — актуально, бирюзовый (пульсирует) — есть обнова,
      янтарный — не удалось проверить. Клик открывает меню обновлений.
    """
    STATUS_COLORS = {
        "checking": "#8a86ad",
        "uptodate": "#57d081",
        "update":   "#37c9c2",
        "error":    "#f0a93a",
    }

    def __init__(self, version: str, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self._version = version
        self._status = "checking"
        self._text = "#f9f8ff"; self._muted = "#c2bde0"
        self._line = "#2a2740"; self._panel = "#191826"
        self._pulse = 1.0
        self._anim = QPropertyAnimation(self, b"pulse", self)
        self._anim.setDuration(1100)
        self._anim.setStartValue(0.35); self._anim.setEndValue(1.0)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self.setFixedHeight(26)
        self._recalc_width()
        self.setToolTip("Версия приложения. Нажми — проверить и установить обновления.")

    def set_palette(self, text: str, muted: str, line: str, panel: str) -> None:
        self._text, self._muted, self._line, self._panel = text, muted, line, panel
        self.update()

    def set_status(self, status: str) -> None:
        self._status = status
        if status == "update":
            self._anim.start()
        else:
            self._anim.stop(); self._pulse = 1.0
        self.update()

    def _recalc_width(self) -> None:
        fm = self.fontMetrics()
        self.setFixedWidth(fm.horizontalAdvance("v" + self._version) + 44)

    def get_pulse(self) -> float:
        return self._pulse

    def set_pulse(self, v: float) -> None:
        self._pulse = v
        self.update()

    pulse = Property(float, get_pulse, set_pulse)

    def paintEvent(self, _e) -> None:
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(0, 1, -1, -2)
        dot = QColor(self.STATUS_COLORS.get(self._status, "#8a86ad"))
        border = QColor(dot) if self._status == "update" else QColor(self._line)
        if self._status == "update":
            border.setAlpha(200)
        pt.setPen(QPen(border, 1.2)); pt.setBrush(QColor(self._panel))
        pt.drawRoundedRect(r, r.height() // 2, r.height() // 2)
        # текст версии (моноширинный-технический вид)
        pt.setPen(QColor(self._muted))
        f = pt.font(); f.setPointSizeF(9.0); f.setBold(True); pt.setFont(f)
        pt.drawText(r.adjusted(11, 0, -20, 0), Qt.AlignVCenter | Qt.AlignLeft,
                    "v" + self._version)
        # статус-точка справа (+ мягкое свечение при обнове)
        cx, cy = r.right() - 12, r.center().y()
        if self._status == "update":
            glow = QColor(dot); glow.setAlphaF(0.22 * self._pulse)
            pt.setPen(Qt.NoPen); pt.setBrush(glow)
            pt.drawEllipse(QPoint(cx, cy), 8, 8)
            dot.setAlphaF(self._pulse)
        pt.setPen(Qt.NoPen); pt.setBrush(dot)
        pt.drawEllipse(QPoint(cx, cy), 4, 4)


class EyeToggle(QPushButton):
    """Глаз-переключатель ВИДИМОСТИ панели: открытый глаз = панель в клипе есть,
    перечёркнутый = скрыта. Рисуется QPainter (без эмодзи → без «тофу»).

    Единый контрол видимости во всём приложении: в легенде редактора (Этап 1,
    одиночный и мульти) и в панели раскладки Этапа 2. Цвет — под зону/тему."""
    def __init__(self, color: str = "#7c5cff", parent=None):
        super().__init__(parent)
        self.setCheckable(True); self.setChecked(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(32, 24)
        self._color = QColor(color)
        self._muted = QColor("#7a7597")
        self.toggled.connect(lambda _=False: (self._sync_tip(), self.update()))
        self._sync_tip()

    def set_color(self, color: str) -> None:
        self._color = QColor(color); self.update()

    def set_muted(self, color: str) -> None:
        self._muted = QColor(color); self.update()

    def _sync_tip(self) -> None:
        self.setToolTip("Панель показана в клипе — нажми, чтобы скрыть"
                        if self.isChecked() else
                        "Панель скрыта — нажми, чтобы показать")

    def paintEvent(self, _e) -> None:
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        col = QColor(self._color) if on else QColor(self._muted)
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        # миндаль глаза
        path = QPainterPath()
        path.moveTo(cx - 9, cy)
        path.quadTo(cx, cy - 7, cx + 9, cy)
        path.quadTo(cx, cy + 7, cx - 9, cy)
        pt.setPen(QPen(col, 1.8)); pt.setBrush(Qt.NoBrush)
        pt.drawPath(path)
        if on:
            pt.setPen(Qt.NoPen); pt.setBrush(col)
            pt.drawEllipse(QPointF(cx, cy), 2.6, 2.6)
        else:
            pt.setPen(QPen(col, 1.8))
            pt.drawLine(QPointF(cx - 9, cy + 7), QPointF(cx + 9, cy - 7))


class ToggleSwitch(QPushButton):
    """Тумблер вкл/выкл с плавной анимацией «шарика»."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40, 24)
        self._accent = QColor("#7c5cff")
        self._pos = 2.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def set_accent(self, color: str) -> None:
        self._accent = QColor(color)
        self.update()

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(18.0 if on else 2.0)
        self._anim.start()

    def get_knob(self) -> float:
        return self._pos

    def set_knob(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = Property(float, get_knob, set_knob)

    def set_theme(self, _theme: str = "") -> None:
        self.update()

    def paintEvent(self, _e) -> None:
        from .theme import p, current
        c = p(current())
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        # Выключенный тумблер берёт цвета из палитры: на светлой теме он был почти
        # чёрным (константы дарк-темы) и выглядел инородным пятном.
        light = current() == "light"
        track = QColor(self._accent) if on else QColor(c["line"])
        if on:
            track.setAlpha(140)
        pt.setPen(QPen(QColor(self._accent if on else c["line"]), 1))
        pt.setBrush(track)
        pt.drawRoundedRect(0, 0, 39, 23, 11, 11)
        # Шарик всегда светлее дорожки — иначе выключенный тумблер читается как «залипший».
        knob = QColor(self._accent) if on else QColor("#ffffff" if light else c["muted"])
        pt.setPen(QPen(QColor(c["line"]), 1) if (light and not on) else Qt.NoPen)
        pt.setBrush(knob)
        pt.drawEllipse(int(self._pos), 3, 18, 18)
