"""scan_link.py — проверялка автопоиска: вставил ссылку → получил моменты.

Запуск для не-программиста: двойной клик по `Проверить-стрим.bat` в корне проекта.
Из консоли:

    .venv\\Scripts\\python.exe -m dev.scan_link https://www.twitch.tv/videos/2829614149
    .venv\\Scripts\\python.exe -m dev.scan_link buster --strict 80
    .venv\\Scripts\\python.exe -m dev.scan_link ССЫЛКА --no-audio         (как в блоке 1)
    .venv\\Scripts\\python.exe -m dev.scan_link ССЫЛКА --speech skip      (без речи)
    .venv\\Scripts\\python.exe -m dev.scan_link ССЫЛКА --file D:\\stream.mp4  (запись на диске)

Что делает: спрашивает у Twitch клипы зрителей, подмешивает метки и журнал чата с этого
эфира, слушает звук (громкость, тишина→взрыв, смех), а потом распознаёт речь ТОЛЬКО в
местах-кандидатах. Видео не качается вообще: по ссылке берётся только звуковая дорожка.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.clipscan import fmt_time                     # noqa: E402
from core.media import MediaError, cache_size_mb       # noqa: E402
from core.scanner import TwitchError, default_scan_path, scan_link   # noqa: E402
from core.speech import MODELS, SpeechPlan             # noqa: E402
from core.twitch_auth import ensure_token              # noqa: E402


def _parse(argv: list[str]) -> dict:
    opts = {"link": "", "strict": 50.0, "audio": True, "speech": "auto",
            "file": "", "yes": False}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else ""
        if a in ("--strict", "-s") and nxt:
            try:
                opts["strict"] = float(nxt)
            except ValueError:
                pass
            i += 2
        elif a == "--no-audio":
            opts["audio"] = False
            i += 1
        elif a == "--speech" and nxt:
            opts["speech"] = nxt
            i += 2
        elif a == "--file" and nxt:
            opts["file"] = nxt
            i += 2
        elif a in ("--yes", "-y"):
            opts["yes"] = True
            i += 1
        else:
            rest.append(a)
            i += 1
    if rest:
        opts["link"] = rest[0]
    return opts


def _ask_plan(plan: SpeechPlan, auto_yes: bool):
    """Спросить человека, если распознавание речи выйдет долгим."""
    if plan.skip or not plan.long or auto_yes or not sys.stdin.isatty():
        return plan
    print()
    print("  " + plan.human())
    print("  " + plan.advice())
    print("  Что делаем?")
    print("    1 — подождать (как есть)")
    for n, (name, title, _g, _c) in enumerate(MODELS, start=2):
        if name != plan.model:
            print(f"    {n} — взять модель «{name}» ({title.lower()})")
    print("    0 — пропустить речь (моменты найдутся, но без цитат)")
    try:
        choice = input("  Номер: ").strip()
    except (EOFError, KeyboardInterrupt):
        return plan
    if choice == "0":
        return None
    if choice.isdigit() and 2 <= int(choice) <= len(MODELS) + 1:
        from core.speech import with_model
        newp = with_model(plan, MODELS[int(choice) - 2][0])
        print("  " + newp.human())
        return newp
    return plan


def _print_result(res, strict: float) -> None:
    """Общий вывод для разбора по ссылке и разбора локальной записи."""
    scan = res.scan
    moments = scan.moments
    if not moments:
        print("\nМоментов не нашлось.")
    else:
        print(f"\nНашлось {len(moments)} из {scan.possible()} возможных "
              f"(строгость {strict:.0f} из 100):\n")
    for i, m in enumerate(moments, 1):
        star = " ★" if m.gold else ""
        name = f" — {m.label}" if m.label else ""
        print(f"{i}. {fmt_time(m.start)} … {fmt_time(m.end)}{star}{name}")
        print(f"   почему: {m.why()}")
        print(f"   очки: {m.score:.1f}")
        print()
    if any(m.gold for m in moments):
        print("★ — за момент проголосовали разные по природе улики (почти не ошибка).")

    counts: dict[str, int] = {}
    for s in scan.signals:
        counts[s.kind] = counts.get(s.kind, 0) + 1
    if counts:
        names = {"viewer_clip": "клипы зрителей", "chat_spike": "взрывы чата",
                 "chat_mark": "метки из чата", "loud": "громкость",
                 "laugh": "смех", "speech": "эмоции в речи"}
        print("Улики: " + ", ".join(f"{names.get(k, k)} — {v}" for k, v in counts.items()))
    for note in scan.notes:
        print(f"! {note}")


def _scan_local(o: dict) -> int:
    """Разбор записи с диска — ни Twitch, ни сети."""
    from core.scanner import scan_file
    print(f"Разбираю запись с диска: {o['file']}\n")
    try:
        res = scan_file(o["file"], strictness=o["strict"], speech=o["speech"],
                        on_plan=lambda p: _ask_plan(p, o["yes"]),
                        progress=lambda s: print("  " + s))
    except MediaError as e:
        print(f"\nНе получилось: {e}")
        return 3
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 4
    print()
    print("=" * 70)
    print(f"Запись: {res.vod.title} · длина: {fmt_time(res.vod.duration)}")
    print("=" * 70)
    _print_result(res, o["strict"])
    path = default_scan_path(res.vod)
    try:
        res.scan.to_json(path)
        print(f"\nРазбор сохранён: {path}")
    except OSError as e:
        print(f"\nНе смог сохранить разбор: {e}")
    return 0


def main(argv: list[str]) -> int:
    o = _parse(argv)

    # Только файл, без ссылки — разбор вообще без интернета.
    if o["file"] and not o["link"]:
        return _scan_local(o)

    if not o["link"]:
        print("Вставь ссылку на запись стрима (или просто ник канала) и нажми Enter.")
        print("Например: https://www.twitch.tv/videos/2829614149")
        try:
            o["link"] = input("Ссылка: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not o["link"]:
        print("Ссылка не введена — нечего разбирать.")
        return 1

    token = ensure_token()
    if not token:
        print("Ты ещё не вошёл в Twitch. Открой приложение → вкладка «Бот» → "
              "«Войти через Twitch», потом запусти проверку снова.")
        return 2

    print()
    try:
        res = scan_link(o["link"], token, strictness=o["strict"],
                        audio=o["audio"], speech=o["speech"], video_path=o["file"],
                        on_plan=lambda p: _ask_plan(p, o["yes"]),
                        progress=lambda s: print("  " + s))
    except (TwitchError, MediaError) as e:
        print(f"\nНе получилось: {e}")
        return 3
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 4

    vod = res.vod
    print()
    print("=" * 70)
    print(f"Стрим: {vod.title or '(без названия)'}")
    print(f"Канал: {vod.channel} · длина записи: {fmt_time(vod.duration)} · "
          f"начало: {vod.created_at}")
    print("=" * 70)

    _print_result(res, o["strict"])

    path = default_scan_path(vod)
    try:
        res.scan.to_json(path)
        print(f"\nРазбор сохранён: {path}")
        print("Строгость можно менять без интернета — файл уже содержит все улики.")
    except OSError as e:
        print(f"\nНе смог сохранить разбор: {e}")
    mb = cache_size_mb()
    if mb > 1:
        print(f"В кэше звука: {mb:.0f} МБ (повторный разбор этого стрима будет мгновенным).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
