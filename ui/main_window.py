"""main_window.py — главное окно «Клип-Полировщик» по UX-эталону (docs/ui-reference.html).

Тонкая оболочка: собирает настройки из мастера и вызывает core через фоновые потоки
(ui.worker). Логики обработки здесь нет.

Компоновка: фон (сфера+сетка) → топбар → [рейка режимов | мастер | превью] → загрузчик.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

from PySide6.QtCore import Qt, QRect, QPoint, QSettings
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox,
    QDoubleSpinBox, QSlider, QFileDialog, QHBoxLayout, QVBoxLayout, QStackedLayout,
    QFrame, QButtonGroup, QMessageBox, QSizePolicy, QScrollArea, QBoxLayout,
    QInputDialog,
)

from core.config import (LayoutConfig, LayoutPreset, ExportConfig, VideoCodec,
                         Corner, Segment, Composition)
from core.captions import CaptionStyle, CaptionAnimation
from core.branding import BrandingConfig, Platform
from core.pipeline import PipelineConfig
from core import profiles as profiles_mod
from core import ffmpeg_utils as ff
from core.layout import load_preset

from .theme import build_qss, PALETTE, STEP_COLORS
from .background import AnimatedBackground
from .wizard import Wizard
from .widgets import HelpIcon, ChipRow, ToggleSwitch, VersionPill
from .preview_panel import EditorPanel
from .clip_strip import ClipStrip, ClipItem
from .marks_mode import MarksModePanel
from .mode_menu import ModeMenuOverlay
from .loader import LoaderOverlay
from . import worker as W

PROFANITY_MODES = {"Не трогать": "off", "Бип": "beep", "Заглушить": "silence"}

# Служебная temp-папка для превью-кадров (НЕ рядом с итоговым клипом).
WORK_DIR = os.path.join(tempfile.gettempdir(), "clip_polisher_work")
OUT_DIR = "out"  # запасная папка вывода, если пользователь не выбрал свою
ANIM_MAP = {"Pop": "pop", "Fade": "fade", "Slide-up": "slide_up", "Караоке": "karaoke"}
PLATFORM_MAP = {"Twitch": "twitch", "YouTube": "youtube", "Kick": "kick", "Без значка": "none"}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Клип-Полировщик — Twitch-клипы")
        self._set_window_icon()
        self.setMinimumSize(880, 580)   # ниже — появляется скролл, а не обрезка
        self._settings = QSettings("ClipPolisher", "Stage1")
        self._theme = self._settings.value("theme", "dark")
        self._out_dir = self._settings.value("out_dir", "") or self._default_out_dir()
        self._input_path: Optional[str] = None
        self._duration = 0.0
        self._threads: list = []          # держим ссылки на живые потоки (иначе краш при GC)
        self._preview_running = False
        self._preview_pending = False
        self._syncing = False   # защита от рекурсии таймлайн↔числовые поля
        self._batch = False               # режим «Несколько клипов» (мульти-редактор)
        self._batch_sources: list[str] = []
        self._clips: list[ClipItem] = []  # клипы мульти-редактора
        self._active_clip = -1            # индекс активного клипа в редакторе
        self._rendering = False           # идёт очередь мульти-рендера (блок редактирования)
        self._provisioning = False        # идёт докачка первого запуска
        self._provisioned_checked = False
        self._update_info = None          # инфо релиза, если он НОВЕЕ (иначе None)
        self._latest_info = None          # инфо последнего релиза (любого)
        self._updates_dialog = None       # открытое меню обновлений
        self._update_notified = False     # одноразовое уведомление показано
        self._updating = False

        self._build()
        self._apply_theme()
        self._restore_geometry()
        self._build_tray()

    # ======================================================================
    # Трей (нужен автопилоту бота: программа должна работать, но не мешать)
    # ======================================================================

    def _build_tray(self) -> None:
        from PySide6.QtGui import QAction, QGuiApplication, QIcon
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon
        from core.resources import res

        self._tray = None
        # На безголовых платформах (офскрин-снимки, тесты) трея нет — попытка его
        # создать роняет процесс при выходе.
        if QGuiApplication.platformName() in ("offscreen", "minimal", "vnc"):
            return
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = QIcon(res("assets/app.ico"))
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Полировщик клипов")
        menu = QMenu(self)
        act_open = QAction("Открыть", self); act_open.triggered.connect(self._show_from_tray)
        act_quit = QAction("Выход", self); act_quit.triggered.connect(self._quit_from_tray)
        menu.addAction(act_open); menu.addSeparator(); menu.addAction(act_quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(
            lambda reason: self._show_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self._tray.show()

    def _should_hide_instead_of_close(self) -> bool:
        if not getattr(self, "_tray", None):
            return False
        if not QSettings("ClipPolisher", "Bot").value("tray_on_close", True, bool):
            return False
        try:
            return self.marks_panel.bot_panel.bot_running
        except AttributeError:
            return False

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._force_quit = True
        self.close()

    def hide_to_tray(self, message: bool = True) -> None:
        """Свернуть в трей (бот при этом продолжает работать)."""
        if not getattr(self, "_tray", None):
            self.showMinimized()
            return
        self.hide()
        if message and not self._settings.value("tray_notified", False, bool):
            self._tray.showMessage("Полировщик клипов работает",
                                   "Бот продолжает ловить метки. Открыть — двойной клик "
                                   "по значку у часов.", self.windowIcon(), 6000)
            self._settings.setValue("tray_notified", True)

    def _set_window_icon(self) -> None:
        """Брендовый бургер в заголовке/таскбаре (иначе — дефолтная иконка Qt/винды)."""
        try:
            from PySide6.QtGui import QIcon
            from core.resources import res
            path = res("assets/app.ico")
            if os.path.isfile(path):
                self.setWindowIcon(QIcon(path))
        except Exception:  # noqa: BLE001
            pass

    # ======================================================================
    # Сборка
    # ======================================================================

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        self._stack = QStackedLayout(central)
        self._stack.setStackingMode(QStackedLayout.StackAll)

        # Слой 1: анимированный фон (под всем).
        self.bg = AnimatedBackground()
        self._stack.addWidget(self.bg)

        # Слой 2: контент НА ВЕСЬ ЭКРАН (без центрирования/гуттеров).
        content = QWidget()
        content.setAttribute(Qt.WA_TranslucentBackground, True)
        inner = QVBoxLayout(content)
        inner.setContentsMargins(16, 16, 16, 16)
        inner.setSpacing(14)
        inner.addWidget(self._topbar())
        inner.addWidget(self._build_mode_stack(), 1)

        # Прозрачный скролл — появляется, когда места не хватает (а не обрезка).
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)
        self._scroll.setWidget(content)
        self._scroll.setStyleSheet(
            "QScrollArea{background:transparent;} "
            "QScrollArea > QWidget > QWidget{background:transparent;}")
        self._stack.addWidget(self._scroll)

        # Слой 3: оверлеи (меню режимов + загрузчик) поверх.
        self.mode_menu = ModeMenuOverlay(central)
        self.mode_menu.mode_selected.connect(self._on_mode_selected)
        self._stack.addWidget(self.mode_menu)
        self.loader = LoaderOverlay(central)
        self._stack.addWidget(self.loader)
        self._stack.setCurrentWidget(self._scroll)

    def _topbar(self) -> QWidget:
        bar = QFrame(); bar.setObjectName("topbar")
        lay = QHBoxLayout(bar); lay.setContentsMargins(14, 10, 14, 10); lay.setSpacing(12)
        from .burger import BurgerButton
        self.burger = BurgerButton()
        self.burger.clicked.connect(self._open_mode_menu)
        lay.addWidget(self.burger)
        self.mode_label = QLabel("Twitch-клипы"); self.mode_label.setObjectName("logo")
        logo = QLabel("КЛИП-ПОЛИРОВЩИК"); logo.setObjectName("logo")
        from core.version import __version__
        # Пилюля версии со статус-точкой (техничный build-индикатор) рядом с логотипом.
        # Она же — вход в меню обновлений (точка «загорается» бирюзовым при обнове).
        self.version_pill = VersionPill(__version__)
        self.version_pill.clicked.connect(self._open_updates)
        lay.addWidget(logo); lay.addWidget(self.mode_label)
        lay.addWidget(self.version_pill); lay.addStretch(1)
        # Один клип / Несколько клипов (мульти-редактор)
        self.batch_single = QPushButton("Один клип"); self.batch_single.setCheckable(True)
        self.batch_single.setChecked(True)
        self.batch_single.setToolTip("Обычный режим: один клип за раз")
        self.batch_many = QPushButton("Несколько"); self.batch_many.setCheckable(True)
        self.batch_many.setToolTip("Мульти-редактор: список клипов, у каждого свои "
                                   "зоны/обрезка/ник, обработка очередью")
        grp = QButtonGroup(self); grp.setExclusive(True)
        grp.addButton(self.batch_single); grp.addButton(self.batch_many)
        self.batch_single.clicked.connect(lambda: self._set_batch_mode(False))
        self.batch_many.clicked.connect(lambda: self._set_batch_mode(True))
        lay.addWidget(self.batch_single); lay.addWidget(self.batch_many)
        self.theme_btn = QPushButton("Тема")
        self.theme_btn.setToolTip("Светлая / тёмная тема")
        self.theme_btn.clicked.connect(self._toggle_theme)
        lay.addWidget(self.theme_btn)
        return bar

    def _build_mode_stack(self) -> QWidget:
        """Стек экранов по режимам бургер-меню (изоляция Этапа 2 от рабочего Этапа 1).

        Страница 0 — Twitch-клипы (мастер|редактор), 1 — «Метки через бота» (склейка),
        2 — «Автопоиск моментов ИИ». Переключается из _on_mode_selected."""
        from PySide6.QtWidgets import QStackedWidget
        from .scan_mode import ScanModePanel
        self._mode_stack = QStackedWidget()
        self._mode_stack.addWidget(self._workspace())          # стр. 0
        self.marks_panel = MarksModePanel(self._theme)         # стр. 1
        self.marks_panel.render_requested.connect(self._render_marks)
        self._mode_stack.addWidget(self.marks_panel)
        self.scan_panel = ScanModePanel(self._theme)           # стр. 2
        self.scan_panel.render_requested.connect(self._render_scan)
        self._mode_stack.addWidget(self.scan_panel)
        return self._mode_stack

    def _workspace(self) -> QWidget:
        """Две колонки ФИКС-пропорции 35/65 (без дивайдера): мастер | редактор.

        Размеры зависят только от размера окна. На узком окне — вертикальная стопка.
        """
        w = QWidget()
        self._ws_layout = QHBoxLayout(w)
        self._ws_layout.setContentsMargins(0, 0, 0, 0)
        self._ws_layout.setSpacing(14)

        # Мастер (настройки, 35%): сверху — бар профиля стримера, ниже — сам мастер.
        master = QWidget()
        mcol = QVBoxLayout(master); mcol.setContentsMargins(0, 0, 0, 0); mcol.setSpacing(10)
        mcol.addWidget(self._profile_bar())
        self.wizard = self._build_wizard()
        self.wizard.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        mcol.addWidget(self.wizard, 1)
        master.setMinimumWidth(300)
        master.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._ws_layout.addWidget(master, 35)

        # Редактор (65%): [лента клипов | сам редактор]. Лента видна в мульти-режиме.
        self._ed_wrap = QWidget()
        ew = QHBoxLayout(self._ed_wrap); ew.setContentsMargins(0, 0, 0, 0); ew.setSpacing(12)
        self.clip_strip = ClipStrip()
        self.clip_strip.add_requested.connect(self._add_clips)
        self.clip_strip.selected.connect(self._select_clip)
        self.clip_strip.removed.connect(self._remove_clip)
        self.clip_strip.setVisible(False)
        ew.addWidget(self.clip_strip)
        self.editor = EditorPanel()
        self.editor.change_frame.connect(self._grab_frames)
        self.editor.composition_changed.connect(self._refresh_result)
        self.editor.trim_changed.connect(self._on_trim_drag)
        self.editor.trim_scrub.connect(lambda _p: self._grab_frames())
        ew.addWidget(self.editor, 1)   # редактор заполняет высоту (холст вписывается)
        self._ed_wrap.setMinimumWidth(340)
        self._ws_layout.addWidget(self._ed_wrap, 65)
        return w

    def _build_wizard(self) -> Wizard:
        titles = [("Вход", "— выбери клип"), ("Раскладка", "— зоны на превью"),
                  ("Обрезка", "— на таймлайне"), ("Субтитры", ""),
                  ("Брендинг", ""), ("Экспорт", "")]
        wiz = Wizard(titles)
        self.wizard = wiz  # нужен уже во время наполнения шагов (footer ссылается)
        wiz.step_changed.connect(self._on_step)
        wiz.finished.connect(self._on_render)
        self._fill_step1(wiz.steps[0])
        self._fill_step2(wiz.steps[1])
        self._fill_step3(wiz.steps[2])
        self._fill_step4(wiz.steps[3])
        self._fill_step5(wiz.steps[4])
        self._fill_step6(wiz.steps[5])
        for st in wiz.steps:
            st.apply_accent_to_children()
        wiz.set_step(0)
        return wiz

    # ---- Наполнение шагов -------------------------------------------------

    def _footer(self, step, last: bool = False) -> QWidget:
        f = QWidget(); fl = QHBoxLayout(f); fl.setContentsMargins(0, 6, 0, 0)
        back = QPushButton("Назад"); back.clicked.connect(self.wizard.back)
        if step.index == 0:
            back.setVisible(False)
        nxt = QPushButton("Отрендерить клип" if last else "Далее")
        nxt.setProperty("class", "primary")
        nxt.setStyleSheet(f"background:{step.color};border-color:{step.color};color:#fff;"
                          f"border-radius:8px;padding:9px 15px;font-weight:700;")
        nxt.clicked.connect(self.wizard.next)
        fl.addWidget(back); fl.addStretch(1); fl.addWidget(nxt)
        return f

    def _lab(self, text: str, tip: str) -> QWidget:
        w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(6)
        lab = QLabel(text); lab.setProperty("class", "lab")
        l.addWidget(lab); l.addWidget(HelpIcon(tip)); l.addStretch(1)
        return w

    def _fill_step1(self, st) -> None:
        b = st.body_layout
        drop = QLabel("Перетащи клип сюда или укажи ссылку/файл ниже")
        drop.setStyleSheet(f"border:1.5px dashed {st.color};border-radius:10px;padding:16px;color:#c2bde0;")
        drop.setAlignment(Qt.AlignCenter)
        drop.setWordWrap(True)
        b.addWidget(drop)
        b.addWidget(self._lab("Файл или ссылка на Twitch-клип", "Вставь ссылку — приложение само скачает клип."))
        self.input_edit = QLineEdit(); self.input_edit.setPlaceholderText("https://clips.twitch.tv/…  или путь к файлу")
        b.addWidget(self.input_edit)
        row = QHBoxLayout()
        browse = QPushButton("Файл…"); browse.clicked.connect(self._browse)
        load = QPushButton("Загрузить"); load.setProperty("class", "primary")
        load.setStyleSheet(f"background:{st.color};border-color:{st.color};color:#fff;border-radius:8px;padding:8px 14px;font-weight:700;")
        load.clicked.connect(self._load_input)
        row.addWidget(browse); row.addWidget(load); row.addStretch(1)
        b.addLayout(row)
        self.input_status = QLabel("—"); self.input_status.setStyleSheet("color:#c2bde0;font-size:11px;")
        self.input_status.setWordWrap(True)
        b.addWidget(self.input_status)
        b.addWidget(self._footer(st))

    def _fill_step2(self, st) -> None:
        b = st.body_layout
        b.addWidget(self._lab("Пресеты раскладки", "Выбери схему — зоны встанут по местам, дальше подгони мышью."))
        # E — для стримеров без камеры: вебка есть не у всех, и выключать её зону
        # вручную каждый раз неудобно.
        # Подписи короткие (пять длинных не влезали в колонку — последний чип
        # обрезался), смысл — в подсказке при наведении.
        self.preset_chips = ChipRow(["A · Сверху", "B · Кружок", "C · Лицо",
                                     "D · Снизу", "E · Без вебки"])
        self.preset_chips.set_tips([
            "Вебка сверху, геймплей снизу",
            "Геймплей на весь экран, вебка кружком в углу",
            "Лицо на весь экран (для разговорных стримов)",
            "Геймплей сверху, вебка снизу",
            "У стримера нет вебки: геймплей на весь экран"])
        self.preset_chips.changed.connect(self._on_preset)
        b.addWidget(self.preset_chips)
        hint = QLabel("Тяни цветные зоны на «Альбомной» версии: перетаскивание — позиция, угол — масштаб.")
        hint.setProperty("class", "hint"); hint.setWordWrap(True)
        b.addWidget(hint)
        b.addWidget(self._footer(st))

    def _fill_step3(self, st) -> None:
        b = st.body_layout
        hint = QLabel("Тяни ручки начала/конца на дорожке под превью, либо задай секунды:")
        hint.setProperty("class", "hint"); hint.setWordWrap(True)
        b.addWidget(hint)
        row = QHBoxLayout()
        self.start_spin = QDoubleSpinBox(); self.start_spin.setMaximum(99999); self.start_spin.setSuffix(" c")
        self.end_spin = QDoubleSpinBox(); self.end_spin.setMaximum(99999); self.end_spin.setSuffix(" c")
        self.start_spin.valueChanged.connect(self._update_timeline)
        self.end_spin.valueChanged.connect(self._update_timeline)
        row.addWidget(QLabel("Начало")); row.addWidget(self.start_spin)
        row.addWidget(QLabel("Конец")); row.addWidget(self.end_spin)
        b.addLayout(row)
        b.addWidget(self._footer(st))

    def _fill_step4(self, st) -> None:
        b = st.body_layout
        b.addWidget(self._lab("Шрифт", "Начертание. В наборе — шрифты с русским."))
        self.font_combo = QComboBox(); self.font_combo.addItems(["PT Sans", "Montserrat", "Rubik"])
        b.addWidget(self.font_combo)
        b.addWidget(self._lab("Анимация", "Пружина, проявление, выезд снизу, караоке."))
        self.anim_chips = ChipRow(["Pop", "Fade", "Slide-up", "Караоке"])
        b.addWidget(self.anim_chips)
        two = QHBoxLayout()
        col1 = QVBoxLayout(); col1.addWidget(self._lab("Обводка", "Контур букв."))
        self.outline_slider = QSlider(Qt.Horizontal); self.outline_slider.setRange(0, 100); self.outline_slider.setValue(50)
        col1.addWidget(self.outline_slider)
        col2 = QVBoxLayout(); col2.addWidget(self._lab("Фон-плашка", "Подложка под текстом."))
        self.box_slider = QSlider(Qt.Horizontal); self.box_slider.setRange(0, 100); self.box_slider.setValue(0)
        col2.addWidget(self.box_slider)
        two.addLayout(col1); two.addLayout(col2)
        b.addLayout(two)
        # Яркая подсветка ключевых слов (MrBeast-стиль). По умолчанию — выкл.
        b.addWidget(self._lab("Подсветка ключевых слов", "Само выделяет «ударные» слова "
                              "ярко (как у MrBeast). «Плашка» — слово в жёлтой заливке; "
                              "«Цвет» — ярким цветом с обводкой; «Выкл» — обычные субтитры."))
        self.hl_chips = ChipRow(["Выкл", "Плашка", "Цвет"])
        b.addWidget(self.hl_chips)
        # Цензура мата: способ. По умолчанию «Не трогать».
        b.addWidget(self._lab("Цензура мата", "Как поступать с матом: не трогать, "
                              "заменить бипом (тон) или заглушить тишиной. В тексте — ***."))
        self.prof_chips = ChipRow(["Не трогать", "Бип", "Заглушить"])
        b.addWidget(self.prof_chips)
        b.addWidget(self._footer(st))

    def _fill_step5(self, st) -> None:
        b = st.body_layout
        b.addWidget(self._lab("Ник", "Имя на клипе."))
        self.nick_edit = QLineEdit(); self.nick_edit.setPlaceholderText("@ник")
        b.addWidget(self.nick_edit)
        b.addWidget(self._lab("Платформа", "Значок площадки."))
        self.platform_chips = ChipRow(["Twitch", "YouTube", "Kick", "Без значка"])
        self.platform_chips.set_current("Без значка")
        b.addWidget(self.platform_chips)
        b.addWidget(self._footer(st))

    def _fill_step6(self, st) -> None:
        b = st.body_layout
        two = QHBoxLayout()
        c1 = QVBoxLayout(); c1.addWidget(self._lab("Качество", "1080×1920 — стандарт."))
        self.res_combo = QComboBox(); self.res_combo.addItems(["1080 × 1920", "720 × 1280"])
        c1.addWidget(self.res_combo)
        c2 = QVBoxLayout(); c2.addWidget(self._lab("FPS", "60 — плавнее, тяжелее."))
        self.fps_combo = QComboBox(); self.fps_combo.addItems(["30", "60"])
        c2.addWidget(self.fps_combo)
        two.addLayout(c1); two.addLayout(c2)
        b.addLayout(two)
        self.cpu_check = QCheckBox("Только CPU (если GPU-кодек недоступен)")
        b.addWidget(self.cpu_check)

        # --- Куда сохранять итоговый клип ----------------------------------
        b.addWidget(self._lab("Куда сохранить", "Папка и имя итогового файла. "
                              "Если имя занято — рядом создастся «имя (2).mp4», "
                              "существующий не перезапишется."))
        drow = QHBoxLayout()
        self.dir_edit = QLineEdit(self._out_dir); self.dir_edit.setReadOnly(True)
        self.dir_edit.setStyleSheet("color:#c2bde0;")
        dbtn = QPushButton("Изменить…"); dbtn.clicked.connect(self._choose_out_dir)
        drow.addWidget(self.dir_edit, 1); drow.addWidget(dbtn)
        b.addLayout(drow)
        nrow = QHBoxLayout()
        self.name_edit = QLineEdit(); self.name_edit.setPlaceholderText("имя файла")
        self.name_edit.setText("clip_vertical")
        nrow.addWidget(self.name_edit, 1); nrow.addWidget(QLabel(".mp4"))
        saveas = QPushButton("Сохранить как…"); saveas.clicked.connect(self._save_as)
        nrow.addWidget(saveas)
        b.addLayout(nrow)

        b.addWidget(self._footer(st, last=True))

    # ---- Выбор места сохранения ------------------------------------------

    @staticmethod
    def _default_out_dir() -> str:
        vids = os.path.join(os.path.expanduser("~"), "Videos")
        return vids if os.path.isdir(vids) else os.path.abspath("out")

    def _choose_out_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка для клипа", self._out_dir)
        if d:
            self._out_dir = d
            self.dir_edit.setText(d)
            self._settings.setValue("out_dir", d)

    def _save_as(self) -> None:
        base = (self.name_edit.text().strip() or "clip_vertical")
        start = os.path.join(self._out_dir, base + ".mp4")
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить клип как…", start,
                                              "Видео MP4 (*.mp4)")
        if path:
            self._out_dir = os.path.dirname(path)
            self.dir_edit.setText(self._out_dir)
            self._settings.setValue("out_dir", self._out_dir)
            name = os.path.splitext(os.path.basename(path))[0]
            self.name_edit.setText(name)

    def _out_filename(self) -> str:
        name = (self.name_edit.text().strip() or "clip_vertical")
        # Убираем расширение, если пользователь его вписал, и запрещённые символы.
        name = os.path.splitext(name)[0]
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "_")
        return (name or "clip_vertical") + ".mp4"

    # ======================================================================
    # Профиль стримера (зоны/ник/платформа/safe-зоны) — %APPDATA%\ClipPolisher
    # ======================================================================

    def _profile_bar(self) -> QWidget:
        bar = QFrame(); bar.setObjectName("panel2")
        lay = QHBoxLayout(bar); lay.setContentsMargins(10, 8, 10, 8); lay.setSpacing(8)
        lab = QLabel("Профиль:"); lab.setProperty("class", "lab")
        lay.addWidget(lab)
        self.profile_combo = QComboBox()
        self.profile_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lay.addWidget(self.profile_combo, 1)
        self.profile_save_btn = QPushButton("Сохранить…")
        self.profile_save_btn.setToolTip("Сохранить текущие зоны, ник и платформу как профиль стримера")
        self.profile_save_btn.clicked.connect(self._save_profile)
        self.profile_del_btn = QPushButton("Удалить")
        self.profile_del_btn.clicked.connect(self._delete_profile)
        lay.addWidget(self.profile_save_btn); lay.addWidget(self.profile_del_btn)
        lay.addWidget(HelpIcon("Сохрани разметку под конкретного стримера — зоны камеры/"
                               "геймплея, ник, платформу и safe-зоны. Потом выбери его из "
                               "списка — всё подтянется. Хранится локально у тебя."))
        self._refresh_profiles(select="— без профиля")
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        return bar

    def _refresh_profiles(self, select: Optional[str] = None) -> None:
        combo = self.profile_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("— без профиля")
        for name in profiles_mod.names():
            combo.addItem(name)
        if select is not None:
            i = combo.findText(select)
            combo.setCurrentIndex(max(0, i))
        combo.blockSignals(False)
        self.profile_del_btn.setEnabled(combo.currentIndex() > 0)

    def _on_profile_selected(self, idx: int) -> None:
        self.profile_del_btn.setEnabled(idx > 0)
        if idx <= 0:
            return
        data = profiles_mod.get(self.profile_combo.currentText())
        if data:
            self._apply_profile(data)

    def _collect_profile(self) -> dict:
        return {
            "nickname": self.nick_edit.text().strip(),
            "platform": PLATFORM_MAP[self.platform_chips.current()],
            "composition": self.editor.get_composition().to_dict(),
            "safezone": self.editor.get_safezone_key(),
        }

    def _apply_profile(self, data: dict) -> None:
        self.nick_edit.setText(data.get("nickname", "") or "")
        rev = {v: k for k, v in PLATFORM_MAP.items()}
        self.platform_chips.set_current(rev.get(data.get("platform", "none"), "Без значка"))
        comp = data.get("composition")
        if comp:
            try:
                self.editor.set_composition(Composition.from_dict(comp))
            except Exception:  # noqa: BLE001 — битый профиль не должен ронять UI
                pass
        self.editor.set_safezone(data.get("safezone"))
        self._refresh_result()

    def _save_profile(self) -> None:
        cur = self.profile_combo.currentText()
        default = "" if cur.startswith("—") else cur
        name, ok = QInputDialog.getText(self, "Сохранить профиль стримера",
                                        "Имя профиля (например, ник стримера):", text=default)
        if not ok or not name.strip():
            return
        try:
            profiles_mod.save(name.strip(), self._collect_profile())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Не удалось сохранить", str(e))
            return
        self._refresh_profiles(select=name.strip())

    def _delete_profile(self) -> None:
        if self.profile_combo.currentIndex() <= 0:
            return
        name = self.profile_combo.currentText()
        if QMessageBox.question(self, "Удалить профиль",
                                f"Удалить профиль «{name}»?") != QMessageBox.Yes:
            return
        profiles_mod.delete(name)
        self._refresh_profiles(select="— без профиля")

    # ======================================================================
    # Тема / фон
    # ======================================================================

    def _apply_theme(self) -> None:
        from . import theme as theme_mod
        theme_mod.set_current(self._theme)      # виджеты, которые рисуют себя сами
        self.setStyleSheet(build_qss(self._theme))
        pal = PALETTE[self._theme]
        # Тумблеры и чипы рисуются вручную — им нужно сказать перекраситься.
        from .widgets import Chip, ToggleSwitch
        for cls in (Chip, ToggleSwitch):
            for w in self.findChildren(cls):
                w.set_theme(self._theme)
        dot = pal["dot"]
        # rgba строку → кортеж
        import re
        m = re.findall(r"[\d.]+", dot)
        rgba = (int(float(m[0])), int(float(m[1])), int(float(m[2])), int(float(m[3]) * 255))
        self.bg.set_palette(pal["bg"], rgba)
        # Пилюля версии — цвета под тему (панель/линия/текст).
        if hasattr(self, "version_pill"):
            self.version_pill.set_palette(pal["text"], pal["muted"], pal["line"], pal["panel2"])
        # Лента клипов мульти-редактора — цвета под тему.
        if hasattr(self, "clip_strip"):
            self.clip_strip.set_palette(pal)
        # Редактор — подписи/легенда под тему (холст-превью остаётся тёмным).
        if hasattr(self, "editor"):
            self.editor.set_theme(self._theme)
        # Панель Этапа 2 (метки) — тема таймлайна/перерисовка.
        if hasattr(self, "marks_panel"):
            self.marks_panel.apply_theme(self._theme)
        # Панель Этапа 3 (автопоиск) — то же самое.
        if hasattr(self, "scan_panel"):
            self.scan_panel.apply_theme(self._theme)
        # Если открыто окно обновлений — перекрасить и его под новую тему.
        dlg = getattr(self, "_updates_dialog", None)
        if dlg is not None:
            dlg.apply_theme(self._theme)

    def _toggle_theme(self) -> None:
        self._theme = "light" if self._theme == "dark" else "dark"
        self._settings.setValue("theme", self._theme)
        self._apply_theme()

    def _on_step(self, idx: int) -> None:
        # Перекрас фоновой сферы в цвет активного окна.
        self.bg.set_target_color(STEP_COLORS[idx % len(STEP_COLORS)])

    def _open_mode_menu(self) -> None:
        central = self.centralWidget()
        tl = self.burger.mapTo(central, QPoint(0, 0))
        anchor = QRect(tl, self.burger.size())
        self.mode_menu.setGeometry(central.rect())
        self.mode_menu.open(anchor)

    def _on_mode_selected(self, idx: int) -> None:
        names = ["Twitch-клипы", "Метки через бота", "Автопоиск ИИ"]
        self.mode_label.setText(names[idx])
        # Заголовок окна тоже под режим — раньше там всегда висел «Этап 1».
        self.setWindowTitle(f"Клип-Полировщик — {names[idx]}")
        self._mode_stack.setCurrentIndex(idx)
        # Режим «Один клип / Несколько» относится только к Этапу 1.
        mode1 = (idx == 0)
        self.batch_single.setVisible(mode1)
        self.batch_many.setVisible(mode1)

    def _render_marks(self) -> None:
        """Этап 2: нарезать клипы из моментов. Раздельно — очередь (каждый в свой файл),
        либо один клип-склейка. Раскладка/звук — общие (из встроенного редактора)."""
        if not self._ready_to_render():
            return
        err = self.marks_panel.validate()
        if err:
            QMessageBox.warning(self, "Нельзя нарезать", err)
            return
        try:
            pcfgs = self.marks_panel.build_pipeline_configs(self._out_dir)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, "Ошибка конфигурации", str(ex))
            return
        if not pcfgs:
            QMessageBox.warning(self, "Нечего рендерить", "Не набралось ни одного клипа.")
            return
        self.loader.start()
        if len(pcfgs) == 1:
            t = self._track(W.RenderThread(pcfgs[0]))
            t.progress.connect(self.loader.set_progress)
            t.finished_ok.connect(self._render_done)
            t.failed.connect(self._render_fail)
        else:
            t = self._track(W.BatchRenderThread(pcfgs))
            t.progress.connect(self.loader.set_progress)
            t.finished_ok.connect(self._marks_batch_done)
            t.failed.connect(self._render_fail)
        t.start()

    def _render_scan(self) -> None:
        """Этап 3: нарезать клипы из моментов автопоиска.

        Куски записи к этому моменту уже скачаны самой панелью (или источник —
        локальный файл), поэтому здесь остаётся обычная очередь рендера."""
        if not self._ready_to_render():
            return
        err = self.scan_panel.validate()
        if err:
            QMessageBox.warning(self, "Нельзя нарезать", err)
            return
        try:
            pcfgs = self.scan_panel.build_pipeline_configs(self._out_dir)
        except Exception as ex:  # noqa: BLE001
            QMessageBox.critical(self, "Ошибка конфигурации", str(ex))
            return
        if not pcfgs:
            QMessageBox.warning(self, "Нечего рендерить", "Не набралось ни одного клипа.")
            return
        self.loader.start()
        if len(pcfgs) == 1:
            t = self._track(W.RenderThread(pcfgs[0]))
            t.progress.connect(self.loader.set_progress)
            t.finished_ok.connect(self._render_done)
            t.failed.connect(self._render_fail)
        else:
            t = self._track(W.BatchRenderThread(pcfgs))
            t.progress.connect(self.loader.set_progress)
            t.finished_ok.connect(self._marks_batch_done)
            t.failed.connect(self._render_fail)
        t.start()

    def _marks_batch_done(self, paths: list) -> None:
        """Готова очередь раздельных клипов Этапа 2 — сообщить и предложить открыть папку."""
        from PySide6.QtCore import QTimer
        self.loader.finish()
        QTimer.singleShot(280, self.loader.stop)
        n = len(paths)
        folder = os.path.dirname(paths[0]) if paths else self._out_dir
        QTimer.singleShot(300, lambda: self._show_batch_done(n, folder))

    def _show_batch_done(self, n: int, folder: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Готово")
        box.setText(f"Нарезано клипов: {n}\nПапка: {folder}")
        open_btn = box.addButton("Открыть папку", QMessageBox.ActionRole)
        box.addButton("ОК", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            import subprocess
            subprocess.Popen(["explorer", os.path.normpath(folder)])

    # ======================================================================
    # Логика (через core + потоки)
    # ======================================================================

    def _browse(self) -> None:
        if self._batch:
            self._add_clips()
            return
        path, _ = QFileDialog.getOpenFileName(self, "Клип", "", "Видео (*.mp4 *.mkv *.mov *.webm)")
        if path:
            self.input_edit.setText(path)

    def _set_batch_mode(self, on: bool) -> None:
        self._batch = on
        self.clip_strip.setVisible(on)
        if on:
            self.input_edit.setPlaceholderText(
                "Мульти-редактор: добавь клипы в ленте слева от редактора")
            self.input_status.setText(
                f"Мульти-редактор: клипов в очереди — {len(self._clips)}. "
                "Выбирай клип слева, правь его зоны/обрезку/ник, потом «Отрендерить».")
        else:
            self.input_status.setText("—" if not self._input_path
                                      else self.input_status.text())

    # ---- Мульти-редактор: добавление / выбор / удаление клипов ------------

    def _add_clips(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Добавить клипы (можно несколько)", "",
            "Видео (*.mp4 *.mkv *.mov *.webm)")
        if not paths:
            return
        from PySide6.QtGui import QGuiApplication as _QGA
        from PySide6.QtCore import Qt as _Qt
        _QGA.setOverrideCursor(_Qt.WaitCursor)
        os.makedirs(WORK_DIR, exist_ok=True)
        existing = {c.source for c in self._clips}
        try:
            for p in paths:
                if p in existing:
                    continue
                item = ClipItem(p)
                # длительность
                try:
                    info = ff.probe_video(p)
                    item.duration = info.duration
                    item.end = info.duration
                except Exception:  # noqa: BLE001
                    pass
                # миниатюра (кадр ~1с)
                idx = len(self._clips)
                thumb = os.path.join(WORK_DIR, f"_clip_thumb_{idx}.png")
                try:
                    tt = min(max(0.5, item.duration * 0.3), max(0.5, item.duration - 0.1))
                    ff.run_ffmpeg(["-y", "-ss", f"{tt:.2f}", "-i", p, "-frames:v", "1",
                                   "-vf", "scale=200:-2", thumb])
                    item.thumb = thumb
                except Exception:  # noqa: BLE001
                    pass
                # композиция-шаблон (копия текущей из редактора) + ник-шаблон
                item.comp = Composition.from_dict(self.editor.get_composition().to_dict())
                item.nick = self.nick_edit.text().strip()
                self._clips.append(item)
        finally:
            _QGA.restoreOverrideCursor()
        self.clip_strip.rebuild(self._clips)
        # если раньше активного не было — выбрать первый добавленный
        if self._active_clip < 0 and self._clips:
            self._select_clip(0)
        self._set_batch_mode(True)   # обновить статус-строку

    def _select_clip(self, index: int) -> None:
        if self._rendering or not (0 <= index < len(self._clips)):
            return
        self._store_active_edits()   # зафиксировать правки прошлого клипа
        self._active_clip = index
        clip = self._clips[index]
        self.clip_strip.set_active(index)
        # загрузить клип в редактор
        self._input_path = clip.source
        self._duration = clip.duration
        self._syncing = True
        self.start_spin.setValue(clip.start)
        self.end_spin.setValue(clip.end if clip.end > 0 else clip.duration)
        self._syncing = False
        self.editor.set_duration(clip.duration)
        self.editor.set_trim(clip.start, clip.end or clip.duration)
        if clip.comp is not None:
            self.editor.set_composition(clip.comp)
        if clip.nick is not None:
            self.nick_edit.setText(clip.nick)
        if clip.thumb and os.path.isfile(clip.thumb):
            self.editor.set_source_frame(clip.thumb)
        self._make_filmstrip(clip.source, clip.duration)
        self._refresh_result()

    def _store_active_edits(self) -> None:
        """Сохранить обрезку/ник активного клипа (композиция мутируется по ссылке)."""
        if self._batch and 0 <= self._active_clip < len(self._clips):
            clip = self._clips[self._active_clip]
            clip.start = self.start_spin.value()
            clip.end = self.end_spin.value()
            clip.nick = self.nick_edit.text().strip()

    def _remove_clip(self, index: int) -> None:
        if not (0 <= index < len(self._clips)):
            return
        del self._clips[index]
        if self._active_clip == index:
            self._active_clip = -1
        elif self._active_clip > index:
            self._active_clip -= 1
        self.clip_strip.rebuild(self._clips)
        if self._clips:
            self._select_clip(min(index, len(self._clips) - 1))
        else:
            self._active_clip = -1
        self._set_batch_mode(True)

    def _track(self, thread):
        """Держать ссылку на поток, пока он жив (иначе GC рушит QThread на ходу)."""
        self._threads.append(thread)
        thread.finished.connect(lambda: self._threads.remove(thread)
                                if thread in self._threads else None)
        return thread

    def _load_input(self) -> None:
        src = self.input_edit.text().strip()
        if not src:
            return
        self.input_status.setText("Загрузка…")
        t = self._track(W.IngestThread(src))
        t.finished_ok.connect(self._input_ready)
        t.failed.connect(self._err)
        t.start()

    def _input_ready(self, path: str) -> None:
        self._input_path = path
        try:
            info = ff.probe_video(path)
            self._duration = info.duration
            self._syncing = True
            self.start_spin.setValue(0.0)
            self.end_spin.setValue(info.duration)
            self._syncing = False
            self.editor.set_duration(info.duration)
            self.editor.set_trim(0.0, info.duration)
            self.input_status.setText(
                f"{os.path.basename(path)} — {info.width}×{info.height}, {info.duration:.1f} c")
            self._make_filmstrip(path, info.duration)
        except Exception as e:  # noqa: BLE001
            self.input_status.setText(f"ffprobe: {e}")
        # Подставим имя итогового файла из имени клипа (если ещё дефолтное).
        if hasattr(self, "name_edit") and self.name_edit.text().strip() in ("", "clip_vertical"):
            stem = os.path.splitext(os.path.basename(path))[0]
            if stem:
                self.name_edit.setText(stem)
        self._grab_frames()

    def _grab_frames(self) -> None:
        if not self._input_path:
            return
        os.makedirs(WORK_DIR, exist_ok=True)
        t = min(max(self.start_spin.value() + 1.0, 0.5), max(self._duration - 0.1, 0.5))
        src = os.path.join(WORK_DIR, "_src_frame.png")
        try:
            ff.run_ffmpeg(["-y", "-ss", f"{t:.2f}", "-i", self._input_path, "-frames:v", "1", src])
            self.editor.set_source_frame(src)
            self._refresh_result()
        except Exception as e:  # noqa: BLE001
            self._err(str(e))

    def _make_filmstrip(self, path: str, duration: float) -> None:
        os.makedirs(WORK_DIR, exist_ok=True)
        out_png = os.path.join(WORK_DIR, "_filmstrip.png")
        t = self._track(W.FilmstripThread(path, out_png, duration=duration))
        t.finished_ok.connect(self.editor.set_filmstrip)
        t.failed.connect(lambda _m: None)   # киноленты нет — таймлайн всё равно работает
        t.start()

    def _current_branding(self) -> BrandingConfig:
        return BrandingConfig(nickname=self.nick_edit.text().strip(),
                              platform=Platform(PLATFORM_MAP[self.platform_chips.current()]))

    def _refresh_result(self) -> None:
        """Пересобрать 9:16-превью результата (композиция) в фоне. С дебаунсом:
        пока один рендер идёт — новые не плодим, а помечаем «нужно ещё раз»."""
        if not self._input_path:
            return
        if self._preview_running:
            self._preview_pending = True
            return
        self._preview_running = True
        os.makedirs(WORK_DIR, exist_ok=True)
        out_png = os.path.join(WORK_DIR, "_result_frame.png")
        t = min(max(self.start_spin.value() + 1.0, 0.5), max(self._duration - 0.1, 0.5))
        cw, ch = self._canvas()
        comp = self.editor.get_composition()
        th = self._track(W.PreviewThread(self._input_path, None, out_png, t,
                                         self._current_branding(), None, cw, ch,
                                         composition=comp))
        th.finished_ok.connect(self.editor.set_result_frame)
        th.finished.connect(self._preview_finished)
        th.start()

    def _preview_finished(self) -> None:
        self._preview_running = False
        if self._preview_pending:
            self._preview_pending = False
            self._refresh_result()

    def _on_preset(self, _txt: str) -> None:
        preset = LayoutPreset(self.preset_chips.current().split(" ")[0])
        self.editor.apply_preset(preset)
        self._refresh_result()

    def _update_timeline(self) -> None:
        s, e = self.start_spin.value(), self.end_spin.value()
        # Числовые поля → дорожка (без эмита, чтобы не зациклить).
        if not self._syncing:
            self._syncing = True
            self.editor.set_trim(s, e)
            self._syncing = False

    def _on_trim_drag(self, start: float, end: float) -> None:
        """Пользователь тянет ручки на дорожке → обновляем числовые поля."""
        if self._syncing:
            return
        self._syncing = True
        self.start_spin.setValue(start)
        self.end_spin.setValue(end)
        self._syncing = False

    @staticmethod
    def _fmt(sec: float) -> str:
        return f"{int(sec)//60:02d}:{int(sec)%60:02d}"

    def _canvas(self) -> tuple[int, int]:
        w, h = self.res_combo.currentText().replace(" ", "").split("×")
        return int(w), int(h)

    def _build_pipeline(self) -> PipelineConfig:
        cw, ch = self._canvas()
        hl_mode = {"Выкл": "none", "Плашка": "box", "Цвет": "color"}.get(
            self.hl_chips.current(), "none")
        style = CaptionStyle(
            outline_width=self.outline_slider.value() / 100.0 * 12.0,
            box=self.box_slider.value() > 5,
            box_opacity=self.box_slider.value() / 100.0,
            highlight_mode=hl_mode,
        )
        prof_mode = PROFANITY_MODES[self.prof_chips.current()]
        comp = self.editor.get_composition()
        return PipelineConfig(
            source=self._input_path or self.input_edit.text().strip(),
            start=self.start_spin.value(),
            end=self.end_spin.value() if self.end_spin.value() > 0 else None,
            composition=comp,                            # свободная компоновка на 9:16
            captions_enabled=getattr(comp.subtitles, "visible", True),  # глаз субтитров в редакторе
            export=ExportConfig(width=cw, height=ch, fps=int(self.fps_combo.currentText()),
                                codec=VideoCodec.X264 if self.cpu_check.isChecked() else VideoCodec.NVENC,
                                out_dir=self._out_dir, filename=self._out_filename()),
            caption_style=style,
            caption_animation=CaptionAnimation(ANIM_MAP[self.anim_chips.current()]),
            highlight_keywords=(hl_mode != "none"),
            profanity_enabled=(prof_mode != "off"),
            profanity_mode=("beep" if prof_mode == "beep" else "silence"),
            branding=self._current_branding(),
        )

    def _ready_to_render(self) -> bool:
        if self._provisioning:
            QMessageBox.information(self, "Идёт подготовка",
                                    "Ещё качаются компоненты первого запуска. Дождись окончания.")
            return False
        try:
            from core import provision
            if not provision.model_ready():
                QMessageBox.warning(self, "Нет модели распознавания",
                                    "Модель ещё не скачана (качается при первом запуске). "
                                    "Проверь интернет и подожди.")
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

    def _on_render(self) -> None:
        if not self._ready_to_render():
            return
        if self._batch:
            self._render_batch()
            return
        src = self._input_path or self.input_edit.text().strip()
        if not src:
            self.wizard.set_step(0)
            QMessageBox.warning(self, "Нет входа", "Сначала загрузите клип на шаге 1.")
            return
        self.loader.start()
        t = self._track(W.RenderThread(self._build_pipeline()))
        t.progress.connect(self.loader.set_progress)
        t.finished_ok.connect(self._render_done)
        t.failed.connect(self._render_fail)
        t.start()

    def _render_batch(self) -> None:
        """Мульти-редактор: очередь клипов. Общий шаблон (стиль субтитров/платформа/
        экспорт) + ПРАВКИ под клип (зоны, обрезка, ник). Каждый — в свой файл."""
        self._store_active_edits()   # зафиксировать правки открытого клипа
        if not self._clips:
            QMessageBox.warning(self, "Нет клипов",
                                "Добавь клипы в ленту слева («+ Добавить клипы»).")
            return
        template = self._build_pipeline()      # шаблон общих настроек
        pcfgs = []
        for clip in self._clips:
            pc = self._build_pipeline()        # копия шаблона
            pc.source = clip.source
            pc.start = clip.start
            pc.end = clip.end if clip.end and clip.end > clip.start else None
            pc.composition = clip.comp or template.composition   # зоны этого клипа
            # ник — индивидуальный, платформа/стиль — из шаблона
            pc.branding = BrandingConfig(nickname=(clip.nick or "").strip(),
                                         platform=template.branding.platform)
            stem = os.path.splitext(os.path.basename(clip.source))[0] or "clip"
            for ch in '<>:"/\\|?*':
                stem = stem.replace(ch, "_")
            pc.export.out_dir = self._out_dir
            pc.export.filename = stem + "_vertical.mp4"
            clip.status = "pending"; clip.frac = 0.0
            pcfgs.append(pc)
        self.clip_strip.refresh_all()
        self._batch_n = len(pcfgs)
        # Мульти-режим: НЕ перекрываем экран загрузчиком — прогресс живьём на карточках.
        self._set_rendering_ui(True)
        t = self._track(W.BatchRenderThread(pcfgs))
        t.progress.connect(self._batch_progress)
        t.finished_ok.connect(self._batch_done)
        t.failed.connect(self._render_fail)
        t.start()

    def _set_rendering_ui(self, on: bool) -> None:
        """Во время очереди блокируем редактирование, но экран не прячем (виден прогресс)."""
        self._rendering = on
        for w in (self.wizard, self.editor):
            w.setEnabled(not on)
        self.clip_strip.set_add_enabled(not on)
        self.batch_single.setEnabled(not on)
        self.batch_many.setEnabled(not on)
        if hasattr(self, "profile_combo"):
            self.profile_combo.setEnabled(not on)
            self.profile_save_btn.setEnabled(not on)
            self.profile_del_btn.setEnabled(not on)
        if not on:
            self.clip_strip.set_header("Клипы")

    def _batch_progress(self, frac: float, stage: str) -> None:
        """Общий прогресс в шапку ленты + пометка статусов/процентов карточек клипов."""
        n = getattr(self, "_batch_n", 0) or len(self._clips)
        if n <= 0:
            return
        cur = min(int(frac * n), n - 1)      # индекс текущего клипа
        for i, clip in enumerate(self._clips):
            if i < cur:
                clip.status = "done"; clip.frac = 1.0
            elif i == cur:
                clip.status = "processing"; clip.frac = max(0.0, frac * n - cur)
            self.clip_strip.update_card(i)
        self.clip_strip.set_header(f"Обработка {cur + 1}/{n} · {int(frac * 100)}%", busy=True)

    def _batch_done(self, outs: list) -> None:
        from PySide6.QtCore import QTimer
        for i, clip in enumerate(self._clips):
            clip.status = "done"; clip.frac = 1.0
            if i < len(outs):
                clip.out_path = outs[i]
        self.clip_strip.refresh_all()
        self._set_rendering_ui(False)
        self.clip_strip.set_header(f"Готово: {len(outs)}")
        QTimer.singleShot(300, lambda: self._show_batch_done(outs))

    def _show_batch_done(self, outs: list) -> None:
        lst = "\n".join(os.path.basename(p) for p in outs)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Пачка готова")
        box.setText(f"Готово клипов: {len(outs)}\n\n{lst}")
        open_btn = box.addButton("Открыть папку", QMessageBox.ActionRole)
        box.addButton("ОК", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn and outs:
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(outs[0])])

    # ======================================================================
    # Первый запуск: докачка модели/GPU
    # ======================================================================

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if not self._provisioned_checked:
            self._provisioned_checked = True
            from PySide6.QtCore import QTimer
            QTimer.singleShot(250, self._check_first_run)
            QTimer.singleShot(1500, self._check_update)   # тихая проверка обновления

    def _check_first_run(self) -> None:
        if os.environ.get("CLIP_SKIP_PROVISION"):
            return
        try:
            from core import provision
            need = provision.needs_provision()
        except Exception:  # noqa: BLE001
            need = False
        if not need:
            return
        self._provisioning = True
        self.loader.start()
        self.loader.title.setText("Первый запуск — подготовка")
        self.loader.msg.setText("Качаю модель распознавания и GPU-библиотеки (один раз)…")
        t = self._track(W.ProvisionThread(want_gpu=True))
        t.progress.connect(self._provision_progress)
        t.finished_ok.connect(self._provision_done)
        t.failed.connect(self._provision_fail)
        t.start()

    def _provision_progress(self, frac: float, stage: str) -> None:
        self.loader.set_progress(frac, stage)
        self.loader.msg.setText(stage)

    def _provision_done(self) -> None:
        from PySide6.QtCore import QTimer
        self._provisioning = False
        self.loader.title.setText("Собираю клип…")
        self.loader.finish()
        QTimer.singleShot(400, self.loader.stop)

    def _provision_fail(self, msg: str) -> None:
        self._provisioning = False
        self.loader.stop()
        QMessageBox.warning(
            self, "Не удалось докачать компоненты",
            "Не получилось скачать модель/библиотеки:\n\n" + str(msg)[:800] +
            "\n\nПроверь интернет и перезапусти приложение. Без модели "
            "распознавание речи не заработает.")

    # ======================================================================
    # Встроенное обновление
    # ======================================================================

    def _style_updates_btn(self, has_update: bool) -> None:
        # Совместимость: перенаправляем на статус пилюли версии.
        self.version_pill.set_status("update" if has_update else "uptodate")

    def _check_update(self) -> None:
        """Тихая фоновая проверка при старте."""
        if os.environ.get("CLIP_SKIP_UPDATE"):
            return
        t = self._track(W.UpdateCheckThread())
        t.found.connect(self._on_update_found)
        t.uptodate.connect(self._on_update_uptodate)
        t.failed.connect(self._on_update_failed)
        t.start()

    def _on_update_found(self, info: dict) -> None:
        self._latest_info = info
        self._update_info = info
        self._style_updates_btn(has_update=True)
        self.version_pill.setToolTip(f"Доступна новая версия v{info.get('version', '?')} — "
                                     "нажми, чтобы открыть меню обновлений.")
        if getattr(self, "_updates_dialog", None):
            self._updates_dialog.set_state("update", info)
        # Одноразовое ненавязчивое предложение зайти в меню обновлений.
        if not getattr(self, "_update_notified", False):
            self._update_notified = True
            ver = info.get("version", "?")
            ans = QMessageBox.information(
                self, "Доступно обновление",
                f"Вышла новая версия v{ver}.\nОткрыть меню обновлений — там «Что нового» "
                "и кнопка обновления?",
                QMessageBox.Open | QMessageBox.Cancel)
            if ans == QMessageBox.Open:
                self._open_updates()

    def _on_update_uptodate(self, info: dict) -> None:
        self._latest_info = info
        self._update_info = None
        self._style_updates_btn(has_update=False)
        if getattr(self, "_updates_dialog", None):
            self._updates_dialog.set_state("uptodate")

    def _on_update_failed(self, err: str) -> None:
        self.version_pill.set_status("error")
        if getattr(self, "_updates_dialog", None):
            self._updates_dialog.set_state("error", error=err)

    def _open_updates(self) -> None:
        from .updates_dialog import UpdatesDialog
        from core.version import __version__
        dlg = getattr(self, "_updates_dialog", None)
        if dlg is None:
            dlg = UpdatesDialog(__version__, theme=self._theme, accent="#37c9c2",
                                parent=self)
            dlg.recheck.connect(self._dialog_recheck)
            dlg.do_update.connect(self._do_update)
            dlg.finished.connect(lambda _r: setattr(self, "_updates_dialog", None))
            self._updates_dialog = dlg
        # начальное состояние из того, что уже знаем
        if self._update_info:
            dlg.set_state("update", self._update_info)
        elif self._latest_info:
            dlg.set_state("uptodate")
        else:
            dlg.set_state("checking")
            self._dialog_recheck()
        dlg.show(); dlg.raise_(); dlg.activateWindow()

    def _dialog_recheck(self) -> None:
        if getattr(self, "_updates_dialog", None):
            self._updates_dialog.set_state("checking")
        self._check_update()

    def _do_update(self) -> None:
        if not self._update_info or self._updating:
            return
        self._updating = True
        if getattr(self, "_updates_dialog", None):
            self._updates_dialog.accept()      # закрываем меню — прогресс в загрузчике
        ver = self._update_info.get("version", "?")
        dst = os.path.join(tempfile.gettempdir(), f"ClipPolisher-Setup-{ver}.exe")
        self.loader.start()
        self.loader.title.setText("Обновление")
        self.loader.msg.setText("Скачиваю новую версию…")
        t = self._track(W.UpdateDownloadThread(self._update_info["url"], dst))
        t.progress.connect(self._provision_progress)   # тот же плавный бар
        t.finished_ok.connect(self._update_ready)
        t.failed.connect(self._update_fail)
        t.start()

    def _update_ready(self, path: str) -> None:
        from PySide6.QtWidgets import QApplication
        self.loader.stop()
        try:
            from core import updater
            updater.launch_installer(path)
        except Exception as e:  # noqa: BLE001
            self._updating = False
            QMessageBox.warning(self, "Не удалось запустить установщик", str(e)[:600])
            return
        # Закрываемся, чтобы установщик заменил файлы (в .iss CloseApplications=yes).
        QApplication.quit()

    def _update_fail(self, msg: str) -> None:
        self._updating = False
        self.loader.stop()
        QMessageBox.warning(self, "Обновление не удалось",
                            "Не получилось скачать обновление:\n\n" + str(msg)[:600] +
                            "\n\nПопробуй позже или скачай вручную со страницы релизов.")

    def _render_done(self, path: str) -> None:
        from PySide6.QtCore import QTimer
        self.loader.finish()   # доводим бар до 100% на миг
        QTimer.singleShot(280, self.loader.stop)
        QTimer.singleShot(300, lambda: self._show_done(path))

    def _show_done(self, path: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Готово")
        box.setText(f"Клип сохранён:\n{path}")
        open_btn = box.addButton("Открыть папку", QMessageBox.ActionRole)
        box.addButton("ОК", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is open_btn:
            import subprocess
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def _render_fail(self, msg: str) -> None:
        self.loader.stop()
        if self._batch:
            for clip in self._clips:
                if clip.status == "processing":
                    clip.status = "error"
            self.clip_strip.refresh_all()
            self._set_rendering_ui(False)
        self._err(msg)

    def _err(self, msg: str) -> None:
        QMessageBox.critical(self, "Ошибка", str(msg)[:2000])

    # ======================================================================
    # Геометрия / фон
    # ======================================================================

    NARROW_BP = 1120   # ниже этой ширины колонки складываются вертикально (иначе тесно)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        w = self.width()
        # Адаптив: на узком окне — вертикальная стопка колонок (как эталон <900px).
        if hasattr(self, "_ws_layout"):
            narrow = w < self.NARROW_BP
            want = QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight
            if self._ws_layout.direction() != want:
                self._ws_layout.setDirection(want)
                self._ws_layout.setStretch(0, 0 if narrow else 35)
                self._ws_layout.setStretch(1, 1 if narrow else 65)
        # Фон на весь экран: сфера гуляет по четвертям (за панелями/в зазорах), не мешая.
        if hasattr(self, "bg"):
            self.bg.set_content_rect(QRect(w // 2, 0, 0, self.height()))
        central = self.centralWidget()
        if hasattr(self, "loader") and self.loader.isVisible():
            self.loader.setGeometry(central.rect())
        if hasattr(self, "mode_menu") and self.mode_menu.isVisible():
            self.mode_menu.setGeometry(central.rect())

    def _restore_geometry(self) -> None:
        g = self._settings.value("geometry")
        if g is not None:
            self.restoreGeometry(g)
        else:
            self.resize(1320, 860)

    def closeEvent(self, e) -> None:
        # Если бот работает, а «закрытие сворачивает в трей» включено — не выходим,
        # иначе автопилот молча умрёт вместе с окном.
        if not getattr(self, "_force_quit", False) and self._should_hide_instead_of_close():
            e.ignore()
            self.hide_to_tray()
            return

        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("theme", self._theme)
        # Бот и вход через Twitch крутят свои потоки — гасим их первыми.
        try:
            self.marks_panel.bot_panel.shutdown()
        except Exception:       # noqa: BLE001 — на выходе не мешаем закрытию окна
            pass
        # Разбор стрима и загрузка кусков — тоже потоки, и очень долгие.
        try:
            self.scan_panel.shutdown()
        except Exception:       # noqa: BLE001
            pass
        # Дождаться живых потоков, чтобы Qt не рушил их на выходе.
        for th in list(self._threads):
            try:
                th.wait(3000)
            except Exception:
                pass
        super().closeEvent(e)
