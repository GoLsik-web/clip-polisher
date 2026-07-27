"""banter.py — «болталка» бота: живые фразы в чате + команда `!отчёт`.

Зачем. Бот меток всё время сидит в чате и молчит — это скучно, особенно у камерных
стримеров, где чат оживает от любого повода. Пусть бот подаёт голос: подбадривает,
хвалит, шутит — на выбор стримера.

Как это устроено (три правила, чтобы бот не превратился в спамера):
  1. Режим выбирает стример: поддержка / похвала / приколы / анекдоты / смешанный.
  2. Повод — либо таймер (раз в N минут), либо СОБЫТИЕ: чат затих, чат взорвался,
     впервые написал новый человек.
  3. Общий кулдаун: между ЛЮБЫМИ двумя фразами бота — минимум `cooldown` секунд,
     а одна и та же фраза не повторяется, пока не кончится её пачка.

`!отчёт` — только для стримера и модераторов: бот отвечает в чат короткой сводкой
эфира (время, метки, темп чата, был ли взрыв). Данные — те же, что копит `chatpulse`.

Чистое ядро: ни Qt, ни сети. Случайность подменяется параметром `rng` — поэтому
болталка проверяется тестами до последней фразы.
"""
from __future__ import annotations

import random
import time
from typing import Optional

# Режимы: (код, как показать в UI). Порядок = порядок в выпадашке.
MODES: list[tuple[str, str]] = [
    ("off", "Молчит (только метки)"),
    ("support", "Поддержка"),
    ("praise", "Похвала"),
    ("fun", "Приколы"),
    ("jokes", "Анекдоты"),
    ("mixed", "Смешанный"),
]
MODE_CODES = [c for c, _ in MODES]
DEFAULT_MODE = "off"           # по умолчанию бот молчит — включает стример осознанно

# Команды отчёта (доступ — стример и модераторы).
REPORT_COMMANDS = ("!отчёт", "!отчет", "!report", "!стата")


# --------------------------------------------------------------------------
# Фразы
# --------------------------------------------------------------------------

SUPPORT = [
    "Идём в своём темпе, и это нормально.",
    "Даже если сейчас не заходит — стрим всё равно состоялся.",
    "Плохой раунд не отменяет хорошего вечера.",
    "Напоминаю: вода рядом, спина прямая, всё под контролем.",
    "Тут сегодня спокойно и по-доброму. Так тоже можно.",
    "Кто-то смотрит молча — это не тишина, это уют.",
    "Если устал — пауза не поражение, а часть плана.",
    "Одна попытка ничего не решает. Решает то, что ты не бросил.",
    "Сложный участок проходят медленно. Это не про скилл, это про терпение.",
    "Стрим — марафон. Дыши ровно.",
    "Всё идёт так, как надо, даже когда идёт не так.",
    "Зрителей может быть немного, но они здесь ради тебя.",
    "Ошибся — и ладно. Впереди ещё вся катка.",
    "Сегодня главное — не результат, а то, что ты вышел.",
    "Голос устал? Помолчать в эфире тоже можно.",
    "Тебя слышно, тебя видно, всё работает. Дальше проще.",
    "Не сравнивай себя с крупными каналами: у них другой этап пути.",
    "Ты уже сделал сложное — начал. Остальное дело техники.",
]

PRAISE = [
    "Вот это было чисто.",
    "Красиво отыграно, без шуток.",
    "Такое надо в клип.",
    "Реакция быстрее, чем у чата.",
    "Спокойно и уверенно. Уважение.",
    "Вот сейчас было мастерски.",
    "Держишь темп — приятно смотреть.",
    "Отличный заход, честно.",
    "Так и надо было. Ноль лишних движений.",
    "Из этого получится хороший короткий ролик.",
    "Хорошая работа. Правда.",
    "Дорогого стоит вытянуть такую ситуацию.",
    "Терпение окупилось.",
    "Вот за это и смотрим.",
    "Чисто, аккуратно, без паники.",
    "Достойно. Продолжай в том же духе.",
]

FUN = [
    "Держу в курсе: я всё ещё бот и всё ещё смотрю.",
    "Мой прогноз на следующую минуту: что-то произойдёт. Или нет.",
    "Считаю метки и делаю вид, что понимаю игру.",
    "Если промолчу — значит думаю. Если напишу — значит притворяюсь.",
    "У меня две задачи: следить за чатом и вовремя молчать. Со второй сложнее.",
    "Кто-нибудь заметил, что стрим идёт подозрительно хорошо?",
    "Ставлю виртуальный чай на то, что дальше будет веселее.",
    "Я тут единственный, кто ни разу не моргнул.",
    "Напоминаю: скриншот бесплатный, клип тоже.",
    "Проверил чат — люди на месте. Проверил стримера — тоже на месте. Порядок.",
    "Работаю без выходных и без мнения.",
    "Иногда я делаю вид, что понимаю, что происходит. Как и все.",
    "Тишина в чате — это тоже реакция. Загадочная.",
    "Мой любимый момент стрима — следующий.",
    "Технически я здесь для меток. Эмоционально — ради вас.",
    "Если что-то пойдёт не так, я это запишу. Такая работа.",
]

JOKES = [
    "Стример — единственный человек, который извиняется перед чатом за то, что пил воду.",
    "Лучший способ уронить фпс — сказать, что фпс стабильный.",
    "Правило стрима: как только скажешь «сейчас будет изи», начинается сложное.",
    "Микрофон включён ровно до того момента, когда нужно сказать что-то важное.",
    "Самая честная категория на Twitch — «Общение», потому что там ничего не обещают.",
    "Ничто так не собирает чат, как фраза «ладно, ещё одна катка».",
    "Интернет всегда стабилен. Ровно до начала эфира.",
    "Игра сохраняется автоматически. В самый неподходящий момент — тоже автоматически.",
    "Донат приходит либо в тишину, либо в крик. Третьего не дано.",
    "Соседи начинают ремонт по расписанию: за минуту до старта стрима.",
    "Опытный стример знает: «последняя катка» — это единица измерения, а не факт.",
    "Все баги игры делятся на два вида: смешные и те, что на записи.",
    "Кто-то приходит на стрим за игрой, кто-то за общением, а кто-то просто мимо шёл. И остался.",
    "Стрим без технических неполадок называется репетицией.",
    "Чат всегда знает, как надо. Особенно чат, который сам не играл.",
    "Самая сложная часть стрима — придумать название стрима.",
]

# Фразы под конкретное событие (важнее обычных — их бот вставляет по поводу).
REVIVE = [
    "Что-то мы притихли. Чат, вы тут?",
    "Тишина в чате — самое время что-нибудь написать.",
    "Проверка связи: кто ещё смотрит, поставьте плюс.",
    "Пока спокойно — расскажите, как день прошёл.",
    "Чат уснул. Будим?",
    "Кажется, все смотрят молча. Это тоже уважение, но пару слов не помешает.",
]
HYPE = [
    "Так, вот это уже интересно.",
    "Чат ожил — значит, было за что.",
    "Записал. Это точно пойдёт в нарезку.",
    "Ого, тут прям волна пошла.",
    "Отметил себе этот момент.",
    "Вот такие моменты и ищем.",
]
GREET = [
    "{user}, привет! Располагайся.",
    "О, {user} на месте. Здорово!",
    "Привет, {user}! Ты вовремя.",
    "{user}, добро пожаловать.",
    "Рад видеть, {user}!",
    "{user}, привет. Чувствуй себя как дома.",
]

_POOLS = {"support": SUPPORT, "praise": PRAISE, "fun": FUN, "jokes": JOKES}


def pool_for(mode: str) -> list[str]:
    """Пачка фраз для режима. «Смешанный» — всё вперемешку."""
    if mode == "mixed":
        return SUPPORT + PRAISE + FUN + JOKES
    return _POOLS.get(mode, [])


# --------------------------------------------------------------------------
# Болталка
# --------------------------------------------------------------------------

class _Bag:
    """«Мешок» фраз: тянем без повторов, пока пачка не кончится, потом мешаем заново."""

    def __init__(self, items: list[str], rng: random.Random):
        self._items = list(items)
        self._rng = rng
        self._left: list[str] = []

    def draw(self) -> Optional[str]:
        if not self._items:
            return None
        if not self._left:
            self._left = list(self._items)
            self._rng.shuffle(self._left)
        return self._left.pop()


class Banter:
    """Решает, ЧТО и КОГДА бот говорит в чат.

    Сам ничего не отправляет — только возвращает текст (или None). Отправку делает
    `BotService`, поэтому болталка тестируется без сети.
    """

    def __init__(self, mode: str = DEFAULT_MODE, period_min: float = 12.0,
                 cooldown: float = 45.0, silence_min: float = 6.0,
                 greet_newcomers: bool = True, react_to_hype: bool = True,
                 rng: Optional[random.Random] = None):
        self.mode = mode if mode in MODE_CODES else DEFAULT_MODE
        self.period = max(60.0, period_min * 60.0)
        self.cooldown = cooldown
        self.silence = max(60.0, silence_min * 60.0)
        self.greet_newcomers = greet_newcomers
        self.react_to_hype = react_to_hype
        self._rng = rng or random.Random()
        self._bags: dict[str, _Bag] = {}
        self._last_sent = 0.0
        # Отсчёт таймера — с момента создания: бот не выпаливает фразу сразу при входе.
        self._last_idle = time.time()
        self._last_greet = 0.0
        self._seen: set[str] = set()
        self._started = self._last_idle

    # ---- настройка на ходу ----
    def configure(self, mode: Optional[str] = None, period_min: Optional[float] = None,
                  greet_newcomers: Optional[bool] = None,
                  react_to_hype: Optional[bool] = None) -> None:
        if mode is not None and mode in MODE_CODES:
            self.mode = mode
            self._bags.pop("idle", None)
        if period_min is not None:
            self.period = max(60.0, period_min * 60.0)
        if greet_newcomers is not None:
            self.greet_newcomers = greet_newcomers
        if react_to_hype is not None:
            self.react_to_hype = react_to_hype

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def reset_session(self, now: Optional[float] = None) -> None:
        """Новый эфир — знакомимся со зрителями заново, таймер с нуля."""
        now = time.time() if now is None else now
        self._seen.clear()
        self._started = now
        self._last_idle = now
        self._last_sent = 0.0
        self._last_greet = 0.0

    # ---- выбор фразы ----
    def _bag(self, key: str, items: list[str]) -> _Bag:
        bag = self._bags.get(key)
        if bag is None:
            bag = _Bag(items, self._rng)
            self._bags[key] = bag
        return bag

    def _ready(self, now: float) -> bool:
        return (now - self._last_sent) >= self.cooldown

    def _take(self, key: str, items: list[str], now: float) -> Optional[str]:
        text = self._bag(key, items).draw()
        if text:
            self._last_sent = now
        return text

    def idle(self, now: Optional[float] = None) -> Optional[str]:
        """Фраза по таймеру: пора ли подать голос просто так."""
        now = time.time() if now is None else now
        if not self.enabled or not self._ready(now):
            return None
        if (now - self._last_idle) < self.period:
            return None
        text = self._take("idle", pool_for(self.mode), now)
        if text:
            self._last_idle = now
        return text

    def revive(self, silence_sec: float, now: Optional[float] = None) -> Optional[str]:
        """Чат давно молчит — расшевелить."""
        now = time.time() if now is None else now
        if not self.enabled or not self._ready(now) or silence_sec < self.silence:
            return None
        self._last_idle = now              # расшевелили — обычный таймер можно отложить
        return self._take("revive", REVIVE, now)

    def hype(self, ratio: float, now: Optional[float] = None) -> Optional[str]:
        """Чат взорвался — поддержать волну (но не на каждый чих)."""
        now = time.time() if now is None else now
        if not (self.enabled and self.react_to_hype) or not self._ready(now):
            return None
        if ratio < 3.0:
            return None
        return self._take("hype", HYPE, now)

    def greet(self, login: str, display: str = "", rate_per_min: float = 0.0,
              now: Optional[float] = None) -> Optional[str]:
        """Поздороваться с тем, кто впервые написал за эфир.

        В шумном чате не здороваемся вовсе: там это выглядит спамом, да и Twitch
        не любит частые сообщения от ботов.
        """
        now = time.time() if now is None else now
        low = (login or "").lower()
        first = low not in self._seen
        self._seen.add(low)
        if not (self.enabled and self.greet_newcomers and first):
            return None
        if rate_per_min > 20.0 or not self._ready(now):
            return None
        if (now - self._last_greet) < 120.0:
            return None
        text = self._take("greet", GREET, now)
        if text:
            self._last_greet = now
            return text.replace("{user}", display or login)
        return None

    def note_sent(self, now: Optional[float] = None) -> None:
        """Сообщить, что бот только что что-то написал (например, «✓ метка»)."""
        self._last_sent = time.time() if now is None else now


# --------------------------------------------------------------------------
# Команда !отчёт
# --------------------------------------------------------------------------

def is_report_command(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in REPORT_COMMANDS or any(t.startswith(c + " ") for c in REPORT_COMMANDS)


def human_span(sec: float) -> str:
    """Секунды → «2 ч 15 мин» / «47 мин»."""
    sec = max(0, int(sec))
    h, m = sec // 3600, (sec % 3600) // 60
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин" if m else "меньше минуты"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def build_report(uptime_sec: float, marks: int, rate_per_min: float,
                 live: bool = True, peak_ratio: float = 0.0,
                 peak_t: Optional[float] = None, chatters: int = 0) -> str:
    """Короткая сводка эфира одной строкой (влезает в сообщение чата)."""
    head = f"Эфир {human_span(uptime_sec)}" if live else f"Не в эфире (бот в чате {human_span(uptime_sec)})"
    parts = [head, f"{marks} {_plural(marks, 'метка', 'метки', 'меток')}",
             f"чат {rate_per_min:.0f} сообщ/мин"]
    if chatters:
        parts.append(f"{chatters} {_plural(chatters, 'человек', 'человека', 'человек')} в чате")
    if peak_ratio >= 2.0 and peak_t is not None:
        from core.clipscan import fmt_time
        parts.append(f"пик ×{peak_ratio:.1f} на {fmt_time(peak_t)}")
    return " · ".join(parts)
