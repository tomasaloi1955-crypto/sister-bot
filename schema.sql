-- Схема базы бота «Бесплатная неделя тренировок».
-- Бот сам выполняет этот файл при каждом запуске (всё через IF NOT EXISTS),
-- поэтому руками ничего создавать не нужно. Файл безопасен для повторного
-- запуска: существующие данные не трогает.

-- Участницы
CREATE TABLE IF NOT EXISTS users (
    tg_id            BIGINT PRIMARY KEY,
    username         TEXT,
    first_name       TEXT,
    source           TEXT,                     -- метка рекламы из /start src_xxx
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),   -- первый /start
    goal             TEXT,                     -- lose | tone | back | energy
    level            TEXT,                     -- new | before | now
    slot             TEXT,                     -- day | evening | both
    tz               TEXT NOT NULL DEFAULT 'Asia/Almaty',
    started_at       TIMESTAMPTZ,              -- анкета пройдена = день 1
    offer_started_at TIMESTAMPTZ,              -- не используется (была спеццена 72 часа)
    paid_at          TIMESTAMPTZ,              -- оплатила клуб
    unsubscribed     BOOLEAN NOT NULL DEFAULT false,
    blocked          BOOLEAN NOT NULL DEFAULT false,  -- заблокировала бота
    awaiting_feedback_day INT                  -- ждём свободный отзыв после «Как ощущения?»
);
CREATE INDEX IF NOT EXISTS users_started_idx ON users (started_at);
CREATE INDEX IF NOT EXISTS users_source_idx  ON users (source);

-- Здоровье: ответы на вопросы перед стартом (только с согласия, видит только тренер)
CREATE TABLE IF NOT EXISTS health (
    tg_id        BIGINT PRIMARY KEY REFERENCES users (tg_id),
    consent_at   TIMESTAMPTZ,                  -- согласилась ответить
    age          TEXT,                         -- u25 | 25_35 | 35_45 | 45p
    limits       TEXT NOT NULL DEFAULT '',     -- через запятую: back,knees,pressure,pregnant,postpartum,other,none
    height_cm    INT,
    weight_kg    INT,
    step         TEXT,                         -- на каком вопросе (params — ждём рост и вес текстом)
    onboarding   BOOLEAN NOT NULL DEFAULT false,  -- вопросы идут сразу после анкеты
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Тренировки: по одной на день (slot — запасное поле, обычно day)
CREATE TABLE IF NOT EXISTS workouts (
    day        INT  NOT NULL CHECK (day BETWEEN 1 AND 60),
    slot       TEXT NOT NULL CHECK (slot IN ('day', 'evening')),
    video      TEXT,                           -- file_id Telegram или ссылка
    text       TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, slot)
);

-- «✅ Сделала!» — одна отметка на день
CREATE TABLE IF NOT EXISTS completions (
    tg_id      BIGINT NOT NULL REFERENCES users (tg_id),
    day        INT    NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, day)
);

-- Ответы на «Как ощущения?» (отзывы); approved: NULL — ждёт модерации
CREATE TABLE IF NOT EXISTS feedback (
    id         BIGSERIAL PRIMARY KEY,
    tg_id      BIGINT NOT NULL REFERENCES users (tg_id),
    day        INT,
    mood       TEXT,                           -- great | ok | hard
    text       TEXT,
    approved   BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feedback_pending_idx ON feedback (approved) WHERE approved IS NULL;

-- Журнал отправок: защита от дублей (каждое плановое сообщение — один раз)
CREATE TABLE IF NOT EXISTS sends (
    tg_id   BIGINT NOT NULL REFERENCES users (tg_id),
    key     TEXT   NOT NULL,                   -- workout:5:day, sale:12, post:43 …
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tg_id, key)
);

-- Напоминания админу (чтобы каждое уходило один раз)
CREATE TABLE IF NOT EXISTS admin_alerts (
    key     TEXT PRIMARY KEY,                  -- например missing:2026-10-21:am
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Товары Tribute и кому отдавать их уведомления (бот — диспетчер одного вебхука).
-- route: school | club3x | club6x | ignore | url:https://…   NULL — ещё не решено (идёт в школу)
CREATE TABLE IF NOT EXISTS tribute_products (
    product_key TEXT PRIMARY KEY,              -- product_id или subscription_id из уведомления
    route       TEXT,
    info        TEXT,                          -- цена/валюта из первого уведомления — чтобы узнать товар
    asked_at    TIMESTAMPTZ,                   -- когда спросили админа
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Очередь пересылки уведомлений Tribute (школе и др.): досылаем, пока не примут
CREATE TABLE IF NOT EXISTS tribute_outbox (
    id           BIGSERIAL PRIMARY KEY,
    target_url   TEXT NOT NULL,
    body         TEXT NOT NULL,                -- тело уведомления как пришло (подпись к нему подходит)
    signature    TEXT NOT NULL,
    attempts     INT  NOT NULL DEFAULT 0,
    last_error   TEXT,
    next_try_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tribute_outbox_pending_idx ON tribute_outbox (next_try_at) WHERE delivered_at IS NULL;

-- Отправленные видео тренировок: удаляем из чата через MEDIA_TTL_HOURS
-- (Telegram даёт боту удалять свои сообщения только первые 48 часов)
CREATE TABLE IF NOT EXISTS media_sends (
    id           BIGSERIAL PRIMARY KEY,
    tg_id        BIGINT NOT NULL REFERENCES users (tg_id),
    workout      INT    NOT NULL,
    message_ids  BIGINT[] NOT NULL,
    is_rewatch   BOOLEAN NOT NULL DEFAULT false,   -- «Посмотреть снова» (разрешён один раз)
    delete_after TIMESTAMPTZ NOT NULL,
    deleted_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS media_sends_due_idx ON media_sends (delete_after) WHERE deleted_at IS NULL;

-- Каналы, где бот — администратор. plan: 3x | 6x — канал клуба этого тарифа,
-- none — не клуб (например, рекламный). Вступила в канал клуба = купила.
CREATE TABLE IF NOT EXISTS club_channels (
    chat_id  BIGINT PRIMARY KEY,
    title    TEXT,
    plan     TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Оплаты: tribute | stars | manual; status: paid | pending | rejected
CREATE TABLE IF NOT EXISTS payments (
    id          BIGSERIAL PRIMARY KEY,
    tg_id       BIGINT NOT NULL,
    provider    TEXT   NOT NULL,
    external_id TEXT   UNIQUE,                 -- id платежа у провайдера (защита от повторов)
    amount      NUMERIC,
    currency    TEXT,
    status      TEXT   NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw         JSONB
);
CREATE INDEX IF NOT EXISTS payments_tg_idx ON payments (tg_id);
