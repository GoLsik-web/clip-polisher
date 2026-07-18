"""widgets.py — маленькие переиспользуемые виджеты интерфейса.

HelpIcon — кружок «i» с подсказкой по-русски (у каждого контрола).
Chip / ChipRow — переключаемые «пилюли» (анимация выбора — через цвет окна).
ToggleSwitch — тумблер вкл/выкл.
Все окрашиваются в цвет активного окна через set_accent().
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QSize, QPropertyAnimation, QEasingCurve, Property
from PySide6.QtGui import QPainter, QColor, QPen
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

    def _restyle(self) -> None:
        self.setStyleSheet(f"""
            QPushButton {{ background:#191826; border:1px solid #2a2740; color:#c2bde0;
                border-radius:999px; padding:5px 12px; font-size:12px; font-weight:600; }}
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

    def paintEvent(self, _e) -> None:
        pt = QPainter(self)
        pt.setRenderHint(QPainter.Antialiasing)
        on = self.isChecked()
        track = QColor(self._accent) if on else QColor("#191826")
        if on:
            track.setAlpha(140)
        pt.setPen(QPen(QColor(self._accent if on else "#2a2740"), 1))
        pt.setBrush(track)
        pt.drawRoundedRect(0, 0, 39, 23, 11, 11)
        pt.setPen(Qt.NoPen)
        pt.setBrush(QColor(self._accent) if on else QColor("#c2bde0"))
        pt.drawEllipse(int(self._pos), 3, 18, 18)
