"""dev/screenshot.py — рендер главного окна в PNG на разных размерах (offscreen).

Для само-проверки вёрстки: запусти и открой out/shots/*.png.
Запуск: .venv\\Scripts\\python.exe dev\\screenshot.py
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

SIZES = [(1920, 1080), (1280, 800), (1000, 700), (820, 900)]
OUT = "out/shots"


def settle(app, ms=250):
    """Дать layout'ам/анимациям устаканиться."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    app.processEvents()


def main():
    app = QApplication([])
    # offscreen-платформа без системных шрифтов → грузим PT Sans, иначе текст «квадратами».
    for f in ("assets/fonts/PTSans-Regular.ttf", "assets/fonts/PTSans-Bold.ttf"):
        if os.path.isfile(f):
            QFontDatabase.addApplicationFont(f)
    app.setFont(QFont("PT Sans", 10))

    from ui.main_window import MainWindow
    os.makedirs(OUT, exist_ok=True)

    win = MainWindow()
    win.show()

    # Загрузим реальный кадр-источник в редактор, если есть.
    if os.path.isfile("out/src1.png"):
        win.editor.set_source_frame("out/src1.png")

    # Заполним пару полей для реалистичности.
    win.input_edit.setText("tests/sample_clips/3100328498.mp4")
    win.nick_edit.setText("eg0rl1ke")

    variants = []
    for step in (0, 3):  # шаг 1 (Вход) и шаг 4 (Субтитры — много контролов)
        win.wizard.set_step(step)
        settle(app, 650)   # дать аккордеону доиграть до снимка
        for w, h in SIZES:
            win.resize(w, h)
            settle(app, 320)
            path = os.path.join(OUT, f"step{step+1}_{w}x{h}.png")
            win.grab().save(path)
            variants.append(path)
            print("saved", path)

    # 9:16 Финалка — редактор свободной композиции.
    win.wizard.set_step(1)   # шаг Раскладка
    win.editor.set_mode("final")
    win.editor._select_zone("Вебка")   # выбрана вебка → у неё ручки
    settle(app, 300)
    for w, h in ((1280, 800), (1000, 700)):
        win.resize(w, h); settle(app, 300)
        win.grab().save(os.path.join(OUT, f"final_{w}x{h}.png"))
        print("saved", os.path.join(OUT, f"final_{w}x{h}.png"))
    win.editor.set_mode("source")

    # Светлая тема — проверка контраста.
    win.wizard.set_step(0)
    win._toggle_theme()
    settle(app, 400)
    win.resize(1280, 800)
    settle(app, 300)
    win.grab().save(os.path.join(OUT, "light_1280x800.png"))
    print("saved", os.path.join(OUT, "light_1280x800.png"))

    # Меню режимов (гамбургер) поверх затемнения.
    win._toggle_theme()  # обратно в тёмную
    win.resize(1280, 800); settle(app, 200)
    from PySide6.QtCore import QRect
    win._open_mode_menu()
    settle(app, 60)
    for tv in (0.25, 0.5, 0.75, 1.0):
        win.mode_menu._anim.stop()
        win.mode_menu.set_morph(tv)
        settle(app, 80)
        win.grab().save(os.path.join(OUT, f"morph_{int(tv*100)}.png"))
        print("saved", os.path.join(OUT, f"morph_{int(tv*100)}.png"))
    win.mode_menu.hide()

    # Загрузчик — бургер на середине сборки.
    for frac in (0.35, 0.7):
        win.loader.setGeometry(win.centralWidget().rect())
        win.loader.start(); win.loader.show(); win.loader.raise_()
        win.loader.set_progress(frac, "Рендер")
        settle(app, 200)
        win.grab().save(os.path.join(OUT, f"loader_{int(frac*100)}.png"))
        print("saved", os.path.join(OUT, f"loader_{int(frac*100)}.png"))
    win.loader.stop()

    print("DONE:", len(variants), "shots")


if __name__ == "__main__":
    main()
