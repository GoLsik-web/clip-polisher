"""app.py — точка входа GUI «Полировщик клипов».

Запуск: python app.py
"""
from __future__ import annotations

import logging
import os
import sys


def main() -> int:
    # В собранном оконном .exe (console=False) sys.stdout/stderr == None.
    # Любая библиотека, пишущая туда (tqdm в HuggingFace, логгинг), роняет приложение
    # с «'NoneType' object has no attribute 'write'». Подставляем безопасный сток.
    if sys.stderr is None or sys.stdout is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = devnull
        if sys.stdout is None:
            sys.stdout = devnull

    # Кириллица в логах и корректная кодировка на Windows.
    os.environ.setdefault("PYTHONUTF8", "1")
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "huggingface_hub", "huggingface_hub.utils._http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
    except ImportError:
        print("\n[ОШИБКА] Не найден PySide6.\n"
              "Похоже, запущен не тот Python. Запускайте через run.bat "
              "или: .venv\\Scripts\\python.exe app.py\n")
        input("Нажмите Enter для выхода...")
        return 1

    # HiDPI: не мылить на 125–200% масштабах.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Полировщик клипов")

    # Регистрируем PT Sans (на чужом ПК его может не быть — иначе UI на фолбэк-шрифте).
    try:
        from PySide6.QtGui import QFontDatabase, QFont
        from core.resources import res
        loaded = False
        for f in ("assets/fonts/PTSans-Regular.ttf", "assets/fonts/PTSans-Bold.ttf"):
            p = res(f)
            if os.path.isfile(p) and QFontDatabase.addApplicationFont(p) != -1:
                loaded = True
        if loaded:
            app.setFont(QFont("PT Sans", 10))
    except Exception:  # noqa: BLE001
        pass
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
