"""Снимок топбара с пилюлей версии: актуально и «есть обнова», тёмная/светлая."""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PYTHONUTF8"] = "1"
os.environ["CLIP_SKIP_UPDATE"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer, QEventLoop
from PySide6.QtGui import QGuiApplication, QFontDatabase, QFont

QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def settle(app, ms=250):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec(); app.processEvents()


def main():
    app = QApplication([])
    for f in ("assets/fonts/PTSans-Regular.ttf", "assets/fonts/PTSans-Bold.ttf"):
        if os.path.isfile(f):
            QFontDatabase.addApplicationFont(f)
    app.setFont(QFont("PT Sans", 10))

    from ui.main_window import MainWindow
    os.makedirs("out/shots", exist_ok=True)
    win = MainWindow(); win.resize(1280, 800); win.show()
    settle(app, 300)

    for theme in ("dark", "light"):
        if win._theme != theme:
            win._toggle_theme(); settle(app, 200)
        for status in ("uptodate", "update"):
            win.version_pill.set_status(status)
            settle(app, 200)
            # снимем верхнюю полосу окна (топбар)
            pix = win.grab().copy(0, 0, 1280, 90)
            path = f"out/shots/pill_{theme}_{status}.png"
            pix.save(path)
            print("saved", path)


if __name__ == "__main__":
    main()
