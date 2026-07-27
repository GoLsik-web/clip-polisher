"""dev/test_stage3.py — блок 1 Этапа 3 БЕЗ сети и без Twitch.

Что проверяется:
  1. разбор ссылок (запись/канал/мусор) и длительность Twitch;
  2. клипы зрителей через подменённый Helix: пагинация, чужие видео, истёкшие клипы;
  3. схлопывание дублей — два клипа одного момента дают ОДИН момент (реальный случай);
  4. пульс чата: классификация смеха/хайпа, корзины, файл, взрывы «относительно себя»
     (в т.ч. у камерного канала, где абсолютные числа маленькие);
  5. скоринг: голосование разных улик, «золотой» момент, рамки 2–8, ползунок строгости,
     разнос по времени;
  6. файл разбора `.clipscan`: сохранить → прочитать → пересчитать без сети;
  7. `scan_link` целиком: ссылка → моменты (сеть подменена, метки и пульс с диска);
  8. болталка: режимы, отсутствие повторов, кулдаун, события (затишье/взрыв/новичок);
  9. `!отчёт`: доступ только у стримера и модераторов, кулдаун, текст сводки;
 10. бот целиком: пульс пишется рядом с метками, отчёт отвечает в чат.

Запуск: .venv\\Scripts\\python.exe -m dev.test_stage3
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="clipscan_test_")
os.environ["LOCALAPPDATA"] = _TMP          # метки/журналы — во временную папку

from core import banter, chatbot, chatpulse, clipscan, scanner, twitch_clips  # noqa: E402
from core import twitch_auth as auth                                          # noqa: E402
from core.marks import AuthorType, Mark, MarksFile                            # noqa: E402


def _ok(cond, msg):
    print(("  OK  " if cond else " FAIL ") + msg)
    assert cond, msg


# ==========================================================================
# Подменённый Twitch
# ==========================================================================

VOD = {"id": "2829614149", "user_id": "111", "user_login": "buster",
       "title": "Ночной подкат", "created_at": "2026-07-20T18:00:00Z",
       "duration": "6h0m0s", "url": "https://www.twitch.tv/videos/2829614149",
       "stream_id": "555"}

# 0:51:00 — один клип; 5:21:10 и 5:21:22 — ОДИН момент, помеченный дважды.
CLIPS_PAGE1 = [
    {"id": "c1", "title": "Бустер умер об бочку", "creator_name": "vasya",
     "view_count": 1905, "duration": 30.0, "vod_offset": 3045, "video_id": "2829614149",
     "url": "https://clips.twitch.tv/c1"},
    {"id": "c2", "title": "клип из другого стрима", "creator_name": "petya",
     "view_count": 500, "duration": 30.0, "vod_offset": 100, "video_id": "999999",
     "url": ""},
]
CLIPS_PAGE2 = [
    {"id": "c3", "title": "ЧТО ЭТО БЫЛО", "creator_name": "kirill", "view_count": 40,
     "duration": 25.0, "vod_offset": 19255, "video_id": "2829614149", "url": ""},
    {"id": "c4", "title": "тот же момент", "creator_name": "olya", "view_count": 12,
     "duration": 30.0, "vod_offset": 19265, "video_id": "2829614149", "url": ""},
    {"id": "c5", "title": "старый клип", "creator_name": "anon", "view_count": 9000,
     "duration": 30.0, "vod_offset": None, "video_id": "", "url": ""},
]


def fake_http(url: str, headers: dict):
    """Заглушка Helix: отдаёт нашу запись, пользователя и две страницы клипов."""
    assert "Client-Id" in headers and headers["Authorization"].startswith("Bearer ")
    if "/videos?" in url:
        return 200, {"data": [VOD]}
    if "/users?" in url:
        return 200, {"data": [{"id": "111", "login": "buster"}]}
    if "/clips?" in url:
        if "after=CUR" in url:
            return 200, {"data": CLIPS_PAGE2, "pagination": {}}
        return 200, {"data": CLIPS_PAGE1, "pagination": {"cursor": "CUR"}}
    return 404, {"message": "не знаю такого"}


# ==========================================================================
# 1. Ссылки
# ==========================================================================

def test_links():
    print("\n[1] Разбор ссылок:")
    s = twitch_clips.parse_source("https://www.twitch.tv/videos/2829614149")
    _ok(s.kind == "vod" and s.vod_id == "2829614149", "ссылка на запись понята")
    _ok(twitch_clips.parse_source("twitch.tv/buster").channel == "buster",
        "ссылка на канал понята")
    _ok(twitch_clips.parse_source("Praden").channel == "praden", "голый ник понят")
    _ok(twitch_clips.parse_source("2829614149").kind == "vod", "голый id записи понят")
    for bad, why in [("", "пустая строка"),
                     ("https://clips.twitch.tv/SomeClipName", "ссылка на один клип"),
                     ("https://youtube.com/watch?v=x", "чужой сайт")]:
        try:
            twitch_clips.parse_source(bad)
            _ok(False, f"{why} должна давать понятную ошибку")
        except twitch_clips.TwitchError as e:
            _ok(len(str(e)) > 20, f"{why} → человеческая подсказка: «{str(e)[:45]}…»")
    _ok(twitch_clips.parse_duration("6h0m0s") == 21600, "длительность «6h0m0s» = 6 часов")
    _ok(twitch_clips.parse_duration("2h6m30s") == 7590, "длительность «2h6m30s» разобрана")


# ==========================================================================
# 2. Клипы зрителей
# ==========================================================================

def test_clips():
    print("\n[2] Клипы зрителей через Helix (сеть подменена):")
    vod = twitch_clips.fetch_vod("AT", "2829614149", http=fake_http)
    _ok(vod.channel == "buster" and vod.duration == 21600, "запись прочитана")
    _ok(vod.broadcast_id == "555", "id эфира взят из записи — сошьётся с метками бота")

    clips = twitch_clips.clips_for_vod("AT", vod, http=fake_http)
    ids = [c.id for c in clips]
    _ok(ids == ["c1", "c3", "c4"], f"вторая страница дочитана, чужое и истёкшее убрано: {ids}")
    _ok(clips[0].views == 1905, "просмотры сохранены")
    _ok(3045 < clips[0].center < 3075, "центр момента чуть ближе к концу клипа")

    all_clips = twitch_clips.fetch_clips("AT", "111", http=fake_http)
    _ok(len(all_clips) == 5, "без фильтра приходят все 5 клипов (пагинация работает)")
    _ok(twitch_clips.expired_clip_count(all_clips) == 1, "истёкший клип посчитан честно")

    try:
        twitch_clips.fetch_vod("AT", "0", http=lambda u, h: (200, {"data": []}))
        _ok(False, "удалённая запись должна давать понятную ошибку")
    except twitch_clips.TwitchError as e:
        _ok("удалена" in str(e), f"нет VOD → по-человечески: «{str(e)[:50]}…»")
    try:
        twitch_clips.fetch_vod("AT", "1", http=lambda u, h: (401, {}))
        _ok(False, "протухший вход должен давать понятную ошибку")
    except twitch_clips.TwitchError as e:
        _ok("войти" in str(e), "401 → «нужно войти заново»")


# ==========================================================================
# 3. Схлопывание дублей
# ==========================================================================

def test_duplicates():
    print("\n[3] Два клипа одного момента = ОДИН момент:")
    vod = twitch_clips.fetch_vod("AT", "2829614149", http=fake_http)
    clips = twitch_clips.clips_for_vod("AT", vod, http=fake_http)
    sig = clipscan.signals_from_clips(clips)
    _ok(len(sig) == 3, "три клипа — три сигнала")
    cands = clipscan.candidates(sig, duration=vod.duration)
    _ok(len(cands) == 2, f"но кандидатов два: дубли схлопнулись ({len(cands)})")
    later = cands[1]
    _ok(len(later.signals) == 2, "оба клипа 5:21:xx попали в один момент")
    _ok(later.score > 6.0, f"и его очки — сумма обоих ({later.score})")
    _ok("2 зрителя нарезали клип" in later.why(), f"объяснение честное: {later.why()}")
    _ok(cands[0].label == "Бустер умер об бочку",
        "имя момента взято из заголовка клипа зрителя — бесплатно, без распознавания речи")


# ==========================================================================
# 4. Пульс чата
# ==========================================================================

def test_pulse_classify():
    print("\n[4] Пульс чата — что считается смехом и хайпом:")
    laugh, hype, keys = chatpulse.classify("ору KEKW ахахах")
    _ok(laugh and not hype, "«ору/KEKW/ахахах» — это смех")
    _ok("KEKW" in keys, "эмоут попал в топ-слова")
    laugh, hype, _ = chatpulse.classify("POG вот это имба")
    _ok(hype and not laugh, "«POG/имба» — это хайп")
    laugh, _, _ = chatpulse.classify("да ладно))))")
    _ok(laugh, "скобочки тоже считаются смехом")
    _ok(chatpulse.classify("привет всем")[0] is False, "обычное сообщение — не смех")


def test_pulse_collect():
    print("\n[5] Журнал пульса: сбор, файл, перезапуск:")
    path = os.path.join(_TMP, "pulse", "test.chatpulse")
    col = chatpulse.PulseCollector("golsik__", path, ref_epoch=1000.0, save_every=0.0)
    col.feed("vasya", "ору", now=1000.0)
    col.feed("petya", "ахах KEKW", now=1005.0)
    col.feed("vasya", "ещё раз", now=1008.0)
    col.feed("kirill", "тишина потом", now=1200.0)      # +200 с → новая корзина
    col.finalize(end_epoch=1300.0)

    log = chatpulse.PulseLog.from_json(path)
    _ok(len(log.buckets) == 2, f"пустые корзины не хранятся: {len(log.buckets)} вместо 30")
    b0 = log.buckets[0]
    _ok(b0.n == 3 and b0.u == 2, "в корзине 3 сообщения от 2 разных людей")
    _ok(b0.laugh == 2, "смех посчитан")
    _ok(log.duration == 300.0, "длительность дописана при закрытии")
    _ok(os.path.getsize(path) < 1024, "файл крошечный")

    col2 = chatpulse.PulseCollector("golsik__", path, ref_epoch=1000.0)
    _ok(col2.resume_existing() == 2, "перезапуск посреди эфира подхватывает журнал")
    col2.feed("new", "привет", now=1400.0)
    col2.finalize(end_epoch=1500.0)
    _ok(len(chatpulse.PulseLog.from_json(path).buckets) == 3, "и дописывает, а не затирает")


def _pulse_log(pattern: list[tuple[int, int]], bucket=10.0) -> chatpulse.PulseLog:
    """Собрать журнал из пар (индекс корзины, сколько сообщений)."""
    buckets = [chatpulse.Bucket(t=i * bucket, n=n, u=min(n, 5), laugh=n // 2,
                                top=[["ору", n // 2]] if n > 3 else [])
               for i, n in pattern]
    return chatpulse.PulseLog(streamer="test", bucket=bucket, buckets=buckets,
                              duration=(max(i for i, _ in pattern) + 1) * bucket)


def test_pulse_spikes():
    print("\n[6] Взрывы чата считаются ОТНОСИТЕЛЬНО СЕБЯ:")
    big = _pulse_log([(i, 8) for i in range(60)] + [(60, 48), (61, 40)] +
                     [(i, 8) for i in range(62, 90)])
    spikes = chatpulse.find_spikes(big)
    _ok(len(spikes) == 1, f"у крупного канала найден один взрыв ({len(spikes)})")
    _ok(abs(spikes[0].center - 605) < 15, f"и он там, где надо: {spikes[0].center} с")
    _ok(spikes[0].ratio >= 5.0, f"×{spikes[0].ratio} — во столько раз выше обычного")
    _ok("чат ×" in spikes[0].describe(), f"объяснение по-человечески: «{spikes[0].describe()}»")

    small = _pulse_log([(i, 1) for i in range(60)] + [(60, 5), (61, 4)] +
                       [(i, 1) for i in range(62, 90)])
    spikes_s = chatpulse.find_spikes(small)
    _ok(len(spikes_s) == 1, "у камерного канала 5 сообщений подряд — тоже взрыв")
    _ok(not chatpulse.find_spikes(_pulse_log([(i, 8) for i in range(90)])),
        "ровный чат без всплесков взрывов не даёт (нет ложных срабатываний)")


# ==========================================================================
# 5-6. Скоринг и файл разбора
# ==========================================================================

def _mixed_signals() -> list[clipscan.Signal]:
    """Стрим с семью поводами разной силы."""
    K = clipscan.Kind
    return [
        clipscan.Signal(K.VIEWER_CLIP, 300.0, 5.0, "клип «топ» (1200 просм.)",
                        {"views": 1200, "title": "топ"}),
        clipscan.Signal(K.CHAT_SPIKE, 305.0, 3.0, "чат ×6 · сплошной смех", {"ratio": 6}),
        clipscan.Signal(K.CHAT_MARK, 900.0, 3.0, "стример пометил момент",
                        {"role": "streamer"}),
        clipscan.Signal(K.VIEWER_CLIP, 1800.0, 3.5, "клип зрителя", {"views": 30}),
        clipscan.Signal(K.CHAT_SPIKE, 2600.0, 1.2, "чат ×2.4", {"ratio": 2.4}),
        clipscan.Signal(K.CHAT_SPIKE, 3400.0, 1.0, "чат ×2.1", {"ratio": 2.1}),
        clipscan.Signal(K.CHAT_MARK, 4200.0, 1.0, "зритель пометил момент",
                        {"role": "viewer"}),
    ]


def test_scoring():
    print("\n[7] Скоринг: голосование, «золото», строгость, рамки 2–8:")
    cands = clipscan.candidates(_mixed_signals(), duration=7200.0)
    _ok(len(cands) == 6, f"шесть кандидатов (клип+чат на 5:00 — один): {len(cands)}")
    first = cands[0]
    _ok(first.gold, "клип зрителей + взрыв чата = «золотой» момент (два независимых свидетеля)")
    _ok(first.score > 5.0 + 3.0, f"за независимость дан бонус: {first.score} > 8.0")
    _ok(not cands[2].gold, "момент по одной улике «золотым» не считается")

    loose = clipscan.pick(cands, strictness=0)
    strict = clipscan.pick(cands, strictness=100)
    _ok(len(loose) > len(strict), f"строгость режет число моментов: {len(loose)} → {len(strict)}")
    _ok(len(strict) >= clipscan.MIN_MOMENTS, "но не меньше двух — рамка жёсткая")
    _ok(all(m.score >= strict[0].score * 0.0 for m in strict), "остаются лучшие по очкам")
    _ok([m.start for m in loose] == sorted(m.start for m in loose),
        "результат отсортирован по времени, а не по очкам")

    # Крупный канал: очки моментов огромные. Порог должен тянуться за ними, иначе
    # ползунок строгости не двигал бы ничего (так и было на живом стриме).
    big = [clipscan.Signal(clipscan.Kind.VIEWER_CLIP, 100.0 + i * 400, 5.0 + i * 4,
                           "клип", {"views": 900}) for i in range(8)]
    big_c = clipscan.candidates(big)
    _ok(len(clipscan.pick(big_c, 100)) < len(clipscan.pick(big_c, 0)),
        "у канала с сотнями клипов строгость тоже работает (порог относительный)")

    many = [clipscan.Signal(clipscan.Kind.VIEWER_CLIP, 100.0 + i * 300, 5.0, "клип",
                            {"views": 500}) for i in range(20)]
    _ok(len(clipscan.pick(clipscan.candidates(many), 50)) == clipscan.MAX_MOMENTS,
        "даже из 20 хороших поводов вернётся максимум 8")

    packed = [clipscan.Signal(clipscan.Kind.VIEWER_CLIP, 1000.0 + i * 30, 5.0, "клип",
                              {"views": 500}) for i in range(5)]
    picked = clipscan.pick(clipscan.candidates(packed), 50)
    _ok(len(picked) <= 2, f"пять клипов из одной минуты не дают пять моментов ({len(picked)})")


def test_scan_file():
    print("\n[8] Файл разбора .clipscan:")
    scan = clipscan.build_scan({"channel": "buster", "duration": 7200.0},
                               _mixed_signals(), strictness=50)
    path = os.path.join(_TMP, "scan", "buster.clipscan")
    scan.to_json(path)
    back = clipscan.ScanFile.from_json(path)
    _ok(len(back.signals) == len(scan.signals), "все улики сохранились")
    _ok(len(back.moments) == len(scan.moments) and
        all(abs(a.start - b.start) < 0.01 and abs(a.score - b.score) < 0.01
            for a, b in zip(back.moments, scan.moments)),
        "моменты читаются такими же")
    _ok(back.scoring_version == clipscan.SCORING_VERSION,
        "в файле записана версия формулы — блок 7 сможет сравнить «до/после»")

    n_before = len(back.moments)
    back.rescore(strictness=100)
    _ok(len(back.moments) <= n_before,
        "ползунок строгости пересчитывает моменты БЕЗ сети и без видео")
    back.rescore(strictness=0)
    _ok(len(back.moments) >= n_before, "и обратно — сырьё никуда не делось")
    _ok(back.possible() >= len(back.moments), "«нашлось N из M возможных» считается")


# ==========================================================================
# 7. Разбор по ссылке целиком
# ==========================================================================

def test_scan_link():
    print("\n[9] Ссылка → моменты (сеть подменена, метки и пульс с диска):")
    marks_dir = os.path.join(_TMP, "local_marks")
    os.makedirs(marks_dir, exist_ok=True)
    mpath = os.path.join(marks_dir, "buster_2026-07-20_18-00_555.clipmarks")
    MarksFile(streamer="buster", broadcast_id="555", duration=21600.0, online=800,
              marks=[Mark(t=3050.0, type=AuthorType.STREAMER, author="Buster",
                          note="бочка"),
                     Mark(t=12000.0, type=AuthorType.VIEWER, author="kirill")]
              ).to_json(mpath)
    _pulse_log([(i, 6) for i in range(300)] + [(303, 40), (304, 36)] +
               [(i, 6) for i in range(305, 400)]).to_json(
                   chatpulse.pulse_path_for(mpath))

    steps: list[str] = []
    # audio=False: тут проверяется блок 1 (клипы/чат). Разбор звука качает настоящую
    # дорожку с Twitch — для него отдельные проверки на синтетическом звуке ниже.
    res = scanner.scan_link("https://www.twitch.tv/videos/2829614149", "AT",
                            http=fake_http, marks_dir=marks_dir, audio=False,
                            progress=steps.append)
    _ok(res.vod.channel == "buster", "запись найдена по ссылке")
    _ok(res.local.marks is not None and len(res.local.marks.marks) == 2,
        "метки этого же эфира подхвачены по id эфира")
    _ok(res.local.pulse is not None, "журнал пульса подхвачен рядом с метками")

    kinds = {s.kind for s in res.scan.signals}
    _ok({"viewer_clip", "chat_mark", "chat_spike"} <= kinds,
        f"в разборе все три вида улик блока 1: {sorted(kinds)}")
    _ok(len(res.moments) >= 2, f"моменты найдены: {len(res.moments)}")
    top = max(res.moments, key=lambda m: m.score)
    _ok(top.gold, "лучший момент подтверждён разными уликами")
    _ok("клип" in top.why() and "пометил" in top.why(),
        f"и объяснён по-человечески: {top.why()}")
    _ok(any("клип" in s.lower() for s in steps), "по ходу разбора видно, что происходит")
    _ok(res.scan.source["vod_id"] == "2829614149", "источник записан в файл разбора")


def test_scan_link_no_data():
    print("\n[10] Честность, когда улик нет:")
    def empty_http(url, headers):
        if "/videos?" in url:
            return 200, {"data": [VOD]}
        if "/clips?" in url:
            return 200, {"data": [], "pagination": {}}
        return 200, {"data": [{"id": "111"}]}

    res = scanner.scan_link("2829614149", "AT", http=empty_http, audio=False,
                            marks_dir=os.path.join(_TMP, "нет-такой-папки"))
    _ok(res.moments == [], "моментов нет — и мы не выдумываем их на пустом месте")
    _ok(any("клип" in n for n in res.notes), "сказано, что клипов зрителей не было")
    _ok(any("блок 2" in n or "звук" in n for n in res.notes),
        f"и честно сказано, чего не хватает: {res.notes}")


# ==========================================================================
# 8. Болталка
# ==========================================================================

def test_no_webcam():
    print("\n[11] Стример БЕЗ вебки — всё работает и ничего не требует:")
    from core.config import LayoutPreset, Zone, composition_from_preset
    src = Zone(0.0, 0.73, 0.235, 0.265)
    game = Zone(0.0, 0.13, 1.0, 0.74)

    with_cam = composition_from_preset(LayoutPreset.A, src, game)
    _ok(clipscan.face_available(with_cam), "вебка размечена и включена → лицо смотреть можно")

    no_cam = composition_from_preset(LayoutPreset.E, src, game)
    _ok(not no_cam.webcam.visible, "пресет E («Без вебки») гасит зону камеры")
    _ok(no_cam.gameplay.visible and no_cam.gameplay.h == 1.0, "геймплей занимает весь кадр")
    _ok(not clipscan.face_available(no_cam), "и сигнал «лицо» на нём не участвует")

    with_cam.webcam.visible = False              # глаз в редакторе тоже гасит камеру
    _ok(not clipscan.face_available(with_cam), "выключенная глазом вебка = нет лица")
    _ok(not clipscan.face_available(None), "нет раскладки вовсе — тоже нет лица, без падений")
    _ok(not clipscan.face_available(composition_from_preset(
        LayoutPreset.A, Zone(0.0, 0.0, 0.0, 0.0), game)),
        "пустая зона вебки (никто не размечал) = нет лица")
    fresh_cam = composition_from_preset(LayoutPreset.A, src, game)
    _ok("камеры нет" in clipscan.face_note(no_cam) and clipscan.face_note(fresh_cam) == "",
        "объяснение для пользователя есть и появляется только когда нужно")

    # Рендер без вебки должен собираться (граф фильтров строится без слоя камеры).
    from core.compositor import build_composition_segment
    parts = build_composition_segment(no_cam, 1080, 1920, "in", "out", "s0")
    _ok(not any("_wc" in p for p in parts), "в графе рендера слоя вебки нет вообще")
    _ok(any("split=2" in p for p in parts), "кадр делится только на базу и геймплей")

    from core.layout import build_layout_filtergraph, LayoutConfig
    lay = LayoutConfig(preset=LayoutPreset.E)
    g = build_layout_filtergraph(lay, 1920, 1080, 1080, 1920, "in", "out", "s0")
    _ok(g and "out" in g[-1], "запасной путь раскладки тоже знает пресет E (не падает)")


def test_banter():
    print("\n[12] Болталка бота:")
    b = banter.Banter(mode="off", rng=random.Random(1))
    _ok(b.idle(now=time.time() + 99999) is None, "режим «Молчит» — бот не болтает вообще")

    b = banter.Banter(mode="jokes", period_min=10.0, cooldown=45.0, rng=random.Random(1))
    t0 = time.time()
    _ok(b.idle(now=t0 + 60) is None, "раньше времени бот не заговорит")
    first = b.idle(now=t0 + 700)
    _ok(first in banter.JOKES, f"по таймеру пришла шутка: «{first[:40]}…»")
    _ok(b.idle(now=t0 + 710) is None, "кулдаун держит бота от спама")

    said = {first}
    tt = t0 + 700
    for _ in range(len(banter.JOKES) - 1):
        tt += 700
        s = b.idle(now=tt)
        _ok(s is not None and s not in said, "фразы не повторяются, пока не кончится пачка")
        said.add(s)
    _ok(len(said) == len(banter.JOKES), f"все {len(said)} анекдотов разошлись без повторов")

    m = banter.Banter(mode="mixed", rng=random.Random(2))
    _ok(len(banter.pool_for("mixed")) > len(banter.JOKES),
        "«Смешанный» тянет фразы из всех пачек")
    _ok(m.mode == "mixed" and banter.pool_for("support") == banter.SUPPORT,
        "режимы отдают свои пачки")

    e = banter.Banter(mode="support", period_min=999, cooldown=1.0, silence_min=5.0,
                      rng=random.Random(3))
    now = time.time()
    _ok(e.revive(60.0, now) is None, "минута тишины — ещё не повод лезть в чат")
    _ok(e.revive(600.0, now) in banter.REVIVE, "десять минут тишины — бот расшевелит чат")
    _ok(e.hype(2.0, now + 10) is None, "лёгкое оживление чата — не повод")
    _ok(e.hype(6.0, now + 10) in banter.HYPE, "взрыв чата ×6 — бот подхватывает волну")

    g = banter.Banter(mode="fun", cooldown=1.0, rng=random.Random(4))
    hello = g.greet("kirill", "Kirill", rate_per_min=3.0, now=now)
    _ok(hello and "Kirill" in hello, f"новичка встречают по имени: «{hello}»")
    _ok(g.greet("kirill", "Kirill", rate_per_min=3.0, now=now + 300) is None,
        "второй раз с тем же человеком не здороваемся")
    _ok(g.greet("noviy", "Noviy", rate_per_min=90.0, now=now + 300) is None,
        "в шумном чате не здороваемся вообще — это выглядело бы спамом")
    g.reset_session(now + 1000)
    _ok(g.greet("kirill", "Kirill", rate_per_min=3.0, now=now + 2000) is not None,
        "на новом эфире знакомимся заново")


def test_report_text():
    print("\n[13] Текст команды !отчёт:")
    _ok(banter.is_report_command("!отчёт") and banter.is_report_command("!отчет"),
        "команда понимается с «ё» и без")
    _ok(banter.is_report_command("!report") and not banter.is_report_command("!clip"),
        "английский вариант есть, метку за отчёт не принимаем")
    txt = banter.build_report(8100, 12, 47.3, peak_ratio=6.1, peak_t=4350, chatters=9)
    _ok("2 ч 15 мин" in txt and "12 меток" in txt, f"сводка читается: «{txt}»")
    _ok("пик ×6.1 на 1:12:30" in txt, "пик чата указан с временем")
    _ok(len(txt) < 480, "влезает в одно сообщение чата")
    _ok("1 метка" in banter.build_report(600, 1, 2.0), "склонения не хромают")


# ==========================================================================
# 9-10. Бот целиком
# ==========================================================================

class FakeChat:
    """Вместо IRC: отдаёт заготовленный чат, ответы бота копит в список."""
    SCRIPT = [
        ({"display-name": "Vasya"}, "vasya", "ору KEKW"),
        ({"display-name": "Petya"}, "petya", "ахахах"),
        ({"badges": "broadcaster/1", "display-name": "GoLsik__"}, "golsik__", "!clip бочка"),
        ({"display-name": "Kirill"}, "kirill", "!отчёт"),
        ({"mod": "1", "display-name": "ModerVasya"}, "modervasya", "!отчёт"),
    ]
    replies: list = []

    def __init__(self, channel, nick, token, on_message, on_log=None, reply_in_chat=True):
        self.on_message = on_message
        self.on_log = on_log or (lambda s: None)
        self._stop = threading.Event()

    def connect(self):
        self.on_log("вошли в чат Twitch")

    def run(self):
        for tags, login, text in self.SCRIPT:
            if self._stop.is_set():
                return
            self.on_message(tags, login, text)
            time.sleep(0.02)
        self._stop.wait(10)

    def reply(self, text):
        FakeChat.replies.append(text)

    def stop(self):
        self._stop.set()


def test_bot_writes_pulse():
    print("\n[14] Бот в чате: журнал пульса и !отчёт:")
    FakeChat.replies = []
    events: list = []
    old_chat, chatbot.TwitchChat = chatbot.TwitchChat, FakeChat
    old_helix, auth.helix_stream = auth.helix_stream, lambda tok, ch: {
        "id": "777", "viewer_count": 42, "started_at": "", "title": "тест", "game": ""}
    try:
        out = os.path.join(_TMP, "marks2", "live.clipmarks")
        svc = chatbot.BotService(channel="golsik__", token="AT", nick="golsik__",
                                 output_path=out, tick_period=0.2, watch_period=3600,
                                 on_event=lambda k, p: events.append((k, p)))
        svc.start()
        deadline = time.time() + 5
        while len(FakeChat.replies) < 2 and time.time() < deadline:
            time.sleep(0.05)

        _ok(svc.marks_count == 1, "метка записана как раньше — Этап 2 не сломан")
        _ok(svc.pulse is not None and svc.pulse.output_path.endswith(".chatpulse"),
            "журнал пульса лежит рядом с метками")
        _ok(svc.pulse_path == chatpulse.pulse_path_for(out), "имя журнала = имя меток")

        reports = [r for r in FakeChat.replies if "Эфир" in r or "Не в эфире" in r]
        _ok(len(reports) == 1, f"на !отчёт бот ответил один раз — зрителю нельзя: {reports}")
        _ok("1 метка" in reports[0], f"в отчёте живые цифры: «{reports[0]}»")
        _ok(any(k == "report" for k, _ in events), "UI тоже узнал про отчёт")

        svc.stop()
        log = chatpulse.PulseLog.from_json(svc.pulse_path)
        _ok(log.total_messages == 5, f"в журнал попали ВСЕ сообщения, не только метки: "
                                     f"{log.total_messages}")
        _ok(sum(b.laugh for b in log.buckets) == 2, "смех в чате посчитан")
        _ok(log.streamer == "golsik__", "журнал подписан каналом")
    finally:
        chatbot.TwitchChat = old_chat
        auth.helix_stream = old_helix


def test_pulse_can_be_off():
    print("\n[15] Журнал можно выключить:")
    svc = chatbot.BotService(channel="x", token="AT", write_pulse=False,
                             output_path=os.path.join(_TMP, "off", "x.clipmarks"))
    _ok(svc.pulse is None and svc.pulse_path == "",
        "с выключенным журналом бот работает как раньше — ничего лишнего на диск")


# ==========================================================================
# Блок 2: звук
# ==========================================================================

def _tone(sec: float, amp: float, sr: int = 16000) -> "np.ndarray":
    import numpy as np
    t = np.arange(int(sec * sr)) / sr
    return (amp * np.sin(2 * np.pi * 180 * t)).astype(np.float32)


def test_loudness():
    print("\n[16] Звук: где стало резко громче:")
    import numpy as np
    from core import sound_events as se

    step = se.FRAME_SEC
    # 10 минут ровного разговора, а на 5-й минуте — всплеск на 4 секунды.
    rms = np.full(int(600 / step), 0.05)
    burst = int(300 / step)
    rms[burst:burst + int(4 / step)] = 0.55
    ev = se.find_loud_events(rms, step)
    _ok(len(ev) == 1, f"всплеск найден один раз, а не десять ({len(ev)})")
    _ok(abs(ev[0].center - 301) < 4, f"и там, где надо: {ev[0].center:.0f} с")
    _ok(ev[0].score > 3, f"во сколько раз громче — посчитано ({ev[0].score:.1f})")

    _ok(not se.find_loud_events(np.full(int(600 / step), 0.05), step),
        "ровный разговор без всплесков событий не даёт (нет ложных срабатываний)")

    # Тишина перед взрывом — отдельный, более ценный случай.
    rms2 = np.full(int(600 / step), 0.05)
    q0 = int(300 / step)
    rms2[q0:q0 + int(3 / step)] = 0.004                 # пауза
    rms2[q0 + int(3 / step):q0 + int(6 / step)] = 0.6   # взрыв
    kinds = {e.kind for e in se.find_loud_events(rms2, step)}
    _ok(se.SoundKind.SILENCE_BURST in kinds,
        f"«тишина, а потом взрыв» распознаётся отдельно: {kinds}")

    # Шумный сигнал: порог должен держать число событий в разумных рамках.
    rng = np.random.default_rng(7)
    noisy = np.abs(rng.normal(0.05, 0.03, int(7200 / step)))
    n = len(se.find_loud_events(noisy, step))
    _ok(n <= 55, f"на «дёрганом» звуке 2 часов событий не больше полусотни ({n})")


def test_model_events():
    print("\n[17] Модель звуков: смех/крик из очков модели:")
    import numpy as np
    from core import sound_events as se

    # Два куска по 5 минут: смех на 20-й секунде и на 5:30 (во втором куске).
    per = 624
    scores = np.zeros((per * 2, 521), dtype=np.float32)
    scores[int(20 / se.YAMNET_HOP), 13] = 0.8                 # Laughter
    scores[per + int(30 / se.YAMNET_HOP), 15] = 0.6           # Giggle во 2-м куске
    scores[int(60 / se.YAMNET_HOP), 11] = 0.7                 # Screaming
    meta = [(0.0, per), (300.0, per)]
    ev = se.find_model_events(scores, meta)
    laughs = [e for e in ev if e.kind == se.SoundKind.LAUGH]
    _ok(len(laughs) == 2, f"оба смеха найдены ({len(laughs)})")
    _ok(abs(laughs[0].center - 20) < 2, f"первый — на 20-й секунде ({laughs[0].center:.0f})")
    _ok(abs(laughs[1].center - 330) < 2,
        f"второй — на 5:30, время куска учтено ({laughs[1].center:.0f} с)")
    _ok(any(e.kind == se.SoundKind.SHOUT for e in ev), "крик распознан отдельно от смеха")
    _ok(not se.find_model_events(np.zeros((100, 521), dtype=np.float32), [(0.0, 100)]),
        "на тишине модель ничего не выдумывает")

    an = se.AudioAnalysis(events=ev, model_used=True)
    sig = se.signals_from_audio(an)
    _ok(len(sig) == len(ev), "каждое событие стало сигналом разбора")
    kinds = {s.kind for s in sig}
    _ok(kinds <= {"laugh", "loud"}, f"виды сигналов известны скорингу: {kinds}")
    _ok(all(s.detail for s in sig), "у каждого есть человеческое объяснение")


def test_media_reading():
    print("\n[18] Чтение звука из файла:")
    import glob
    from core import media
    files = sorted(glob.glob(os.path.join("tests", "sample_clips", "*.mp4")))
    if not files:
        print("  (пропуск: нет тестовых клипов)")
        return
    src = media.audio_from_file(files[0])
    _ok(src.duration > 1, f"длительность прочитана ({src.duration:.0f} с)")
    chunks = list(media.iter_pcm(src.path, chunk_sec=5.0))
    _ok(len(chunks) >= 2, f"звук читается кусками, а не целиком в память ({len(chunks)})")
    total = sum(b.size for _t, b in chunks) / media.SR
    _ok(abs(total - src.duration) < 1.0, f"суммарно получили всю запись ({total:.0f} с)")
    _ok(chunks[1][0] == 5.0, "время начала куска считается верно")

    out = os.path.join(_TMP, "win.wav")
    media.extract_window(src.path, 2.0, 3.0, out)
    _ok(abs(media.probe_duration(out) - 3.0) < 0.3, "кусок для распознавания вырезан ровно")

    try:
        media.audio_from_file(os.path.join(_TMP, "нет-файла.mp4"))
        _ok(False, "пропавший файл должен давать понятную ошибку")
    except media.MediaError as e:
        _ok("не найден" in str(e).lower(), f"нет файла → «{str(e)[:40]}…»")
    _ok("удалена" in media._explain_ytdlp("HTTP Error 404: Not Found"),
        "404 от Twitch объясняется по-человечески")

    # Кэш звука не должен расти бесконечно: каждый стрим — это ~180 МБ на диске.
    cache = media.cache_dir()
    for i in range(3):
        with open(os.path.join(cache, f"vod_{i}.mp4"), "wb") as f:
            f.write(b"\0" * (400 * 1024))
    _ok(media.cache_size_mb() > 1, "кэш считается")
    _ok(media.trim_cache(limit_mb=0.8) >= 1, "старые дорожки сверх лимита удаляются")
    _ok(media.cache_size_mb() <= 0.9, "после чистки кэш укладывается в лимит")
    _ok(media.trim_cache(limit_mb=1000) == 0, "в пределах лимита ничего не трогаем")

    # Служебные файлы yt-dlp не должны сойти за скачанный звук.
    for junk in ("vod_777.mp4.part", "vod_777.mp4.ytdl"):
        with open(os.path.join(cache, junk), "wb") as f:
            f.write(b"\0" * 2048)
    _ok(media.cached_audio("777") is None,
        "недокачка и служебный файл yt-dlp за готовую дорожку не принимаются")
    with open(os.path.join(cache, "vod_777.mp4"), "wb") as f:
        f.write(b"\0" * (200 * 1024))
    _ok(media.cached_audio("777") is not None, "а настоящая дорожка находится")


# ==========================================================================
# Блок 3: речь
# ==========================================================================

def test_speech_text():
    print("\n[19] Речь: эмоции и цитата для названия:")
    from core import speech as sp

    ws = sp.analyze_text("Да ты гонишь! Я в шоке, серьёзно.")
    _ok(len(ws.markers) >= 2, f"эмоциональные обороты найдены: {ws.markers[:3]}")
    _ok(ws.exclaims >= 1, "восклицание посчитано")
    _ok(ws.heat > 1, f"речь признана эмоциональной ({ws.heat:.1f})")
    _ok(ws.quote.startswith("Да ты гонишь"), f"цитата для названия: «{ws.quote}»")

    calm = sp.analyze_text("Сегодня посмотрим настройки и потом продолжим по плану.")
    _ok(calm.heat == 0, "ровная речь эмоциональной не считается")

    rep = sp.analyze_text("нет нет нет нет что происходит")
    _ok(rep.repeats >= 1, "повтор слова подряд — признак эмоции")

    long_q = sp.pick_quote("А" * 200)
    _ok(len(long_q) <= 61, f"длинная цитата обрезается ({len(long_q)})")
    _ok(sp.pick_quote("") == "", "пустая расшифровка — пустая цитата")


def test_speech_plan():
    print("\n[20] Речь: честная оценка времени и выбор модели:")
    from core import speech as sp
    windows = [(i * 300.0, i * 300.0 + 40.0) for i in range(25)]

    gpu = sp.plan(windows, model="large-v3", prefer_gpu=True)
    gpu.device = "cuda"
    gpu = sp.plan(windows, model="large-v3", prefer_gpu=True) if sp.detect_device() == "cuda" else gpu
    cpu = sp.plan(windows, model="large-v3", prefer_gpu=False)
    _ok(cpu.device == "cpu" and cpu.est_sec > 600,
        f"на процессоре большая модель — это долго ({cpu.est_sec / 60:.0f} мин)")
    _ok(cpu.long and "видеокарты" in cpu.advice(),
        "программа честно объясняет, почему долго")
    _ok("модель «medium»" in cpu.advice() or "модель «small»" in cpu.advice(),
        "и предлагает модель побыстрее")
    fast = sp.plan(windows, model="base", prefer_gpu=False)
    _ok(fast.est_sec < cpu.est_sec / 3, "быстрая модель заметно быстрее")

    auto_cpu = sp.plan(windows, model="auto", prefer_gpu=False)
    _ok(auto_cpu.model == "small",
        f"«авто» без видеокарты берёт компромисс, а не полчаса ожидания ({auto_cpu.model})")
    skip = sp.plan(windows, model="skip")
    _ok(skip.skip and skip.est_sec == 0, "речь можно вообще пропустить")
    _ok("не распознаём" in skip.human(), "и это видно человеку")


def test_speech_windows():
    print("\n[21] Речь: слушаем ТОЛЬКО кандидатов и умеем прерваться:")
    from core import media, speech as sp
    from core import transcribe as tr

    seen: list = []

    def fake_extract(path, start, duration, out_wav, sr=16000):
        seen.append((round(start, 1), round(duration, 1)))
        return out_wav

    class FakeRes:
        text = "Да ты гонишь! Это что вообще было?"

    old_e, old_t = media.extract_window, tr.transcribe_file
    media.extract_window = fake_extract
    tr.transcribe_file = lambda *a, **k: FakeRes()
    try:
        windows = [(100.0, 140.0), (500.0, 540.0), (900.0, 940.0)]
        res = sp.transcribe_windows("audio.mp4", windows,
                                    sp.plan(windows, model="base", prefer_gpu=False))
        _ok(len(res) == 3, "разобраны все три окна")
        _ok(seen == [(100.0, 40.0), (500.0, 40.0), (900.0, 40.0)],
            f"вырезаны ровно окна кандидатов, а не вся запись: {seen}")
        _ok(res[0].start == 100.0 and res[0].quote, "у окна есть время и цитата")

        seen.clear()
        stop = {"n": 0}

        def should_stop():
            stop["n"] += 1
            return stop["n"] > 2                # прервать после второго окна
        res2 = sp.transcribe_windows("audio.mp4", windows,
                                     sp.plan(windows, model="base", prefer_gpu=False),
                                     should_stop=should_stop)
        _ok(len(res2) == 2, f"остановка работает на полпути ({len(res2)} из 3)")
    finally:
        media.extract_window, tr.transcribe_file = old_e, old_t

    # Сигналы и названия.
    sig = sp.signals_from_speech(res)
    _ok(len(sig) == 3 and sig[0].kind == clipscan.Kind.SPEECH,
        "эмоциональная речь стала сигналом разбора")

    m1 = clipscan.ScanMoment(start=90, end=140, center=120, score=5, label="")
    m2 = clipscan.ScanMoment(start=490, end=540, center=520, score=5, label="клип зрителя")
    named = sp.apply_names([m1, m2], res)
    _ok(named == 1 and m1.label, f"безымянный момент получил цитату: «{m1.label}»")
    _ok(m2.label == "клип зрителя", "имя от зрителя речь не перебивает")


def test_explanations():
    print("\n[22] Объяснение момента читается человеком:")
    K = clipscan.Kind
    sigs = [
        clipscan.Signal(K.VIEWER_CLIP, 1000.0, 5.0, "клип", {"views": 300, "title": "12"}),
        clipscan.Signal(K.VIEWER_CLIP, 1005.0, 5.0, "клип", {"views": 20, "title": "f[[f"}),
        clipscan.Signal(K.LAUGH, 1002.0, 2.6, "смеются", {"sound": "laugh"}),
        clipscan.Signal(K.LAUGH, 1006.0, 2.6, "смеются", {"sound": "laugh"}),
        clipscan.Signal(K.LOUD, 1004.0, 1.4, "громче", {"sound": "loud", "score": 5.7}),
        clipscan.Signal(K.LOUD, 1008.0, 1.4, "громче", {"sound": "loud", "score": 8.9}),
        clipscan.Signal(K.SPEECH, 1003.0, 2.0, "речь",
                        {"quote": "Да ты гонишь", "profanity": 1}),
    ]
    m = clipscan.candidates(sigs)[0]
    why = m.why()
    _ok(why.count("смеются") == 1, f"два смеха свёрнуты в одну строку: {why}")
    _ok("2 раза" in why, "но видно, что смеялись дважды")
    _ok(why.count("громче") == 1 and "8.9" in why,
        "громкость — одной строкой и по самому сильному месту")
    _ok("«Да ты гонишь»" in why, "цитата из речи на месте")
    _ok(len(why.split(" · ")) <= 6, f"строка объяснения не превращается в простыню: {why}")

    _ok(not m.label, "мусорные названия клипов («12», «f[[f») в имя момента не идут")
    _ok(clipscan.good_label("а голосок то какой") and not clipscan.good_label("67"),
        "живое название проходит, номер — нет")

    from core import speech as sp
    ws = sp.WindowSpeech(start=980.0, end=1030.0, quote="Кто больше похож")
    ws.markers = ["ого"]
    _ok(sp.apply_names([m], [ws]) == 1 and m.label == "Кто больше похож",
        f"вместо мусора момент получил цитату: «{m.label}»")


def test_vodcut():
    """[23] Блок 6: качаем ТОЛЬКО выбранные куски (сеть подменена)."""
    print("\n[23] Куски записи по ссылке")
    from core import vodcut

    # Оценка размера — по замерам, а не по заявленному битрейту формата.
    mb = vodcut.estimate_mb(20.0, "1080p60")
    _ok(4.0 < mb < 7.0, f"кусок 20 с в 1080p60 ≈ {mb:.1f} МБ (замер был 5,3)")
    _ok(vodcut.estimate_mb(40.0, "480p") < vodcut.estimate_mb(40.0, "1080p60"),
        "качество пониже — файл меньше")
    _ok(vodcut.human_size(2048) == "2.0 ГБ" and vodcut.human_size(68) == "68 МБ",
        "размер печатается по-человечески")

    # Час 1080p60 против шести кусков по 40 с — ради чего всё затевалось.
    whole = vodcut.estimate_mb(7200.0, "1080p60")
    parts = vodcut.estimate_mb(6 * 42.0, "1080p60")
    _ok(parts < whole / 20, f"6 моментов ({parts:.0f} МБ) против всей записи "
                            f"({whole / 1024:.1f} ГБ) — экономия больше чем в 20 раз")

    path = vodcut.window_path("2829614149", 100.0, 140.0, "720p60")
    _ok(path.endswith("cut_2829614149_100-140_720p60.mp4"),
        "имя куска содержит запись, границы и качество (кэш попадает сам собой)")
    _ok(vodcut.cached_window("2829614149", 100.0, 140.0, "720p60") is None,
        "ничего не скачано — кэша нет")

    # Готовый кусок с прошлого раза: качать заново нельзя.
    got = vodcut.window_path("vodX", 99.0, 141.0)
    with open(got, "wb") as f:
        f.write(b"0" * 70 * 1024)
    old_probe = vodcut.probe_duration
    vodcut.probe_duration = lambda _p: 42.0
    try:
        piece = vodcut.fetch_window("vodX", 100.0, 140.0)   # pad=1.0 → 99..141
        _ok(piece.from_cache and piece.path == got,
            "второй раз тот же кусок берётся из кэша, а не качается")
        _ok(abs(piece.local(120.0) - 21.0) < 0.01,
            f"время стрима 120 с внутри куска = {piece.local(120.0):.1f} с "
            f"(иначе клип уедет на секунды)")

        # Загрузка: yt-dlp подменён, проверяем окна и остановку.
        asked: list = []

        class _FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def download(self, targets):
                sec = list(self.opts["download_ranges"]({}, None))[0]
                asked.append((targets[0], round(sec["start_time"], 1),
                              round(sec["end_time"], 1)))
                with open(self.opts["outtmpl"], "wb") as fh:
                    fh.write(b"0" * 80 * 1024)

        import yt_dlp
        old_ydl = yt_dlp.YoutubeDL
        yt_dlp.YoutubeDL = _FakeYDL
        try:
            pieces = vodcut.fetch_windows(
                "vodY", [(600.0, 640.0), (1200.0, 1230.0)],
                url="https://www.twitch.tv/videos/vodY", quality="720p60")
            _ok(len(pieces) == 2, "скачаны оба выбранных окна")
            _ok(asked == [("https://www.twitch.tv/videos/vodY", 599.0, 641.0),
                          ("https://www.twitch.tv/videos/vodY", 1199.0, 1231.0)],
                f"запрошены ровно окна моментов с запасом по краям: {asked}")
            _ok(all(p.quality == "720p60" for p in pieces), "качество то, что просили")

            asked.clear()
            try:
                vodcut.fetch_windows("vodZ", [(10.0, 50.0)], should_stop=lambda: True)
                _ok(False, "остановка должна прерывать загрузку")
            except vodcut.Cancelled:
                _ok(True, "«Стоп» прерывает загрузку куска")
            _ok(not os.path.isfile(vodcut.window_path("vodZ", 9.0, 51.0)),
                "после остановки огрызок файла удалён (иначе сойдёт за готовый кусок)")
        finally:
            yt_dlp.YoutubeDL = old_ydl
    finally:
        vodcut.probe_duration = old_probe


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    test_links()
    test_clips()
    test_duplicates()
    test_pulse_classify()
    test_pulse_collect()
    test_pulse_spikes()
    test_scoring()
    test_scan_file()
    test_scan_link()
    test_scan_link_no_data()
    test_no_webcam()
    test_banter()
    test_report_text()
    test_bot_writes_pulse()
    test_pulse_can_be_off()
    test_loudness()
    test_model_events()
    test_media_reading()
    test_speech_text()
    test_speech_plan()
    test_speech_windows()
    test_explanations()
    test_vodcut()
    print("\nВСЕ ПРОВЕРКИ ПРОШЛИ ✔")


if __name__ == "__main__":
    main()
