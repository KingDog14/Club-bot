"""Операции над ПК, soft delete и массовый импорт CSV/текста."""
from __future__ import annotations
import csv
import io
import aiosqlite
from app.config import DB_PATH
from app.config_loader import reload_config
from app.utils import now

async def add_pc(number: str, zone_id: int, label: str = "", notes: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "INSERT INTO pcs(number,zone_id,label,notes,created_at) VALUES(?,?,?,?,?)",
            (str(number).strip(), zone_id, label.strip(), notes.strip(), now().isoformat()))
        await conn.commit()
        pc_id = cur.lastrowid
    await reload_config()
    return pc_id

async def update_pc(pc_id: int, number: str, zone_id: int, label: str = "", notes: str = "") -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE pcs SET number=?,zone_id=?,label=?,notes=? WHERE id=? AND is_active=1",
                           (str(number).strip(), zone_id, label.strip(), notes.strip(), pc_id))
        await conn.commit()
    await reload_config()

async def update_pc_status(pc_id: int, status: str) -> None:
    if status not in {"active", "maintenance", "disabled"}:
        raise ValueError("Недопустимый статус ПК")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE pcs SET status=? WHERE id=? AND is_active=1", (status, pc_id))
        await conn.commit()
    await reload_config()

async def soft_delete_pc(pc_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE pcs SET is_active=0 WHERE id=?", (pc_id,))
        await conn.commit()
    await reload_config()

def parse_pc_import(text: str) -> list[dict[str, str]]:
    """Формат строк: number,zone_code,label; пустые строки и заголовок пропускаются."""
    result = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not any(cell.strip() for cell in row): continue
        if row[0].strip().lower() in {"number", "номер"}: continue
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            raise ValueError("Каждая строка должна содержать номер ПК и код зоны")
        result.append({"number": row[0].strip(), "zone": row[1].strip().lower(),
                       "label": row[2].strip() if len(row) > 2 else ""})
    if not result: raise ValueError("Не найдено ни одного ПК для импорта")
    return result

async def import_pcs(rows: list[dict[str, str]]) -> int:
    """Импортировать атомарно; неизвестная зона отменяет импорт целиком."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        zones = {row[0]: row[1] for row in await (await conn.execute(
            "SELECT code,id FROM zones WHERE is_active=1")).fetchall()}
        if any(row["zone"] not in zones for row in rows):
            raise ValueError("В импорте есть неизвестный код зоны")
        for row in rows:
            await conn.execute(
                "INSERT INTO pcs(number,zone_id,label,created_at) VALUES(?,?,?,?)",
                (row["number"], zones[row["zone"]], row.get("label", ""), now().isoformat()))
        await conn.commit()
    await reload_config()
    return len(rows)
