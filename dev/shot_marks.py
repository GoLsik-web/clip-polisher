"""dev/shot_marks.py — офскрин-снимки экрана Этапа 2 «Метки через бота».

Снимает панель с синтетическими метками (тёмная/светлая тема, 2 размера) + smoke-тест
переключения режима в целом окне. Запуск: .venv\\Scripts\\python.exe -m dev.shot_marks
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


def _wait_threads(timeout_ms=8000):
    """Дождаться всех живых QThread перед выходом.

    Иначе интерпретатор рушится с «QThread: Destroyed while thread is still running»
    — процессу нечем отличить «тест закончился» от «поток ещё в сети».
    Возвращает список тех, кто так и не завершился.
    """
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


def settle(app, ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def _synthetic_marks():
    """Реалистичный набор меток одного 2-часового стрима (разные авторы/кучность)."""
    from core.marks import MarksFile, Mark, AuthorType as A
    import random
    random.seed(7)
    marks = []
    # несколько «горячих» моментов: стример + кучка зрителей + модер
    hot_centers = [420, 1180, 2600, 4100, 5200, 6300]
    for i, cen in enumerate(hot_centers):
        marks.append(Mark(cen, A.STREAMER, "egoric", note=[
            "рофл про кота", "", "эпик клатч", "", "разнос в чате", "фейл века"][i % 6]))
        for k in range(random.randint(2, 8)):
            marks.append(Mark(cen + random.uniform(-8, 6), A.VIEWER, f"viewer{k}"))
        if i % 2 == 0:
            marks.append(Mark(cen + random.uniform(-4, 4), A.MODERATOR, "mod_kolya"))
        if i % 3 == 0:
            marks.append(Mark(cen + random.uniform(-3, 3), A.VIP, "vip_dima"))
    # одиночные зрительские метки-шум (в 'большом' не пройдут)
    for _ in range(20):
        marks.append(Mark(random.uniform(60, 7000), A.VIEWER, f"rnd{random.randint(0,999)}"))
    return MarksFile(platform="twitch", streamer="egoric", broadcast_id="998877",
                     duration=7200, online=180, marks=marks)


def main():
    app = QApplication([])
    for f in ("assets/fonts/PTSans-Regular.ttf", "assets/fonts/PTSans-Bold.ttf"):
        if os.path.isfile(f):
            QFontDatabase.addApplicationFont(f)
    app.setFont(QFont("PT Sans", 10))
    os.makedirs(OUT, exist_ok=True)

    from ui.marks_mode import MarksModePanel
    from ui.theme import build_qss

    mf = _synthetic_marks()

    for theme in ("dark", "light"):
        panel = MarksModePanel(theme)
        panel.setStyleSheet(build_qss(theme))
        panel.apply_theme(theme)        # как это делает главное окно (красит тумблеры/чипы)
        # эмулируем загрузку видео (длительность) без реального файла
        panel._video = "stream_vod.mp4"
        panel._video_dur = 7200.0
        panel.video_lbl.setText("stream_vod.mp4")
        panel.set_marks_file(mf, "egoric_2026-07-19.clipmarks")
        panel.timeline.set_active(1)
        panel._select_moment(1)
        panel.resize(1280, 820)
        panel.show()
        settle(app, 300)
        p = os.path.join(OUT, f"marks_{theme}_1280.png")
        panel.grab().save(p); print("saved", p)
        # вкладка «Раскладка» — ручной редактор зон (финалка)
        panel.tab_layout.setChecked(True); panel.right_stack.setCurrentIndex(1)
        panel.layout_editor.set_mode("final")
        panel.layout_editor._select_zone("Вебка")
        settle(app, 250)
        p = os.path.join(OUT, f"marks_{theme}_layout.png")
        panel.grab().save(p); print("saved", p)
        # назад на моменты, узкий вид
        panel.tab_moments.setChecked(True); panel.right_stack.setCurrentIndex(0)
        panel.resize(1040, 760); settle(app, 200)
        p = os.path.join(OUT, f"marks_{theme}_1040.png")
        panel.grab().save(p); print("saved", p)
        panel.bot_panel.shutdown()      # гасим фоновую проверку входа этой панели

    # smoke: целое окно, переключение в режим 2
    from ui.main_window import MainWindow
    win = MainWindow()
    win.resize(1280, 820)
    win.show(); settle(app, 200)
    win._on_mode_selected(1)          # переключиться на «Метки через бота»
    win.marks_panel._video = "stream_vod.mp4"
    win.marks_panel._video_dur = 7200.0
    win.marks_panel.video_lbl.setText("stream_vod.mp4")
    win.marks_panel.set_marks_file(mf, "egoric.clipmarks")
    settle(app, 250)
    p = os.path.join(OUT, "marks_window_mode2.png")
    win.grab().save(p); print("saved", p)

    # вкладка «Бот» в целом окне (вход не выполнен — состояние по умолчанию)
    win.marks_panel._show_bot_tab()
    win.marks_panel.bot_panel._on_account({})
    settle(app, 250)
    p = os.path.join(OUT, "marks_window_bot.png")
    win.grab().save(p); print("saved", p)
    win.marks_panel.tab_moments.setChecked(True)
    win.marks_panel.right_stack.setCurrentIndex(0)

    # проверим сборку конфигурации рендера (без запуска ffmpeg)
    win.marks_panel._take_top(n=4)
    err = win.marks_panel.validate()
    print("validate():", err)
    # раздельно (дефолт): по конфигу на момент, у каждого своё имя
    sep = win.marks_panel.build_pipeline_configs("out")
    print("раздельно: клипов =", len(sep),
          "| имена:", [os.path.basename(p.export.filename) for p in sep[:3]])
    # склейка в один
    win.marks_panel.export_chips.set_current("Склеить в один")
    one = win.marks_panel.build_pipeline_configs("out")
    print("склейка: конфигов =", len(one), "| сегментов =", len(one[0].segments),
          "| captions:", one[0].captions_enabled, "| nick:", one[0].branding.nickname)

    # Гасим фоновые потоки панели бота: без этого Qt рушит процесс на выходе
    # («QThread: Destroyed while thread is still running»).
    win.marks_panel.bot_panel.shutdown()
    win.close()
    app.processEvents()

    # Контроль: к этому моменту фоновых потоков остаться не должно (иначе Qt рушит
    # процесс на выходе — так мы поймали автозапуск бота на закрытии окна).
    print("живые потоки после закрытия:", _wait_threads() or "нет")
    print("DONE")


if __name__ == "__main__":
    main()
