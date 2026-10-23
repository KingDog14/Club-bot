"""Загрузчик кэшируемого каталога клуба (настройки, зоны и компьютеры)."""
from __future__ import annotations

import asyncio
from typing import Any

import aiosqlite

from app.config import DB_PATH

_cache: dict[str, Any] = {}
_lock = asyncio.Lock()


async def reload_config() -> dict[str, Any]:
    """Перечитать из БД настройки и активный каталог; вернуть новый snapshot."""
    global _cache
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            settings = await (await conn.execute(
                "SELECT key, value FROM settings"
            )).fetchall()
            legacy_settings = await (await conn.execute(
                "SELECT key, value FROM admin_settings"
            )).fetchall()
            zones = await (await conn.execute(
                "SELECT * FROM zones WHERE is_active = 1 ORDER BY sort_order, name"
            )).fetchall()
            pcs = await (await conn.execute(
                "SELECT * FROM pcs ORDER BY sort_order, number"
            )).fetchall()
        _cache = {
            "settings": ({row["key"]: row["value"] for row in settings} |
                         {row["key"]: row["value"] for row in legacy_settings}),
            "zones": [dict(row) for row in zones],
            "pcs": [dict(row) for row in pcs],
        }
        # Ленивая связь исключает цикл импортов при старте приложения.
        from app import keyboards, texts
        texts.apply_runtime_settings(_cache["settings"])
        keyboards.apply_runtime_settings(_cache["settings"])
        return _cache


async def load_config() -> dict[str, Any]:
    """Вернуть кэш, загрузив его при первом обращении."""
    if not _cache:
        return await reload_config()
    return _cache


async def get_zones() -> list[dict[str, Any]]:
    return list((await load_config())["zones"])


async def get_pcs_by_zone(zone_id: int) -> list[dict[str, Any]]:
    return [pc for pc in (await load_config())["pcs"]
            if pc["zone_id"] == zone_id and pc["status"] == "active" and pc["is_active"] == 1]


async def get_pc_by_id(pc_id: int) -> dict[str, Any] | None:
    return next((pc for pc in (await load_config())["pcs"] if pc["id"] == pc_id), None)


async def get_available_pcs_for_time(date: str, time: str, duration: int,
                                     zone_id: int | None = None) -> list[dict[str, Any]]:
    """ПК без пересекающейся подтверждённой брони, созданной именно этим ботом."""
    from app.booking_units import available_units
    if zone_id is not None:
        return await available_units(date, time, duration, zone_id)
    result = []
    for zone in await get_zones():
        result.extend(await available_units(date, time, duration, zone["id"]))
    return result
