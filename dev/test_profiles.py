"""Функциональный тест профилей: собрать → сохранить → применить, проверить круг."""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PYTHONUTF8"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import Qt

QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def main():
    app = QApplication([])
    from ui.main_window import MainWindow
    from core import profiles as pf

    # Изолируем тест: пишем профили во временную папку.
    tmp = os.path.join(os.environ.get("TEMP", "."), "clip_polisher_test_profiles")
    os.makedirs(tmp, exist_ok=True)
    pf.profiles_dir = lambda: tmp
    pf.profiles_path = lambda: os.path.join(tmp, "profiles.json")
    if os.path.isfile(pf.profiles_path()):
        os.remove(pf.profiles_path())

    win = MainWindow()

    # Настроим состояние: ник, платформа, safe-зона, двинем зону вебки.
    win.nick_edit.setText("eg0rl1ke")
    win.platform_chips.set_current("Twitch")
    win.editor.set_safezone("tiktok")
    comp = win.editor.get_composition()
    comp.webcam.x = 0.123   # характерное значение для проверки
    comp.nick.y = 0.045

    data = win._collect_profile()
    assert data["nickname"] == "eg0rl1ke", data
    assert data["platform"] == "twitch", data
    assert data["safezone"] == "tiktok", data
    assert abs(data["composition"]["webcam"]["x"] - 0.123) < 1e-6, data["composition"]["webcam"]
    pf.save("eg0rl1ke", data)
    print("SAVE OK:", pf.names())

    # Сбросим состояние и применим профиль обратно.
    win.nick_edit.setText("")
    win.platform_chips.set_current("Без значка")
    win.editor.set_safezone(None)
    loaded = pf.get("eg0rl1ke")
    win._apply_profile(loaded)

    assert win.nick_edit.text() == "eg0rl1ke", win.nick_edit.text()
    assert win.platform_chips.current() == "Twitch", win.platform_chips.current()
    assert win.editor.get_safezone_key() == "tiktok", win.editor.get_safezone_key()
    c2 = win.editor.get_composition()
    assert abs(c2.webcam.x - 0.123) < 1e-6, c2.webcam.x
    assert abs(c2.nick.y - 0.045) < 1e-6, c2.nick.y
    print("APPLY OK: ник/платформа/safe-зона/зоны восстановлены")

    # Удаление
    assert pf.delete("eg0rl1ke") is True
    assert "eg0rl1ke" not in pf.names()
    print("DELETE OK")
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
