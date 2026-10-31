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
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import config, db, funnel, keyboards, messages, payments, sender, texts, tribute_router

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.chat.type == "private", F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))
_background: set[asyncio.Task] = set()

HELP = (
    "<b>Админка</b>\n\n"
    "/stats — статистика и источники\n"
    "/upload 2 — загрузить тренировку №2 (всего 3)\n"
    "/workouts — какие тренировки загружены\n"
    "/show 2 — посмотреть тренировку как участница\n"
    "/broadcast — рассылка по сегменту\n"
    "/reviews — модерация отзывов\n"
    "/list — последние 30 сестёр: id, день, сколько сделала, купила ли\n"
    "/who 123456789 — где сейчас сестра: анкета, тренировки, оплата\n"
    "/purge 123456789 — убрать у сестры из чата все видео тренировок сейчас\n"
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


async def _ask_content(message: Message, state: FSMContext, day: int, slot: str) -> None:
    await state.set_state(Upload.content)
    await state.update_data(day=day, slot=slot)
    await message.answer(
        f"Тренировка №{day}.\n\n"
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


@router.message(Upload.content, F.text)
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


@router.message(Upload.text, F.text)
async def upload_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    text = "" if message.text.strip() == "-" else message.html_text
    await _save(message, state, bot, data["video"], text)


async def _save(message: Message, state: FSMContext, bot: Bot, video: str | None, text: str) -> None:
    data = await state.get_data()
    await db.save_workout(data["day"], data["slot"], video, text.strip())
    await state.clear()
    await message.answer(f"✅ Сохранено: тренировка №{data['day']}. Вот как увидит участница:")
    await messages.send_workout(bot, message.from_user.id, data["day"], data["slot"], track=False)


@router.message(Command("workouts"))
async def workouts(message: Message) -> None:
    rows = await db.list_workouts()
    have: dict[int, bool] = {}
    for r in rows:
        have[r["day"]] = have.get(r["day"], False) or r["has_video"]

    def mark(d: int) -> str:
        return "🎬" if have.get(d) else "📝" if d in have else "·"
    lines = [f"{d:>2}: {mark(d)}" for d in range(1, funnel.WORKOUTS + 1)]
    await message.answer("<b>Тренировки</b> (🎬 видео, 📝 только текст, · нет)\n<code>"
                         + "\n".join(lines) + "</code>")


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


@router.message(Broadcast.message)
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


SEND_NAMES = {"workout": "тренировка", "checkin": "«Как ощущения?»", "nudge": "напоминание",
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
                     + (f"; {h['height_cm']} см / {h['weight_kg']} кг" if h["height_cm"] else "")))
    lines.append("«Сделала»: " + (", ".join(f"№{d}" for d in card["done"]) or "пока ни одной"))
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
                             "(Telegram id участницы из /export; 3 — 3 раза в неделю, 6 — каждый день)")
        return
    tg_id = int(args[0])
    await db.add_payment(tg_id, "manual", None, None, None, "paid", {"by": message.from_user.id, "plan": plan})
    await payments.grant_access(bot, tg_id, plan)


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
                             "Вместо school: 3 (клуб 3 раза), 6 (клуб каждый день), ignore или адрес https://…")
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
