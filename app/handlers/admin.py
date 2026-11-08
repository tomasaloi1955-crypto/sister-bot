"""Админка — только для ADMIN_IDS."""
from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message)

from .. import config, db, funnel, keyboards, messages, payments, sender, texts, tribute_router

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.chat.type == "private", F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))
_background: set[asyncio.Task] = set()

HELP = (
    "<b>Админка</b>\n\n"
    "/stats — статистика и источники\n"
    "/upload 2 — загрузить тренировку №2 бесплатной недели (всего 3)\n"
    "/live — эфиры «3 раза в неделю»: /live ср пт вс 19:00, /live rec — сегодня по записи, /live link …, /live off\n"
    "Тренировка в записи: просто пришлите видео — номер бот даст сам\n"
    "/upload_club 5 — заменить тренировку клуба №5\n"
    "/tofree 8 1 — поставить тренировку клуба №8 вместо тренировки №1 бесплатной недели\n"
    "/repeat on — вы на отдыхе: бот сразу шлёт повторы пройденного; /repeat off — снова новые\n"
    "/channels — каналы и группы клуба: какая группа к какому тарифу (поменять кнопкой)\n"
    "/clubhere — написать в группе: бот спросит в личке, чья это группа\n"
    "/workouts — какие тренировки загружены\n"
    "/show 2 — посмотреть тренировку как участница\n"
    "/broadcast — рассылка по сегменту\n"
    "/reweek — предложить неделю бесплатных тренировок тем, кто нажал /start, но не прошёл анкету\n"
    "/reviews — модерация отзывов\n"
    "/list — последние 30 сестёр: id, день, сколько сделала, купила ли\n"
    "/who 123456789 — где сейчас сестра: анкета, тренировки, оплата\n"
    "/testme — сбросить СВОЮ бесплатную неделю, чтобы пройти анкету заново (проверка)\n"
    "/purge 123456789 — убрать у сестры из чата все видео тренировок сейчас\n"
    "/syncclub — найти в группах клуба сестёр, которых бот не записал в клуб (добавили руками)\n"
    "/grant 123456789 3 — выдать клуб вручную (id участницы; 3 или 6 — тариф)\n"
    "/export — выгрузка участниц в CSV\n"
    "/products — товары Tribute и куда идут их оплаты\n"
    "/route 12345 school — направить товар (school, 3, 6, ignore или https://…)\n"
    "/tick — прогнать рассылку по расписанию прямо сейчас\n"
    "/cancel — отменить текущее действие"
)


class Upload(StatesGroup):
    content = State()
    text = State()


class Broadcast(StatesGroup):
    message = State()
    confirm = State()


@router.message(Command("admin"))
async def admin_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")


# ─────────────────────────────── /stats ───────────────────────────────

def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "—"


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    s = await db.stats()
    t = s["total"]
    now = datetime.now(timezone.utc)
    buckets = {"бесплатная неделя": 0, "продажа (7–9)": 0, "после": 0}
    workout_days = 0
    for r in s["started"]:
        d = funnel.funnel_day(r["started_at"], now, r["tz"])
        workout_days += funnel.workouts_received(d)
        key = ("бесплатная неделя" if d < funnel.SALE_DAYS[0]
               else "продажа (7–9)" if d <= funnel.SALE_DAYS[-1] else "после")
        buckets[key] += 1
    lines = [
        "<b>📊 Статистика</b>",
        f"Пришли в бота: <b>{t['users']}</b> (за сутки +{t['new_today']})",
        f"Прошли анкету: <b>{t['surveyed']}</b> ({_pct(t['surveyed'], t['users'])})",
        "Сейчас в клубе: " + (", ".join(f"{texts.PLAN_NAMES.get(r['plan'], r['plan'])}: {r['n']}"
                                         for r in s["club"]) or "никого"),
        f"Купили клуб: <b>{t['paid']}</b> ("
        + ", ".join(f"{texts.PLAN_NAMES.get(r['plan'], r['plan'])}: {r['n']}" for r in s["by_plan"]) + ")",
        f"Конверсия анкета → покупка: <b>{_pct(t['paid'], t['surveyed'])}</b>",
        f"Конверсия вход → покупка: {_pct(t['paid'], t['users'])}",
        f"Отписались: {t['unsubscribed']} · заблокировали бота: {t['blocked']}",
        "",
        f"Выполнения: <b>{s['done_total']}</b> из {workout_days} тренировок "
        f"({_pct(s['done_total'], workout_days)})",
        f"Активные за 7 дней (жали «Сделала»): {s['active_7d']}",
        "Где сейчас участницы (день воронки): "
        + ", ".join(f"{k}: {v}" for k, v in buckets.items()),
    ]
    if s["by_day"]:
        lines += ["", "«Сделала» по дням:",
                  " ".join(f"{r['day']}:{r['n']}" for r in s["by_day"])]
    lines += ["", "<b>Источники</b> (пришли / анкета / купили / конверсия):"]
    for r in s["by_source"]:
        lines.append(f"• {html.escape(r['source'])}: {r['users']} / {r['surveyed']} / "
                     f"{r['paid']} / {_pct(r['paid'], r['users'])}")
    await message.answer("\n".join(lines))


# ─────────────────────────────── тренировки ───────────────────────────────

def _parse_day_slot(args: str | None) -> tuple[int | None, str | None]:
    parts = (args or "").split()
    day = int(parts[0]) if parts and parts[0].isdigit() else None
    slot = parts[1] if len(parts) > 1 and parts[1] in ("day", "evening") else None
    return day, slot


@router.message(Command("upload"))
async def upload(message: Message, command: CommandObject, state: FSMContext) -> None:
    day, slot = _parse_day_slot(command.args)
    if day is None or not 1 <= day <= funnel.WORKOUTS:
        await message.answer(f"Формат: <code>/upload 1</code> (номер тренировки 1–{funnel.WORKOUTS}).")
        return
    # Тренировка одна на день: её получают и «днём», и «вечером» (db.get_workout).
    await _ask_content(message, state, day, slot or "day")


@router.message(Command("upload_club"))
async def upload_club(message: Message, command: CommandObject, state: FSMContext) -> None:
    arg = (command.args or "").strip()
    if not arg:                                   # без номера — следующий по порядку
        arg = str(max(await db.club_workout_numbers(), default=0) + 1)
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Формат: <code>/upload_club</code> (следующий номер) или <code>/upload_club 5</code>.")
        return
    await _ask_content(message, state, int(arg), "day", club=True)


async def _ask_content(message: Message, state: FSMContext, day: int, slot: str, club: bool = False) -> None:
    await state.set_state(Upload.content)
    await state.update_data(day=day, slot=slot, club=club)
    await message.answer(
        ("Клуб · тренировка" if club else "Тренировка") + f" №{day}.\n\n"
        "Пришли <b>видео</b> (подпись к видео = текст тренировки)\n"
        "или <b>текст</b>: если первая строка — ссылка, она станет видео.\n"
        "/cancel — отмена.")


@router.message(Upload.content, F.video | F.document)
async def upload_video(message: Message, state: FSMContext, bot: Bot) -> None:
    video = message.video.file_id if message.video else "doc:" + message.document.file_id
    if message.caption:
        await _save(message, state, bot, video, message.html_text)
        return
    await state.update_data(video=video)
    await state.set_state(Upload.text)
    await message.answer("Видео есть. Теперь пришли текст тренировки (или «-» — без текста).")


@router.message(Upload.content, F.text, ~F.text.startswith("/"))
async def upload_text_only(message: Message, state: FSMContext, bot: Bot) -> None:
    first, _, rest = message.text.partition("\n")
    if first.strip().startswith("http"):
        await _save(message, state, bot, first.strip(), _html_rest(message, rest))
    else:
        await _save(message, state, bot, None, message.html_text)


def _html_rest(message: Message, rest: str) -> str:
    # Текст после строки-ссылки; разметку первой строки не переносим.
    full = message.html_text
    return full.split("\n", 1)[1].strip() if "\n" in full else html.escape(rest.strip())


@router.message(Upload.text, F.text, ~F.text.startswith("/"))
async def upload_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    text = "" if message.text.strip() == "-" else message.html_text
    await _save(message, state, bot, data["video"], text)


async def _save(message: Message, state: FSMContext, bot: Bot, video: str | None, text: str) -> None:
    data = await state.get_data()
    await state.clear()
    if data.get("club"):
        await db.save_club_workout(data["day"], video, text.strip())
        await message.answer(f"✅ Сохранено: клуб, тренировка №{data['day']}. Вот как увидит участница:")
        await messages.send_club_workout(bot, message.from_user.id, data["day"], track=False)
        return
    await db.save_workout(data["day"], data["slot"], video, text.strip())
    await message.answer(f"✅ Сохранено: тренировка №{data['day']}. Вот как увидит участница:")
    await messages.send_workout(bot, message.from_user.id, data["day"], data["slot"], track=False)


@router.message(StateFilter(None), F.video | F.document)
async def club_video_quick(message: Message, bot: Bot) -> None:
    """Просто прислали видео (без команды) — это следующая тренировка клуба.
    Номер, учёт и рассылка — на боте; кнопка «Отменить», если отправили не то."""
    video = message.video.file_id if message.video else "doc:" + message.document.file_id
    n = max(await db.club_workout_numbers(), default=0) + 1
    await db.save_club_workout(n, video, (message.html_text if message.caption else "").strip())
    await message.answer(
        f"✅ Сохранила как <b>клуб, тренировка №{n}</b>. Сёстры получат её по очереди, каждая в свой день.\n"
        "Вот как она выглядит:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Отменить", callback_data=f"adm:cundo:{n}")]]))
    await messages.send_club_workout(bot, message.from_user.id, n, track=False)


@router.callback_query(F.data.startswith("adm:cundo:"))
async def club_video_undo(cb: CallbackQuery) -> None:
    n = int(cb.data.split(":")[2])
    if await db.delete_club_workout(n):
        await cb.answer("Отменено")
        await cb.message.edit_text(f"↩️ Тренировка клуба №{n} удалена.")
    else:
        await cb.answer("Уже нельзя: её получили сёстры или после неё загружены другие. "
                        "Заменить: /upload_club " + str(n), show_alert=True)


@router.message(Command("tofree"))
async def to_free(message: Message, command: CommandObject, bot: Bot) -> None:
    """/tofree 8 1 — видео и текст тренировки клуба №8 становятся тренировкой №1
    бесплатной недели. Без заново загруженного видео: берём уже присланное."""
    parts = (command.args or "").split()
    if len(parts) != 2 or not all(x.isdigit() for x in parts) or not 1 <= int(parts[1]) <= funnel.WORKOUTS:
        lines = []
        for r in await db.recent_club_workouts():
            when = r["updated_at"].astimezone(funnel.zone(config.DEFAULT_TZ)).strftime("%d.%m %H:%M")
            about = html.escape((r["text"] or "").split("\n", 1)[0][:40]) or "без текста"
            lines.append(f"№{r['n']} — {when}{'' if r['has_video'] else ' (без видео)'} — {about}")
        await message.answer(
            "Формат: <code>/tofree 8 1</code> — тренировка клуба №8 станет тренировкой №1 "
            f"бесплатной недели (номера 1–{funnel.WORKOUTS}).\n\n"
            "<b>Последние тренировки клуба</b> (номер — когда загружена):\n"
            + ("\n".join(lines) or "пока нет")
            + "\n\nПосмотреть любую: <code>/show 2</code> — бесплатная неделя.")
        return
    n, day = int(parts[0]), int(parts[1])
    w = await db.get_club_workout(n)
    if w is None:
        await message.answer(f"Тренировки клуба №{n} нет. Список — /tofree")
        return
    await db.replace_workout(day, w["video"], w["text"])
    log.info("Бесплатная неделя: тренировка №%s ← клуб №%s", day, n)
    await message.answer(f"✅ Тренировка №{day} бесплатной недели теперь — клуб №{n}. "
                         "Кто ещё не получил №{day}, получит новую. Вот как увидит участница:".replace("{day}", str(day)))
    await messages.send_workout(bot, message.from_user.id, day, "day", track=False)


@router.message(Command("repeat"))
async def repeat_mode(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lower()
    if arg in ("on", "off"):
        await db.set_setting("club_repeat", arg)
    state = await db.get_setting("club_repeat") or "off"
    await message.answer(
        "🔁 Режим повтора <b>включён</b>: если новой тренировки нет, бот сразу (в обычное время сестёр) "
        "присылает повтор пройденной с усложнением и не напоминает вам о загрузке. "
        "Вернуться: <code>/repeat off</code>" if state == "on" else
        "Режим повтора <b>выключен</b>: если новой тренировки нет, бот ждёт до 21:00 и напоминает вам; "
        "не загрузили — шлёт повтор пройденной. Включить на время отдыха: <code>/repeat on</code>")


@router.message(Command("live"))
async def live_cmd(message: Message, command: CommandObject, bot: Bot) -> None:
    """Живые эфиры: показать расписание или поменять его."""
    arg = (command.args or "").strip()
    word = arg.split()[0].lower() if arg else ""
    if word in ("on", "off"):
        await db.set_setting("live_on", word)
    elif word in ("rec", "live"):
        await message.answer(await _set_today_mode(bot, word))
        return
    elif word == "link":
        link = arg[4:].strip()
        if link not in ("", "-") and not link.startswith(("https://", "http://", "tg://")):
            await message.answer("Ссылка должна начинаться с https:// — например, ссылка-приглашение в группу клуба.")
            return
        await db.set_setting("live_link", "" if link == "-" else link)
    elif word == "skip":
        await _live_skip(message, bot)
        return
    elif arg:
        days = funnel.parse_days(arg)
        hhmm = next((t for t in map(funnel.parse_time, arg.split()) if t), None) if any(
            ch.isdigit() for ch in arg) else None
        if not days and not hhmm:
            await message.answer("Не поняла 🙈 Пример: <code>/live ср пт вс 19:00</code>")
            return
        if days:
            await db.set_setting("live_days", " ".join(funnel.WEEKDAYS[d] for d in sorted(days)))
        if hhmm:
            await db.set_setting("live_time", f"{hhmm[0]:02d}:{hhmm[1]:02d}")
    await message.answer(await _live_status())


async def _set_today_mode(bot: Bot, mode: str, day: str | None = None) -> str:
    """Сегодня эфир (live) или по записи (rec). По записи — сразу прогнать тик:
    сёстры «3 раза в неделю» получат тренировку, не дожидаясь будильника."""
    now = datetime.now(timezone.utc)
    today = funnel.live_today(await messages.live_schedule(), now)
    if today is None or (day and day != today.isoformat()):
        return "Сегодня эфира по расписанию нет (/live)."
    if await db.already_sent_any(f"liverec:{today}"):
        return "Сёстрам «3 раза в неделю» сегодняшняя запись уже отправлена 🤍"
    await db.set_setting(f"live_mode:{today}", mode)
    if mode == "live":
        return "📡 Отлично, сегодня эфир — напомню сёстрам утром и за час до начала."
    from .. import tick
    task = asyncio.create_task(tick.run(bot))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return ("🎬 Сегодня по записи — сёстры «3 раза в неделю» получат тренировку "
            f"{'сейчас' if now.astimezone(funnel.zone(config.DEFAULT_TZ)).hour >= config.LIVE_REMIND_HOUR else f'в {config.LIVE_REMIND_HOUR}:00'} "
            "(следующую по номеру; если новой нет — повтор с усложнением).")


@router.callback_query(F.data.startswith("adm:lm:"))
async def live_mode_button(cb: CallbackQuery, bot: Bot) -> None:
    _, _, day, mode = cb.data.split(":", 3)
    answer = await _set_today_mode(bot, mode, day)
    await cb.answer()
    await cb.message.edit_text(cb.message.html_text + "\n\n→ " + answer)


async def _live_status() -> str:
    live = await messages.live_schedule()
    link = await messages.live_link()
    now = datetime.now(timezone.utc)
    nxt = funnel.next_live(live, now)
    today = funnel.live_today(live, now)
    mode = await db.get_setting(f"live_mode:{today}") if today else None
    slots = await db.slot_counts()
    day, eve = slots.get("day", (0, 0)), slots.get("evening", (0, 0))
    return (
        "📡 <b>Живые эфиры клуба</b> " + ("" if live.on else "— <b>на паузе</b> (/live on)") + "\n"
        f"Когда: {messages.live_text(live)}\n"
        + (f"Ближайший: {nxt:%d.%m} в {nxt:%H:%M}\n" if nxt else "")
        + (f"Сегодня: {'🎬 по записи' if mode == 'rec' else '📡 эфир'}\n" if today else "")
        + "Ссылка на эфир: " + (link or "не указана — сёстры увидят «заходи в группу клуба»") + "\n"
        "Это для тарифа «3 раза в неделю»; «6 раз в неделю» всегда получает записи.\n"
        f"Эфир не начался через {config.LIVE_GRACE_MIN} мин — сёстрам уходит тренировка в записи.\n\n"
        "Сёстрам удобнее (по анкете):\n"
        f"• {texts.SLOTS.get('day', 'днём')}: {day[0]} всего, из них в клубе {day[1]}\n"
        f"• {texts.SLOTS.get('evening', 'вечером')}: {eve[0]} всего, из них в клубе {eve[1]}\n\n"
        "Поменять: <code>/live ср пт вс 19:00</code>\n"
        "Ссылка на группу: <code>/live link https://t.me/+…</code> (убрать: <code>/live link -</code>)\n"
        "Сегодня без эфира — по записи: <code>/live rec</code> (передумали: <code>/live live</code>)\n"
        "Отменить ближайший эфир совсем (без записи): <code>/live skip</code>\n"
        "Отпуск — без напоминаний: <code>/live off</code>, вернуться: <code>/live on</code>")


async def _live_skip(message: Message, bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    live = await messages.live_schedule()
    start = funnel.next_live(live, now)
    if start is None:
        await message.answer("Ближайших эфиров нет (расписание на паузе?). /live")
        return
    today = now.astimezone(funnel.zone(config.DEFAULT_TZ)).date().isoformat()
    skip = {d for d in live.skip if d >= today} | {start.date().isoformat()}
    await db.set_setting("live_skip", " ".join(sorted(skip)))
    nxt = funnel.next_live(await messages.live_schedule(), now)
    members = [m for m in await db.club_members_active() if m["plan"] != "6x"]
    await message.answer(f"Эфир {start:%d.%m} в {start:%H:%M} отменён, предупреждаю сестёр ({len(members)}).")

    async def notify() -> None:
        ok = 0
        for m in members:
            next_text = (texts.LIVE_NEXT.format(date=f"{nxt:%d.%m}", time=messages.local_time(nxt, m["tz"]))
                         if nxt else "")
            ok += await messages.say(bot, m["tg_id"], texts.LIVE_CANCELLED.format(
                name=messages.name_of(m), date=f"{start:%d.%m}", time=messages.local_time(start, m["tz"]),
                next=next_text)) == sender.OK
        await bot.send_message(message.chat.id, f"✅ Об отмене эфира узнали {ok} из {len(members)}.")
    task = asyncio.create_task(notify())
    _background.add(task)
    task.add_done_callback(_background.discard)


@router.message(Command("workouts"))
async def workouts(message: Message) -> None:
    rows = await db.list_workouts()
    have: dict[int, bool] = {}
    for r in rows:
        have[r["day"]] = have.get(r["day"], False) or r["has_video"]

    def mark(d: int) -> str:
        return "🎬" if have.get(d) else "📝" if d in have else "·"
    lines = [f"{d:>2}: {mark(d)}" for d in range(1, funnel.WORKOUTS + 1)]
    club = await db.club_workout_numbers()
    top = max(club, default=0)
    club_marks = " ".join(f"{n}{'🎬' if club.get(n) else '📝' if n in club else '·'}" for n in range(1, top + 1))
    members = await db.club_members_active()
    need = max((m["next_n"] for m in members), default=0)
    await message.answer("<b>Бесплатная неделя</b> (🎬 видео, 📝 только текст, · нет)\n<code>"
                         + "\n".join(lines) + "</code>\n\n<b>Клуб</b>: "
                         + (club_marks or "пока ни одной")
                         + (f"\nСамая «старшая» участница ждёт №{need}." if need else "")
                         + "\nСледующую — просто пришлите видео боту."
                         + ("\n🔁 Режим повтора включён (/repeat off)." if await db.get_setting("club_repeat") == "on" else ""))


@router.message(Command("show"))
async def show(message: Message, command: CommandObject, bot: Bot) -> None:
    day, slot = _parse_day_slot(command.args)
    if day is None:
        await message.answer("Формат: <code>/show 2</code>")
        return
    await messages.send_workout(bot, message.from_user.id, day, slot or "day", track=False)


# ─────────────────────────────── рассылка ───────────────────────────────

@router.message(Command("broadcast"))
async def broadcast(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Кому отправить?", reply_markup=keyboards.segments())


@router.callback_query(F.data.startswith("adm:seg:"))
async def broadcast_segment(cb: CallbackQuery, state: FSMContext) -> None:
    segment = cb.data.split(":")[2]
    await cb.answer()
    await state.set_state(Broadcast.message)
    await state.update_data(segment=segment)
    await cb.message.answer(f"Сегмент: <b>{keyboards.SEGMENT_NAMES[segment]}</b>.\n"
                            "Пришли сообщение для рассылки (текст, фото, видео — что угодно). /cancel — отмена.")


@router.message(Broadcast.message, ~F.text.startswith("/"))   # команда — не текст рассылки
async def broadcast_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ids = await db.segment_ids(data["segment"])
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Broadcast.confirm)
    await message.answer(f"Получательниц: <b>{len(ids)}</b>. Отправляем?",
                         reply_markup=keyboards.confirm_broadcast())


@router.callback_query(Broadcast.confirm, F.data.startswith("adm:bc:"))
async def broadcast_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await cb.answer()
    if cb.data.endswith("cancel"):
        await cb.message.edit_text("Рассылка отменена.")
        return
    await cb.message.edit_text("🚀 Рассылка пошла. Пришлю итог, когда закончу.")
    task = asyncio.create_task(_run_broadcast(bot, cb.from_user.id, data))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _run_broadcast(bot: Bot, admin_id: int, data: dict) -> None:
    ids = await db.segment_ids(data["segment"])
    counts = {sender.OK: 0, sender.BLOCKED: 0, sender.FAILED: 0}
    for tg_id in ids:
        status = await sender.call(tg_id, lambda t=tg_id: bot.copy_message(
            chat_id=t, from_chat_id=data["chat_id"], message_id=data["message_id"]))
        counts[status] += 1
    log.info("Рассылка %s: %s", data["segment"], counts)
    await bot.send_message(admin_id, f"✅ Рассылка готова: доставлено {counts['ok']}, "
                                     f"заблокировали бота {counts['blocked']}, ошибок {counts['failed']}.")


# ─────────────────────── предложение недели ───────────────────────

@router.message(Command("reweek"))
async def reweek(message: Message, state: FSMContext) -> None:
    await state.clear()
    ids = await db.reweek_ids()
    await message.answer(
        "Предложение «неделя бесплатных тренировок в записи» получат те, кто нажал /start, "
        "но не прошёл анкету (в воронку не попал), не отписался и не заблокировал бота. "
        "Кто уже в воронке или уже получал это предложение, его не получит — оно разовое. "
        f"Получательниц: <b>{len(ids)}</b>.\n\n"
        "Так выглядит сообщение 👇")
    await message.answer(texts.REWEEK_OFFER.format(name=messages.name_of(None)),
                         reply_markup=keyboards.reweek())
    await message.answer("Отправляем?", reply_markup=keyboards.confirm_reweek())


@router.callback_query(F.data.startswith("adm:rw:"))
async def reweek_confirm(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    if cb.data.endswith("cancel"):
        await cb.message.edit_text("Рассылка отменена.")
        return
    await cb.message.edit_text("🚀 Рассылка пошла. Пришлю итог, когда закончу.")
    task = asyncio.create_task(_run_reweek(bot, cb.from_user.id))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _run_reweek(bot: Bot, admin_id: int) -> None:
    """Разово: каждой — одно предложение за всё время (ключ в журнале sends),
    сколько бы раз ни запускали /reweek. Перед отправкой — проверка канала клуба."""
    key = "reweek"
    counts = {sender.OK: 0, sender.BLOCKED: 0, sender.FAILED: 0, "in_club": 0, "dup": 0}
    for tg_id in await db.reweek_ids():
        if await payments.club_plan_by_membership(bot, tg_id):
            counts["in_club"] += 1
            continue
        if not await db.claim_send(tg_id, key):
            counts["dup"] += 1
            continue
        u = await db.get_user(tg_id)
        status = await messages.say(bot, tg_id, texts.REWEEK_OFFER.format(name=messages.name_of(u)),
                                    keyboards.reweek())
        if status == sender.FAILED:
            await db.release_send(tg_id, key)
        counts[status] += 1
    log.info("Предложение недели: %s", counts)
    await bot.send_message(
        admin_id, f"✅ Предложение недели разослано: доставлено {counts['ok']}, "
                  f"заблокировали бота {counts['blocked']}, ошибок {counts['failed']}, "
                  f"уже в клубе {counts['in_club']}, уже получали раньше {counts['dup']}.")


# ─────────────────────────────── отзывы ───────────────────────────────

async def _show_next_review(message: Message) -> None:
    f = await db.next_pending_feedback()
    if f is None:
        await message.answer("Новых отзывов нет 🎉")
        return
    who = html.escape(f["first_name"] or "—") + (f" @{f['username']}" if f["username"] else "")
    await message.answer(f"Отзыв #{f['id']} (день {f['day']}) от {who}:\n\n«{html.escape(f['text'])}»",
                         reply_markup=keyboards.moderate(f["id"]))


@router.message(Command("reviews"))
async def reviews(message: Message) -> None:
    await _show_next_review(message)


@router.callback_query(F.data.startswith("adm:fb:"))
async def moderate(cb: CallbackQuery) -> None:
    _, _, ok, fid = cb.data.split(":")
    await db.moderate_feedback(int(fid), ok == "1")
    await cb.answer("Одобрен" if ok == "1" else "Отклонён")
    await cb.message.edit_text(cb.message.html_text + ("\n\n✅ Одобрен" if ok == "1" else "\n\n❌ Отклонён"))
    await _show_next_review(cb.message)


# ─────────────────────────────── оплаты ───────────────────────────────

@router.callback_query(F.data.startswith("adm:pay:"))
async def payment_decision(cb: CallbackQuery, bot: Bot) -> None:
    _, _, ok, pid = cb.data.split(":")
    p = await db.get_payment(int(pid))
    if p is None or not await db.set_payment_status(p["id"], "paid" if ok == "1" else "rejected"):
        await cb.answer("Уже обработано", show_alert=True)
        return
    await cb.answer()
    if ok == "1":
        await payments.grant_access(bot, p["tg_id"], json.loads(p["raw"] or "{}").get("plan"))
        verdict = "\n\n✅ Доступ выдан"
    else:
        await messages.say(bot, p["tg_id"], texts.PAYMENT_REJECTED)
        verdict = "\n\n❌ Отклонено"
    await cb.message.edit_text(cb.message.html_text + verdict)


SEND_NAMES = {"workout": "тренировка", "club": "тренировка клуба", "checkin": "«Как ощущения?»", "nudge": "напоминание",
              "warm_reviews": "отзывы", "warm_stats": "итоги недели", "sale": "предложение клуба",
              "post": "письмо после недели"}


def _when(dt, tz: str) -> str:
    return dt.astimezone(funnel.zone(tz)).strftime("%d.%m %H:%M") if dt else "—"


@router.message(Command("list"))
async def people_list(message: Message) -> None:
    rows = await db.recent_people()
    if not rows:
        await message.answer("Пока никого нет.")
        return
    now = datetime.now(timezone.utc)
    lines = ["<b>Последние сёстры</b> (новые сверху). Подробно: /who id\n"]
    for r in rows:
        name = messages.name_of(r) + (f" @{html.escape(r['username'])}" if r["username"] else "")
        if r["started_at"] is None:
            where = "анкету не закончила"
        else:
            where = f"день {funnel.funnel_day(r['started_at'], now, r['tz'])}, сделала {r['done']}"
        marks = ("💰" if r["paid_at"] else "") + ("⛔" if r["blocked"] else "") + ("🔕" if r["unsubscribed"] else "")
        lines.append(f"• {name} — <code>{r['tg_id']}</code> — {where} {marks}".rstrip())
    await message.answer("\n".join(lines))


@router.message(Command("testme"))
async def testme(message: Message, state: FSMContext) -> None:
    """Проверка воронки на себе: сбрасываем только свою анкету и неделю."""
    await state.clear()
    await db.reset_own_week(message.from_user.id)
    await message.answer("🔄 Ваша бесплатная неделя сброшена. Нажмите /start — анкета начнётся заново, "
                         "как у новой сестры (уведомления «Новый лид» не будет).")


@router.message(Command("who"))
async def who(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip().lstrip("@")
    if not arg.isdigit():
        await message.answer("Формат: <code>/who 123456789</code> (id есть в сообщении о заявке и в /export)")
        return
    card = await db.person_card(int(arg))
    if card is None:
        await message.answer("Такой сестры в боте нет: она ещё ни разу не нажимала «Начать» в боте.")
        return
    u, h = card["user"], card["health"]
    tz = u["tz"]
    lines = [f"<b>{messages.name_of(u)}</b>" + (f" @{html.escape(u['username'])}" if u["username"] else "")
             + f' · <a href="tg://user?id={u["tg_id"]}">✉️ написать</a>'
             + f" (id {u['tg_id']})",
             f"Пришла: {_when(u['created_at'], tz)}, метка: {html.escape(u['source'] or '—')}"]
    if u["started_at"] is None:
        lines.append("❗ Анкету не закончила (не выбрала время тренировок)")
    else:
        day = funnel.funnel_day(u["started_at"], datetime.now(timezone.utc), tz)
        lines.append(f"Анкета: {texts.GOALS.get(u['goal'], '—')}, {texts.LEVELS.get(u['level'], '—')}, "
                     f"{texts.SLOTS.get(u['slot'], '—')}")
        lines.append(f"Старт: {_when(u['started_at'], tz)} — сегодня у неё <b>день {day}</b>")
    if h:
        limits = ", ".join(texts.LIMITS.get(x, x) for x in (h["limits"] or "").split(",") if x) or "—"
        lines.append("Здоровье: " + ("пропустила" if not h["consent_at"] else
                     f"{texts.AGES.get(h['age'], '—')}; {limits}"
                     + (f" («{html.escape(h['note'])}»)" if h["note"] else "")
                     + (f"; {h['height_cm']} см / {h['weight_kg']} кг" if h["height_cm"] else "")))
    lines.append("«Сделала»: " + (", ".join(f"№{d}" for d in card["done"]) or "пока ни одной"))
    cm = card["club"]
    if cm:
        lines.append(f"Клуб: {texts.PLAN_NAMES.get(cm['plan'], cm['plan'])}, "
                     + (f"с {_when(cm['started_at'], tz)}, следующая №{cm['next_n']}, сделала {card['club_done']}"
                        if cm["active"] else f"вышла {_when(cm['left_at'], tz)}"))
    if u["paid_at"]:
        lines.append(f"💰 Купила клуб: {_when(u['paid_at'], tz)}")
    for p in card["payments"]:
        lines.append(f"Оплата: {p['provider']}, {p['status']}, тариф {p['plan'] or '—'}, {_when(p['created_at'], tz)}")
    if u["unsubscribed"]:
        lines.append("🔕 Отписалась от рассылки")
    if u["blocked"]:
        lines.append("⛔ Заблокировала бота")
    if card["sends"]:
        lines.append("\nПоследнее отправленное:")
        for s in card["sends"]:
            kind, _, rest = s["key"].partition(":")
            day = int(rest.split(":")[0]) if rest.split(":")[0].isdigit() else 0
            what = (f"тренировка №{funnel.WORKOUT_DAYS.get(day, '?')} (день {day})" if kind == "workout"
                    else f"тренировка клуба (день клуба {day + 1})" if kind == "club"
                    else f"{SEND_NAMES.get(kind, kind)} (день {day})")
            lines.append(f"• {_when(s['sent_at'], tz)} — {what}")
    await message.answer("\n".join(lines))


@router.message(Command("purge"))
async def purge(message: Message, command: CommandObject, bot: Bot) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: <code>/purge 123456789</code> (id сестры — в /who или в сообщениях о заявках)")
        return
    n = await messages.purge_media(bot, int(arg))
    await message.answer(f"Убрано видео тренировок: {n}." if n else
                         "Видео для удаления не нашла (бот не отправлял ей видео или они уже убраны).")


@router.message(Command("grant"))
async def grant(message: Message, command: CommandObject, bot: Bot) -> None:
    args = (command.args or "").split()
    plan = f"{args[1]}x" if len(args) > 1 else config.DEFAULT_PLAN
    if not args or not args[0].isdigit() or plan not in config.PLANS:
        await message.answer("Формат: <code>/grant 123456789 3</code> или <code>/grant 123456789 6</code>\n"
                             "(Telegram id участницы из /export; 3 или 6 раз в неделю)")
        return
    tg_id = int(args[0])
    await db.add_payment(tg_id, "manual", None, None, None, "paid", {"by": message.from_user.id, "plan": plan})
    await payments.grant_access(bot, tg_id, plan)


@router.message(Command("syncclub"))
async def syncclub(message: Message, bot: Bot) -> None:
    await message.answer("🔎 Проверяю, кто из сестёр уже в группах клуба… Это займёт минуту-две.")
    found = await payments.sync_club(bot)
    if not found:
        await message.answer("Все, кто в группах клуба, уже записаны в клуб 👍")
        return
    lines = []
    for tg_id, plan in found:
        u = await db.get_user(tg_id)
        who = messages.name_of(u) + (f" @{html.escape(u['username'])}" if u and u["username"] else "")
        lines.append(f"• {who} (id {tg_id}) — {payments.plan_name(plan)}")
    await message.answer(f"✅ Записала в клуб: {len(found)}\n" + "\n".join(lines)
                         + "\n\nКаждой бот прислал приветствие клуба, дальше — напоминания об эфирах "
                           "или тренировки по её тарифу.")


# ─────────────────────────────── товары Tribute ───────────────────────────────

ROUTE_ALIASES = {"school": "school", "школа": "school", "3": "club3x", "3x": "club3x",
                 "6": "club6x", "6x": "club6x", "ignore": "ignore", "ничей": "ignore"}


@router.callback_query(F.data.startswith("adm:rt:"))
async def route_button(cb: CallbackQuery) -> None:
    _, _, route, key = cb.data.split(":", 3)
    if route not in tribute_router.ROUTES:
        return await cb.answer()
    await db.set_product_route(key, route)
    await cb.answer("Запомнила")
    await cb.message.edit_text(cb.message.html_text + f"\n\n→ {tribute_router.route_name(route)}")
    if route.startswith("club"):
        n = await tribute_router.replay_for_club(cb.bot, key, route)
        if n:
            await cb.message.answer(f"Оплат клуба по этому товару, которые пришли раньше: {n} — обработала.")


@router.message(Command("route"))
async def route_cmd(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    route = None
    if len(args) == 2:
        route = ROUTE_ALIASES.get(args[1].lower()) or (f"url:{args[1]}" if args[1].startswith("https://") else None)
    if route is None:
        await message.answer("Формат: <code>/route 12345 school</code>\n"
                             "Вместо school: 3 (клуб 3 раза), 6 (клуб 6 раз), ignore или адрес https://…")
        return
    await db.set_product_route(args[0], route)
    await message.answer(f"✅ Товар {html.escape(args[0])} → {html.escape(tribute_router.route_name(route))}")
    if route.startswith("club"):
        n = await tribute_router.replay_for_club(message.bot, args[0], route)
        if n:
            await message.answer(f"Оплат клуба по этому товару, которые пришли раньше: {n} — обработала.")


@router.message(Command("products"))
async def products(message: Message) -> None:
    lines = [f"• {key} → {tribute_router.route_name(route)} (из настроек Render)"
             for key, route in tribute_router.env_routes().items()]
    for r in await db.list_products():
        lines.append(f"• {html.escape(r['product_key'])} → {html.escape(tribute_router.route_name(r['route']))}"
                     + (f" ({html.escape(r['info'])})" if r["info"] else ""))
    await message.answer("<b>Товары Tribute</b>\n" + ("\n".join(lines) or "Пока ни одного уведомления не было.")
                         + "\n\nИзменить: <code>/route 12345 school</code>")


# ─────────────────────────────── выгрузка и тик ───────────────────────────────

@router.message(Command("export"))
async def export(message: Message) -> None:
    rows = await db.export_rows()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    if rows:
        w.writerow(rows[0].keys())
    for r in rows:
        w.writerow(["" if v is None else v for v in r.values()])
    data = buf.getvalue().encode("utf-8-sig")      # utf-8-sig — чтобы Excel открыл кириллицу
    name = f"users_{datetime.now(timezone.utc):%Y-%m-%d}.csv"
    await message.answer_document(BufferedInputFile(data, filename=name),
                                  caption=f"Участниц: {len(rows)}")


@router.message(Command("tick"))
async def tick_now(message: Message, bot: Bot) -> None:
    from .. import tick
    result = await tick.run(bot)
    await message.answer(f"Тик: {result}")
