"""Снимок мульти-редактора: лента клипов слева + редактор активного клипа."""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PYTHONUTF8"] = "1"
os.environ["CLIP_SKIP_UPDATE"] = "1"
os.environ["CLIP_SKIP_PROVISION"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtCore import Qt, QTimer, QEventLoop
from PySide6.QtGui import QGuiApplication, QFontDatabase, QFont

QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def settle(app, ms=300):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec(); app.processEvents()


def main():
    app = QApplication([])
    for f in ("assets/fonts/PTSans-Regular.ttf", "assets/fonts/PTSans-Bold.ttf"):
        if os.path.isfile(f):
            QFontDatabase.addApplicationFont(f)
    app.setFont(QFont("PT Sans", 10))

    from ui.main_window import MainWindow
    os.makedirs("out/shots", exist_ok=True)

    clips = [os.path.abspath("tests/sample_clips/3100328498.mp4"),
             os.path.abspath("tests/sample_clips/3173737928.mp4")]
    # подменяем диалог выбора файлов
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: (clips, ""))

    win = MainWindow(); win.resize(1400, 860); win.show()
    settle(app, 400)

    # включаем мульти-редактор и добавляем клипы
    win.batch_many.setChecked(True)
    win._set_batch_mode(True)
    win._add_clips()
    win.wizard.set_step(1)   # шаг «Раскладка» — виден редактор
    win.editor.set_mode("final")
    settle(app, 600)

    # пометим статусы для наглядности (как во время рендера)
    win._clips[0].status = "done"; win._clips[0].frac = 1.0
    win._clips[1].status = "processing"; win._clips[1].frac = 0.42
    win.clip_strip.refresh_all()
    settle(app, 200)

    for w, h in ((1400, 860), (1120, 760)):
        win.resize(w, h); settle(app, 300)
        path = f"out/shots/multi_{w}x{h}.png"
        win.grab().save(path); print("saved", path)

    # светлая тема
    win._toggle_theme(); settle(app, 300)
    win.grab().save("out/shots/multi_light.png"); print("saved out/shots/multi_light.png")


if __name__ == "__main__":
    main()
