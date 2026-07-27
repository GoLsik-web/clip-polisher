"""bot_panel.py — вкладка «Бот»: вход через Twitch и бот меток прямо в приложении.

Смысл: обычному стримеру не нужно ставить Python, править config.json и добывать токен
на стороннем сайте. Здесь он жмёт «Войти через Twitch», подтверждает короткий код на
twitch.tv/activate — и нажимает «Подключить бота». Дальше бот сидит в его чате и пишет
метки, а кнопка «Взять эти метки в нарезку» отдаёт их в режим склейки.

Вся логика — в `core/twitch_auth.py` (вход) и `core/chatbot.py` (бот). Здесь только Qt:
два фоновых потока (вход и сам бот) и перевод их событий в сигналы.
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import Qt, QSettings, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QComboBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSpinBox,
                               QTextEdit, QVBoxLayout, QWidget)

from core.marks import MarksFile
from .theme import PALETTE
from .widgets import ToggleSwitch

# Цвета точки-состояния (как в пилюле версии: серый/зелёный/янтарный).
DOT_IDLE = "#8b86a8"
DOT_OK = "#57d081"
DOT_WARN = "#f0a93a"
DOT_ERR = "#ff5f6d"


# --------------------------------------------------------------------------
# Фоновые потоки
# --------------------------------------------------------------------------

class LoginThread(QThread):
    """Вход через Twitch (Device Code Flow): показать код → ждать подтверждения."""
    code_ready = Signal(str, str, int)     # user_code, verification_uri, секунд осталось
    tick = Signal(int)                     # секунд осталось
    done = Signal(str)                     # login вошедшего
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        from core import twitch_auth as auth
        try:
            flow = auth.DeviceFlow()
            dc = flow.start()
            self.code_ready.emit(dc.user_code, dc.verification_uri, dc.seconds_left)
            tok = flow.wait(should_stop=lambda: self._cancel,
                            on_tick=lambda left: self.tick.emit(left))
            auth.save_tokens(tok)
            info = auth.validate(tok["access_token"]) or {}
            self.done.emit(info.get("login", ""))
        except Exception as e:                      # noqa: BLE001
            self.failed.emit(str(e))


class BotThread(QThread):
    """Держит `core.chatbot.BotService` и переводит его события в сигналы Qt.

    Сам сервис крутит свои потоки, поэтому здесь поток нужен только для того, чтобы
    старт/стоп не морозили интерфейс, а события приходили в UI безопасно (через сигналы).
    """
    event = Signal(str, dict)              # kind, payload

    def __init__(self, channel: str, token: str, nick: str, opts: dict, parent=None):
        super().__init__(parent)
        self.channel, self.token, self.nick, self.opts = channel, token, nick, opts
        self.svc = None

    def run(self) -> None:
        from core.chatbot import BotService
        self.svc = BotService(
            channel=self.channel, token=self.token, nick=self.nick,
            who_can_mark=self.opts.get("who_can_mark", "all"),
            viewer_cooldown_sec=self.opts.get("cooldown", 30),
            reply_in_chat=self.opts.get("reply", True),
            use_stream_start_as_zero=self.opts.get("zero_at_start", True),
            autopilot=self.opts.get("autopilot", False),
            write_pulse=self.opts.get("write_pulse", True),
            banter_mode=self.opts.get("banter_mode", "off"),
            banter_period_min=self.opts.get("banter_period_min", 12),
            greet_newcomers=self.opts.get("greet_newcomers", True),
            react_to_hype=self.opts.get("react_to_hype", True),
            on_event=lambda k, p: self.event.emit(k, p))
        self.svc.start()
        # Ждём, пока бот жив (или пока его не остановят снаружи).
        while self.svc.running:
            self.msleep(200)

    def stop_bot(self) -> None:
        if self.svc:
            self.svc.stop()

    def connect_now(self) -> None:
        if self.svc:
            self.svc.connect_now()

    @property
    def marks_path(self) -> str:
        return self.svc.output_path if self.svc else ""

    @property
    def marks_count(self) -> int:
        return self.svc.marks_count if self.svc else 0


# --------------------------------------------------------------------------
# Панель
# --------------------------------------------------------------------------

class BotPanel(QWidget):
    """Вкладка «Бот» внутри режима «Метки через бота»."""

    marks_ready = Signal(object, str)      # MarksFile, имя файла — забирает режим склейки

    def __init__(self, theme: str = "dark", parent=None):
        super().__init__(parent)
        self._theme = theme
        self._login_thread: Optional[LoginThread] = None
        self._bot_thread: Optional[BotThread] = None
        self._account: Optional[dict] = None
        self._marks_path = ""
        self._steppers: list[QPushButton] = []      # кнопки «−/+» — красим при смене темы
        self._settings = QSettings("ClipPolisher", "Bot")
        self._build()
        self._load_settings()
        QTimer.singleShot(0, self._refresh_account)   # тихо проверяем сохранённый вход

    # ================= сборка =================
    def _build(self) -> None:
        c = PALETTE[self._theme]
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;}")
        inner = QWidget(); col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 6, 0); col.setSpacing(12)

        col.addWidget(self._account_card())
        col.addWidget(self._bot_card())
        col.addWidget(self._banter_card())
        col.addWidget(self._autostart_card())
        col.addWidget(self._marks_card(), 1)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

    def _card(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        c = PALETTE[self._theme]
        card = QFrame(); card.setObjectName("card")
        card.setStyleSheet(f"QFrame#card{{background:{c['panel']};border:1px solid {c['line']};"
                           f"border-radius:12px;}}")
        v = QVBoxLayout(card); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(10)
        h = QLabel(title); h.setStyleSheet("font-weight:800;font-size:12px;letter-spacing:.5px;")
        v.addWidget(h)
        return card, v

    def _muted(self, text: str) -> QLabel:
        c = PALETTE[self._theme]
        lab = QLabel(text); lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{c['muted']};font-size:11px;")
        return lab

    def _form_label(self, text: str) -> QLabel:
        """Подпись поля в форме — одинаковая для всех строк, без переносов."""
        c = PALETTE[self._theme]
        lab = QLabel(text); lab.setWordWrap(False)
        lab.setStyleSheet(f"color:{c['muted']};font-size:12px;")
        return lab

    def _switch_row(self, text: str, on: bool = False,
                    tip: str = "") -> tuple[QWidget, ToggleSwitch]:
        """Строка «подпись … тумблер» одной высоты — чтобы список настроек был ровным."""
        w = QWidget()
        l = QHBoxLayout(w); l.setContentsMargins(0, 0, 0, 0); l.setSpacing(8)
        lab = self._muted(text); lab.setWordWrap(False)
        if tip:
            lab.setToolTip(tip); w.setToolTip(tip)
        sw = ToggleSwitch(); sw.setChecked(on)
        l.addWidget(lab); l.addStretch(1); l.addWidget(sw)
        return w, sw

    def _stepper(self, spin: QSpinBox, width: int = 74) -> QWidget:
        """Числовое поле с кнопками «−» и «+» вместо крошечных стрелок Qt.

        Родные стрелки в QSS выходили то квадратами, то вовсе невидимыми, да и
        попасть по ним мышкой трудно. Свои кнопки рисуются одинаково в обеих темах
        и нажимаются спокойно.
        """
        spin.setButtonSymbols(QSpinBox.NoButtons)
        spin.setAlignment(Qt.AlignCenter)
        spin.setFixedWidth(width)

        box = QWidget()
        lay = QHBoxLayout(box); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(6)
        # «–» (короткое тире) вместо дефиса: дефис рисуется мелким и висит высоко.
        minus, plus = self._step_btn("–"), self._step_btn("+")
        minus.clicked.connect(spin.stepDown)
        plus.clicked.connect(spin.stepUp)
        lay.addWidget(minus); lay.addWidget(spin); lay.addWidget(plus); lay.addStretch(1)
        return box

    def _step_btn(self, text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(30, 30)
        b.setAutoRepeat(True)                # зажал — крутится, как у обычных стрелок
        b.setAutoRepeatDelay(400); b.setAutoRepeatInterval(90)
        b.setProperty("stepper", True)
        self._style_step_btn(b)
        self._steppers.append(b)
        return b

    def _style_step_btn(self, b: QPushButton) -> None:
        c = PALETTE[self._theme]
        b.setStyleSheet(
            f"QPushButton{{background:{c['panel2']};border:1px solid {c['line']};"
            f"border-radius:8px;color:{c['text']};font-size:16px;font-weight:800;"
            f"padding:0;}}"
            f"QPushButton:hover{{border-color:{c['accent']};color:{c['accent']};}}"
            f"QPushButton:pressed{{background:{c['accent']};color:#fff;"
            f"border-color:{c['accent']};}}")

    def _primary(self, text: str) -> QPushButton:
        c = PALETTE[self._theme]
        b = QPushButton(text); b.setCursor(Qt.PointingHandCursor)
        # У выключенной кнопки свой вид — иначе она выглядит нажимаемой и путает.
        b.setStyleSheet(
            f"QPushButton{{background:{c['accent']};border:1px solid {c['accent']};color:#fff;"
            f"border-radius:9px;padding:10px 16px;font-weight:800;font-size:13px;}}"
            f"QPushButton:disabled{{background:{c['panel2']};border-color:{c['line']};"
            f"color:{c['muted']};}}")
        return b

    # ---- карточка аккаунта ----
    def _account_card(self) -> QFrame:
        c = PALETTE[self._theme]
        card, v = self._card("АККАУНТ TWITCH")

        # Состояние «не вошёл».
        self.login_hint = self._muted(
            "Нажми «Войти через Twitch» — откроется страница Twitch, где надо подтвердить "
            "короткий код. Пароль мы не видим: вход идёт на стороне Twitch. Права нужны "
            "только на чтение и отправку сообщений в чат.")
        v.addWidget(self.login_hint)
        self.login_btn = self._primary("Войти через Twitch")
        self.login_btn.clicked.connect(self._start_login)
        v.addWidget(self.login_btn)

        # Состояние «показываем код» (скрыто до входа).
        self.code_box = QWidget()
        cb = QVBoxLayout(self.code_box); cb.setContentsMargins(0, 4, 0, 0); cb.setSpacing(8)
        cb.addWidget(self._muted("1. Открой страницу Twitch  ·  2. Введи/сверь код  ·  3. Подтверди"))
        self.code_lbl = QLabel("--------")
        self.code_lbl.setAlignment(Qt.AlignCenter)
        self.code_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.code_lbl.setStyleSheet(
            f"background:{c['panel2']};border:1px dashed {c['accent']};border-radius:10px;"
            f"padding:14px;font-size:26px;font-weight:800;letter-spacing:6px;color:{c['text']};")
        cb.addWidget(self.code_lbl)
        row = QHBoxLayout(); row.setSpacing(8)
        self.open_btn = QPushButton("Открыть страницу Twitch")
        self.open_btn.clicked.connect(self._open_verify)
        self.copy_btn = QPushButton("Скопировать код")
        self.copy_btn.clicked.connect(self._copy_code)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.clicked.connect(self._cancel_login)
        for b in (self.open_btn, self.copy_btn, self.cancel_btn):
            b.setCursor(Qt.PointingHandCursor)
        row.addWidget(self.open_btn); row.addWidget(self.copy_btn)
        row.addStretch(1); row.addWidget(self.cancel_btn)
        cb.addLayout(row)
        self.wait_lbl = self._muted("Жду подтверждения…")
        cb.addWidget(self.wait_lbl)
        self.code_box.setVisible(False)
        v.addWidget(self.code_box)

        # Состояние «вошёл».
        self.acc_box = QWidget()
        ab = QHBoxLayout(self.acc_box); ab.setContentsMargins(0, 0, 0, 0); ab.setSpacing(8)
        self.acc_dot = QLabel(); self.acc_dot.setFixedSize(9, 9)
        self.acc_dot.setStyleSheet(f"background:{DOT_OK};border-radius:4px;")
        self.acc_lbl = QLabel("—")
        self.acc_lbl.setStyleSheet(f"color:{c['text']};font-size:13px;font-weight:700;")
        self.logout_btn = QPushButton("Выйти")
        self.logout_btn.setCursor(Qt.PointingHandCursor)
        self.logout_btn.clicked.connect(self._logout)
        ab.addWidget(self.acc_dot); ab.addWidget(self.acc_lbl); ab.addStretch(1)
        ab.addWidget(self.logout_btn)
        self.acc_box.setVisible(False)
        v.addWidget(self.acc_box)

        self.auth_err = QLabel(""); self.auth_err.setWordWrap(True)
        self.auth_err.setStyleSheet("color:#ff8149;font-size:11px;")
        self.auth_err.setVisible(False)
        v.addWidget(self.auth_err)
        return card

    # ---- карточка бота ----
    def _bot_card(self) -> QFrame:
        c = PALETTE[self._theme]
        card, v = self._card("БОТ В ЧАТЕ")

        # Поля-настройки — в форму, чтобы подписи и поля стояли по одной линии
        # (раньше «Канал:» и «Метить могут:» разной длины разъезжали ввод по строкам).
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10); form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.channel_edit = QLineEdit()
        self.channel_edit.setPlaceholderText("твой канал на Twitch")
        self.channel_edit.setToolTip("Обычно это твой же канал — подставится сам после входа")
        form.addRow(self._form_label("Канал"), self.channel_edit)

        self.who_combo = QComboBox()
        self.who_combo.addItems(["Все зрители", "Только стример и модеры"])
        form.addRow(self._form_label("Метить могут"), self.who_combo)

        self.cooldown_spin = QSpinBox(); self.cooldown_spin.setRange(0, 600)
        self.cooldown_spin.setValue(30); self.cooldown_spin.setSuffix(" с")
        self.cooldown_spin.setToolTip("Чтобы один зритель не мог засыпать чат метками")
        form.addRow(self._form_label("Пауза для зрителя"), self._stepper(self.cooldown_spin))
        v.addLayout(form)

        # Без «галочки» в тексте: в PT Sans такого символа нет — вылезал бы пустой квадрат.
        row, self.reply_sw = self._switch_row("Отвечать в чат на метку", on=True)
        v.addWidget(row)
        row, self.auto_sw = self._switch_row("Включать бота вместе с эфиром", on=True)
        self.auto_sw.toggled.connect(self._on_autopilot_toggled)
        v.addWidget(row)
        self.auto_hint = self._muted(
            "Бот сам зайдёт в чат, когда начнётся эфир, и выйдет после его конца. "
            "Программа для этого должна быть запущена (можно свернуть в трей).")
        v.addWidget(self.auto_hint)

        v.addWidget(self._muted("Команды в чате: !clip · !клип · !метка (можно с текстом — "
                                "он станет названием момента)."))

        self.connect_btn = self._primary("Включить автопилот")
        self.connect_btn.clicked.connect(self._toggle_bot)
        self.connect_btn.setEnabled(False)
        v.addWidget(self.connect_btn)

        srow = QHBoxLayout(); srow.setSpacing(8)
        self.status_dot = QLabel(); self.status_dot.setFixedSize(9, 9)
        self.status_dot.setStyleSheet(f"background:{DOT_IDLE};border-radius:4px;")
        self.status_lbl = QLabel("Бот выключен")
        self.status_lbl.setStyleSheet(f"color:{c['muted']};font-size:12px;")
        self.online_lbl = QLabel("")
        self.online_lbl.setStyleSheet(f"color:{c['muted']};font-size:12px;")
        srow.addWidget(self.status_dot); srow.addWidget(self.status_lbl)
        srow.addStretch(1); srow.addWidget(self.online_lbl)
        v.addLayout(srow)

        # Проверить бота без эфира: чат канала работает и вне трансляции, но автопилот
        # сам туда не пойдёт — иначе кажется, будто бот «не работает».
        self.test_btn = QPushButton("Зайти в чат сейчас (проверка без эфира)")
        self.test_btn.setCursor(Qt.PointingHandCursor)
        self.test_btn.setToolTip("Бот зайдёт в чат прямо сейчас, чтобы можно было "
                                 "проверить команду !clip без стрима")
        self.test_btn.clicked.connect(self._connect_now)
        self.test_btn.setVisible(False)
        v.addWidget(self.test_btn)
        return card

    def _connect_now(self) -> None:
        if self._bot_thread:
            self._bot_thread.connect_now()
            self.test_btn.setVisible(False)
            self._log_line("Проверка без эфира: захожу в чат")

    # ---- карточка болталки ----
    def _banter_card(self) -> QFrame:
        """Что бот ГОВОРИТ в чат (не путать с журналом чата — тот невидимый)."""
        from core.banter import MODES
        card, v = self._card("БОТ ОБЩАЕТСЯ В ЧАТЕ")
        v.addWidget(self._muted(
            "Бот может не только молча ставить метки, но и подавать голос: подбадривать "
            "зрителей, хвалить, шутить. Фразы он берёт из выбранной пачки и не "
            "повторяется, пока она не кончится."))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(10); form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.banter_combo = QComboBox()
        for _code, title in MODES:
            self.banter_combo.addItem(title)
        self.banter_combo.currentIndexChanged.connect(self._on_banter_changed)
        form.addRow(self._form_label("Настроение"), self.banter_combo)

        self.banter_spin = QSpinBox(); self.banter_spin.setRange(1, 120)
        self.banter_spin.setValue(12); self.banter_spin.setSuffix(" мин")
        self.banter_spin.setToolTip("Как часто бот подаёт голос просто так")
        self.banter_spin.valueChanged.connect(lambda _v: self._on_banter_changed())
        form.addRow(self._form_label("Раз в"), self._stepper(self.banter_spin))
        v.addLayout(form)

        row, self.greet_sw = self._switch_row(
            "Здороваться с новичками", on=True,
            tip="Только с теми, кто написал впервые за эфир. В шумном чате бот молчит")
        self.greet_sw.toggled.connect(lambda _o: self._on_banter_changed())
        v.addWidget(row)
        row, self.hype_sw = self._switch_row(
            "Отзываться, когда чат взорвался", on=True,
            tip="Если чат вдруг заговорил втрое активнее обычного")
        self.hype_sw.toggled.connect(lambda _o: self._on_banter_changed())
        v.addWidget(row)

        self.banter_hint = self._muted(
            "Команда !отчёт (только стример и модеры) — бот пришлёт в чат сводку эфира: "
            "сколько идёт, сколько меток, насколько живой чат.")
        v.addWidget(self.banter_hint)
        return card

    def _on_banter_changed(self) -> None:
        """Настройки болталки применяются на лету — бота перезапускать не нужно."""
        from core.banter import MODE_CODES
        self._save_settings()
        svc = getattr(self._bot_thread, "svc", None) if self._bot_thread else None
        if svc is None:
            return
        idx = max(0, min(self.banter_combo.currentIndex(), len(MODE_CODES) - 1))
        svc.banter.configure(mode=MODE_CODES[idx], period_min=self.banter_spin.value(),
                             greet_newcomers=self.greet_sw.isChecked(),
                             react_to_hype=self.hype_sw.isChecked())

    # ---- карточка автозапуска ----
    def _autostart_card(self) -> QFrame:
        card, v = self._card("ЧТОБЫ НЕ ЗАБЫВАТЬ ВКЛЮЧАТЬ")
        v.addWidget(self._muted(
            "Автопилот ловит начало эфира только пока программа запущена. С этими "
            "галочками про неё можно вообще не вспоминать."))

        row, self.startup_sw = self._switch_row(
            "Запускать вместе с Windows",
            tip="Программа стартует свёрнутой в трей и ловит начало эфира сама")
        self.startup_sw.toggled.connect(self._on_startup_toggled)
        v.addWidget(row)

        row, self.tray_sw = self._switch_row(
            "Закрытие окна сворачивает в трей", on=True,
            tip="Бот продолжает работать; открыть — двойной клик по значку у часов")
        self.tray_sw.toggled.connect(
            lambda on: self._settings.setValue("tray_on_close", on))
        v.addWidget(row)

        self.startup_err = QLabel(""); self.startup_err.setWordWrap(True)
        self.startup_err.setStyleSheet("color:#ff8149;font-size:11px;")
        self.startup_err.setVisible(False)
        v.addWidget(self.startup_err)
        return card

    def _on_startup_toggled(self, on: bool) -> None:
        from core import autostart
        if autostart.set_enabled(on):
            self.startup_err.setVisible(False)
            return
        # Не получилось — честно говорим и возвращаем переключатель назад.
        self.startup_err.setText("Не удалось изменить автозапуск Windows "
                                 "(возможно, его блокирует антивирус или политика).")
        self.startup_err.setVisible(True)
        self.startup_sw.blockSignals(True)
        self.startup_sw.setChecked(not on)
        self.startup_sw.blockSignals(False)

    # ---- карточка меток ----
    def _marks_card(self) -> QFrame:
        c = PALETTE[self._theme]
        card, v = self._card("МЕТКИ ЭФИРА")

        # Какой именно эфир сейчас пишется — чтобы записи разных стримов не путались.
        self.session_lbl = QLabel("Эфир не начался — метки пойдут в файл «вне эфира»")
        self.session_lbl.setWordWrap(True)
        self.session_lbl.setStyleSheet(
            f"background:{c['panel2']};border:1px solid {c['line']};border-radius:8px;"
            f"padding:8px 10px;color:{c['muted']};font-size:11px;")
        v.addWidget(self.session_lbl)

        # Журнал чата — НЕВИДИМЫЙ файл рядом с метками (сырьё для автопоиска моментов).
        # В чат из него ничего не уходит, поэтому он и живёт здесь, а не в болталке.
        row, self.pulse_sw = self._switch_row(
            "Вести журнал чата (для автопоиска моментов)", on=True,
            tip="Раз в 10 секунд записывает, сколько было сообщений и смеха. "
                "Файл лежит рядом с метками, в чат ничего не пишется")
        v.addWidget(row)
        v.addWidget(self._muted(
            "По этому журналу программа потом сама находит места, где чат взорвался, — "
            "и объясняет, почему выбрала момент. Никакие тексты зрителей не сохраняются."))

        hrow = QHBoxLayout()
        self.count_lbl = QLabel("Меток: 0")
        self.count_lbl.setStyleSheet(f"color:{c['text']};font-size:13px;font-weight:800;")
        hrow.addWidget(self.count_lbl); hrow.addStretch(1)
        self.folder_btn = QPushButton("Папка с метками")
        self.folder_btn.setCursor(Qt.PointingHandCursor)
        self.folder_btn.clicked.connect(self._open_folder)
        hrow.addWidget(self.folder_btn)
        v.addLayout(hrow)

        self.log = QTextEdit(); self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        self.log.setStyleSheet(
            f"QTextEdit{{background:{c['panel2']};border:1px solid {c['line']};"
            f"border-radius:10px;color:{c['muted']};font-size:12px;padding:8px;}}")
        self.log.setPlaceholderText("Здесь будут появляться метки из чата…")
        v.addWidget(self.log, 1)

        self.take_btn = self._primary("Взять эти метки в нарезку")
        self.take_btn.setEnabled(False)
        self.take_btn.clicked.connect(self._take_marks)
        v.addWidget(self.take_btn)
        v.addWidget(self._muted("Метки сохраняются сразу, файл не потеряется даже если "
                                "закрыть программу. Их можно взять и позже — кнопкой "
                                "«Импортировать файл меток» слева."))
        return card

    # ================= настройки =================
    def _load_settings(self) -> None:
        s = self._settings
        self.channel_edit.setText(s.value("channel", "", str))
        self.who_combo.setCurrentIndex(int(s.value("who", 0)))
        self.cooldown_spin.setValue(int(s.value("cooldown", 30)))
        self.reply_sw.setChecked(s.value("reply", True, bool))
        self.auto_sw.setChecked(s.value("autopilot", True, bool))
        self.auto_hint.setVisible(self.auto_sw.isChecked())
        from core.banter import MODE_CODES
        mode = s.value("banter_mode", "off", str)
        self.banter_combo.blockSignals(True)
        self.banter_combo.setCurrentIndex(MODE_CODES.index(mode) if mode in MODE_CODES else 0)
        self.banter_combo.blockSignals(False)
        self.banter_spin.setValue(int(s.value("banter_period", 12)))
        self.greet_sw.setChecked(s.value("banter_greet", True, bool))
        self.hype_sw.setChecked(s.value("banter_hype", True, bool))
        self.pulse_sw.setChecked(s.value("write_pulse", True, bool))
        self.tray_sw.setChecked(s.value("tray_on_close", True, bool))
        # Состояние автозапуска берём из самой Windows, а не из своих настроек:
        # его могли снять снаружи (диспетчер задач, чистилки).
        from core import autostart
        self.startup_sw.blockSignals(True)
        self.startup_sw.setChecked(autostart.is_enabled())
        self.startup_sw.blockSignals(False)
        autostart.refresh_if_enabled()      # программа могла переехать после обновления
        self._sync_button()

    def _save_settings(self) -> None:
        s = self._settings
        s.setValue("channel", self.channel_edit.text().strip())
        s.setValue("who", self.who_combo.currentIndex())
        s.setValue("cooldown", self.cooldown_spin.value())
        s.setValue("reply", self.reply_sw.isChecked())
        s.setValue("autopilot", self.auto_sw.isChecked())
        from core.banter import MODE_CODES
        idx = max(0, min(self.banter_combo.currentIndex(), len(MODE_CODES) - 1))
        s.setValue("banter_mode", MODE_CODES[idx])
        s.setValue("banter_period", self.banter_spin.value())
        s.setValue("banter_greet", self.greet_sw.isChecked())
        s.setValue("banter_hype", self.hype_sw.isChecked())
        s.setValue("write_pulse", self.pulse_sw.isChecked())

    # ================= вход =================
    def _refresh_account(self) -> None:
        """Тихо проверить сохранённый вход (сеть — в потоке, чтобы не морозить окно)."""
        if getattr(self, "_closing", False):
            return          # окно уже закрывают — не плодим потоки на выходе
        self._acc_thread = _AccountThread(self)
        self._acc_thread.done.connect(self._on_account)
        self._acc_thread.start()

    def apply_theme(self, theme: str) -> None:
        """Перекрасить самодельные виджеты (тумблеры) под тему."""
        from . import theme as theme_mod
        theme_mod.set_current(theme)
        self._theme = theme
        for sw in self.findChildren(ToggleSwitch):
            sw.set_theme(theme)
        for b in self._steppers:                 # кнопки «−/+» тоже живут на инлайн-стиле
            self._style_step_btn(b)

    @property
    def bot_running(self) -> bool:
        return bool(self._bot_thread and self._bot_thread.isRunning())

    def _on_account(self, acc: dict) -> None:
        # Ответ проверки входа мог прийти уже после закрытия окна — тогда ничего не
        # трогаем (иначе автопилот поднимал бы бота на выходе из программы).
        if getattr(self, "_closing", False):
            return
        self._account = acc or None
        logged = bool(acc)
        self.acc_box.setVisible(logged)
        self.login_btn.setVisible(not logged)
        self.login_hint.setVisible(not logged)
        self.code_box.setVisible(False)
        self.connect_btn.setEnabled(logged)
        if logged:
            self.acc_lbl.setText(f"Вошёл как {acc['login']}")
            if not self.channel_edit.text().strip():
                self.channel_edit.setText(acc["login"])
            # Автопилот должен работать сам после запуска программы (в т.ч. из
            # автозапуска Windows) — иначе смысл теряется.
            if (self.auto_sw.isChecked() and not self.bot_running
                    and not getattr(self, "_auto_started", False)):
                self._auto_started = True
                self._start_bot()

    def _start_login(self) -> None:
        self.auth_err.setVisible(False)
        self.login_btn.setVisible(False)
        self.login_hint.setVisible(False)
        self.code_box.setVisible(True)
        self.code_lbl.setText("…")
        self.wait_lbl.setText("Получаю код у Twitch…")
        self._login_thread = LoginThread(self)
        self._login_thread.code_ready.connect(self._on_code)
        self._login_thread.tick.connect(self._on_tick)
        self._login_thread.done.connect(self._on_login_done)
        self._login_thread.failed.connect(self._on_login_failed)
        self._login_thread.start()

    def _on_code(self, code: str, uri: str, left: int) -> None:
        self._verify_uri = uri
        self.code_lbl.setText(code)
        self.wait_lbl.setText(f"Жду подтверждения… (код живёт {left // 60} мин)")
        QDesktopServices.openUrl(QUrl(uri))     # сразу открываем — юзеру меньше действий

    def _on_tick(self, left: int) -> None:
        self.wait_lbl.setText(f"Жду подтверждения… (осталось {left // 60} мин {left % 60} с)")

    def _on_login_done(self, login: str) -> None:
        self.code_box.setVisible(False)
        self._refresh_account()

    def _on_login_failed(self, msg: str) -> None:
        self.code_box.setVisible(False)
        self.login_btn.setVisible(True)
        self.login_hint.setVisible(True)
        self.auth_err.setText("Вход не удался: " + msg)
        self.auth_err.setVisible(True)

    def _cancel_login(self) -> None:
        if self._login_thread:
            self._login_thread.cancel()
        self.code_box.setVisible(False)
        self.login_btn.setVisible(True)
        self.login_hint.setVisible(True)

    def _open_verify(self) -> None:
        if getattr(self, "_verify_uri", ""):
            QDesktopServices.openUrl(QUrl(self._verify_uri))

    def _copy_code(self) -> None:
        QGuiApplication.clipboard().setText(self.code_lbl.text())
        self.copy_btn.setText("Скопировано")
        QTimer.singleShot(1500, lambda: self.copy_btn.setText("Скопировать код"))

    def _logout(self) -> None:
        if self._bot_thread:
            self._stop_bot()
        from core import twitch_auth as auth
        auth.logout()
        self._on_account({})

    # ================= бот =================
    def _toggle_bot(self) -> None:
        if self._bot_thread and self._bot_thread.isRunning():
            self._stop_bot()
        else:
            self._start_bot()

    def _start_bot(self) -> None:
        if getattr(self, "_closing", False):
            return
        channel = self.channel_edit.text().strip().lstrip("#")
        if not channel:
            self._set_status("Укажи канал", DOT_WARN)
            return
        if not self._account:
            self._set_status("Сначала войди через Twitch", DOT_WARN)
            return
        auto = self.auto_sw.isChecked()
        from core.banter import MODE_CODES
        bidx = max(0, min(self.banter_combo.currentIndex(), len(MODE_CODES) - 1))
        opts = {"who_can_mark": "all" if self.who_combo.currentIndex() == 0 else "trusted",
                "cooldown": self.cooldown_spin.value(),
                "reply": self.reply_sw.isChecked(), "zero_at_start": True,
                "autopilot": auto, "write_pulse": self.pulse_sw.isChecked(),
                "banter_mode": MODE_CODES[bidx],
                "banter_period_min": self.banter_spin.value(),
                "greet_newcomers": self.greet_sw.isChecked(),
                "react_to_hype": self.hype_sw.isChecked()}
        self._save_settings()
        self._bot_thread = BotThread(channel, self._account["token"],
                                     self._account["login"], opts, self)
        self._bot_thread.event.connect(self._on_bot_event)
        self._bot_thread.start()
        self.connect_btn.setText("Выключить автопилот" if auto else "Остановить бота")
        self.channel_edit.setEnabled(False)
        self.auto_sw.setEnabled(False)
        self._log_line(f"Запуск бота на канале #{channel}"
                       + (" (автопилот)" if auto else ""))

    def _stop_bot(self) -> None:
        if self._bot_thread:
            self._bot_thread.stop_bot()
            self._bot_thread.wait(4000)
            self._marks_path = self._bot_thread.marks_path or self._marks_path
            self._bot_thread = None
        self._sync_button()
        self.channel_edit.setEnabled(True)
        self.auto_sw.setEnabled(True)
        self.test_btn.setVisible(False)
        self._set_status("Бот выключен", DOT_IDLE)

    def _on_autopilot_toggled(self, on: bool) -> None:
        self.auto_hint.setVisible(on)
        self._sync_button()
        self._save_settings()

    def _sync_button(self) -> None:
        running = bool(self._bot_thread and self._bot_thread.isRunning())
        auto = self.auto_sw.isChecked()
        if running:
            self.connect_btn.setText("Выключить автопилот" if auto else "Остановить бота")
        else:
            self.connect_btn.setText("Включить автопилот" if auto else "Подключить бота")

    def _on_bot_event(self, kind: str, p: dict) -> None:
        if kind == "mark":
            self._marks_path = p.get("path", self._marks_path)
            self.count_lbl.setText(f"Меток: {p['n']}")
            self.take_btn.setEnabled(True)
            note = f" — «{p['note']}»" if p.get("note") else ""
            self._log_line(f"#{p['n']}  {_fmt(p['t'])}  {p['author']} "
                           f"({_ROLE_RU.get(p['role'], p['role'])}){note}")
        elif kind == "session":
            self._marks_path = p.get("path", self._marks_path)
            self.count_lbl.setText(f"Меток: {p.get('marks', 0)}")
            self.take_btn.setEnabled(bool(p.get("marks")))
            self._show_session(p)
        elif kind == "status":
            text = p.get("text", "")
            self._set_status(text, DOT_OK if p.get("running") else DOT_IDLE)
            # Кнопка проверки нужна ровно в одном состоянии: автопилот ждёт эфира.
            self.test_btn.setVisible("жду начала эфира" in text)
        elif kind == "online":
            self.online_lbl.setText(f"онлайн: {p['viewers']}" if p.get("live") else "эфир не идёт")
        elif kind == "error":
            self._set_status(p.get("text", "Ошибка"), DOT_ERR)
            self._log_line("Ошибка: " + p.get("text", ""))
            if p.get("relogin"):
                self._on_account({})
            self._stop_bot()
        elif kind == "say":
            # Что бот сказал в чат — видно в том же журнале, чтобы не гадать.
            self._log_line("бот: " + p.get("text", ""))
        elif kind == "log":
            self._log_line(p.get("text", ""))

    def _show_session(self, p: dict) -> None:
        """Подписать, метки какого именно эфира сейчас пишутся."""
        if not p.get("live"):
            self.session_lbl.setText("Эфир не идёт — метки пойдут в файл «вне эфира», "
                                     "он не смешается с записью стрима.")
            return
        when = _human_time(p.get("started_at", ""))
        title = p.get("title") or "без названия"
        game = f" · {p['game']}" if p.get("game") else ""
        extra = (f" · продолжаю файл ({p['marks']} меток)" if p.get("resumed") else "")
        self.session_lbl.setText(f"Пишу эфир: «{title}»{game}\nначало {when}"
                                 f" · свой файл меток{extra}")

    def _set_status(self, text: str, color: str) -> None:
        self.status_lbl.setText(text)
        self.status_dot.setStyleSheet(f"background:{color};border-radius:4px;")

    def _log_line(self, text: str) -> None:
        self.log.append(text)

    # ================= метки =================
    def _take_marks(self) -> None:
        path = self._marks_path or (self._bot_thread.marks_path if self._bot_thread else "")
        if not path or not os.path.isfile(path):
            self._log_line("Файл меток ещё не создан — метка появится после первой команды.")
            return
        try:
            mf = MarksFile.from_json(path)
        except (OSError, ValueError) as e:
            self._log_line(f"Не удалось прочитать метки: {e}")
            return
        self.marks_ready.emit(mf, os.path.basename(path))

    def _open_folder(self) -> None:
        from core.chatbot import default_marks_dir
        folder = os.path.dirname(self._marks_path) if self._marks_path else default_marks_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    # ================= прочее =================
    def shutdown(self) -> None:
        """Аккуратно погасить потоки при закрытии окна."""
        self._closing = True
        self._save_settings()
        if self._login_thread:
            self._login_thread.cancel()
            self._login_thread.wait(2000)
        acc = getattr(self, "_acc_thread", None)
        if acc and acc.isRunning():
            # Он может висеть на медленном ответе Twitch. Ждём, а совсем упрямый
            # снимаем силой: иначе Qt уронит процесс при закрытии окна.
            if not acc.wait(6000):
                acc.terminate()
                acc.wait(500)
        if self._bot_thread:
            self._stop_bot()


class _AccountThread(QThread):
    """Проверка сохранённого входа (ходит в сеть — поэтому не в UI-потоке)."""
    done = Signal(dict)

    def run(self) -> None:
        from core import twitch_auth as auth
        try:
            self.done.emit(auth.current_account() or {})
        except Exception:            # noqa: BLE001 — нет сети: считаем, что входа нет
            self.done.emit({})


_ROLE_RU = {"streamer": "стример", "moderator": "модер", "vip": "вип", "viewer": "зритель"}
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _human_time(iso: str) -> str:
    """'2026-07-26T21:04:00+03:00' → '26 июля, 21:04'."""
    import datetime
    try:
        d = datetime.datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or "—"
    return f"{d.day} {_MONTHS[d.month - 1]}, {d:%H:%M}"
