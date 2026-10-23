"""Сервисные операции follow-up: доступность, запросы и отмена броней."""
from __future__ import annotations
import aiosqlite
from datetime import timedelta
from app.config import DB_PATH
from app.utils import booking_start_end, effective_duration_minutes, now
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

def manage_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить место",callback_data=f"fu:add:{booking_id}")],
        [InlineKeyboardButton(text="⏱ Продлить",callback_data=f"fu:extend:{booking_id}")],
        [InlineKeyboardButton(text="⏰ Опоздаю",callback_data=f"fu:late:{booking_id}")],
        [InlineKeyboardButton(text="⏩ Приеду раньше",callback_data=f"fu:early:{booking_id}")],
        [InlineKeyboardButton(text="❌ Отменить",callback_data=f"fu:cancel:{booking_id}")],
        [InlineKeyboardButton(text="⬅️ Назад",callback_data="m:my")],
    ])

def extend_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="30 минут",callback_data=f"fu:extend_request:{booking_id}:30")],
        [InlineKeyboardButton(text="1 час",callback_data=f"fu:extend_request:{booking_id}:60"),InlineKeyboardButton(text="2 часа",callback_data=f"fu:extend_request:{booking_id}:120")],
        [InlineKeyboardButton(text="⬅️ Назад",callback_data=f"fu:manage:{booking_id}")],
    ])

def late_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 мин",callback_data=f"fu:late_request:{booking_id}:10"),InlineKeyboardButton(text="20 мин",callback_data=f"fu:late_request:{booking_id}:20")],
        [InlineKeyboardButton(text="30 мин",callback_data=f"fu:late_request:{booking_id}:30")],
        [InlineKeyboardButton(text="⬅️ Назад",callback_data=f"fu:manage:{booking_id}")],
    ])

def early_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10 мин",callback_data=f"fu:early_request:{booking_id}:10"),InlineKeyboardButton(text="20 мин",callback_data=f"fu:early_request:{booking_id}:20")],
        [InlineKeyboardButton(text="30 мин",callback_data=f"fu:early_request:{booking_id}:30")],
        [InlineKeyboardButton(text="⬅️ Назад",callback_data=f"fu:manage:{booking_id}")],
    ])

def cancel_confirm_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, отменить",callback_data=f"fu:cancel_yes:{booking_id}"),
        InlineKeyboardButton(text="↩️ Нет",callback_data=f"fu:manage:{booking_id}")]])


async def active_booking_for_user(booking_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute("SELECT * FROM bookings WHERE id=? AND user_id=? AND status='confirmed'",
                                        (booking_id,user_id))).fetchone()
    if not row: return None
    booking=dict(row)
    _,end=booking_start_end(booking["date"],booking["time"],booking["duration"],booking.get("duration_minutes",0))
    return booking if end > now() else None

async def create_request(booking_id: int,user_id: int,kind: str,value: int | None=None,payload: str="") -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur=await conn.execute("INSERT INTO booking_followups(booking_id,user_id,kind,value,payload,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                               (booking_id,user_id,kind,value,payload,now().isoformat()))
        await conn.commit(); return cur.lastrowid

async def get_request(request_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory=aiosqlite.Row
        row=await (await conn.execute("SELECT * FROM booking_followups WHERE id=?",(request_id,))).fetchone()
        return dict(row) if row else None

async def set_request_status(request_id: int,status: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur=await conn.execute("UPDATE booking_followups SET status=? WHERE id=? AND status='pending'",(status,request_id))
        await conn.commit(); return cur.rowcount==1

async def extension_conflicts(booking_id: int,additional_minutes: int) -> list[dict]:
    """Возвращает конфликтующие брони на назначенных местах при продлении."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory=aiosqlite.Row
        booking=await (await conn.execute("SELECT * FROM bookings WHERE id=? AND status='confirmed'",(booking_id,))).fetchone()
        if not booking: return []
        units=await (await conn.execute("SELECT unit_id FROM booking_units WHERE booking_id=? AND status='assigned' AND unit_id IS NOT NULL",(booking_id,))).fetchall()
        if not units and booking["assigned_pc_id"]: units=[{"unit_id":booking["assigned_pc_id"]}]
        ids=[r["unit_id"] for r in units]
        if not ids: return []
        marks=",".join("?" for _ in ids)
        others=await (await conn.execute(
            f"SELECT DISTINCT b.* FROM bookings b LEFT JOIN booking_units bu ON bu.booking_id=b.id AND bu.status='assigned' "
            f"WHERE b.status='confirmed' AND b.id!=? AND b.date>=? AND (bu.unit_id IN ({marks}) OR b.assigned_pc_id IN ({marks}))",
            (booking_id,booking["date"],*ids,*ids))).fetchall()
    start,end=booking_start_end(booking["date"],booking["time"],booking["duration"],booking["duration_minutes"])
    extended_end=end+timedelta(minutes=additional_minutes)
    conflicts=[]
    for row in others:
        other=dict(row)
        ostart,oend=booking_start_end(other["date"],other["time"],other["duration"],other.get("duration_minutes",0))
        if start < oend and ostart < extended_end: conflicts.append(other)
    return conflicts

async def approve_add_unit_request(request_id: int) -> bool:
    """Назначить добавочные места и закрыть запрос одной транзакцией."""
    import json
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        conn.row_factory=aiosqlite.Row
        request=await (await conn.execute("SELECT * FROM booking_followups WHERE id=? AND kind='add_unit' AND status='pending'",(request_id,))).fetchone()
        if not request: await conn.rollback(); return False
        try: pc_ids=json.loads(request["payload"] or "[]")
        except (TypeError,ValueError): await conn.rollback(); return False
        booking=await (await conn.execute("SELECT * FROM bookings WHERE id=? AND status='confirmed'",(request["booking_id"],))).fetchone()
        rows=await (await conn.execute("SELECT * FROM booking_units WHERE booking_id=? AND followup_id=? AND status='pending' ORDER BY id",(request["booking_id"],request_id))).fetchall()
        if not booking or len(rows)!=len(pc_ids): await conn.rollback(); return False
        _,booking_end=booking_start_end(booking["date"],booking["time"],booking["duration"],booking["duration_minutes"])
        if booking_end<=now(): await conn.rollback(); return False
        wanted=_minutes_for(dict(booking))
        for row,pc_id in zip(rows,pc_ids):
            valid=await (await conn.execute("SELECT 1 FROM pcs WHERE id=? AND zone_id=? AND status='active' AND is_active=1",(pc_id,booking["zone_id"]))).fetchone()
            if not valid: await conn.rollback(); return False
            conflicts=await (await conn.execute("SELECT b.time,b.duration,b.duration_minutes FROM bookings b LEFT JOIN booking_units bu ON bu.booking_id=b.id AND bu.status='assigned' WHERE COALESCE(bu.unit_id,b.assigned_pc_id)=? AND b.status='confirmed' AND b.date=? AND b.id!=?",(pc_id,booking["date"],booking["id"]))).fetchall()
            if any(wanted & _minutes_for({"date":booking["date"],"time":x["time"],"duration":x["duration"],"duration_minutes":x["duration_minutes"]}) for x in conflicts):
                await conn.rollback(); return False
            await conn.execute("UPDATE booking_units SET unit_id=?,status='assigned' WHERE id=?",(pc_id,row["id"]))
        await conn.execute("UPDATE bookings SET pcs=pcs+? WHERE id=?",(len(pc_ids),booking["id"]))
        await conn.execute("UPDATE booking_followups SET status='approved' WHERE id=?",(request_id,))
        await conn.commit(); return True


def _minutes_for(booking: dict) -> set[int]:
    start=int(booking["time"][:2])*60+int(booking["time"][3:5])
    length=int(booking.get("duration_minutes") or 0) or int(booking.get("duration",1))*60
    return {(start+i)%1440 for i in range(length)}


async def cancel_booking(booking_id: int,user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        cur=await conn.execute("UPDATE bookings SET status='cancelled' WHERE id=? AND user_id=? AND status='confirmed'",
                               (booking_id,user_id))
        if not cur.rowcount: await conn.rollback(); return False
        await conn.execute("UPDATE booking_units SET status='cancelled' WHERE booking_id=?",(booking_id,))
        await conn.execute("UPDATE booking_followups SET status='cancelled' WHERE booking_id=? AND status='pending'",(booking_id,))
        await conn.commit(); return True

async def approve_extension(request_id: int) -> tuple[bool,dict | None]:
    """Атомарно применить продление, если заявка pending и бронь ещё активна."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        conn.row_factory=aiosqlite.Row
        req=await (await conn.execute("SELECT * FROM booking_followups WHERE id=? AND status='pending'",(request_id,))).fetchone()
        if not req or req["kind"]!="extend": await conn.rollback(); return False,None
        booking=await (await conn.execute("SELECT * FROM bookings WHERE id=? AND status='confirmed'",(req["booking_id"],))).fetchone()
        if not booking: await conn.rollback(); return False,None
        add=int(req["value"] or 0)
        start,end=booking_start_end(booking["date"],booking["time"],booking["duration"],booking["duration_minutes"])
        if end<=now(): await conn.rollback(); return False,None
        units=await (await conn.execute("SELECT unit_id FROM booking_units WHERE booking_id=? AND status='assigned' AND unit_id IS NOT NULL",(booking["id"],))).fetchall()
        ids=[r["unit_id"] for r in units] or ([booking["assigned_pc_id"]] if booking["assigned_pc_id"] else [])
        if ids:
            marks=','.join('?' for _ in ids)
            others=await (await conn.execute(f"SELECT b.* FROM bookings b LEFT JOIN booking_units bu ON bu.booking_id=b.id AND bu.status='assigned' WHERE b.status='confirmed' AND b.id!=? AND b.date>=? AND (bu.unit_id IN ({marks}) OR b.assigned_pc_id IN ({marks}))",(booking['id'],booking['date'],*ids,*ids))).fetchall()
            extended_end=end+timedelta(minutes=add)
            for row in others:
                other=dict(row); other_start,other_end=booking_start_end(other['date'],other['time'],other['duration'],other.get('duration_minutes',0))
                if start<other_end and other_start<extended_end:
                    await conn.rollback(); return False,None
        total=effective_duration_minutes(dict(booking))+add
        hours=total//60
        await conn.execute("UPDATE bookings SET duration=?,duration_minutes=? WHERE id=?",(hours,total,booking['id']))
        await conn.execute("UPDATE booking_followups SET status='approved' WHERE id=? AND status='pending'",(request_id,))
        await conn.commit()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory=aiosqlite.Row
        updated=await (await conn.execute("SELECT * FROM bookings WHERE id=?",(booking['id'],))).fetchone()
        return True,dict(updated)
