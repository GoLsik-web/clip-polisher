"""Офскрин-снимок редактора: 9:16 финалка с safe-зонами площадки."""
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


def settle(app, ms=250):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def main():
    app = QApplication([])
    for f in ("assets/fonts/PTSans-Regular.ttf", "assets/fonts/PTSans-Bold.ttf"):
        if os.path.isfile(f):
            QFontDatabase.addApplicationFont(f)
    app.setFont(QFont("PT Sans", 10))

    from ui.theme import build_qss
    from ui.preview_panel import EditorPanel
    os.makedirs("out/shots", exist_ok=True)

    ed = EditorPanel()
    ed.setStyleSheet(build_qss("dark"))
    ed.resize(560, 900)
    ed.show()
    settle(app, 300)

    for plat in ("TikTok", "Shorts", "Reels"):
        ed.safezone_chips.set_current(plat)
        ed._on_safezone(plat)
        ed._select_zone("Субтитры")
        settle(app, 250)
        path = f"out/shots/safezone_{plat.lower()}.png"
        ed.grab().save(path)
        print("saved", path)


if __name__ == "__main__":
    main()
