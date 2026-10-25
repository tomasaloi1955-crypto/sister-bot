"""Кнопки. Формат callback_data: короткие строки через «:»."""
from aiogram.types import InlineKeyboardButton as B
from aiogram.types import InlineKeyboardMarkup

from . import config, texts


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def choices(prefix: str, options: dict) -> InlineKeyboardMarkup:
    return _kb([[B(text=label, callback_data=f"{prefix}:{key}")] for key, label in options.items()])


def done(day: int) -> InlineKeyboardMarkup:
    return _kb([[B(text=texts.DONE_BUTTON, callback_data=f"done:{day}")]])


def done_marked() -> InlineKeyboardMarkup:
    return _kb([[B(text=texts.DONE_ALREADY_BUTTON, callback_data="noop")]])


def checkin(day: int) -> InlineKeyboardMarkup:
    return _kb([[B(text=label, callback_data=f"fb:{day}:{key}")] for key, label in texts.MOODS.items()])


def checkin_skip() -> InlineKeyboardMarkup:
    return _kb([[B(text=texts.CHECKIN_SKIP_BUTTON, callback_data="fbskip")]])


def free_chat() -> InlineKeyboardMarkup | None:
    if not config.FREE_CHAT_LINK:
        return None
    return _kb([[B(text=texts.FREE_CHAT_BUTTON, url=config.FREE_CHAT_LINK)]])


def club(with_unsubscribe: bool = False) -> InlineKeyboardMarkup:
    rows = [[B(text=texts.CLUB_BUTTON, callback_data="club")]]
    if with_unsubscribe:
        rows.append([B(text=texts.UNSUBSCRIBE_BUTTON, callback_data="unsub")])
    return _kb(rows)


def pay() -> InlineKeyboardMarkup:
    """По кнопке оплаты на каждый тариф (Tribute и/или звёзды) + «Я оплатила»."""
    rows = []
    for code, plan in config.PLANS.items():
        name = texts.PLAN_NAMES[code]
        if plan["link"]:
            rows.append([B(text=texts.PAY_TRIBUTE_BUTTON.format(plan=name, price=plan["price"]),
                           url=plan["link"])])
        if plan["stars"] > 0:
            rows.append([B(text=texts.PAY_STARS_BUTTON.format(plan=name, stars=plan["stars"]),
                           callback_data=f"stars:{code}")])
    rows.append([B(text=texts.I_PAID_BUTTON, callback_data="ipaid")])
    return _kb(rows)


def which_plan(prefix: str) -> InlineKeyboardMarkup:
    return _kb([[B(text=f"{texts.PLAN_NAMES[code]} — {plan['price']}", callback_data=f"{prefix}:{code}")]
                for code, plan in config.PLANS.items()])


def settings(unsubscribed: bool) -> InlineKeyboardMarkup:
    return _kb([
        [B(text=texts.SET_SLOT_BUTTON, callback_data="set:slot")],
        [B(text=texts.SET_TZ_BUTTON, callback_data="set:tz")],
        [B(text=texts.RESUB_BUTTON, callback_data="resub") if unsubscribed
         else B(text=texts.UNSUB_BUTTON, callback_data="unsub")],
        [B(text=texts.CLUB_BUTTON, callback_data="club")],
    ])


# ─────────────────────────────── админка ───────────────────────────────

def moderate(fid: int) -> InlineKeyboardMarkup:
    return _kb([[B(text="✅ Одобрить", callback_data=f"adm:fb:1:{fid}"),
                 B(text="❌ Отклонить", callback_data=f"adm:fb:0:{fid}")]])


def payment_check(pid: int) -> InlineKeyboardMarkup:
    return _kb([[B(text="✅ Оплата есть — выдать доступ", callback_data=f"adm:pay:1:{pid}")],
                [B(text="❌ Оплаты нет", callback_data=f"adm:pay:0:{pid}")]])


SEGMENT_NAMES = {"all": "Всем", "notpaid": "Не купили", "paid": "Купили",
                 "inactive": "Неактивные (7 дней без «Сделала»)"}


def segments() -> InlineKeyboardMarkup:
    return _kb([[B(text=name, callback_data=f"adm:seg:{key}")] for key, name in SEGMENT_NAMES.items()])


def confirm_broadcast() -> InlineKeyboardMarkup:
    return _kb([[B(text="🚀 Отправить", callback_data="adm:bc:go"),
                 B(text="Отмена", callback_data="adm:bc:cancel")]])

