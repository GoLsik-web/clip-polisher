"""dev/shot_editor_eyes.py — снимок легенды редактора с глаз-выключателями панелей.

Проверяет новый контрол видимости (Этап 1, все режимы). Запуск:
.venv\\Scripts\\python.exe -m dev.shot_editor_eyes
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
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec(); app.processEvents()


def main():
    app = QApplication([])
    for f in ("assets/fonts/PTSans-Regular.ttf", "assets/fonts/PTSans-Bold.ttf"):
        if os.path.isfile(f):
            QFontDatabase.addApplicationFont(f)
    app.setFont(QFont("PT Sans", 10))
    os.makedirs(OUT, exist_ok=True)

    from ui.preview_panel import EditorPanel
    from ui.theme import build_qss

    for theme in ("dark", "light"):
        ed = EditorPanel()
        ed.setStyleSheet(build_qss(theme))
        ed.set_theme(theme)
        if os.path.isfile("out/src1.png"):
            ed.set_source_frame("out/src1.png")
        ed.set_mode("final")            # глаза активны на финалке
        ed._select_zone("Вебка")
        # выключим «Лого платформы» — покажем перечёркнутый глаз + исчезновение зоны
        ed._eye_btns["Платформа"].setChecked(False)
        ed.resize(760, 720)
        ed.show(); settle(app, 300)
        p = os.path.join(OUT, f"editor_eyes_{theme}.png")
        ed.grab().save(p); print("saved", p)
        # проверим, что видимость реально записалась в композицию
        comp = ed.get_composition()
        print(f"  [{theme}] platform.visible =", comp.platform.visible,
              "| webcam.visible =", comp.webcam.visible)

    print("DONE")


if __name__ == "__main__":
    main()
