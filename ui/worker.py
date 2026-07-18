"""worker.py — запуск тяжёлых операций core в фоновом потоке (QThread),
чтобы интерфейс не зависал. Прогресс и результат приходят через сигналы.

core-логика не знает про Qt: воркер лишь оборачивает вызовы core.pipeline /
core.preview и транслирует прогресс в сигналы Qt.
"""
from __future__ import annotations

import traceback
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal


class PipelineWorker(QObject):
    """Выполняет функцию в потоке и шлёт сигналы прогресса/результата.

    Использование: создать worker, переместить в QThread, соединить сигналы.
    Здесь для простоты — самодостаточный QThread-наследник ниже (RenderThread).
    """
    progress = Signal(float, str)   # доля 0..1, текст стадии
    finished = Signal(str)          # путь к результату
    failed = Signal(str)            # текст ошибки


class RenderThread(QThread):
    """Поток рендера полного клипа через core.pipeline.run."""
    progress = Signal(float, str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, pipeline_config, parent=None):
        super().__init__(parent)
        self._pcfg = pipeline_config

    def run(self) -> None:
        try:
            from core import pipeline
            def cb(frac: float, stage: str) -> None:
                self.progress.emit(frac, stage)
            out = pipeline.run(self._pcfg, on_progress=cb)
            self.finished_ok.emit(out)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


class BatchRenderThread(QThread):
    """Поток рендера пачкой через core.pipeline.run_batch."""
    progress = Signal(float, str)
    finished_ok = Signal(list)
    failed = Signal(str)

    def __init__(self, pipeline_configs: list, parent=None):
        super().__init__(parent)
        self._pcfgs = pipeline_configs

    def run(self) -> None:
        try:
            from core import pipeline
            def cb(frac: float, stage: str) -> None:
                self.progress.emit(frac, stage)
            outs = pipeline.run_batch(self._pcfgs, on_progress=cb)
            self.finished_ok.emit(outs)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


class PreviewThread(QThread):
    """Поток генерации стоп-кадра превью (core.preview.freeze_preview)."""
    finished_ok = Signal(str)     # путь к PNG
    failed = Signal(str)

    def __init__(self, input_path, layout, out_png, at_time, branding, ass_path,
                 canvas_w, canvas_h, composition=None, parent=None):
        super().__init__(parent)
        self._args = (input_path, layout, out_png, at_time, branding, ass_path,
                      canvas_w, canvas_h, composition)

    def run(self) -> None:
        try:
            from core import preview
            (input_path, layout, out_png, at_time, branding, ass_path,
             canvas_w, canvas_h, composition) = self._args
            path = preview.freeze_preview(
                input_path, layout, out_png=out_png, at_time=at_time,
                canvas_w=canvas_w, canvas_h=canvas_h,
                branding=branding, ass_path=ass_path, composition=composition)
            self.finished_ok.emit(path)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


class UpdateCheckThread(QThread):
    """Фоновая проверка нового релиза на GitHub (тихо, без блокировки UI)."""
    found = Signal(dict)     # {version, url, size}
    none = Signal()

    def run(self) -> None:
        try:
            from core import updater
            info = updater.check_for_update()
            if info:
                self.found.emit(info)
            else:
                self.none.emit()
        except Exception:  # noqa: BLE001 — нет сети/лимит API → просто молчим
            self.none.emit()


class UpdateDownloadThread(QThread):
    """Скачивание установщика обновления с прогрессом."""
    progress = Signal(float, str)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, url: str, dst: str, parent=None):
        super().__init__(parent)
        self._url = url
        self._dst = dst

    def run(self) -> None:
        try:
            from core import updater
            def cb(got, total):
                if total:
                    self.progress.emit(got / total,
                                       f"Скачиваю обновление {got // (1<<20)}/{total // (1<<20)} МБ")
            updater.download_installer(self._url, self._dst, cb)
            self.finished_ok.emit(self._dst)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class ProvisionThread(QThread):
    """Первый запуск: докачка модели Whisper (+CUDA при NVIDIA) в папку пользователя."""
    progress = Signal(float, str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, want_gpu: bool = True, parent=None):
        super().__init__(parent)
        self._want_gpu = want_gpu

    def run(self) -> None:
        try:
            from core import provision
            provision.ensure_runtime(
                on_progress=lambda f, s: self.progress.emit(f, s),
                want_gpu=self._want_gpu)
            self.finished_ok.emit()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


class FilmstripThread(QThread):
    """Поток сборки киноленты стоп-кадров для таймлайна обрезки."""
    finished_ok = Signal(str)     # путь к PNG-киноленте
    failed = Signal(str)

    def __init__(self, input_path: str, out_png: str, duration: float = 0.0,
                 n: int = 14, parent=None):
        super().__init__(parent)
        self._args = (input_path, out_png, duration, n)

    def run(self) -> None:
        try:
            from core import ffmpeg_utils as ff
            input_path, out_png, duration, n = self._args
            path = ff.make_filmstrip(input_path, out_png, n=n, duration=duration)
            self.finished_ok.emit(path)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class IngestThread(QThread):
    """Поток скачивания/приёма входа (core.ingest.ingest)."""
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, source: str, dest_dir: str = "tests/sample_clips", parent=None):
        super().__init__(parent)
        self._source = source
        self._dest = dest_dir

    def run(self) -> None:
        try:
            from core import ingest
            path = ingest.ingest(self._source, dest_dir=self._dest)
            self.finished_ok.emit(path)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")
