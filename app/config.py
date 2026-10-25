"""Все настройки — из переменных окружения (Render → Environment или .env)."""
import os
from pathlib import Path


def _load_dotenv() -> None:
    """Локально читаем .env рядом с папкой бота (на Render его нет — там Environment)."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    value = _str(name)
    return int(value) if value else default


BOT_TOKEN = _str("BOT_TOKEN")
DATABASE_URL = _str("DATABASE_URL")
# Адрес сервиса. На Render задаётся сам (RENDER_EXTERNAL_URL), руками — не нужно.
BASE_URL = (_str("BASE_URL") or _str("RENDER_EXTERNAL_URL")).rstrip("/")
WEBHOOK_SECRET = _str("WEBHOOK_SECRET")      # секрет вебхука Telegram (латиница/цифры)
TICK_KEY = _str("TICK_KEY")                  # ключ для /tick?key=...
PORT = _int("PORT", 10000)

ADMIN_IDS = {int(x) for x in _str("ADMIN_IDS").replace(" ", "").split(",") if x}
COACH_NAME = _str("COACH_NAME", "Тренер")

DEFAULT_TZ = _str("DEFAULT_TZ", "Asia/Almaty")
DAY_HOUR = _int("DAY_HOUR", 11)              # дневная тренировка
EVENING_HOUR = _int("EVENING_HOUR", 19)      # вечерняя тренировка
INFO_HOUR = _int("INFO_HOUR", 13)            # прогрев, продажа, письма после недели
NUDGE_HOUR = _int("NUDGE_HOUR", 17)          # мягкое напоминание
CHECKIN_HOUR = _int("CHECKIN_HOUR", 20)      # «Как ощущения?»
QUIET_HOUR = _int("QUIET_HOUR", 22)          # после этого часа ничего не шлём
# Видео дня ещё не загружено — ждём его до этого часа, потом шлём мягкую заглушку.
MISSING_HOUR = _int("MISSING_HOUR", 21)
# Напоминания вам «загрузи тренировку на завтра» (по DEFAULT_TZ).
ADMIN_MORNING_HOUR = _int("ADMIN_MORNING_HOUR", 9)
ADMIN_EVENING_HOUR = _int("ADMIN_EVENING_HOUR", 20)

FREE_CHAT_LINK = _str("FREE_CHAT_LINK")      # закрытый женский чат бесплатной недели
CLUB_CHAT_ID = _str("CLUB_CHAT_ID")          # id платного канала/чата клуба (-100…)
# Свой канал для тарифа «каждый день». Пусто — оба тарифа в CLUB_CHAT_ID.
CLUB_CHAT_ID_6X = _str("CLUB_CHAT_ID_6X")


def _ids(name: str) -> set[str]:
    return {x for x in _str(name).replace(" ", "").split(",") if x}


TRIBUTE_API_KEY = _str("TRIBUTE_API_KEY")

# Тарифы клуба, оплата за месяц. Ключи 3x / 6x — коды тарифов в базе и кнопках.
# products — id товаров/подписок Tribute этого тарифа (через запятую): по ним
# бот понимает, какой тариф оплачен. stars — цена в звёздах (0 — не принимаем).
PLANS = {
    "3x": {"price": _str("PRICE_3X", "2 500 ₽"),
           "link": _str("TRIBUTE_LINK_3X", "https://web.tribute.tg/s/1aiG"),
           "products": _ids("TRIBUTE_PRODUCT_IDS_3X"),
           "stars": _int("STARS_PRICE_3X", 0),
           "chat": CLUB_CHAT_ID},
    "6x": {"price": _str("PRICE_6X", "5 000 ₽"),
           "link": _str("TRIBUTE_LINK_6X", "https://web.tribute.tg/s/1aiI"),
           "products": _ids("TRIBUTE_PRODUCT_IDS_6X"),
           "stars": _int("STARS_PRICE_6X", 0),
           "chat": CLUB_CHAT_ID_6X or CLUB_CHAT_ID},
}
DEFAULT_PLAN = "3x"

SEND_RATE = _int("SEND_RATE", 25)            # сообщений в секунду, не больше
