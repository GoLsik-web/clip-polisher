"""wizard.py — пошаговый мастер-аккордеон (эталон: docs/ui-reference.html).

Раскрыт ровно ОДИН шаг. «Далее» плавно сворачивает текущий и разворачивает
следующий (синхронно), номер завершённого → галочка. По завершённым шагам можно
кликнуть и вернуться. Анимация maximumHeight + opacity, прерываемая (меняем endValue).
Каждому шагу — свой цвет (STEP_COLORS): красит номер, рамку, контролы шага.
"""
from __future__ import annotations

from PySide6.QtCore import (Qt, Signal, QPropertyAnimation, QEasingCurve,
                            QParallelAnimationGroup)
from PySide6.QtWidgets import (QFrame, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
                               QGraphicsOpacityEffect, QScrollArea)

from .theme import STEP_COLORS, step_qss


class WizardStep(QFrame):
    """Один шаг: шапка (номер/заголовок/состояние) + сворачиваемое тело."""
    header_clicked = Signal(int)

    def __init__(self, index: int, title: str, subtitle: str, color: str, parent=None):
        super().__init__(parent)
        self.index = index
        self.color = color
        self.setProperty("class", "wstep")
        self.setObjectName(f"wstep{index}")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Шапка.
        head = QWidget()
        head.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(head)
        h.setContentsMargins(15, 13, 15, 13)
        h.setSpacing(11)
        self.num = QLabel(str(index + 1))
        self.num.setProperty("class", "wnum")
        self.num.setFixedSize(26, 26)
        self.num.setAlignment(Qt.AlignCenter)
        title_lab = QLabel(title)
        title_lab.setProperty("class", "wtitle")
        self.sub = QLabel(subtitle)
        self.sub.setProperty("class", "wsub")
        self.state = QLabel("ждёт")
        self.state.setProperty("class", "wstate")
        h.addWidget(self.num)
        h.addWidget(title_lab)
        h.addWidget(self.sub)
        h.addStretch(1)
        h.addWidget(self.state)
        head.mousePressEvent = lambda e: self.header_clicked.emit(self.index)
        root.addWidget(head)

        # Тело.
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(15, 2, 15, 15)
        self.body_layout.setSpacing(10)
        self._opacity = QGraphicsOpacityEffect(self.body)
        self._opacity.setOpacity(0.0)
        self.body.setGraphicsEffect(self._opacity)
        self.body.setMaximumHeight(0)
        root.addWidget(self.body)

        self._expanded = False
        self._spring = QEasingCurve(QEasingCurve.OutBack)
        self._spring.setOvershoot(1.05)   # лёгкий overshoot на раскрытии
        self._anim = QParallelAnimationGroup(self)
        self._h_anim = QPropertyAnimation(self.body, b"maximumHeight")
        self._h_anim.setDuration(440)
        self._o_anim = QPropertyAnimation(self._opacity, b"opacity")
        self._o_anim.setDuration(300)
        self._o_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.addAnimation(self._h_anim)
        self._anim.addAnimation(self._o_anim)
        self._h_anim.finished.connect(self._on_h_finished)

        # Цвет окна для контролов этого шага.
        self.setStyleSheet(step_qss(color))

    # ---- Состояния -------------------------------------------------------

    def set_state(self, kind: str) -> None:
        """kind: 'active' | 'done' | 'todo'."""
        self.setProperty("active", "true" if kind == "active" else "false")
        self.num.setProperty("active", "true" if kind == "active" else "false")
        self.num.setProperty("done", "true" if kind == "done" else "false")
        self.state.setText({"active": "настройка…", "done": "готово", "todo": "ждёт"}[kind])
        self.setStyleSheet(step_qss(self.color))  # repolish
        if kind == "active":
            self._expand()
        else:
            self._collapse()
        # ВАЖНО: opacity-эффект живёт ТОЛЬКО на self.body (задан в __init__).
        # Нельзя вешать его на весь шаг — иначе у свёрнутых (opacity→0) пропадёт заголовок.

    _RELEASED = 16777215  # снятое ограничение maxHeight (карточка = по контенту)

    def _target_height(self) -> int:
        return self.body_layout.sizeHint().height() + 6

    def _cur_height(self) -> int:
        mh = self.body.maximumHeight()
        return self.body.height() if mh >= 100000 else mh

    def _expand(self) -> None:
        self._expanded = True
        self._anim.stop()
        self.body.setMaximumHeight(self._cur_height())  # зафиксировать перед анимацией
        self._h_anim.setEasingCurve(self._spring)       # пружинка только на раскрытии
        self._h_anim.setStartValue(self.body.maximumHeight())
        self._h_anim.setEndValue(self._target_height())
        self._o_anim.setStartValue(self._opacity.opacity())
        self._o_anim.setEndValue(1.0)
        self._anim.start()

    def _collapse(self) -> None:
        self._expanded = False
        self._anim.stop()
        self.body.setMaximumHeight(self._cur_height())
        self._h_anim.setEasingCurve(QEasingCurve.OutCubic)  # без overshoot → без отрицательной высоты
        self._h_anim.setStartValue(self.body.maximumHeight())
        self._h_anim.setEndValue(0)
        self._o_anim.setStartValue(self._opacity.opacity())
        self._o_anim.setEndValue(0.0)
        self._anim.start()

    def _on_h_finished(self) -> None:
        # После раскрытия снимаем ограничение — карточка ровно по содержимому, без «дыры».
        if self._expanded:
            self.body.setMaximumHeight(self._RELEASED)

    def apply_accent_to_children(self) -> None:
        """Прокинуть цвет окна в дочерние виджеты с set_accent (HelpIcon/Chip/Toggle)."""
        for w in self.body.findChildren(QWidget):
            fn = getattr(w, "set_accent", None)
            if callable(fn):
                fn(self.color)


class Wizard(QScrollArea):
    """Контейнер шагов: текущий индекс, next/back, автоскролл, клики по шапкам."""
    step_changed = Signal(int)          # индекс активного шага
    finished = Signal()                 # нажали «Отрендерить» на последнем шаге

    def __init__(self, titles: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        self._lay = QVBoxLayout(holder)
        self._lay.setContentsMargins(2, 2, 2, 2)
        self._lay.setSpacing(10)
        self.setWidget(holder)

        self.steps: list[WizardStep] = []
        for i, (title, sub) in enumerate(titles):
            color = STEP_COLORS[i % len(STEP_COLORS)]
            st = WizardStep(i, title, sub, color)
            st.header_clicked.connect(self._on_header)
            self.steps.append(st)
            self._lay.addWidget(st)
        self._lay.addStretch(1)
        self._cur = 0

    def color_of(self, idx: int) -> str:
        return self.steps[idx].color

    def current(self) -> int:
        return self._cur

    def set_step(self, n: int) -> None:
        self._cur = max(0, min(len(self.steps) - 1, n))
        for i, st in enumerate(self.steps):
            st.set_state("active" if i == self._cur else ("done" if i < self._cur else "todo"))
        self.ensureWidgetVisible(self.steps[self._cur])
        self.step_changed.emit(self._cur)

    def next(self) -> None:
        if self._cur >= len(self.steps) - 1:
            self.finished.emit()
        else:
            self.set_step(self._cur + 1)

    def back(self) -> None:
        self.set_step(self._cur - 1)

    def _on_header(self, i: int) -> None:
        if i <= self._cur:      # назад по завершённым — можно
            self.set_step(i)
