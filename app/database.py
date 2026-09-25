"""
База данных SQLite (через aiosqlite).

Таблицы:
  clients  — user_id, username, name, phone, visits, last_visit, created_at
  bookings — id, user_id, date, time, duration, pcs, status, created_at
  reviews  — id, user_id, rating, text, created_at

При первом запуске база предзаполняется ДЕМО-данными (SEED_DEMO_DATA = True),
чтобы владелец клуба сразу увидел /today, /week и /stats «живыми».
"""
from __future__ import annotations

import logging
from datetime import date as _date
from datetime import timedelta as _timedelta

import aiosqlite

from app.config import DB_PATH, SEED_DEMO_DATA
from app.utils import now

logger = logging.getLogger(__name__)

# Статусы броней и их отображение
STATUS_NEW = "new"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_NOSHOW = "noshow"

STATUS_LABELS = {
    STATUS_NEW: "⏳ ожидает",
    STATUS_CONFIRMED: "✅ подтверждено",
    STATUS_REJECTED: "❌ отклонено",
    STATUS_CANCELLED: "🚫 отменено",
    STATUS_NOSHOW: "👻 неявка",
}

# Активные (занимают место)
STATUSES_ACTIVE = (STATUS_NEW, STATUS_CONFIRMED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    name       TEXT,
    phone      TEXT,
    visits     INTEGER NOT NULL DEFAULT 0,
    last_visit TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    date       TEXT NOT NULL,          -- ГГГГ-ММ-ДД
    time       TEXT NOT NULL,          -- ЧЧ:ММ
    duration   INTEGER NOT NULL,       -- часов
    pcs        INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL           -- ISO, нужен для /stats за месяц
);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL,       -- 1..5
    text       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_tariffs (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    price TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_faq (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(date);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status);
"""


# ──────────────────────────────
# Инициализация и демо-данные
# ──────────────────────────────

async def init_db() -> None:
    """Создать таблицы (если их нет) и предзаполнить демо-данными."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # Неблокирующая миграция: старые базы продолжают работать.
        columns = {row[1] for row in await (await db.execute("PRAGMA table_info(clients) ")).fetchall()}
        if "blocked" not in columns:
            await db.execute("ALTER TABLE clients ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        await db.commit()
    if SEED_DEMO_DATA:
        await _seed_demo_data()


async def _seed_demo_data() -> None:
    """
    Вымышленные данные для демонстрации владельцу клуба.
    Заполняются один раз — только если таблица clients пустая.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM clients")
        (count,) = await cur.fetchone()
        if count:
            return

        today = now().date().isoformat()
        created = now().isoformat()

        # Демо-клиенты (user_id вымышленные — таких пользователей не существует)
        demo_clients = [
            (900000001, "ivan_demo", "Иван", "+7 (000) 111-11-11", 12, today, created),
            (900000002, "maria_demo", "Мария", "+7 (000) 222-22-22", 5, today, created),
            (900000003, "alex_demo", "Алексей", None, 7, today, created),
        ]
        await db.executemany(
            "INSERT INTO clients (user_id, username, name, phone, visits, last_visit, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            demo_clients,
        )

        # Демо-брони на сегодня
        demo_bookings = [
            (900000001, today, "18:00", 3, 2, STATUS_CONFIRMED, created),  # 18:00–21:00
            (900000002, today, "20:00", 3, 1, STATUS_NEW, created),        # 20:00–23:00
            (900000003, today, "22:00", 4, 3, STATUS_CONFIRMED, created),  # 22:00–02:00
        ]
        await db.executemany(
            "INSERT INTO bookings (user_id, date, time, duration, pcs, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            demo_bookings,
        )

        # Демо-отзывы
        demo_reviews = [
            (900000001, 5, "Всё круто, ПК мощные", created),
            (900000002, 4, "Хорошо, но хотелось бы больше снеков", created),
        ]
        await db.executemany(
            "INSERT INTO reviews (user_id, rating, text, created_at) VALUES (?, ?, ?, ?)",
            demo_reviews,
        )

        await db.commit()
    logger.info("База предзаполнена демо-данными (3 клиента, 3 брони, 2 отзыва)")


# ──────────────────────────────
# Клиенты
# ──────────────────────────────

async def upsert_client(user_id: int, username: str | None, name: str | None) -> None:
    """Добавить клиента при первом обращении или обновить username/name."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO clients (user_id, username, name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                name     = COALESCE(clients.name, excluded.name)
            """,
            (user_id, username, name, now().isoformat()),
        )
        await db.commit()


async def set_client_name(user_id: int, name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clients SET name = ? WHERE user_id = ?", (name, user_id))
        await db.commit()


async def set_client_phone(user_id: int, phone: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE clients SET phone = ? WHERE user_id = ?", (phone, user_id))
        await db.commit()


async def add_visit(user_id: int) -> None:
    """+1 визит и отметка last_visit — вызывается при подтверждении брони."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE clients SET visits = visits + 1, last_visit = ? WHERE user_id = ?",
            (now().date().isoformat(), user_id),
        )
        await db.commit()


async def get_client(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM clients WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_client_ids() -> list[int]:
    """Все user_id — для рассылки /broadcast."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM clients")
        return [r[0] for r in await cur.fetchall()]


# ──────────────────────────────
# Брони
# ──────────────────────────────

async def create_booking(
    user_id: int, date: str, time: str, duration: int, pcs: int
) -> int:
    """Создать заявку в статусе 'new', вернуть её id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO bookings (user_id, date, time, duration, pcs, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, date, time, duration, pcs, STATUS_NEW, now().isoformat()),
        )
        await db.commit()
        return cur.lastrowid


async def get_booking(booking_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_booking_status(booking_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id)
        )
        await db.commit()


async def get_bookings_for_date(date: str) -> list[dict]:
    """Брони конкретного дня (для /today), отсортированные по времени."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM bookings WHERE date = ? ORDER BY time", (date,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def get_week_load(date_from: str, days: int) -> dict[str, dict]:
    """
    Загрузка по дням (для /week): количество активных броней и ПК·часов.
    Возвращает {дата: {"count": N, "pc_hours": H}}.
    """
    date_to = (_date.fromisoformat(date_from) + _timedelta(days=days - 1)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in STATUSES_ACTIVE)
        cur = await db.execute(
            f"""
            SELECT date, COUNT(*) AS cnt, SUM(duration * pcs) AS pc_hours
            FROM bookings
            WHERE date BETWEEN ? AND ? AND status IN ({placeholders})
            GROUP BY date
            """,
            (date_from, date_to, *STATUSES_ACTIVE),
        )
        return {r["date"]: {"count": r["cnt"], "pc_hours": r["pc_hours"] or 0}
                for r in await cur.fetchall()}


async def get_future_confirmed(date_from: str) -> list[dict]:
    """Подтверждённые брони с сегодняшнего дня — для восстановления задач планировщика."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM bookings WHERE date >= ? AND status = ?",
            (date_from, STATUS_CONFIRMED),
        )
        return [dict(r) for r in await cur.fetchall()]


# ──────────────────────────────
# Отзывы
# ──────────────────────────────

async def add_review(user_id: int, rating: int, text: str | None = None) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reviews (user_id, rating, text, created_at) VALUES (?, ?, ?, ?)",
            (user_id, rating, text, now().isoformat()),
        )
        await db.commit()


async def get_reviews_summary() -> dict:
    """Средняя оценка и количество отзывов (для /stats)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*), AVG(rating) FROM reviews"
        )
        count, avg = await cur.fetchone()
        return {"count": count or 0, "avg": round(avg, 1) if avg else None}


# ──────────────────────────────
# Статистика
# ──────────────────────────────

async def get_month_stats(year: int, month: int) -> dict:
    """Статистика за месяц по дате создания заявки (для /stats)."""
    prefix = f"{year:04d}-{month:02d}"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            "SELECT status, COUNT(*) AS cnt FROM bookings"
            " WHERE substr(created_at, 1, 7) = ? GROUP BY status",
            (prefix,),
        )
        by_status = {r["status"]: r["cnt"] for r in await cur.fetchall()}

        cur = await db.execute("SELECT COUNT(*) FROM clients")
        (clients_total,) = await cur.fetchone()

    return {
        "total": sum(by_status.values()),
        "confirmed": by_status.get(STATUS_CONFIRMED, 0),
        "rejected": by_status.get(STATUS_REJECTED, 0),
        "cancelled": by_status.get(STATUS_CANCELLED, 0),
        "noshow": by_status.get(STATUS_NOSHOW, 0),
        "clients_total": clients_total,
    }
