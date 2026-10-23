#!/usr/bin/env python3
"""Первичная загрузка JSON-конфига клуба. Запуск: python setup_new_club.py --config configs/demo.json"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path

# Импорт БД должен происходить уже с отключённым демо-сидированием.
os.environ["SEED_DEMO_DATA"] = "0"

import aiosqlite
import app.database as db
from app.config import DB_PATH
from app.utils import now


def validate(data: dict) -> None:
    if not isinstance(data.get("club"), dict): raise ValueError("Нужен объект club")
    if not data.get("zones"): raise ValueError("Добавьте хотя бы одну зону")
    codes = {zone.get("code") for zone in data["zones"]}
    for zone in data["zones"]:
        if not zone.get("code") or not zone.get("name") or int(zone.get("price", -1)) < 0:
            raise ValueError("У каждой зоны обязательны code, name, price >= 0")
    for pc in data.get("pcs", []):
        if not pc.get("number") or pc.get("zone") not in codes:
            raise ValueError(f"ПК {pc.get('number')} ссылается на неизвестную зону")


async def setup(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    validate(data)
    await db.init_db()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        stamp = now().isoformat()
        settings = {f"club_{key}": str(value) for key, value in data["club"].items()}
        settings.update({"welcome_text": data.get("welcome_text", ""),
                         "faq_json": json.dumps(data.get("faq", {}), ensure_ascii=False)})
        await conn.executemany(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            ((key, value, stamp) for key, value in settings.items()))
        zone_ids = {}
        for i, zone in enumerate(data["zones"]):
            await conn.execute(
                "INSERT INTO zones(code,name,description,price_per_hour,color,sort_order,requires_quantity,created_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name, "
                "description=excluded.description, price_per_hour=excluded.price_per_hour, "
                "color=excluded.color, sort_order=excluded.sort_order, requires_quantity=excluded.requires_quantity, is_active=1",
                (zone["code"], zone["name"], zone.get("description", ""), int(zone["price"]),
                 zone.get("color", "⚪"), int(zone.get("sort_order", i)), int(zone.get("requires_quantity", True)), stamp))
            row = await (await conn.execute("SELECT id FROM zones WHERE code=?", (zone["code"],))).fetchone()
            zone_ids[zone["code"]] = row[0]
            price = f"{int(zone['price'])} ₽/час"
            await conn.execute("INSERT INTO admin_tariffs(name,price) SELECT ?,? WHERE NOT EXISTS "
                               "(SELECT 1 FROM admin_tariffs WHERE name=?)", (zone["name"], price, zone["name"]))
        for pc in data.get("pcs", []):
            number, zone_id = str(pc["number"]), zone_ids[pc["zone"]]
            existing = await (await conn.execute(
                "SELECT id FROM pcs WHERE number=? AND zone_id=? AND is_active=1", (number, zone_id))).fetchone()
            values = (pc.get("label", ""), pc.get("notes", ""), int(pc.get("sort_order", 0)))
            if existing:
                await conn.execute("UPDATE pcs SET label=?,notes=?,sort_order=? WHERE id=?",
                                   (*values, existing[0]))
            else:
                await conn.execute(
                    "INSERT INTO pcs(number,zone_id,label,notes,sort_order,created_at) VALUES(?,?,?,?,?,?)",
                    (number, zone_id, *values, stamp))
        for question, answer in data.get("faq", {}).items():
            await conn.execute("INSERT INTO admin_faq(question,answer) SELECT ?,? WHERE NOT EXISTS "
                               "(SELECT 1 FROM admin_faq WHERE question=?)", (question, answer, question))
        await conn.commit()
    print(f"Клуб «{data['club'].get('name', 'Без названия')}» загружен: {len(data['zones'])} зон, {len(data.get('pcs', []))} ПК.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(setup(args.config))
