"""dev/shot_scan.py — офскрин-снимки экрана Этапа 3 «Автопоиск моментов ИИ».

Снимает панель с синтетическим разбором (обе темы, два размера, ход разбора,
вкладка «Раскладка») + smoke-тест: переключение режима 3 в целом окне и сборка
конфигураций рендера БЕЗ сети и без ffmpeg.

Запуск: .venv\\Scripts\\python.exe -m dev.shot_scan
"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer, QEventLoop
from PySide6.QtGui import QGuiApplication, QFontDatabase, QFont

QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

OUT = "out/shots"


def settle(app, ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def _wait_threads(timeout_ms=8000):
    import gc
    from PySide6.QtCore import QThread
    stuck = []
    for obj in gc.get_objects():
        if isinstance(obj, QThread):
            try:
                if obj.isRunning() and not obj.wait(timeout_ms):
                    stuck.append(obj.__class__.__name__)
            except RuntimeError:
                pass
    return stuck


def _synthetic_scan():
    """Разбор 2-часового стрима, похожий на живой: клипы, чат, звук, речь.

    Числа взяты с реального прогона (Buster, 2:06): десятки улик разных семей,
    часть моментов «золотая» (проголосовали разные по природе улики).
    """
    import random
    from core.clipscan import Kind, Signal, build_scan
    random.seed(11)

    signals = []
    hot = [612.0, 1840.0, 3120.0, 3670.0, 4980.0, 6210.0, 6980.0]
    titles = ["а голосок то какой", "ЭТО ЧТО БЫЛО", "", "клатч века", "", "12", "СЫН"]
    for i, cen in enumerate(hot):
        for k in range(random.randint(1, 9)):
            views = random.randint(3, 900)
            signals.append(Signal(kind=Kind.VIEWER_CLIP, t=cen + random.uniform(-6, 6),
                                  weight=3.0 + views / 900.0,
                                  detail=f"клип «{titles[i]}» ({views} просм.)",
                                  meta={"title": titles[i], "views": views}))
        if i % 2 == 0:
            signals.append(Signal(kind=Kind.CHAT_SPIKE, t=cen + random.uniform(-4, 4),
                                  weight=2.4, detail="чат взорвался в 6.1 раза"))
        signals.append(Signal(kind=Kind.LOUD, t=cen + random.uniform(-3, 3), weight=1.6,
                              detail="стало громче обычного в 8.9 раза"))
        if i % 3 != 2:
            signals.append(Signal(kind=Kind.LAUGH, t=cen + random.uniform(-3, 3),
                                  weight=1.8, detail="смеются"))
        if i % 2 == 1:
            signals.append(Signal(kind=Kind.SPEECH, t=cen, weight=1.5,
                                  detail="сказал «Да ты гонишь, я в шоке просто»"))
    # фоновый шум: одиночные зацепки по звуку по всей записи
    for _ in range(40):
        signals.append(Signal(kind=Kind.LOUD, t=random.uniform(30, 7100), weight=1.0,
                              detail="стало громче обычного в 3.1 раза"))

    source = {"platform": "twitch", "channel": "buster", "vod_id": "2829614149",
              "url": "https://www.twitch.tv/videos/2829614149",
              "title": "Аниме и разговоры", "duration": 7200.0,
              "started_at": "2026-07-26T18:04:00+03:00", "broadcast_id": "998877",
              "clips": 390, "face_source": False, "audio": True, "speech": 25}
    notes = ["Журнала чата с этого эфира нет (бот тогда не работал или это чужой "
             "канал) — сигнал «взрыв чата» участвует только по клипам."]
    return build_scan(source, signals, strictness=50.0, notes=notes)


def main():
    app = QApplication([])
    for f in ("assets/fonts/PTSans-Regular.ttf", "assets/fonts/PTSans-Bold.ttf"):
        if os.path.isfile(f):
            QFontDatabase.addApplicationFont(f)
    app.setFont(QFont("PT Sans", 10))
    os.makedirs(OUT, exist_ok=True)

    from ui.scan_mode import ScanModePanel
    from ui.theme import build_qss

    scan = _synthetic_scan()
    print("моментов в синтетическом разборе:", len(scan.moments),
          "| золотых:", sum(1 for m in scan.moments if m.gold))

    for theme in ("dark", "light"):
        panel = ScanModePanel(theme)
        panel.setStyleSheet(build_qss(theme))
        panel.apply_theme(theme)
        panel.link_edit.setText("https://www.twitch.tv/videos/2829614149")
        panel._apply_scan(scan)
        panel._select(1)
        panel.resize(1280, 820)
        panel.show()
        settle(app, 300)
        p = os.path.join(OUT, f"scan_{theme}_1280.png")
        panel.grab().save(p); print("saved", p)

        # ход разбора (как во время работы)
        panel.progress_card.setVisible(True)
        for line in ["Спрашиваю Twitch про запись…", "Клипов зрителей: 390",
                     "Качаю звук записи: 60%  (108 из 181 МБ)",
                     "Звук разобран: 49 зацепок, из них смех — 16",
                     "Речь по 30 местам (25:10 звука), модель «large-v3» "
                     "на видеокарте — примерно 3:36"]:
            panel._log(line)
        settle(app, 200)
        p = os.path.join(OUT, f"scan_{theme}_progress.png")
        panel.grab().save(p); print("saved", p)
        panel.progress_card.setVisible(False)

        # вкладка «Раскладка»
        panel.tab_layout.setChecked(True); panel.right_stack.setCurrentIndex(1)
        panel.layout_editor.set_mode("final")
        settle(app, 250)
        p = os.path.join(OUT, f"scan_{theme}_layout.png")
        panel.grab().save(p); print("saved", p)

        panel.tab_moments.setChecked(True); panel.right_stack.setCurrentIndex(0)
        panel.resize(1040, 760); settle(app, 200)
        p = os.path.join(OUT, f"scan_{theme}_1040.png")
        panel.grab().save(p); print("saved", p)
        panel.shutdown()

    # ---- smoke в целом окне ----
    from ui.main_window import MainWindow
    win = MainWindow()
    win.resize(1280, 820)
    win.show(); settle(app, 200)
    win._on_mode_selected(2)               # «Автопоиск ИИ»
    win.scan_panel._apply_scan(scan)
    settle(app, 250)
    p = os.path.join(OUT, "scan_window_mode3.png")
    win.grab().save(p); print("saved", p)

    sp = win.scan_panel
    print("строгость 50 →", len(sp._moments()), "моментов;",
          sp.found_lbl.text())
    sp.strict_slider.setValue(100); settle(app, 100)
    print("строгость 100 →", len(sp._moments()), "моментов")
    sp.strict_slider.setValue(0); settle(app, 100)
    print("строгость 0 →", len(sp._moments()), "моментов")
    sp.strict_slider.setValue(50); settle(app, 100)

    # честная проверка: без скачанных кусков нарезать нельзя
    print("validate() без кусков:", sp.validate())
    print("оценка загрузки:", sp.estimate_lbl.text())

    # подкладываем «уже скачанные» куски (сеть не трогаем) и собираем конфиги
    from core.vodcut import ClipPiece
    for i, mo in enumerate(sp._moments()):
        sp._pieces[i] = ClipPiece(path=f"out/cut_{i}.mp4", start=mo.start - 1.0,
                                  end=mo.end + 1.0, duration=mo.duration + 2.0)
    print("validate() с кусками:", sp.validate())
    cfgs = sp.build_pipeline_configs("out")
    print("конфигов:", len(cfgs))
    for cfg in cfgs[:3]:
        seg = cfg.segments[0]
        print("  ", os.path.basename(cfg.export.filename),
              f"| источник {os.path.basename(cfg.source)}"
              f" | внутри куска {seg.start:.1f}–{seg.end:.1f} с"
              f" | ник {cfg.branding.nickname!r}")

    # «не тот момент» — убирает карточку и запоминает промах
    before = len(sp._moments())
    sp._reject(0)
    print(f"«не тот»: было {before}, стало {len(sp._moments())}, "
          f"запомнено промахов: {len(sp._rejected)}")

    win.scan_panel.shutdown()
    win.marks_panel.bot_panel.shutdown()
    win.close()
    app.processEvents()
    print("живые потоки после закрытия:", _wait_threads() or "нет")
    print("DONE")


if __name__ == "__main__":
    main()
