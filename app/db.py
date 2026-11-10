"""Работа с PostgreSQL (Neon / Supabase) через asyncpg."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import asyncpg

from . import config, funnel

pool: asyncpg.Pool | None = None
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


async def connect() -> None:
    global pool
    # statement_cache_size=0 — чтобы работало и через пулер Neon/Supabase (pgbouncer).
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5,
                                     statement_cache_size=0, command_timeout=30)
    async with pool.acquire() as con:
        await con.execute(SCHEMA.read_text(encoding="utf-8"))


async def ping() -> None:
    await _p().fetchval("SELECT 1")


async def close() -> None:
    if pool is not None:
        await pool.close()


def _p() -> asyncpg.Pool:
    assert pool is not None, "база не подключена"
    return pool


# ─────────────────────────────── участницы ───────────────────────────────

async def upsert_user(tg_id: int, username: str | None, first_name: str | None,
                      source: str | None) -> asyncpg.Record:
    """Создать участницу или обновить имя. Метку источника не перезаписываем:
    засчитываем ту рекламу, которая привела первой.
    Поле inserted = true, если участница создана только что (первый /start)."""
    return await _p().fetchrow(
        """INSERT INTO users (tg_id, username, first_name, source)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (tg_id) DO UPDATE
             SET username = EXCLUDED.username,
                 first_name = EXCLUDED.first_name,
                 source = COALESCE(users.source, EXCLUDED.source),
                 blocked = false
           RETURNING *, (xmax = 0) AS inserted""",
        tg_id, username, first_name, source)


async def reset_own_week(tg_id: int) -> None:
    """/testme: админ проходит бесплатную неделю заново — анкета, здоровье, отметки,
    журнал отправок. Оплаты, клуб и метка источника не трогаются."""
    async with _p().acquire() as con, con.transaction():
        for table in ("sends", "completions", "feedback", "health"):
            await con.execute(f"DELETE FROM {table} WHERE tg_id = $1", tg_id)
        await con.execute(
            """UPDATE users SET goal = NULL, level = NULL, slot = NULL, started_at = NULL,
                      first_now = false, awaiting_feedback_day = NULL, unsubscribed = false
               WHERE tg_id = $1""", tg_id)


async def get_user(tg_id: int) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM users WHERE tg_id = $1", tg_id)


ALLOWED_FIELDS = {"goal", "level", "slot", "tz", "unsubscribed", "blocked",
                  "awaiting_feedback_day", "first_now"}


async def set_field(tg_id: int, name: str, value) -> None:
    assert name in ALLOWED_FIELDS
    await _p().execute(f"UPDATE users SET {name} = $2 WHERE tg_id = $1", tg_id, value)


async def start_month(tg_id: int, slot: str) -> asyncpg.Record:
    """Анкета пройдена: запоминаем время и дату старта (день 1)."""
    return await _p().fetchrow(
        """UPDATE users SET slot = $2, started_at = COALESCE(started_at, now())
           WHERE tg_id = $1 RETURNING *""", tg_id, slot)


async def funnel_users() -> list[asyncpg.Record]:
    """Кому тик вообще может что-то слать, с данными о выполнениях."""
    return await _p().fetch(
        """SELECT u.*, COALESCE(c.last_day, 0) AS last_done_day
           FROM users u
           LEFT JOIN (SELECT tg_id, max(day) AS last_day FROM completions GROUP BY tg_id) c
             USING (tg_id)
           WHERE u.started_at IS NOT NULL AND NOT u.blocked AND NOT u.unsubscribed""")


# ─────────────────────────────── здоровье ───────────────────────────────

HEALTH_FIELDS = {"consent_at", "age", "limits", "height_cm", "weight_kg", "step", "onboarding", "note"}


async def get_health(tg_id: int) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM health WHERE tg_id = $1", tg_id)


async def set_health(tg_id: int, **fields) -> asyncpg.Record:
    assert fields and set(fields) <= HEALTH_FIELDS
    names = list(fields)
    cols = ", ".join(names)
    params = ", ".join(f"${i + 2}" for i in range(len(names)))
    updates = ", ".join(f"{n} = EXCLUDED.{n}" for n in names)
    return await _p().fetchrow(
        f"""INSERT INTO health (tg_id, {cols}) VALUES ($1, {params})
            ON CONFLICT (tg_id) DO UPDATE SET {updates}, updated_at = now()
            RETURNING *""",
        tg_id, *fields.values())


# ─────────────────────────────── товары Tribute и пересылка ───────────────────────────────

async def product_routes(keys: list[str]) -> dict[str, str | None]:
    rows = await _p().fetch("SELECT product_key, route FROM tribute_products WHERE product_key = ANY($1)", keys)
    return {r["product_key"]: r["route"] for r in rows}


async def remember_product(key: str, info: str) -> bool:
    """Записать незнакомый товар. True — админа ещё не спрашивали (спросить сейчас)."""
    await _p().execute(
        "INSERT INTO tribute_products (product_key, info) VALUES ($1, $2) ON CONFLICT DO NOTHING", key, info)
    row = await _p().fetchrow(
        """UPDATE tribute_products SET asked_at = now()
           WHERE product_key = $1 AND asked_at IS NULL AND route IS NULL RETURNING product_key""", key)
    return row is not None


async def set_product_route(key: str, route: str) -> None:
    await _p().execute(
        """INSERT INTO tribute_products (product_key, route) VALUES ($1, $2)
           ON CONFLICT (product_key) DO UPDATE SET route = EXCLUDED.route""", key, route)


async def list_products() -> list[asyncpg.Record]:
    return await _p().fetch("SELECT * FROM tribute_products ORDER BY created_at")


async def outbox_add(target_url: str, body: str, signature: str) -> int:
    return await _p().fetchval(
        "INSERT INTO tribute_outbox (target_url, body, signature) VALUES ($1, $2, $3) RETURNING id",
        target_url, body, signature)


async def outbox_due(limit: int = 50) -> list[asyncpg.Record]:
    return await _p().fetch(
        """SELECT * FROM tribute_outbox WHERE delivered_at IS NULL AND next_try_at <= now()
           ORDER BY id LIMIT $1""", limit)


async def outbox_recent(days: int = 14) -> list[asyncpg.Record]:
    return await _p().fetch(
        "SELECT * FROM tribute_outbox WHERE created_at > now() - make_interval(days => $1) ORDER BY id", days)


async def outbox_get(oid: int) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM tribute_outbox WHERE id = $1", oid)


async def outbox_done(oid: int) -> None:
    await _p().execute("UPDATE tribute_outbox SET delivered_at = now(), attempts = attempts + 1 WHERE id = $1", oid)


async def outbox_failed(oid: int, error: str, delay_minutes: int) -> int:
    return await _p().fetchval(
        """UPDATE tribute_outbox SET attempts = attempts + 1, last_error = $2,
               next_try_at = now() + make_interval(mins => $3)
           WHERE id = $1 RETURNING attempts""", oid, error[:500], delay_minutes)


# ─────────────────────────────── видео в чатах (автоудаление) ───────────────────────────────

async def media_add(tg_id: int, workout: int, message_ids: list[int], is_rewatch: bool, ttl_hours: int,
                    club: bool = False) -> None:
    await _p().execute(
        """INSERT INTO media_sends (tg_id, workout, message_ids, is_rewatch, delete_after, club)
           VALUES ($1, $2, $3, $4, now() + make_interval(hours => $5), $6)""",
        tg_id, workout, message_ids, is_rewatch, ttl_hours, club)


async def media_due(limit: int = 200) -> list[asyncpg.Record]:
    return await _p().fetch(
        """SELECT * FROM media_sends WHERE deleted_at IS NULL AND delete_after <= now()
           ORDER BY id LIMIT $1""", limit)


async def media_club_kept(limit: int = 200) -> list[asyncpg.Record]:
    """Видео клуба, отправленные раньше «навсегда» (до автоудаления клуба)."""
    return await _p().fetch(
        """SELECT * FROM media_sends WHERE deleted_at IS NULL AND club
             AND delete_after > now() + interval '1 year' ORDER BY id LIMIT $1""", limit)


async def media_set_delete_after(media_id: int, ttl_hours: int) -> None:
    await _p().execute(
        "UPDATE media_sends SET delete_after = created_at + make_interval(hours => $2) WHERE id = $1",
        media_id, ttl_hours)


async def media_of(tg_id: int) -> list[asyncpg.Record]:
    return await _p().fetch("SELECT * FROM media_sends WHERE tg_id = $1 ORDER BY id", tg_id)


async def media_mark_deleted(mid: int) -> None:
    await _p().execute("UPDATE media_sends SET deleted_at = now() WHERE id = $1", mid)


async def media_rewatch_state(tg_id: int, workout: int, club: bool = False) -> tuple[bool, bool]:
    """(видео этой тренировки ей отправлялось, повтор уже был)."""
    row = await _p().fetchrow(
        """SELECT count(*) > 0 AS sent, bool_or(is_rewatch) AS rewatched
           FROM media_sends WHERE tg_id = $1 AND workout = $2 AND club = $3""", tg_id, workout, club)
    return bool(row["sent"]), bool(row["rewatched"])


# ─────────────────────────────── журнал отправок ───────────────────────────────

async def already_sent(pairs: list[tuple[int, str]]) -> set[tuple[int, str]]:
    if not pairs:
        return set()
    rows = await _p().fetch(
        """SELECT s.tg_id, s.key FROM sends s
           JOIN unnest($1::bigint[], $2::text[]) AS c(tg_id, key) USING (tg_id, key)""",
        [p[0] for p in pairs], [p[1] for p in pairs])
    return {(r["tg_id"], r["key"]) for r in rows}


async def claim_send(tg_id: int, key: str) -> bool:
    """True — ключ наш, можно отправлять; False — уже отправлено кем-то."""
    row = await _p().fetchrow(
        """INSERT INTO sends (tg_id, key) VALUES ($1, $2)
           ON CONFLICT DO NOTHING RETURNING key""", tg_id, key)
    return row is not None


async def release_send(tg_id: int, key: str) -> None:
    """Отправка не удалась по временной причине — попробуем на следующем тике."""
    await _p().execute("DELETE FROM sends WHERE tg_id = $1 AND key = $2", tg_id, key)


# ─────────────────────────────── тренировки ───────────────────────────────

async def get_workout(day: int, slot: str) -> asyncpg.Record | None:
    """Тренировка дня; нет нужного слота — берём другой слот того же дня."""
    return await _p().fetchrow(
        """SELECT * FROM workouts WHERE day = $1
           ORDER BY (slot = $2) DESC LIMIT 1""", day, slot)


async def save_workout(day: int, slot: str, video: str | None, text: str) -> None:
    await _p().execute(
        """INSERT INTO workouts (day, slot, video, text) VALUES ($1, $2, $3, $4)
           ON CONFLICT (day, slot) DO UPDATE
             SET video = EXCLUDED.video, text = EXCLUDED.text, updated_at = now()""",
        day, slot, video, text)


async def replace_workout(day: int, video: str | None, text: str) -> None:
    """Заменить тренировку бесплатной недели целиком: старые варианты «днём» и
    «вечером» убираем, чтобы никому не пришло прежнее видео."""
    async with _p().acquire() as con, con.transaction():
        await con.execute("DELETE FROM workouts WHERE day = $1", day)
        await con.execute("INSERT INTO workouts (day, slot, video, text) VALUES ($1, 'day', $2, $3)",
                          day, video, text)


async def recent_club_workouts(limit: int = 6) -> list[asyncpg.Record]:
    return await _p().fetch(
        "SELECT n, video IS NOT NULL AS has_video, text, updated_at FROM club_workouts ORDER BY n DESC LIMIT $1",
        limit)


async def workout_days() -> set[int]:
    """Дни, на которые загружена хоть одна тренировка."""
    return {r["day"] for r in await _p().fetch("SELECT DISTINCT day FROM workouts")}


async def claim_alert(key: str) -> bool:
    row = await _p().fetchrow(
        "INSERT INTO admin_alerts (key) VALUES ($1) ON CONFLICT DO NOTHING RETURNING key", key)
    return row is not None


async def list_workouts() -> list[asyncpg.Record]:
    return await _p().fetch("SELECT day, slot, video IS NOT NULL AS has_video FROM workouts ORDER BY day, slot")


# ─────────────────────────────── клуб ───────────────────────────────

async def save_club_workout(n: int, video: str | None, text: str) -> None:
    await _p().execute(
        """INSERT INTO club_workouts (n, video, text) VALUES ($1, $2, $3)
           ON CONFLICT (n) DO UPDATE SET video = EXCLUDED.video, text = EXCLUDED.text, updated_at = now()""",
        n, video, text)


async def delete_club_workout(n: int) -> bool:
    """Отменить последнюю загрузку: только если это последний номер и его ещё никому не слали."""
    row = await _p().fetchrow(
        """DELETE FROM club_workouts WHERE n = $1
             AND n = (SELECT max(n) FROM club_workouts)
             AND NOT EXISTS (SELECT 1 FROM club_members WHERE next_n > $1)
           RETURNING n""", n)
    return row is not None


async def get_club_workout(n: int) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM club_workouts WHERE n = $1", n)


async def club_workout_numbers() -> dict[int, bool]:
    """№ → есть ли видео."""
    rows = await _p().fetch("SELECT n, video IS NOT NULL AS has_video FROM club_workouts ORDER BY n")
    return {r["n"]: r["has_video"] for r in rows}


async def club_activate(tg_id: int, plan: str) -> bool:
    """Купила/продлила клуб. True — только что стала (или снова стала) участницей:
    новая начинает с №1, вернувшаяся продолжает с того номера, где остановилась."""
    return await _p().fetchval(
        """WITH old AS (SELECT active FROM club_members WHERE tg_id = $1)
           INSERT INTO club_members (tg_id, plan) VALUES ($1, $2)
           ON CONFLICT (tg_id) DO UPDATE SET plan = EXCLUDED.plan, active = true, left_at = NULL,
               started_at = CASE WHEN club_members.active THEN club_members.started_at ELSE now() END
           RETURNING COALESCE((SELECT NOT active FROM old), true)""", tg_id, plan)


async def club_deactivate(tg_id: int) -> bool:
    """Вышла из клуба. True — была активной."""
    row = await _p().fetchrow(
        """UPDATE club_members SET active = false, left_at = now()
           WHERE tg_id = $1 AND active RETURNING tg_id""", tg_id)
    return row is not None


async def users_outside_club() -> list[int]:
    """Все, кто не в клубе (для /syncclub: вдруг их добавили в группу клуба руками)."""
    rows = await _p().fetch(
        """SELECT u.tg_id FROM users u
           WHERE NOT u.blocked
             AND NOT EXISTS (SELECT 1 FROM club_members m WHERE m.tg_id = u.tg_id AND m.active)""")
    return [r["tg_id"] for r in rows]


async def club_member(tg_id: int) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM club_members WHERE tg_id = $1", tg_id)


async def club_members_active() -> list[asyncpg.Record]:
    return await _p().fetch(
        """SELECT m.*, u.slot, u.tz, u.first_name, u.username
           FROM club_members m JOIN users u USING (tg_id)
           WHERE m.active AND NOT u.blocked""")


async def club_set_repeat(tg_id: int, n: int) -> None:
    await _p().execute("UPDATE club_members SET repeat_n = $2 WHERE tg_id = $1", tg_id, n)


async def slot_counts() -> dict:
    """Сколько выбрали утро (day) и вечер (evening): все сёстры и участницы клуба."""
    rows = await _p().fetch(
        """SELECT u.slot, count(*) AS all_n,
                  count(*) FILTER (WHERE EXISTS (SELECT 1 FROM club_members m
                                                 WHERE m.tg_id = u.tg_id AND m.active)) AS club_n
           FROM users u WHERE u.slot IS NOT NULL AND NOT u.blocked GROUP BY u.slot""")
    return {r["slot"]: (r["all_n"], r["club_n"]) for r in rows}


async def already_sent_any(key: str) -> bool:
    return await _p().fetchval("SELECT EXISTS (SELECT 1 FROM sends WHERE key = $1)", key)


async def get_setting(key: str) -> str | None:
    return await _p().fetchval("SELECT value FROM settings WHERE key = $1", key)


async def del_setting(key: str) -> None:
    await _p().execute("DELETE FROM settings WHERE key = $1", key)


async def set_setting(key: str, value: str) -> None:
    await _p().execute(
        """INSERT INTO settings (key, value) VALUES ($1, $2)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", key, value)


async def club_advance(tg_id: int) -> None:
    await _p().execute("UPDATE club_members SET next_n = next_n + 1 WHERE tg_id = $1", tg_id)


async def club_backfill(default_plan: str) -> int:
    """Купившие до появления клуба в боте — тоже участницы (тариф из последней оплаты)."""
    rows = await _p().fetch(
        """INSERT INTO club_members (tg_id, plan)
           SELECT u.tg_id, COALESCE((SELECT p.raw->>'plan' FROM payments p WHERE p.tg_id = u.tg_id
                                      AND p.status = 'paid' AND p.raw->>'plan' IN ('3x', '6x')
                                      ORDER BY p.id DESC LIMIT 1), $1)
           FROM users u WHERE u.paid_at IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM club_members m WHERE m.tg_id = u.tg_id)
           RETURNING tg_id""", default_plan)
    return len(rows)


async def club_mark_done(tg_id: int, n: int) -> tuple[bool, int]:
    async with _p().acquire() as con:
        new = await con.fetchrow(
            "INSERT INTO club_done (tg_id, n) VALUES ($1, $2) ON CONFLICT DO NOTHING RETURNING n", tg_id, n)
        total = await con.fetchval("SELECT count(*) FROM club_done WHERE tg_id = $1", tg_id)
    return new is not None, total


# ─────────────────────────────── «Сделала» ───────────────────────────────

async def mark_done(tg_id: int, day: int) -> tuple[bool, int]:
    """(новая ли отметка, всего отметок)."""
    async with _p().acquire() as con:
        new = await con.fetchrow(
            """INSERT INTO completions (tg_id, day) VALUES ($1, $2)
               ON CONFLICT DO NOTHING RETURNING day""", tg_id, day)
        total = await con.fetchval("SELECT count(*) FROM completions WHERE tg_id = $1", tg_id)
    return new is not None, total


async def done_count(tg_id: int) -> int:
    return await _p().fetchval("SELECT count(*) FROM completions WHERE tg_id = $1", tg_id)


# ─────────────────────────────── отзывы ───────────────────────────────

async def add_feedback(tg_id: int, day: int, mood: str) -> None:
    await _p().execute("INSERT INTO feedback (tg_id, day, mood) VALUES ($1, $2, $3)",
                       tg_id, day, mood)


async def add_feedback_text(tg_id: int, day: int, text: str) -> int:
    """Дописать текст к последнему ответу этого дня (или создать новый). Вернёт id."""
    row = await _p().fetchrow(
        """UPDATE feedback SET text = $3 WHERE id = (
               SELECT id FROM feedback WHERE tg_id = $1 AND day = $2 AND text IS NULL
               ORDER BY id DESC LIMIT 1)
           RETURNING id""", tg_id, day, text)
    if row:
        return row["id"]
    return await _p().fetchval(
        "INSERT INTO feedback (tg_id, day, text) VALUES ($1, $2, $3) RETURNING id",
        tg_id, day, text)


async def get_feedback(fid: int) -> asyncpg.Record | None:
    return await _p().fetchrow(
        """SELECT f.*, u.first_name, u.username FROM feedback f
           JOIN users u USING (tg_id) WHERE f.id = $1""", fid)


async def next_pending_feedback() -> asyncpg.Record | None:
    return await _p().fetchrow(
        """SELECT f.*, u.first_name, u.username FROM feedback f JOIN users u USING (tg_id)
           WHERE f.approved IS NULL AND f.text IS NOT NULL AND f.text <> ''
           ORDER BY f.id LIMIT 1""")


async def moderate_feedback(fid: int, approved: bool) -> None:
    await _p().execute("UPDATE feedback SET approved = $2 WHERE id = $1", fid, approved)


async def approved_reviews(exclude_tg: int, limit: int = 3) -> list[asyncpg.Record]:
    return await _p().fetch(
        """SELECT f.text, u.first_name FROM feedback f JOIN users u USING (tg_id)
           WHERE f.approved AND f.tg_id <> $1 ORDER BY random() LIMIT $2""",
        exclude_tg, limit)


# ─────────────────────────────── оплаты ───────────────────────────────

async def add_payment(tg_id: int, provider: str, external_id: str | None, amount,
                      currency: str | None, status: str, raw: dict | None = None) -> int | None:
    """Новая запись об оплате. None — такой платёж уже был (повтор вебхука)."""
    return await _p().fetchval(
        """INSERT INTO payments (tg_id, provider, external_id, amount, currency, status, raw)
           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
           ON CONFLICT (external_id) DO NOTHING RETURNING id""",
        tg_id, provider, external_id, amount, currency, status,
        json.dumps(raw or {}, ensure_ascii=False))


async def get_payment(pid: int) -> asyncpg.Record | None:
    return await _p().fetchrow("SELECT * FROM payments WHERE id = $1", pid)


async def set_payment_status(pid: int, status: str, only_if: str = "pending") -> bool:
    row = await _p().fetchrow(
        "UPDATE payments SET status = $2 WHERE id = $1 AND status = $3 RETURNING id",
        pid, status, only_if)
    return row is not None


async def pending_claim(tg_id: int) -> asyncpg.Record | None:
    return await _p().fetchrow(
        """SELECT * FROM payments WHERE tg_id = $1 AND provider = 'manual'
           AND status = 'pending' ORDER BY id DESC LIMIT 1""", tg_id)


async def paid_plan(tg_id: int) -> str | None:
    """Тариф последней подтверждённой оплаты."""
    return await _p().fetchval(
        """SELECT raw->>'plan' FROM payments WHERE tg_id = $1 AND status = 'paid'
           ORDER BY id DESC LIMIT 1""", tg_id)


async def remember_channel(chat_id: int, title: str | None) -> None:
    await _p().execute(
        """INSERT INTO club_channels (chat_id, title) VALUES ($1, $2)
           ON CONFLICT (chat_id) DO UPDATE SET title = EXCLUDED.title""", chat_id, title)


async def set_channel_plan(chat_id: int, plan: str) -> None:
    await _p().execute("UPDATE club_channels SET plan = $2 WHERE chat_id = $1", chat_id, plan)


async def move_channel(old_id: int, new_id: int) -> None:
    """Telegram переделал группу в супергруппу — у неё новый id. Тариф переносим."""
    async with _p().acquire() as con, con.transaction():
        old = await con.fetchrow("SELECT * FROM club_channels WHERE chat_id = $1", old_id)
        if old is None:
            return
        await con.execute(
            """INSERT INTO club_channels (chat_id, title, plan) VALUES ($1, $2, $3)
               ON CONFLICT (chat_id) DO UPDATE SET plan = COALESCE(club_channels.plan, EXCLUDED.plan),
                                                   title = COALESCE(club_channels.title, EXCLUDED.title)""",
            new_id, old["title"], old["plan"])
        await con.execute("DELETE FROM club_channels WHERE chat_id = $1", old_id)


async def club_channels() -> list[asyncpg.Record]:
    return await _p().fetch("SELECT * FROM club_channels ORDER BY added_at")


async def mark_paid(tg_id: int) -> bool:
    """Отметить покупку. True — впервые. Участницы, которая ни разу не
    открывала бота, может не быть в таблице — тогда создаём её."""
    return await _p().fetchval(
        """WITH old AS (SELECT paid_at FROM users WHERE tg_id = $1)
           INSERT INTO users (tg_id, paid_at) VALUES ($1, now())
           ON CONFLICT (tg_id) DO UPDATE SET paid_at = COALESCE(users.paid_at, now())
           RETURNING (SELECT paid_at FROM old) IS NULL""", tg_id)


# ─────────────────────────────── админка ───────────────────────────────

async def user_id_by_username(username: str) -> int | None:
    """Ник без @ → id (без учёта регистра). None — такого ника в боте нет."""
    return await _p().fetchval(
        "SELECT tg_id FROM users WHERE lower(username) = lower($1) ORDER BY created_at DESC LIMIT 1",
        username)


async def person_card(tg_id: int) -> dict | None:
    """Всё о сестре для /who: анкета, здоровье, отметки, отправленное, оплаты."""
    u = await get_user(tg_id)
    if u is None:
        return None
    p = _p()
    return {
        "user": u,
        "health": await get_health(tg_id),
        "done": await p.fetch(
            "SELECT day, created_at FROM completions WHERE tg_id = $1 ORDER BY day", tg_id),
        "sends": await p.fetch(
            "SELECT key, sent_at FROM sends WHERE tg_id = $1 ORDER BY sent_at DESC LIMIT 8", tg_id),
        "club": await club_member(tg_id),
        "club_done": await p.fetch(
            "SELECT n, created_at FROM club_done WHERE tg_id = $1 ORDER BY created_at DESC", tg_id),
        "payments": await p.fetch(
            "SELECT provider, status, raw->>'plan' AS plan, created_at FROM payments "
            "WHERE tg_id = $1 ORDER BY id DESC LIMIT 3", tg_id),
    }


async def done_between(start: datetime, end: datetime) -> list[asyncpg.Record]:
    """Все нажатия «Сделала» за отрезок времени (для /today): и неделя, и клуб."""
    return await _p().fetch(
        """SELECT d.tg_id, d.club, d.num, d.created_at, u.first_name, u.username
             FROM (SELECT tg_id, false AS club, day AS num, created_at FROM completions
                   UNION ALL
                   SELECT tg_id, true AS club, n AS num, created_at FROM club_done) d
             JOIN users u ON u.tg_id = d.tg_id
            WHERE d.created_at >= $1 AND d.created_at < $2
            ORDER BY d.created_at""", start, end)


SEGMENTS = {
    "all": "TRUE",
    "notpaid": "u.paid_at IS NULL",
    "paid": "u.paid_at IS NOT NULL",
    "inactive": """u.started_at IS NOT NULL AND u.paid_at IS NULL AND NOT EXISTS (
                     SELECT 1 FROM completions c WHERE c.tg_id = u.tg_id
                     AND c.created_at > now() - interval '7 days')""",
}


async def segment_ids(segment: str) -> list[int]:
    where = SEGMENTS[segment]
    rows = await _p().fetch(
        f"SELECT u.tg_id FROM users u WHERE NOT u.blocked AND NOT u.unsubscribed AND {where}")
    return [r["tg_id"] for r in rows]


# ─────────────────────── предложение недели (/reweek) ───────────────────────

async def reweek_ids() -> list[int]:
    """Кому предложить бесплатную неделю: нажали /start, но анкету не прошли —
    в воронку не попали. Разово: кто уже получал предложение — больше не получит.
    Кто уже в воронке или купил клуб — не трогаем."""
    rows = await _p().fetch(
        """SELECT u.tg_id FROM users u
           WHERE u.started_at IS NULL AND u.paid_at IS NULL AND NOT u.blocked AND NOT u.unsubscribed
             AND NOT EXISTS (SELECT 1 FROM club_members m WHERE m.tg_id = u.tg_id AND m.active)
             AND NOT EXISTS (SELECT 1 FROM sends s WHERE s.tg_id = u.tg_id AND s.key LIKE 'reweek%')""")
    return [r["tg_id"] for r in rows]


async def stats() -> dict:
    p = _p()
    total = await p.fetchrow(
        """SELECT count(*) AS users,
                  count(started_at) AS surveyed,
                  count(paid_at) AS paid,
                  count(*) FILTER (WHERE blocked) AS blocked,
                  count(*) FILTER (WHERE unsubscribed) AS unsubscribed,
                  count(*) FILTER (WHERE created_at > now() - interval '1 day') AS new_today
           FROM users""")
    by_source = await p.fetch(
        """SELECT COALESCE(source, '—') AS source, count(*) AS users,
                  count(started_at) AS surveyed, count(paid_at) AS paid
           FROM users GROUP BY 1 ORDER BY users DESC LIMIT 30""")
    by_day = await p.fetch("SELECT day, count(*) AS n FROM completions WHERE day <= $1 GROUP BY day ORDER BY day", funnel.WORKOUTS)
    started = await p.fetch("SELECT tg_id, started_at, tz FROM users WHERE started_at IS NOT NULL")
    done_total = await p.fetchval("SELECT count(*) FROM completions WHERE day <= $1", funnel.WORKOUTS)
    active_7d = await p.fetchval(
        "SELECT count(DISTINCT tg_id) FROM completions WHERE created_at > now() - interval '7 days'")
    by_plan = await p.fetch(
        """SELECT COALESCE(raw->>'plan', '?') AS plan, count(DISTINCT tg_id) AS n
           FROM payments WHERE status = 'paid' GROUP BY 1 ORDER BY 1""")
    club = await p.fetch("SELECT plan, count(*) AS n FROM club_members WHERE active GROUP BY plan ORDER BY plan")
    return {"total": total, "by_source": by_source, "by_plan": by_plan, "club": club, "by_day": by_day,
            "started": started, "done_total": done_total, "active_7d": active_7d}


async def recent_people(limit: int = 30) -> list[asyncpg.Record]:
    return await _p().fetch(
        """SELECT u.tg_id, u.username, u.first_name, u.started_at, u.tz, u.paid_at, u.blocked,
                  u.unsubscribed, u.created_at,
                  (SELECT count(*) FROM completions c WHERE c.tg_id = u.tg_id) AS done
           FROM users u ORDER BY u.created_at DESC LIMIT $1""", limit)


async def export_rows() -> list[asyncpg.Record]:
    return await _p().fetch(
        """SELECT u.tg_id, u.username, u.first_name, u.source, u.goal, u.level, u.slot, u.tz,
                  u.created_at, u.started_at, u.offer_started_at, u.paid_at,
                  u.unsubscribed, u.blocked,
                  (SELECT count(*) FROM completions c WHERE c.tg_id = u.tg_id) AS done,
                  h.age, h.limits, h.height_cm, h.weight_kg
           FROM users u LEFT JOIN health h USING (tg_id) ORDER BY u.created_at""")
