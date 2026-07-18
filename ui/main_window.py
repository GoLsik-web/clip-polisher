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
)

from core.config import (LayoutConfig, LayoutPreset, ExportConfig, VideoCodec,
                         Corner, Segment)
from core.captions import CaptionStyle, CaptionAnimation
from core.branding import BrandingConfig, Platform
from core.pipeline import PipelineConfig
from core import ffmpeg_utils as ff
from core.layout import load_preset

from .theme import build_qss, PALETTE, STEP_COLORS
from .background import AnimatedBackground
from .wizard import Wizard
from .widgets import HelpIcon, ChipRow, ToggleSwitch
from .preview_panel import EditorPanel
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
        self.setWindowTitle("Клип-Полировщик — Этап 1")
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
        self._batch = False               # режим «Пачкой»
        self._batch_sources: list[str] = []
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
        inner.addWidget(self._workspace(), 1)

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
        tag = QLabel(f"v{__version__}"); tag.setObjectName("tag")
        lay.addWidget(logo); lay.addWidget(self.mode_label); lay.addWidget(tag); lay.addStretch(1)
        # Кнопка «Обновления» — ВСЕГДА видна. Тихая, когда версия актуальна;
        # «загорается», когда доступно обновление. Открывает меню обновлений.
        self.updates_btn = QPushButton("Обновления")
        self.updates_btn.setToolTip("Проверить и установить обновления")
        self.updates_btn.clicked.connect(self._open_updates)
        self._style_updates_btn(has_update=False)
        lay.addWidget(self.updates_btn)
        # По одному / Пачкой
        self.batch_single = QPushButton("По одному"); self.batch_single.setCheckable(True)
        self.batch_single.setChecked(True)
        self.batch_many = QPushButton("Пачкой"); self.batch_many.setCheckable(True)
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

    def _workspace(self) -> QWidget:
        """Две колонки ФИКС-пропорции 35/65 (без дивайдера): мастер | редактор.

        Размеры зависят только от размера окна. На узком окне — вертикальная стопка.
        """
        w = QWidget()
        self._ws_layout = QHBoxLayout(w)
        self._ws_layout.setContentsMargins(0, 0, 0, 0)
        self._ws_layout.setSpacing(14)

        # Мастер (настройки, 35%).
        self.wizard = self._build_wizard()
        self.wizard.setMinimumWidth(300)
        self.wizard.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._ws_layout.addWidget(self.wizard, 35)

        # Редактор (65%) — прижат к верху, снизу фон.
        self._ed_wrap = QWidget()
        ew = QVBoxLayout(self._ed_wrap); ew.setContentsMargins(0, 0, 0, 0); ew.setSpacing(0)
        self.editor = EditorPanel()
        self.editor.change_frame.connect(self._grab_frames)
        self.editor.composition_changed.connect(self._refresh_result)
        self.editor.trim_changed.connect(self._on_trim_drag)
        self.editor.trim_scrub.connect(lambda _p: self._grab_frames())
        ew.addWidget(self.editor)   # редактор заполняет высоту (холст вписывается)
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
        self.preset_chips = ChipRow(["A · Вебка сверху", "B · Кружок", "C · Лицо", "D · Вебка снизу"])
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
    # Тема / фон
    # ======================================================================

    def _apply_theme(self) -> None:
        self.setStyleSheet(build_qss(self._theme))
        pal = PALETTE[self._theme]
        dot = pal["dot"]
        # rgba строку → кортеж
        import re
        m = re.findall(r"[\d.]+", dot)
        rgba = (int(float(m[0])), int(float(m[1])), int(float(m[2])), int(float(m[3]) * 255))
        self.bg.set_palette(pal["bg"], rgba)

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
        if idx != 0:
            QMessageBox.information(self, "Скоро",
                                    f"Режим {idx+1} — каркас готов, внутрянка на Этапе {idx+1}.")

    # ======================================================================
    # Логика (через core + потоки)
    # ======================================================================

    def _browse(self) -> None:
        if self._batch:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Клипы для пачки (можно несколько)", "",
                "Видео (*.mp4 *.mkv *.mov *.webm)")
            if paths:
                for p in paths:
                    if p not in self._batch_sources:
                        self._batch_sources.append(p)
                self._show_batch()
            return
        path, _ = QFileDialog.getOpenFileName(self, "Клип", "", "Видео (*.mp4 *.mkv *.mov *.webm)")
        if path:
            self.input_edit.setText(path)

    def _set_batch_mode(self, on: bool) -> None:
        self._batch = on
        if on:
            self.input_edit.setPlaceholderText("В режиме пачки жми «Файл…» и выбирай несколько клипов")
            self._show_batch()
        else:
            self.input_status.setText("—" if not self._input_path
                                      else self.input_status.text())

    def _show_batch(self) -> None:
        n = len(self._batch_sources)
        if n == 0:
            self.input_status.setText("Пачка пуста — добавь клипы кнопкой «Файл…».")
            return
        names = ", ".join(os.path.basename(p) for p in self._batch_sources[:6])
        more = "" if n <= 6 else f" и ещё {n - 6}"
        self.input_status.setText(f"В пачке {n} клип(ов): {names}{more}. "
                                  "Настрой раскладку/субтитры и жми «Отрендерить».")

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
        style = CaptionStyle(
            outline_width=self.outline_slider.value() / 100.0 * 12.0,
            box=self.box_slider.value() > 5,
            box_opacity=self.box_slider.value() / 100.0,
        )
        prof_mode = PROFANITY_MODES[self.prof_chips.current()]
        return PipelineConfig(
            source=self._input_path or self.input_edit.text().strip(),
            start=self.start_spin.value(),
            end=self.end_spin.value() if self.end_spin.value() > 0 else None,
            composition=self.editor.get_composition(),   # свободная компоновка на 9:16
            export=ExportConfig(width=cw, height=ch, fps=int(self.fps_combo.currentText()),
                                codec=VideoCodec.X264 if self.cpu_check.isChecked() else VideoCodec.NVENC,
                                out_dir=self._out_dir, filename=self._out_filename()),
            caption_style=style,
            caption_animation=CaptionAnimation(ANIM_MAP[self.anim_chips.current()]),
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
        """Рендер пачкой: каждый клип с ТЕКУЩИМИ настройками, свой файл по имени клипа."""
        srcs = list(self._batch_sources)
        if not srcs:  # запасной вариант — одиночный вход, если пачка пуста
            one = self._input_path or self.input_edit.text().strip()
            if one:
                srcs = [one]
        if not srcs:
            self.wizard.set_step(0)
            QMessageBox.warning(self, "Пачка пуста",
                                "Добавь клипы кнопкой «Файл…» на шаге 1 (в режиме «Пачкой»).")
            return
        base = self._build_pipeline()          # общие настройки (композиция/субтитры/брендинг)
        pcfgs = []
        for s in srcs:
            pc = self._build_pipeline()
            pc.source = s
            pc.start, pc.end = 0.0, None        # в пачке берём клип целиком
            pc.composition = base.composition
            stem = os.path.splitext(os.path.basename(s))[0] or "clip"
            for ch in '<>:"/\\|?*':
                stem = stem.replace(ch, "_")
            pc.export.out_dir = self._out_dir
            pc.export.filename = stem + "_vertical.mp4"
            pcfgs.append(pc)
        self.loader.start()
        t = self._track(W.BatchRenderThread(pcfgs))
        t.progress.connect(self.loader.set_progress)
        t.finished_ok.connect(self._batch_done)
        t.failed.connect(self._render_fail)
        t.start()

    def _batch_done(self, outs: list) -> None:
        from PySide6.QtCore import QTimer
        self.loader.finish()
        QTimer.singleShot(280, self.loader.stop)
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
        if has_update:
            self.updates_btn.setText("Обновление есть")
            self.updates_btn.setStyleSheet(
                "background:#37c9c2;border:none;border-radius:8px;color:#06231f;"
                "padding:7px 13px;font-weight:800;")
        else:
            self.updates_btn.setText("Обновления")
            self.updates_btn.setStyleSheet("")   # обычный вид из QSS — ненавязчиво

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
        if getattr(self, "_updates_dialog", None):
            self._updates_dialog.set_state("error", error=err)

    def _open_updates(self) -> None:
        from .updates_dialog import UpdatesDialog
        from core.version import __version__
        dlg = getattr(self, "_updates_dialog", None)
        if dlg is None:
            dlg = UpdatesDialog(__version__, accent="#37c9c2", parent=self)
            dlg.setStyleSheet(self.styleSheet())   # единая тема с приложением
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
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("theme", self._theme)
        # Дождаться живых потоков, чтобы Qt не рушил их на выходе.
        for th in list(self._threads):
            try:
                th.wait(3000)
            except Exception:
                pass
        super().closeEvent(e)
