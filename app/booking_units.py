"""Транзакционные операции над множеством ПК, назначенных одной броне."""
from __future__ import annotations
import aiosqlite
from app.config import DB_PATH
from app.database import STATUS_CONFIRMED, STATUS_NEW, STATUS_REJECTED
from app.utils import now


def _minutes(date: str, time: str, duration: int, duration_minutes: int = 0) -> set[int]:
    start = int(time[:2]) * 60 + int(time[3:5])
    length = duration_minutes or max(1, duration) * 60
    return {(start + i) % 1440 for i in range(length)}


async def units_for_booking(booking_id: int) -> list[dict]:
    """Связанные места; поддерживает legacy booking без строк booking_units."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT * FROM booking_units WHERE booking_id=? ORDER BY sort_order,id", (booking_id,)
        )).fetchall()
        booking = await (await conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))).fetchone()
        if rows:
            result = [dict(row) for row in rows]
            if booking and booking["assigned_pc_id"] and not any(row["status"] == "assigned" for row in result):
                result[0]["unit_id"] = booking["assigned_pc_id"]
                result[0]["status"] = "assigned"
            return result
        if not booking:
            return []
        count = max(1, booking["pcs"])
        assigned = booking["assigned_pc_id"]
        requested = booking["pc_id"]
        return [{"id": None, "booking_id": booking_id,
                 "unit_id": assigned if i == 0 else None,
                 "requested_unit_id": requested if i == 0 else None,
                 "status": "assigned" if i == 0 and assigned else "pending", "sort_order": i}
                for i in range(count)]


async def available_units(date: str, time: str, duration: int, zone_id: int,
                          exclude_booking_id: int | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        units = await (await conn.execute(
            "SELECT * FROM pcs WHERE zone_id=? AND status='active' AND is_active=1 ORDER BY sort_order,number",
            (zone_id,),
        )).fetchall()
        bookings = await (await conn.execute(
            "SELECT b.id,b.date,b.time,b.duration,b.duration_minutes,COALESCE(bu.unit_id,b.assigned_pc_id) AS unit_id "
            "FROM bookings b LEFT JOIN booking_units bu ON bu.booking_id=b.id AND bu.status='assigned' "
            "WHERE b.status='confirmed' AND b.date=? AND "
            "COALESCE(bu.unit_id,b.assigned_pc_id) IS NOT NULL AND (? IS NULL OR b.id!=?)",
            (date, exclude_booking_id, exclude_booking_id),
        )).fetchall()
    wanted_minutes = _minutes(date, time, duration)
    busy = {row["unit_id"] for row in bookings
            if wanted_minutes & _minutes(row["date"], row["time"], row["duration"], row["duration_minutes"])}
    return [dict(unit) for unit in units if unit["id"] not in busy]


async def _booking_and_pending(conn, booking_id: int):
    conn.row_factory = aiosqlite.Row
    booking = await (await conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))).fetchone()
    rows = await (await conn.execute(
        "SELECT * FROM booking_units WHERE booking_id=? AND status='pending' ORDER BY sort_order,id",
        (booking_id,),
    )).fetchall()
    return booking, rows


async def confirm_all(booking_id: int) -> tuple[bool, int, int]:
    """Назначить все requested/any места и подтвердить атомарно; (ok, assigned, requested)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        booking, rows = await _booking_and_pending(conn, booking_id)
        if not booking or booking["status"] != STATUS_NEW or not rows:
            await conn.rollback(); return False, 0, len(rows)
        zone_id = booking["zone_id"]
        if not zone_id:
            await conn.rollback(); return False, 0, len(rows)
        units = await (await conn.execute(
            "SELECT id FROM pcs WHERE zone_id=? AND status='active' AND is_active=1 ORDER BY sort_order,number",
            (zone_id,),
        )).fetchall()
        wanted = _minutes(booking["date"], booking["time"], booking["duration"], booking["duration_minutes"])
        conflicts = await (await conn.execute(
            "SELECT COALESCE(bu.unit_id,b.assigned_pc_id) AS unit_id,b.time,b.duration,b.duration_minutes "
            "FROM bookings b LEFT JOIN booking_units bu ON bu.booking_id=b.id AND bu.status='assigned' "
            "WHERE b.status='confirmed' AND COALESCE(bu.unit_id,b.assigned_pc_id) IS NOT NULL AND b.date=?",
            (booking["date"],),
        )).fetchall()
        busy = {row["unit_id"] for row in conflicts
                if wanted & _minutes(booking["date"], row["time"], row["duration"], row["duration_minutes"])}
        used: set[int] = set()
        assignment: list[tuple[int, int]] = []
        active_ids = {row["id"] for row in units}
        for row in rows:
            wanted_id = row["requested_unit_id"]
            if wanted_id is not None:
                candidate = wanted_id if wanted_id in active_ids and wanted_id not in busy and wanted_id not in used else None
            else:
                candidate = next((unit["id"] for unit in units
                                  if unit["id"] not in busy and unit["id"] not in used), None)
            if candidate is None:
                await conn.rollback()
                return False, len(assignment), len(rows)
            used.add(candidate); assignment.append((row["id"], candidate))
        for row_id, unit_id in assignment:
            await conn.execute("UPDATE booking_units SET unit_id=?,status='assigned' WHERE id=? AND status='pending'",
                               (unit_id, row_id))
        first_id = assignment[0][1]
        await conn.execute("UPDATE bookings SET status=?,assigned_pc_id=? WHERE id=? AND status=?",
                           (STATUS_CONFIRMED, first_id, booking_id, STATUS_NEW))
        await conn.commit()
        return True, len(assignment), len(rows)


async def assign_partial(booking_id: int, unit_ids: list[int]) -> tuple[bool, list[int], int]:
    """Подтвердить выбранное количество мест; оставшиеся строки отметить cancelled."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        booking, rows = await _booking_and_pending(conn, booking_id)
        if not booking or booking["status"] != STATUS_NEW or not rows:
            await conn.rollback(); return False, [], len(rows)
        unit_ids = list(dict.fromkeys(unit_ids))
        if not unit_ids or len(unit_ids) > len(rows):
            await conn.rollback(); return False, [], len(rows)
        ids = {row["id"] for row in await (await conn.execute(
            f"SELECT id FROM pcs WHERE id IN ({','.join('?' for _ in unit_ids)}) "
            "AND zone_id=? AND is_active=1 AND status='active'", (*unit_ids, booking["zone_id"])
        )).fetchall()}
        if len(ids) != len(unit_ids):
            await conn.rollback(); return False, [], len(rows)
        wanted = _minutes(booking["date"], booking["time"], booking["duration"], booking["duration_minutes"])
        conflicts = await (await conn.execute(
            "SELECT COALESCE(bu.unit_id,b.assigned_pc_id) AS unit_id,b.time,b.duration,b.duration_minutes "
            "FROM bookings b LEFT JOIN booking_units bu ON bu.booking_id=b.id AND bu.status='assigned' "
            "WHERE b.status='confirmed' AND COALESCE(bu.unit_id,b.assigned_pc_id) IS NOT NULL AND b.date=?",
            (booking["date"],),
        )).fetchall()
        busy = {row["unit_id"] for row in conflicts
                if wanted & _minutes(booking["date"], row["time"], row["duration"], row["duration_minutes"])}
        if ids & busy:
            await conn.rollback(); return False, [], len(rows)
        for index, row in enumerate(rows):
            if index < len(unit_ids):
                await conn.execute("UPDATE booking_units SET unit_id=?,status='assigned' WHERE id=?",
                                   (unit_ids[index], row["id"]))
            else:
                await conn.execute("UPDATE booking_units SET status='cancelled' WHERE id=?", (row["id"],))
        await conn.execute("UPDATE bookings SET status=?,assigned_pc_id=? WHERE id=?",
                           (STATUS_CONFIRMED, unit_ids[0], booking_id))
        await conn.commit()
        return True, unit_ids, len(rows)


async def add_pending_units(booking_id: int, unit_ids: list[int], followup_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        count = (await (await conn.execute("SELECT COUNT(*) FROM booking_units WHERE booking_id=?", (booking_id,))).fetchone())[0]
        await conn.executemany(
            "INSERT INTO booking_units(booking_id,requested_unit_id,status,sort_order,followup_id) VALUES(?,?,'pending',?,?)",
            [(booking_id, unit_id, count + i, followup_id) for i, unit_id in enumerate(unit_ids)])
        await conn.commit()


async def assign_additional_units(booking_id: int, requested_ids: list[int]) -> bool:
    """Подтвердить ожидающие дополнительные места у уже подтверждённой брони."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        conn.row_factory = aiosqlite.Row
        booking = await (await conn.execute("SELECT * FROM bookings WHERE id=? AND status='confirmed'", (booking_id,))).fetchone()
        if not booking: await conn.rollback(); return False
        rows = await (await conn.execute("SELECT * FROM booking_units WHERE booking_id=? AND status='pending' ORDER BY id", (booking_id,))).fetchall()
        if len(rows) != len(requested_ids): await conn.rollback(); return False
        wanted = _minutes(booking["date"], booking["time"], booking["duration"], booking["duration_minutes"])
        for row, unit_id in zip(rows, requested_ids):
            valid = await (await conn.execute("SELECT 1 FROM pcs WHERE id=? AND zone_id=? AND is_active=1 AND status='active'", (unit_id, booking["zone_id"]))).fetchone()
            if not valid: await conn.rollback(); return False
            conflicts = await (await conn.execute("SELECT bu.unit_id,b.time,b.duration,b.duration_minutes FROM booking_units bu JOIN bookings b ON b.id=bu.booking_id WHERE b.status='confirmed' AND bu.status='assigned' AND bu.unit_id=? AND b.date=? AND b.id!=?", (unit_id,booking["date"],booking_id))).fetchall()
            if any(wanted & _minutes(booking["date"], x["time"], x["duration"], x["duration_minutes"]) for x in conflicts):
                await conn.rollback(); return False
            await conn.execute("UPDATE booking_units SET unit_id=?,status='assigned' WHERE id=?", (unit_id,row["id"]))
        await conn.commit()
        return True
