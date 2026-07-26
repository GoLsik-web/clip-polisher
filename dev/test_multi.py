"""Функц. тест мульти-редактора: очередь строит правильные конфиги на каждый клип."""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PYTHONUTF8"] = "1"
os.environ["CLIP_SKIP_UPDATE"] = "1"
os.environ["CLIP_SKIP_PROVISION"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtGui import QGuiApplication
from PySide6.QtCore import Qt

QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)


def main():
    app = QApplication([])
    import ui.worker as W
    from ui.main_window import MainWindow

    captured = {}

    class FakeBatchThread:
        def __init__(self, pcfgs):
            captured["pcfgs"] = pcfgs
        def start(self):
            pass
        # чтобы _track не падал на .finished.connect
        class _Sig:
            def connect(self, *a, **k):
                pass
        finished = _Sig(); progress = _Sig(); finished_ok = _Sig(); failed = _Sig()
    W.BatchRenderThread = FakeBatchThread

    # обойти проверку готовности модели
    MainWindow._ready_to_render = lambda self: True

    clips = [os.path.abspath("tests/sample_clips/3100328498.mp4"),
             os.path.abspath("tests/sample_clips/3173737928.mp4")]
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: (clips, ""))

    win = MainWindow(); win.resize(1400, 860); win.show()

    win.batch_many.setChecked(True); win._set_batch_mode(True)
    win._add_clips()
    assert len(win._clips) == 2, len(win._clips)

    # шаблон: платформа Twitch, подсветка «Плашка»
    win.platform_chips.set_current("Twitch")
    win.hl_chips.set_current("Плашка")

    # правки клипа 0: ник + обрезка + сдвиг зоны вебки
    win._select_clip(0)
    win.nick_edit.setText("streamer_one")
    win.start_spin.setValue(2.0); win.end_spin.setValue(9.0)
    win.editor.get_composition().webcam.x = 0.321

    # правки клипа 1: другой ник + другая зона
    win._select_clip(1)
    win.nick_edit.setText("streamer_two")
    win.editor.get_composition().webcam.x = 0.654

    # запуск очереди (перехватится FakeBatchThread)
    win._on_render()
    pcfgs = captured.get("pcfgs")
    assert pcfgs and len(pcfgs) == 2, pcfgs

    p0, p1 = pcfgs
    assert p0.source.endswith("3100328498.mp4"), p0.source
    assert p1.source.endswith("3173737928.mp4"), p1.source
    # индивидуальные ники
    assert p0.branding.nickname == "streamer_one", p0.branding.nickname
    assert p1.branding.nickname == "streamer_two", p1.branding.nickname
    # общая платформа из шаблона
    assert p0.branding.platform.value == "twitch" and p1.branding.platform.value == "twitch"
    # индивидуальные зоны
    assert abs(p0.composition.webcam.x - 0.321) < 1e-6, p0.composition.webcam.x
    assert abs(p1.composition.webcam.x - 0.654) < 1e-6, p1.composition.webcam.x
    # индивидуальная обрезка клипа 0
    assert abs(p0.start - 2.0) < 1e-6 and abs(p0.end - 9.0) < 1e-6, (p0.start, p0.end)
    # общий стиль-шаблон: подсветка «box» у обоих
    assert p0.caption_style.highlight_mode == "box" and p1.highlight_keywords
    # разные имена файлов
    assert p0.export.filename != p1.export.filename
    assert p0.export.filename.endswith("_vertical.mp4")
    print("OK: очередь строит по-клипно верные конфиги")
    print(f"  clip0: {os.path.basename(p0.source)} ник={p0.branding.nickname} "
          f"trim={p0.start}-{p0.end} cam.x={p0.composition.webcam.x} -> {p0.export.filename}")
    print(f"  clip1: {os.path.basename(p1.source)} ник={p1.branding.nickname} "
          f"cam.x={p1.composition.webcam.x} -> {p1.export.filename}")
    # В окне живёт панель бота с фоновой проверкой входа — гасим, иначе Qt рушит
    # процесс на выходе (проверки при этом успевают пройти, и падение легко проглядеть).
    try:
        win.marks_panel.bot_panel.shutdown()
    except AttributeError:
        pass
    win.close()
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
