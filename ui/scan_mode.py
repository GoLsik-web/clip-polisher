"""scan_mode.py — Этап 3, блок 5: экран «Автопоиск моментов ИИ».

Ради этого экрана делалось всё ядро Этапа 3. Сценарий целиком:

    вставил ссылку → программа послушала стрим (клипы зрителей, чат, звук, речь)
    → показала 2–8 моментов с честным «почему выбрано» → ты выбрал и поправил
    границы → скачались ТОЛЬКО выбранные куски → готовые вертикальные клипы.

Раскладка экрана — как в режиме 2 («Метки через бота»): слева колонка настроек,
справа дорожка + карточки. Так задумано: человек уже знает этот экран, и половина
виджетов (таймлайн, редактор раскладки) переиспользуется как есть.

Что здесь ВАЖНО и легко сломать:
  * разбор и загрузка идут в отдельных потоках — окно не должно замирать ни на
    минуту (разбор 3-часового стрима — это до получаса);
  * ползунок строгости НЕ трогает сеть: `ScanFile.rescore()` пересчитывает моменты
    по уже собранным уликам мгновенно;
  * речь — самый долгий шаг, и про долгое ожидание человека спрашивают ДО начала
    (ждать / модель побыстрее / пропустить);
  * «почему выбрано» показывается всегда: без объяснения автопоиск выглядит магией,
    и не видно, где он ошибся.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from PySide6.QtCore import Qt, Signal, QThread, QUrl, QRect, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QBrush, QLinearGradient, QDesktopServices
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QSlider, QStackedWidget, QTextEdit,
                               QVBoxLayout, QWidget)

from core.clipscan import FAMILY, ScanFile, ScanMoment, fmt_time
from core.config import Segment
from .marks_mode import MarksTimeline, _section, _toggle_row
from .preview_panel import EditorPanel
from .theme import PALETTE
from .widgets import ChipRow, Chip, ToggleSwitch

# Спрашивать про долгую речь начиная с этого времени. Ядро считает «долгим» 5 минут
# (`speech.LONG_WAIT_SEC`), но в окне лучше предупредить раньше: три минуты немого
# прогресс-бара человек уже воспринимает как зависание.
ASK_ABOVE_SEC = 180.0

# Цвета семей улик — те же роли, что в легенде: у каждого сигнала свой цвет на дорожке.
FAMILY_COLORS = {
    "clips": "#9146ff",     # клипы зрителей — фирменный фиолетовый Twitch
    "chat": "#00ad03",      # чат — зелёный (как модератор в режиме 2)
    "audio": "#f0a93a",     # звук — янтарный
    "speech": "#37c9c2",    # речь — бирюза
    "face": "#ff6ba6",      # лицо — розовый (зона вебки)
}
FAMILY_LABEL = {"clips": "Клипы зрителей", "chat": "Чат", "audio": "Звук",
                "speech": "Речь", "face": "Лицо"}
FAMILY_ORDER = ["clips", "chat", "audio", "speech", "face"]

QUALITY_LABELS = ["1080p60", "720p60", "480p"]


def _star_pixmap(color: str, h: int = 13):
    """Нарисованная звезда «золотого» момента.

    ⚠️ Символ «★» ставить НЕЛЬЗЯ: в сборке нет шрифта с этим глифом, и на скриншотах
    он выходил пустым квадратом-«тофу» (та же грабля, что с эмодзи в режиме 2 —
    см. `_flame_pixmap`). Рисуем сами — выглядит одинаково везде.
    """
    import math
    from PySide6.QtGui import QPixmap, QPolygonF
    pm = QPixmap(h, h); pm.fill(Qt.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
    cx = cy = h / 2.0
    outer, inner = h / 2.0 - 0.5, h / 4.6
    poly = QPolygonF()
    for i in range(10):
        r = outer if i % 2 == 0 else inner
        a = -math.pi / 2 + i * math.pi / 5
        poly.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
    p.setPen(Qt.NoPen); p.setBrush(QColor(color)); p.drawPolygon(poly)
    p.end()
    return pm
SPEECH_LABELS = {
    "Авто (рекомендуется)": "auto",
    "Побыстрее": "small",
    "Точнее": "large-v3",
    "Не распознавать": "skip",
}


# ==========================================================================
# Дорожка стрима с уликами
# ==========================================================================

class ScanTimeline(MarksTimeline):
    """Та же дорожка, что в режиме 2, но вместо меток чата — улики автопоиска.

    Наследуемся ради готового: перетаскивание границ момента, выбор кликом,
    линейка времени. Своё — тепловая полоса по уликам и цветные пины по семьям.
    """

    EMPTY_TEXT = ("Вставь ссылку на стрим слева и нажми «Разобрать» — "
                  "здесь появятся найденные моменты")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signals: list = []

    def set_signals(self, signals: list) -> None:
        self._signals = list(signals)
        self.update()

    def _draw_heat(self, p: QPainter, c: dict) -> None:
        """Полоса «где вообще что-то происходило» — по весу улик, а не по числу."""
        hr = self._heat_rect()
        p.setPen(Qt.NoPen); p.setBrush(QColor(c["panel2"]))
        p.drawRoundedRect(hr, 6, 6)
        if not self._signals or self._dur <= 0:
            return
        bins = max(24, hr.width() // 5)
        dens = [0.0] * bins
        for s in self._signals:
            b = int((s.t / self._dur) * (bins - 1))
            b = min(max(0, b), bins - 1)
            w = max(0.2, float(getattr(s, "weight", 1.0)))
            dens[b] += w
            if b > 0:
                dens[b - 1] += w * 0.5
            if b < bins - 1:
                dens[b + 1] += w * 0.5
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
        g1 = QColor(acc); g1.setAlpha(190)
        g2 = QColor(acc); g2.setAlpha(40)
        grad.setColorAt(0, g1); grad.setColorAt(1, g2)
        p.save()
        clip = QPainterPath(); clip.addRoundedRect(QRectF(hr), 6, 6)
        p.setClipPath(clip)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(grad)); p.drawPath(path)
        p.restore()
        p.setPen(QColor(c["muted"]))
        f = p.font(); f.setPointSizeF(8.0); p.setFont(f)
        p.drawText(hr.adjusted(8, 0, -8, 0), Qt.AlignLeft | Qt.AlignVCenter,
                   "Где что-то происходило")

    def _draw_pins(self, p: QPainter, tr: QRect) -> None:
        """Каждая улика — своя палочка своего цвета: видно, ЧЕМ набран момент."""
        for s in self._signals:
            x = self._t2x(s.t)
            fam = FAMILY.get(getattr(s, "kind", ""), "audio")
            col = QColor(FAMILY_COLORS.get(fam, "#5a86d8"))
            col.setAlpha(210)
            tall = fam in ("clips", "chat")
            h = tr.height() * (0.58 if tall else 0.40)
            y0 = tr.bottom() - h
            p.setPen(QPen(col, 2 if tall else 1.4))
            p.drawLine(QPointF(x, tr.bottom() - 2), QPointF(x, y0))
            p.setPen(Qt.NoPen); p.setBrush(col)
            p.drawEllipse(QPointF(x, y0), 2.4 if tall else 1.9, 2.4 if tall else 1.9)


# ==========================================================================
# Карточка найденного момента
# ==========================================================================

class ScanCard(QFrame):
    """Момент в списке: имя, время, ★ и главное — «почему выбрано» словами."""

    toggled = Signal(int, bool)
    clicked = Signal(int)
    watch = Signal(int)
    rejected = Signal(int)

    def __init__(self, idx: int, mo: ScanMoment, theme: str, watchable: bool = True,
                 parent=None):
        super().__init__(parent)
        self.idx = idx
        self._theme = theme
        self.setObjectName("panel2")
        self.setCursor(Qt.PointingHandCursor)
        c = PALETTE[theme]
        lay = QHBoxLayout(self); lay.setContentsMargins(10, 8, 10, 8); lay.setSpacing(10)

        self.chk = QCheckBox(); self.chk.setChecked(True)
        self.chk.setToolTip("Брать этот момент в нарезку")
        self.chk.toggled.connect(lambda on: self.toggled.emit(self.idx, on))
        lay.addWidget(self.chk)

        num = QLabel(str(idx + 1)); num.setFixedSize(24, 24); num.setAlignment(Qt.AlignCenter)
        num.setStyleSheet(f"background:{c['accent']};color:#fff;border-radius:7px;"
                          f"font-weight:800;font-size:12px;")
        lay.addWidget(num)

        mid = QVBoxLayout(); mid.setSpacing(3)
        head = QHBoxLayout(); head.setSpacing(6)
        if mo.gold:
            star = QLabel()
            star.setPixmap(_star_pixmap(c["accent"]))
            star.setToolTip("За этот момент проголосовали РАЗНЫЕ по природе улики — "
                            "например, и зрители, и звук. Такие почти не бывают ошибкой.")
            head.addWidget(star)
        title = QLabel(mo.label or f"Момент {idx + 1}")
        title.setWordWrap(True)
        title.setStyleSheet("font-weight:700;font-size:13px;")
        head.addWidget(title, 1)
        mid.addLayout(head)

        rng = QLabel(f"{fmt_time(mo.start)} – {fmt_time(mo.end)}  ·  {mo.duration:.0f} с")
        rng.setStyleSheet(f"color:{c['muted']};font-size:11px;")
        mid.addWidget(rng)

        why = QLabel(mo.why()); why.setWordWrap(True)
        why.setStyleSheet(f"color:{c['text']};font-size:11px;")
        why.setToolTip("Почему программа выбрала этот момент")
        mid.addWidget(why)
        lay.addLayout(mid, 1)

        # Кнопки справа: посмотреть на Twitch и «не тот момент».
        side = QVBoxLayout(); side.setSpacing(4)
        if watchable:
            self.watch_btn = QPushButton("Глянуть")
            self.watch_btn.setToolTip("Открыть запись на Twitch ровно на этом месте — "
                                      "проверить момент глазами, ничего не скачивая")
            self.watch_btn.clicked.connect(lambda: self.watch.emit(self.idx))
            side.addWidget(self.watch_btn)
        drop = QPushButton("Не тот")
        drop.setToolTip("Убрать момент из списка: программа запомнит промах")
        drop.clicked.connect(lambda: self.rejected.emit(self.idx))
        side.addWidget(drop)
        side.addStretch(1)
        lay.addLayout(side)

        self.setStyleSheet(f"QFrame#panel2{{background:{c['panel2']};"
                           f"border:1px solid {c['line']};border-radius:10px;}}")

    def set_selected(self, on: bool) -> None:
        c = PALETTE[self._theme]
        border = c["accent"] if on else c["line"]
        self.setStyleSheet(f"QFrame#panel2{{background:{c['panel2']};"
                           f"border:1px solid {border};border-radius:10px;}}")

    def mousePressEvent(self, e) -> None:
        self.clicked.emit(self.idx)
        super().mousePressEvent(e)


# ==========================================================================
# Потоки: разбор и загрузка кусков
# ==========================================================================

class ScanThread(QThread):
    """Разбор стрима. Всё тяжёлое (сеть, звук, Whisper) — здесь, окно живое."""

    progress = Signal(str)
    plan_needed = Signal(object)         # спросить человека про долгую речь
    done = Signal(object)                # ScanResult
    failed = Signal(str)

    def __init__(self, link: str = "", video: str = "", token: str = "",
                 strictness: float = 50.0, audio: bool = True, speech: str = "auto",
                 composition=None, parent=None):
        super().__init__(parent)
        self._link = link
        self._video = video
        self._token = token
        self._strict = strictness
        self._audio = audio
        self._speech = speech
        self._comp = composition
        self._stop = False
        self._answer: Optional[object] = None
        self._answered = threading.Event()

    # ---- управление снаружи ----
    def stop(self) -> None:
        self._stop = True
        self._answered.set()          # разбудить ожидание ответа про речь

    def answer_plan(self, plan) -> None:
        """Ответ из окна: план (может быть с другой моделью) или None — пропустить."""
        self._answer = plan
        self._answered.set()

    # ---- внутреннее ----
    def _on_plan(self, plan):
        if self._stop:
            return None
        # Быстрое распознавание не стоит модального окна — спрашиваем только про долгое.
        if plan.est_sec < ASK_ABOVE_SEC:
            return plan
        self._answer = plan
        self._answered.clear()
        self.plan_needed.emit(plan)
        self._answered.wait()
        return None if self._stop else self._answer

    def run(self) -> None:
        try:
            from core import scanner
            if self._video and not self._link:
                res = scanner.scan_file(
                    self._video, strictness=self._strict, speech=self._speech,
                    composition=self._comp, should_stop=lambda: self._stop,
                    on_plan=self._on_plan, progress=self.progress.emit)
            else:
                res = scanner.scan_link(
                    self._link, self._token, strictness=self._strict,
                    composition=self._comp, audio=self._audio, speech=self._speech,
                    video_path=self._video, should_stop=lambda: self._stop,
                    on_plan=self._on_plan, progress=self.progress.emit)
        except Exception as ex:                     # noqa: BLE001 — показываем как есть
            if not self._stop:
                self.failed.emit(str(ex))
            return
        if not self._stop:
            self.done.emit(res)


class CutThread(QThread):
    """Скачать выбранные куски записи (и только их) перед нарезкой."""

    progress = Signal(str)
    done = Signal(object)                # list[ClipPiece]
    failed = Signal(str)

    def __init__(self, vod_id: str, url: str, windows: list, quality: str, parent=None):
        super().__init__(parent)
        self._vod = vod_id
        self._url = url
        self._windows = list(windows)
        self._quality = quality
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            from core import vodcut
            pieces = vodcut.fetch_windows(
                self._vod, self._windows, url=self._url, quality=self._quality,
                progress=self.progress.emit, should_stop=lambda: self._stop)
        except Exception as ex:                     # noqa: BLE001
            if not self._stop:
                self.failed.emit(str(ex))
            return
        if not self._stop:
            self.done.emit(pieces)


# ==========================================================================
# Сам экран
# ==========================================================================

class ScanModePanel(QWidget):
    """Экран «Автопоиск моментов ИИ»: слева настройки, справа дорожка и моменты."""

    render_requested = Signal()

    def __init__(self, theme: str = "dark", parent=None):
        super().__init__(parent)
        self._theme = theme
        self._scan: Optional[ScanFile] = None
        self._res = None                              # ScanResult
        self._video: str = ""                         # локальная запись, если выбрана
        self._pieces: dict = {}                       # индекс момента → ClipPiece
        self._included: list[bool] = []
        self._cards: list[ScanCard] = []
        self._rejected: list[float] = []              # центры «не тех» — задел на блок 7
        self._scan_thread: Optional[ScanThread] = None
        self._cut_thread: Optional[CutThread] = None
        self._closing = False
        self._build()
        self._update_state()

    # ---------------------------------------------------------------- сборка
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
        s1, v1 = _section("ИСТОЧНИК", self._theme)
        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText("twitch.tv/videos/… или twitch.tv/канал")
        self.link_edit.returnPressed.connect(self._start_scan)
        v1.addWidget(self.link_edit)
        self.scan_btn = QPushButton("Разобрать стрим")
        self.scan_btn.setStyleSheet(self._primary_css())
        self.scan_btn.clicked.connect(self._start_scan)
        v1.addWidget(self.scan_btn)
        v1.addWidget(self._muted("Видео не качается: для разбора берётся только "
                                 "звуковая дорожка."))
        self.file_btn = QPushButton("Взять запись с компьютера")
        self.file_btn.setToolTip("Разбор локального файла: без интернета, клипов и чата "
                                 "у него нет — работают звук и речь")
        self.file_btn.clicked.connect(self._pick_file)
        v1.addWidget(self.file_btn)
        self.file_lbl = self._muted("")
        self.file_lbl.setVisible(False)
        v1.addWidget(self.file_lbl)
        self.recent_combo = QComboBox()
        self.recent_combo.setToolTip("Прошлые разборы — открываются мгновенно, без сети")
        self.recent_combo.activated.connect(self._open_recent)
        v1.addWidget(self.recent_combo)
        col.addWidget(s1)

        # --- Строгость ---
        s2, v2 = _section("СТРОГОСТЬ ОТБОРА", self._theme)
        self.strict_slider = QSlider(Qt.Horizontal)
        self.strict_slider.setRange(0, 100); self.strict_slider.setValue(50)
        self.strict_slider.setToolTip("Мягко — берём всё подозрительное; строго — только "
                                      "самые верные моменты. Пересчёт мгновенный.")
        self.strict_slider.valueChanged.connect(self._on_strictness)
        srow = QWidget(); sl = QHBoxLayout(srow); sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(8)
        sl.addWidget(self._muted("мягко")); sl.addWidget(self.strict_slider, 1)
        sl.addWidget(self._muted("строго"))
        v2.addWidget(srow)
        self.found_lbl = QLabel("Моментов пока нет")
        self.found_lbl.setStyleSheet("font-size:12px;font-weight:700;")
        v2.addWidget(self.found_lbl)
        col.addWidget(s2)

        # --- Что слушать ---
        s3, v3 = _section("ЧТО СЛУШАТЬ", self._theme)
        rowa, self.audio_sw = _toggle_row("Слушать звук стрима", True, self._theme)
        self.audio_sw.setToolTip("Крик, смех и «тишина→взрыв». Работает на любом стриме, "
                                 "даже без чата и без клипов зрителей")
        v3.addWidget(rowa)
        v3.addWidget(self._muted("Распознавать речь:"))
        self.speech_combo = QComboBox()
        self.speech_combo.addItems(list(SPEECH_LABELS.keys()))
        self.speech_combo.setToolTip("Речь даёт моментам названия цитатой и ловит "
                                     "эмоции. Это самый долгий шаг разбора")
        v3.addWidget(self.speech_combo)
        col.addWidget(s3)

        # --- Раскладка (подсказка) ---
        s4, v4 = _section("РАСКЛАДКА", self._theme)
        v4.addWidget(self._muted(
            "Настрой зоны и выключи ненужные панели во вкладке «Раскладка» справа "
            "(пресеты A–E, включая «без вебки»)."))
        col.addWidget(s4)

        # --- Звук клипа ---
        s5, v5 = _section("ЗВУК КЛИПА", self._theme)
        self.audio_switches: dict[str, ToggleSwitch] = {}
        for key, name, on in [("loudnorm", "Нормализация громкости", True),
                              ("denoise", "Шумодав", False),
                              ("clarity", "Чёткость голоса", False),
                              ("gate", "Гейт (тишина в паузах)", False)]:
            row, sw = _toggle_row(name, on, self._theme)
            self.audio_switches[key] = sw
            v5.addWidget(row)
        v5.addWidget(self._muted("Мат:"))
        self.prof_chips = ChipRow(["Не трогать", "Бип", "Заглушить"])
        v5.addWidget(self.prof_chips)
        col.addWidget(s5)

        # --- Экспорт ---
        s6, v6 = _section("КАЧЕСТВО И ЭКСПОРТ", self._theme)
        v6.addWidget(self._muted("Качество кусков:"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(QUALITY_LABELS)
        self.quality_combo.currentTextChanged.connect(lambda _t: self._update_estimate())
        v6.addWidget(self.quality_combo)
        nrow = QWidget(); nl = QHBoxLayout(nrow); nl.setContentsMargins(0, 0, 0, 0); nl.setSpacing(6)
        nl.addWidget(self._muted("Ник:"))
        self.nick_edit = QLineEdit(); self.nick_edit.setPlaceholderText("подставится из ссылки")
        nl.addWidget(self.nick_edit, 1)
        v6.addWidget(nrow)
        prow = QWidget(); pl = QHBoxLayout(prow); pl.setContentsMargins(0, 0, 0, 0); pl.setSpacing(6)
        pl.addWidget(self._muted("Платформа:"))
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["Twitch", "YouTube", "Kick", "Без значка"])
        pl.addWidget(self.platform_combo, 1)
        v6.addWidget(prow)
        self.estimate_lbl = self._muted("")
        v6.addWidget(self.estimate_lbl)
        self.render_btn = QPushButton("Сделать клипы")
        self.render_btn.setStyleSheet(self._primary_css())
        self.render_btn.clicked.connect(self._export)
        v6.addWidget(self.render_btn)
        col.addWidget(s6)

        col.addStretch(1)
        scroll.setWidget(inner)
        scroll.setStyleSheet("QScrollArea{background:transparent;}")
        self._refresh_recent()
        return scroll

    def _right_column(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)
        c = PALETTE[self._theme]

        seg = QWidget(); sl = QHBoxLayout(seg); sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(6)
        self.tab_moments = QPushButton("Моменты"); self.tab_moments.setCheckable(True)
        self.tab_moments.setChecked(True)
        self.tab_layout = QPushButton("Раскладка"); self.tab_layout.setCheckable(True)
        tabcss = (f"QPushButton{{background:{c['panel2']};border:1px solid {c['line']};"
                  f"border-radius:8px;padding:8px 18px;font-weight:700;}}"
                  f"QPushButton:checked{{background:{c['accent']};border-color:{c['accent']};"
                  f"color:#fff;}}")
        for b in (self.tab_moments, self.tab_layout):
            b.setCursor(Qt.PointingHandCursor); b.setStyleSheet(tabcss)
        grp = QButtonGroup(self); grp.setExclusive(True)
        grp.addButton(self.tab_moments); grp.addButton(self.tab_layout)
        self.tab_moments.clicked.connect(lambda: self.right_stack.setCurrentIndex(0))
        self.tab_layout.clicked.connect(lambda: self.right_stack.setCurrentIndex(1))
        sl.addWidget(self.tab_moments); sl.addWidget(self.tab_layout); sl.addStretch(1)
        v.addWidget(seg)

        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self._moments_page())
        self.layout_editor = EditorPanel(show_trim=False)
        self.layout_editor.set_theme(self._theme)
        self.right_stack.addWidget(self.layout_editor)
        v.addWidget(self.right_stack, 1)
        return wrap

    def _moments_page(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(12)
        c = PALETTE[self._theme]

        # --- Ход разбора (виден только во время работы) ---
        self.progress_card = QFrame(); self.progress_card.setObjectName("card")
        self.progress_card.setStyleSheet(f"QFrame#card{{background:{c['panel']};"
                                         f"border:1px solid {c['line']};border-radius:12px;}}")
        pl = QVBoxLayout(self.progress_card)
        pl.setContentsMargins(12, 10, 12, 10); pl.setSpacing(6)
        hrow = QWidget(); hl = QHBoxLayout(hrow); hl.setContentsMargins(0, 0, 0, 0)
        self.progress_head = QLabel("РАЗБИРАЮ СТРИМ")
        self.progress_head.setStyleSheet("font-weight:800;font-size:12px;letter-spacing:.5px;")
        hl.addWidget(self.progress_head); hl.addStretch(1)
        self.stop_btn = QPushButton("Стоп")
        self.stop_btn.setToolTip("Прервать разбор. Уже собранные улики не потеряются")
        self.stop_btn.clicked.connect(self._stop_work)
        hl.addWidget(self.stop_btn)
        pl.addWidget(hrow)
        self.progress_log = QTextEdit(); self.progress_log.setReadOnly(True)
        self.progress_log.setFixedHeight(96)
        self.progress_log.setStyleSheet(
            f"QTextEdit{{background:{c['panel2']};border:1px solid {c['line']};"
            f"border-radius:8px;color:{c['text']};font-size:11px;}}")
        pl.addWidget(self.progress_log)
        self.progress_card.setVisible(False)
        v.addWidget(self.progress_card)

        # --- Дорожка ---
        tcard = QFrame(); tcard.setObjectName("card")
        tcard.setStyleSheet(f"QFrame#card{{background:{c['panel']};border:1px solid {c['line']};"
                            f"border-radius:12px;}}")
        tl = QVBoxLayout(tcard); tl.setContentsMargins(12, 10, 12, 10); tl.setSpacing(6)
        self.stream_head = QLabel("ДОРОЖКА СТРИМА  ·  улики и найденные моменты")
        self.stream_head.setStyleSheet("font-weight:800;font-size:12px;letter-spacing:.5px;")
        tl.addWidget(self.stream_head)
        tl.addWidget(self._family_legend())
        self.timeline = ScanTimeline()
        self.timeline.set_theme(self._theme)
        self.timeline.moment_edited.connect(self._on_moment_edited)
        self.timeline.moment_clicked.connect(self._select)
        tl.addWidget(self.timeline)
        v.addWidget(tcard)

        # --- Моменты ---
        mcard = QFrame(); mcard.setObjectName("card")
        mcard.setStyleSheet(f"QFrame#card{{background:{c['panel']};border:1px solid {c['line']};"
                            f"border-radius:12px;}}")
        ml = QVBoxLayout(mcard); ml.setContentsMargins(12, 10, 12, 12); ml.setSpacing(8)
        hr2 = QWidget(); hl2 = QHBoxLayout(hr2); hl2.setContentsMargins(0, 0, 0, 0)
        self.moments_head = QLabel("МОМЕНТЫ")
        self.moments_head.setStyleSheet("font-weight:800;font-size:12px;letter-spacing:.5px;")
        hl2.addWidget(self.moments_head); hl2.addStretch(1)
        self.all_btn = QPushButton("Отметить все")
        self.all_btn.clicked.connect(self._take_all)
        hl2.addWidget(self.all_btn)
        ml.addWidget(hr2)

        self.notes_lbl = QLabel(""); self.notes_lbl.setWordWrap(True)
        self.notes_lbl.setStyleSheet("color:#f0a93a;font-size:11px;")
        self.notes_lbl.setVisible(False)
        ml.addWidget(self.notes_lbl)

        self.moments_scroll = QScrollArea(); self.moments_scroll.setWidgetResizable(True)
        self.moments_scroll.setFrameShape(QFrame.NoFrame)
        # Прозрачны и область, и viewport, и хост — иначе на тёмной теме пустой
        # список светится белым прямоугольником (грабля проекта, см. CLAUDE.md).
        self.moments_scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}")
        self.moments_scroll.viewport().setAutoFillBackground(False)
        self.moments_host = QWidget(); self.moments_host.setAutoFillBackground(False)
        self.moments_vl = QVBoxLayout(self.moments_host)
        self.moments_vl.setContentsMargins(0, 0, 4, 0); self.moments_vl.setSpacing(7)
        self.moments_vl.addStretch(1)
        self.moments_scroll.setWidget(self.moments_host)
        ml.addWidget(self.moments_scroll, 1)
        self.moments_empty = QLabel(
            "Моментов пока нет.\n\nВставь ссылку на стрим слева и нажми «Разобрать» —\n"
            "программа послушает запись и сама найдёт лучшие места.")
        self.moments_empty.setAlignment(Qt.AlignCenter)
        self.moments_empty.setStyleSheet(f"color:{c['muted']};font-size:12px;")
        ml.addWidget(self.moments_empty, 1)
        v.addWidget(mcard, 1)
        return wrap

    def _family_legend(self) -> QWidget:
        c = PALETTE[self._theme]
        w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(14)
        self._legend_items: dict[str, QWidget] = {}
        for fam in FAMILY_ORDER:
            item = QWidget(); il = QHBoxLayout(item)
            il.setContentsMargins(0, 0, 0, 0); il.setSpacing(5)
            d = QLabel(); d.setFixedSize(10, 10)
            d.setStyleSheet(f"background:{FAMILY_COLORS[fam]};border-radius:5px;")
            t = QLabel(FAMILY_LABEL[fam])
            t.setStyleSheet(f"color:{c['muted']};font-size:11px;")
            il.addWidget(d); il.addWidget(t); l.addWidget(item)
            self._legend_items[fam] = item
        l.addStretch(1)
        return w

    def _update_legend(self, signals: list) -> None:
        """Показывать только те семьи улик, которые в этом разборе ЕСТЬ.

        Иначе легенда обещает «Лицо» и «Чат» там, где вебки нет, а бот не работал, —
        человек ищет на дорожке цвета, которых там быть не может.
        """
        present = {FAMILY.get(getattr(s, "kind", ""), "") for s in signals}
        for fam, item in self._legend_items.items():
            item.setVisible(fam in present if signals else True)

    def _muted(self, text: str) -> QLabel:
        c = PALETTE[self._theme]
        lab = QLabel(text); lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{c['muted']};font-size:11px;")
        return lab

    def _primary_css(self) -> str:
        c = PALETTE[self._theme]
        return (f"background:{c['accent']};border:1px solid {c['accent']};color:#fff;"
                f"border-radius:9px;padding:11px 16px;font-weight:800;font-size:13px;")

    # ------------------------------------------------------------- источники
    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Запись стрима", "",
                                              "Видео (*.mp4 *.mkv *.mov *.webm *.flv)")
        if not path:
            return
        self._video = path
        self.file_lbl.setText(f"Запись: {os.path.basename(path)}")
        self.file_lbl.setVisible(True)
        self._start_scan()

    def _refresh_recent(self) -> None:
        """Список прошлых разборов — открываются мгновенно, сеть не нужна."""
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("Недавние разборы…", "")
        try:
            from core.chatbot import default_marks_dir
            folder = default_marks_dir()
            files = [os.path.join(folder, n) for n in os.listdir(folder)
                     if n.endswith(".clipscan")]
        except OSError:
            files = []
        for path in sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)[:12]:
            self.recent_combo.addItem(os.path.basename(path)[:-9], path)
        self.recent_combo.setVisible(self.recent_combo.count() > 1)
        self.recent_combo.blockSignals(False)

    def _open_recent(self, index: int) -> None:
        path = self.recent_combo.itemData(index)
        if not path:
            return
        try:
            scan = ScanFile.from_json(path)
        except (OSError, ValueError) as ex:
            QMessageBox.warning(self, "Не открылось", f"Файл разбора не прочитался: {ex}")
            return
        self._res = None
        self._pieces = {}
        self._apply_scan(scan)
        self._log(f"Открыт прошлый разбор: {os.path.basename(path)}")

    # ---------------------------------------------------------------- разбор
    def _start_scan(self) -> None:
        if self._scan_thread and self._scan_thread.isRunning():
            return
        link = self.link_edit.text().strip()
        if not link and not self._video:
            QMessageBox.information(self, "Нужна ссылка",
                                    "Вставь ссылку на запись стрима (twitch.tv/videos/…) "
                                    "или выбери запись с компьютера.")
            return
        token = ""
        if link:
            try:
                from core.twitch_auth import ensure_token
                token = ensure_token() or ""
            except Exception:                       # noqa: BLE001 — вход не обязан быть
                token = ""
            if not token:
                QMessageBox.information(
                    self, "Нужен вход через Twitch",
                    "Чтобы спросить у Twitch клипы зрителей, программа должна быть "
                    "залогинена. Зайди в режим «Метки через бота» → вкладка «Бот» → "
                    "«Войти через Twitch» (одна кнопка, код на экране), потом вернись "
                    "сюда.\n\nБез входа можно разобрать запись с компьютера — кнопка "
                    "«Взять запись с компьютера».")
                return

        self._pieces = {}
        self.progress_log.clear()
        self.progress_card.setVisible(True)
        self.progress_head.setText("РАЗБИРАЮ СТРИМ")
        self.stop_btn.setVisible(True)
        self._set_busy(True)
        comp = self.layout_editor.get_composition()
        speech = SPEECH_LABELS.get(self.speech_combo.currentText(), "auto")
        self._scan_thread = ScanThread(
            link=link, video=self._video, token=token,
            strictness=float(self.strict_slider.value()),
            audio=self.audio_sw.isChecked(), speech=speech, composition=comp, parent=self)
        self._scan_thread.progress.connect(self._log)
        self._scan_thread.plan_needed.connect(self._ask_plan)
        self._scan_thread.done.connect(self._scan_done)
        self._scan_thread.failed.connect(self._scan_failed)
        self._scan_thread.start()

    def _ask_plan(self, plan) -> None:
        """Долгая речь: спросить человека ДО начала, а не заставлять гадать."""
        thread = self._scan_thread
        if thread is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Распознавание речи")
        box.setIcon(QMessageBox.Question)
        box.setText(plan.human())
        box.setInformativeText(plan.advice() or
                               "Речь даёт моментам названия цитатой. Можно подождать, "
                               "взять модель побыстрее или пропустить этот шаг.")
        wait_btn = box.addButton("Подождать", QMessageBox.AcceptRole)
        fast_btn = box.addButton("Побыстрее", QMessageBox.ActionRole)
        skip_btn = box.addButton("Пропустить речь", QMessageBox.RejectRole)
        box.setDefaultButton(wait_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip_btn:
            thread.answer_plan(None)
            return
        if clicked is fast_btn:
            from core import speech as sp
            # На ступеньку быстрее текущей: small почти везде, а если уже small —
            # остаётся base (грубее, но ждать почти нечего).
            faster = sp.with_model(plan, "base" if plan.model in ("small", "base")
                                   else "small")
            self._log(faster.human())
            thread.answer_plan(faster)
            return
        thread.answer_plan(plan)

    def _scan_done(self, res) -> None:
        if self._closing:
            return
        self._res = res
        self._set_busy(False)
        self.progress_head.setText("РАЗБОР ГОТОВ")
        self.stop_btn.setVisible(False)
        if not self.nick_edit.text().strip() and getattr(res.vod, "channel", ""):
            self.nick_edit.setText(res.vod.channel)
        self._apply_scan(res.scan)
        # Сохраняем разбор: повторно открыть его можно будет мгновенно, без сети.
        try:
            from core import scanner
            path = scanner.default_scan_path(res.vod)
            res.scan.to_json(path)
            self._log(f"Разбор сохранён: {os.path.basename(path)}")
            self._refresh_recent()
        except (OSError, ValueError) as ex:
            self._log(f"Разбор не сохранился на диск: {ex}")

    def _scan_failed(self, msg: str) -> None:
        if self._closing:
            return
        self._set_busy(False)
        self.progress_head.setText("РАЗБОР НЕ ПОЛУЧИЛСЯ")
        self.stop_btn.setVisible(False)
        self._log(msg)
        QMessageBox.warning(self, "Разбор не получился", msg)

    def _stop_work(self) -> None:
        if self._scan_thread and self._scan_thread.isRunning():
            self._scan_thread.stop()
            self._log("Останавливаю разбор…")
        if self._cut_thread and self._cut_thread.isRunning():
            self._cut_thread.stop()
            self._log("Останавливаю загрузку…")
        self._set_busy(False)
        self.stop_btn.setVisible(False)

    def _log(self, text: str) -> None:
        self.progress_log.append(text)
        bar = self.progress_log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_busy(self, on: bool) -> None:
        for w in (self.scan_btn, self.file_btn, self.link_edit, self.render_btn,
                  self.recent_combo, self.speech_combo, self.audio_sw):
            w.setEnabled(not on)
        self.scan_btn.setText("Разбираю…" if on else "Разобрать стрим")

    # ------------------------------------------------------------- результат
    def _apply_scan(self, scan: ScanFile) -> None:
        self._scan = scan
        self._rejected = []
        self.strict_slider.blockSignals(True)
        self.strict_slider.setValue(int(scan.strictness))
        self.strict_slider.blockSignals(False)
        self.timeline.set_duration(scan.duration or 0.0)
        self.timeline.set_signals(scan.signals)
        self._update_legend(scan.signals)
        # Ник нужен для водяного знака и имён файлов: у разбора он уже известен, и
        # заставлять человека печатать его руками незачем.
        if not self.nick_edit.text().strip() and scan.channel:
            self.nick_edit.setText(scan.channel)
        notes = list(scan.notes or [])
        self.notes_lbl.setText("\n".join(notes))
        self.notes_lbl.setVisible(bool(notes))
        self._rebuild()

    def _on_strictness(self, value: int) -> None:
        """Ползунок строгости: пересчёт по уже собранным уликам — сеть не нужна."""
        if self._scan is None:
            return
        self._scan.rescore(float(value))
        self._pieces = {}                 # границы поехали — старые куски не годятся
        self._rebuild()

    def _rebuild(self) -> None:
        moments = self._moments()
        self._included = [True] * len(moments)
        self.timeline.set_moments(moments)
        self._rebuild_cards()
        self._update_state()

    def _moments(self) -> list[ScanMoment]:
        """ЖИВОЙ список моментов разбора, а не копия.

        ⚠️ Раньше здесь был `list(...)`, и «не тот момент» удалял запись из копии:
        карточка исчезала на миг и возвращалась при следующей перерисовке. Правка
        границ мышью работала (объекты-то те же), а удаление — нет.
        """
        return self._scan.moments if self._scan else []

    def _rebuild_cards(self) -> None:
        for card in self._cards:
            card.setParent(None)
        self._cards = []
        watchable = bool(self._scan and self._scan.source.get("url"))
        for i, mo in enumerate(self._moments()):
            card = ScanCard(i, mo, self._theme, watchable=watchable)
            card.toggled.connect(self._on_toggle)
            card.clicked.connect(self._select)
            card.watch.connect(self._watch)
            card.rejected.connect(self._reject)
            self.moments_vl.insertWidget(self.moments_vl.count() - 1, card)
            self._cards.append(card)

    def _update_state(self) -> None:
        moments = self._moments()
        has = bool(moments)
        self.moments_empty.setVisible(not has)
        self.moments_scroll.setVisible(has)
        if self._scan is None:
            self.found_lbl.setText("Моментов пока нет")
        else:
            possible = max(len(moments), self._scan.possible())
            self.found_lbl.setText(f"Нашлось {len(moments)} из {possible} возможных")
        self.moments_head.setText(f"МОМЕНТЫ  ·  выбрано {sum(self._included)}"
                                  if has else "МОМЕНТЫ")
        self._update_estimate()

    def _update_estimate(self) -> None:
        """Честно сказать, сколько скачается, ДО нажатия кнопки."""
        chosen = self._chosen()
        if not chosen or self._video:
            self.estimate_lbl.setText("")
            return
        from core import vodcut
        seconds = sum(mo.duration + 2 * vodcut.PAD for _i, mo in chosen)
        mb = vodcut.estimate_mb(seconds, self.quality_combo.currentText())
        self.estimate_lbl.setText(f"Скачается примерно {vodcut.human_size(mb)} — "
                                  f"только выбранные куски, а не весь стрим.")

    # ---------------------------------------------------------- взаимодействие
    def _select(self, idx: int) -> None:
        self.timeline.set_active(idx)
        for i, card in enumerate(self._cards):
            card.set_selected(i == idx)

    def _on_toggle(self, idx: int, on: bool) -> None:
        if 0 <= idx < len(self._included):
            self._included[idx] = on
        self._update_state()

    def _take_all(self) -> None:
        for card in self._cards:
            card.chk.setChecked(True)

    def _on_moment_edited(self, idx: int, start: float, end: float) -> None:
        moments = self._moments()
        if not (0 <= idx < len(moments)):
            return
        moments[idx].start, moments[idx].end = start, end
        self._pieces.pop(idx, None)       # границы изменились — качать заново
        if idx < len(self._cards):
            self._cards[idx].setToolTip(f"{fmt_time(start)} – {fmt_time(end)}")
        self._update_estimate()

    def _watch(self, idx: int) -> None:
        """Открыть запись на Twitch ровно на этом месте — проверка глазами без загрузки."""
        moments = self._moments()
        if self._scan is None or not (0 <= idx < len(moments)):
            return
        url = self._scan.source.get("url") or ""
        if not url:
            return
        t = int(max(0.0, moments[idx].start))
        stamp = f"{t // 3600:d}h{(t % 3600) // 60:02d}m{t % 60:02d}s"
        QDesktopServices.openUrl(QUrl(f"{url}?t={stamp}"))

    def _reject(self, idx: int) -> None:
        """«Не тот момент»: убрать из списка и запомнить промах (задел на блок 7)."""
        moments = self._moments()
        if not (0 <= idx < len(moments)):
            return
        self._rejected.append(moments[idx].center)
        del moments[idx]
        # Куски скачаны под СТАРЫЕ номера: после удаления они съедут на соседние
        # моменты, и клип уехал бы не туда. Дешевле забыть — кэш всё равно на диске.
        self._pieces = {}
        self._included = [True] * len(moments)
        self.timeline.set_moments(moments)
        self._rebuild_cards()
        self._update_state()

    # ------------------------------------------------------------------ экспорт
    def _chosen(self) -> list[tuple[int, ScanMoment]]:
        return [(i, mo) for i, mo in enumerate(self._moments())
                if i < len(self._included) and self._included[i]]

    def validate(self) -> Optional[str]:
        if self._scan is None:
            return "Стрим ещё не разобран — вставь ссылку и нажми «Разобрать»."
        if not self._chosen():
            return "Не выбран ни один момент."
        if not self._video and not self._pieces:
            return "Куски записи ещё не скачаны."
        return None

    def _export(self) -> None:
        """Кнопка «Сделать клипы»: сперва куски записи, потом обычная нарезка."""
        if self._scan is None:
            QMessageBox.information(self, "Нечего нарезать",
                                    "Сначала разбери стрим: вставь ссылку и нажми "
                                    "«Разобрать».")
            return
        chosen = self._chosen()
        if not chosen:
            QMessageBox.information(self, "Нечего нарезать",
                                    "Отметь галочками моменты, которые берём в нарезку.")
            return
        if self._video:                       # локальная запись — качать нечего
            self.render_requested.emit()
            return
        vod_id = self._scan.source.get("vod_id") or ""
        url = self._scan.source.get("url") or ""
        if not vod_id and not url:
            QMessageBox.warning(self, "Нет записи",
                                "У этого разбора нет ссылки на запись — выбери файл "
                                "записи с компьютера.")
            return
        windows = [(mo.start, mo.end) for _i, mo in chosen]
        self.progress_log.clear()
        self.progress_card.setVisible(True)
        self.progress_head.setText("КАЧАЮ ВЫБРАННЫЕ КУСКИ")
        self.stop_btn.setVisible(True)
        self._set_busy(True)
        self._cut_thread = CutThread(vod_id, url, windows,
                                     self.quality_combo.currentText(), parent=self)
        self._cut_thread.progress.connect(self._log)
        self._cut_thread.done.connect(lambda pieces: self._cut_done(chosen, pieces))
        self._cut_thread.failed.connect(self._cut_failed)
        self._cut_thread.start()

    def _cut_done(self, chosen: list, pieces: list) -> None:
        if self._closing:
            return
        self._set_busy(False)
        self.stop_btn.setVisible(False)
        self.progress_head.setText("КУСКИ СКАЧАНЫ")
        for (idx, _mo), piece in zip(chosen, pieces):
            self._pieces[idx] = piece
        self.render_requested.emit()

    def _cut_failed(self, msg: str) -> None:
        if self._closing:
            return
        self._set_busy(False)
        self.stop_btn.setVisible(False)
        self.progress_head.setText("КУСКИ НЕ СКАЧАЛИСЬ")
        self._log(msg)
        QMessageBox.warning(self, "Не удалось скачать куски", msg)

    def build_pipeline_configs(self, out_dir: str) -> list:
        """По одному PipelineConfig на выбранный момент.

        Источник у каждого свой: либо скачанный кусок (время внутри него своё, поэтому
        границы пересчитываются через `ClipPiece.local`), либо локальная запись целиком.
        """
        from core.pipeline import PipelineConfig
        from core.config import ExportConfig
        from core.captions import CaptionStyle, CaptionAnimation
        from core.branding import BrandingConfig, Platform

        comp = self.layout_editor.get_composition()
        pmap = {"Twitch": "twitch", "YouTube": "youtube", "Kick": "kick",
                "Без значка": "none"}
        prof = {"Не трогать": ("off", "beep"), "Бип": ("beep", "beep"),
                "Заглушить": ("silence", "silence")}[self.prof_chips.current()]
        cfgs = []
        for n, (idx, mo) in enumerate(self._chosen()):
            piece = self._pieces.get(idx)
            if piece is not None:
                source = piece.path
                segs = [Segment(start=piece.local(mo.start), end=piece.local(mo.end))]
            elif self._video:
                source = self._video
                segs = [Segment(start=mo.start, end=mo.end)]
            else:
                continue
            cfgs.append(PipelineConfig(
                source=source,
                segments=segs,
                composition=comp,
                export=ExportConfig(out_dir=out_dir, filename=self._clip_name(n, mo)),
                captions_enabled=getattr(comp.subtitles, "visible", True),
                caption_style=CaptionStyle(),
                caption_animation=CaptionAnimation.POP,
                profanity_enabled=(prof[0] != "off"),
                profanity_mode=("beep" if prof[0] == "beep" else "silence"),
                loudnorm=self.audio_switches["loudnorm"].isChecked(),
                denoise=self.audio_switches["denoise"].isChecked(),
                clarity=self.audio_switches["clarity"].isChecked(),
                gate=self.audio_switches["gate"].isChecked(),
                branding=BrandingConfig(
                    nickname=self.nick_edit.text().strip(),
                    platform=Platform(pmap[self.platform_combo.currentText()])),
            ))
        return cfgs

    def _stem(self) -> str:
        name = self.nick_edit.text().strip() or (self._scan.channel if self._scan else "")
        if not name and self._video:
            name = os.path.splitext(os.path.basename(self._video))[0]
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "_")
        day = ((self._scan.source.get("started_at") or "")[:10]) if self._scan else ""
        return f"{name}_{day}" if day else (name or "stream")

    def _clip_name(self, idx: int, mo: ScanMoment) -> str:
        label = (mo.label or "").strip()
        for ch in '<>:"/\\|?*':
            label = label.replace(ch, "_")
        label = label.replace(" ", "_")[:40]
        tail = f"{idx + 1:02d}_{label}" if label else f"{idx + 1:02d}"
        return f"{self._stem()}_{tail}_vertical.mp4"

    # --------------------------------------------------------------------- тема
    def apply_theme(self, theme: str) -> None:
        from . import theme as theme_mod
        theme_mod.set_current(theme)
        self._theme = theme
        self.timeline.set_theme(theme)
        for cls in (Chip, ToggleSwitch):
            for w in self.findChildren(cls):
                w.set_theme(theme)
        self.layout_editor.set_theme(theme)
        self.scan_btn.setStyleSheet(self._primary_css())
        self.render_btn.setStyleSheet(self._primary_css())
        self.update()

    def shutdown(self) -> None:
        """Погасить потоки при закрытии окна: иначе Qt рушит процесс на выходе."""
        self._closing = True
        for th in (self._scan_thread, self._cut_thread):
            if th and th.isRunning():
                th.stop()
                if not th.wait(4000):
                    th.terminate()
                    th.wait(1000)


__all__ = ["FAMILY_COLORS", "FAMILY_LABEL", "ScanCard", "ScanModePanel", "ScanTimeline",
           "ScanThread", "CutThread"]
