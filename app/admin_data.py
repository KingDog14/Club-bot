"""Запросы данных, используемые только новой inline-панелью администратора."""
from __future__ import annotations

from datetime import date, timedelta
import aiosqlite

from app.config import DB_PATH, FAQ_BASE, TARIFFS
from app.utils import now


async def list_clients(kind: str = "all", query: str = "") -> list[dict]:
    where, args = [], []
    if query:
        where.append("(lower(COALESCE(name,'')) LIKE ? OR lower(COALESCE(username,'')) LIKE ? OR COALESCE(phone,'') LIKE ?)")
        q = f"%{query.lower().lstrip('@')}%"
        args.extend((q, q, f"%{query}%"))
    cutoff = (now().date() - timedelta(days=30)).isoformat()
    if kind == "regular": where.append("visits >= 5")
    elif kind == "inactive": where.append("(last_visit IS NULL OR last_visit < ?)"); args.append(cutoff)
    elif kind == "new": where.append("substr(created_at,1,10) >= ?"); args.append(cutoff)
    sql = "SELECT * FROM clients" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY COALESCE(last_visit, created_at) DESC"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(sql, args)).fetchall()
        return [dict(r) for r in rows]


async def client_rating(user_id: int) -> float | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        row = await (await conn.execute("SELECT AVG(rating) FROM reviews WHERE user_id=?", (user_id,))).fetchone()
        return round(row[0], 1) if row and row[0] is not None else None


async def client_bookings(user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT * FROM bookings WHERE user_id=? ORDER BY date DESC,time DESC", (user_id,))).fetchall()
        return [dict(r) for r in rows]


async def set_blocked(user_id: int, blocked: bool) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE clients SET blocked=? WHERE user_id=?", (int(blocked), user_id)); await conn.commit()


async def update_booking(booking_id: int, field: str, value: str | int) -> None:
    if field not in {"date", "time", "duration", "pcs"}: raise ValueError("bad field")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(f"UPDATE bookings SET {field}=? WHERE id=?", (value, booking_id)); await conn.commit()


async def audience(kind: str) -> list[int]:
    return [c["user_id"] for c in await list_clients(kind) if not c.get("blocked")]


async def new_clients_count(year: int, month: int) -> int:
    prefix = f"{year:04d}-{month:02d}"
    async with aiosqlite.connect(DB_PATH) as conn:
        row = await (await conn.execute("SELECT COUNT(*) FROM clients WHERE substr(created_at,1,7)=?", (prefix,))).fetchone()
        return row[0]


async def setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as conn:
        row = await (await conn.execute("SELECT value FROM admin_settings WHERE key=?", (key,))).fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("INSERT INTO admin_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)); await conn.commit()


async def tariffs() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT * FROM admin_tariffs ORDER BY id")).fetchall()
        if not rows:
            await conn.executemany("INSERT INTO admin_tariffs(name,price) VALUES(?,?)", [(n, p) for n, p in TARIFFS]); await conn.commit()
            rows = await (await conn.execute("SELECT * FROM admin_tariffs ORDER BY id")).fetchall()
        return [dict(r) for r in rows]


async def update_tariff(tid: int, price: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE admin_tariffs SET price=? WHERE id=?", (price, tid)); await conn.commit()


async def add_tariff(name: str, price: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("INSERT INTO admin_tariffs(name,price) VALUES(?,?)", (name, price)); await conn.commit()


async def faqs() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT * FROM admin_faq ORDER BY id")).fetchall()
        if not rows:
            await conn.executemany("INSERT INTO admin_faq(question,answer) VALUES(?,?)", [(v['question'], v['answer']) for v in FAQ_BASE.values()]); await conn.commit()
            rows = await (await conn.execute("SELECT * FROM admin_faq ORDER BY id")).fetchall()
        return [dict(r) for r in rows]


async def update_faq(fid: int, answer: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE admin_faq SET answer=? WHERE id=?", (answer, fid)); await conn.commit()


async def add_faq(question: str, answer: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("INSERT INTO admin_faq(question,answer) VALUES(?,?)", (question, answer)); await conn.commit()
