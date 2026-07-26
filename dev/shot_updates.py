"""Быстрый офскрин-снимок окна обновлений в тёмной и светлой теме (проверка стиля)."""
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


def settle(app, ms=200):
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

    from ui.updates_dialog import UpdatesDialog
    os.makedirs("out/shots", exist_ok=True)

    info = {"version": "1.0.5", "notes": (
        "## Что нового в v1.0.5\n\n"
        "- **Яркие субтитры** с подсветкой ключевых слов (MrBeast-стиль)\n"
        "- **Safe-zone** оверлеи под TikTok / Shorts / Reels\n"
        "- **Профили стримеров** — зоны/ник/платформа под конкретного стримера\n"
        "- Починили стиль этого самого окна обновлений")}

    for theme in ("dark", "light"):
        dlg = UpdatesDialog("1.0.4", theme=theme)
        dlg.resize(480, 380)
        dlg.set_state("update", info)
        settle(app, 150)
        path = f"out/shots/updates_{theme}.png"
        dlg.grab().save(path)
        print("saved", path)


if __name__ == "__main__":
    main()
