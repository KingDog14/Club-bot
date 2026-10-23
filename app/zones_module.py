"""Операции каталога зон. Обновления всегда инвалидируют кэш."""
from __future__ import annotations
import aiosqlite
from app.config import DB_PATH
from app.config_loader import reload_config
from app.utils import now

async def add_zone(code: str, name: str, price_per_hour: int, description: str = "",
                   color: str = "⚪", sort_order: int = 0, requires_quantity: bool = True) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO zones(code,name,description,price_per_hour,color,sort_order,requires_quantity,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (code.lower().strip(), name.strip(), description.strip(), price_per_hour, color, sort_order, int(requires_quantity), now().isoformat()))
        await conn.execute("INSERT INTO admin_tariffs(name,price) VALUES(?,?)", (name.strip(), f"{price_per_hour} ₽/час"))
        await conn.commit()
        zone_id = cur.lastrowid
    await reload_config()
    return zone_id

async def update_zone(zone_id: int, **fields) -> None:
    allowed = {"name", "description", "price_per_hour", "color", "sort_order", "is_active", "requires_quantity"}
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values: return
    async with aiosqlite.connect(DB_PATH) as conn:
        old = await (await conn.execute("SELECT name FROM zones WHERE id=?", (zone_id,))).fetchone()
        await conn.execute("UPDATE zones SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
                           (*values.values(), zone_id))
        if old and ("name" in values or "price_per_hour" in values):
            name = values.get("name", old[0])
            price = values.get("price_per_hour")
            if price is None:
                row = await (await conn.execute("SELECT price_per_hour FROM zones WHERE id=?", (zone_id,))).fetchone()
                price = row[0]
            await conn.execute("UPDATE admin_tariffs SET name=?,price=? WHERE name=?",
                               (name, f"{price} ₽/час", old[0]))
        await conn.commit()
    await reload_config()

async def delete_zone(zone_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        count = (await (await conn.execute(
            "SELECT COUNT(*) FROM pcs WHERE zone_id=?", (zone_id,))).fetchone())[0]
        if count: return False
        await conn.execute("UPDATE zones SET is_active=0 WHERE id=?", (zone_id,))
        await conn.commit()
    await reload_config()
    return True
