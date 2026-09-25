"""
Админские обработчики:
  кнопки карточки заявки (✅ Подтвердить / ❌ Отклонить)
  /today     — все брони на сегодня
  /week      — загрузка на неделю
  /stats     — заявки / подтверждено / отменено / неявки за месяц
  /broadcast — рассылка по базе клиентов

Доступ — по списку ADMINS (ADMIN_ID + OWNER_ID из config.py).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import app.database as db
import app.texts as texts
from app.config import ADMIN_ID, CLUB_PCS_TOTAL, CLUB_PHONE, OWNER_ID
from app.keyboards import broadcast_confirm_kb
from app.scheduler import cancel_booking_jobs, schedule_booking_jobs
from app.states import BroadcastForm
from app.utils import MONTHS, WEEKDAYS, end_time_str, human_date, load_bar, now

from .common import edit_card_verdict, plural

logger = logging.getLogger(__name__)
router = Router()

ADMINS = {ADMIN_ID, OWNER_ID}

# Знаменатель загрузки дня: такое число «ПК·часов» считаем 100 % загрузкой
_HOURS_FULL_DAY = 12


class IsAdmin(BaseFilter):
    """Пропускает апдейты только от админа/владельца."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user) and event.from_user.id in ADMINS


# ════════════════════════════════════════
# Карточка заявки: подтвердить / отклонить
# ════════════════════════════════════════

@router.callback_query(F.data.startswith("adm:ok:"), IsAdmin())
async def admin_confirm(cb: CallbackQuery) -> None:
    booking = await db.get_booking(int(cb.data.rsplit(":", 1)[1]))
    if not booking:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if booking["status"] != db.STATUS_NEW:
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    await db.set_booking_status(booking["id"], db.STATUS_CONFIRMED)
    await db.add_visit(booking["user_id"])          # +1 визит клиенту
    await edit_card_verdict(cb, "✅ <b>Подтверждена</b>")

    # Клиенту — «✅ Бронь подтверждена»
    try:
        await cb.bot.send_message(
            booking["user_id"],
            texts.T(texts.CLIENT_CONFIRMED.format(
                date=human_date(booking["date"]), time=booking["time"])),
        )
    except TelegramAPIError as exc:
        logger.warning("Клиенту %s не доставлено подтверждение: %s",
                       booking["user_id"], exc)

    # Напоминание за 2 часа + запрос отзыва после сеанса
    schedule_booking_jobs(cb.bot, booking)
    logger.info("Заявка #%s подтверждена админом %s", booking["id"], cb.from_user.id)
    await cb.answer("Подтверждено ✅")


@router.callback_query(F.data.startswith("adm:no:"), IsAdmin())
async def admin_reject(cb: CallbackQuery) -> None:
    booking = await db.get_booking(int(cb.data.rsplit(":", 1)[1]))
    if not booking:
        await cb.answer("Заявка не найдена", show_alert=True)
        return
    if booking["status"] != db.STATUS_NEW:
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    await db.set_booking_status(booking["id"], db.STATUS_REJECTED)
    cancel_booking_jobs(booking["id"])  # на всякий случай снимаем задачи
    await edit_card_verdict(cb, "❌ <b>Отклонена</b>")

    # Клиенту — «❌ На это время мест нет»
    try:
        await cb.bot.send_message(
            booking["user_id"],
            texts.T(texts.CLIENT_REJECTED.format(phone=CLUB_PHONE)),
        )
    except TelegramAPIError as exc:
        logger.warning("Клиенту %s не доставлен отказ: %s", booking["user_id"], exc)

    logger.info("Заявка #%s отклонена админом %s", booking["id"], cb.from_user.id)
    await cb.answer("Отклонено")


# ════════════════════════════════════════
# /today — все брони на сегодня
# ════════════════════════════════════════

@router.message(Command("today"), IsAdmin())
async def cmd_today(message: Message) -> None:
    today_iso = now().date().isoformat()
    bookings = await db.get_bookings_for_date(today_iso)

    # Имена/username клиентов одним проходом
    clients: dict[int, dict] = {}
    for booking in bookings:
        uid = booking["user_id"]
        if uid not in clients:
            clients[uid] = await db.get_client(uid) or {}

    active_lines = []
    closed_count = 0
    for booking in bookings:
        if booking["status"] not in db.STATUSES_ACTIVE:
            closed_count += 1
            continue
        client = clients.get(booking["user_id"]) or {}
        name = client.get("name") or f"id {booking['user_id']}"
        if client.get("username"):
            name += f" (@{client['username']})"
        time_range = f"{booking['time']}–{end_time_str(booking['time'], booking['duration'])}"
        mark = "⏳" if booking["status"] == db.STATUS_NEW else "✅"
        active_lines.append(f"{mark} <b>{time_range}</b> · {booking['pcs']} ПК · {name}")

    text = f"📅 <b>Брони на сегодня</b> · {human_date(today_iso)}\n\n"
    text += "\n".join(active_lines) if active_lines else "Пока пусто — активных заявок нет."
    legend = "\n\n⏳ — ожидает подтверждения · ✅ — подтверждено"
    if any(l.startswith("⏳") for l in active_lines):
        text += legend
    if closed_count:
        text += f"\n🚫 Отменено/отклонено: {closed_count}"

    await message.answer(text)


# ════════════════════════════════════════
# /week — загрузка на неделю
# ════════════════════════════════════════

@router.message(Command("week"), IsAdmin())
async def cmd_week(message: Message) -> None:
    base = now().date()
    load = await db.get_week_load(base.isoformat(), 7)
    day_capacity = max(1, CLUB_PCS_TOTAL * _HOURS_FULL_DAY)  # ПК·часов = 100 % загрузки

    lines = ["📈 <b>Загрузка на неделю</b>\n"]
    for i in range(7):
        day = base + timedelta(days=i)
        info = load.get(day.isoformat(), {"count": 0, "pc_hours": 0})
        count, pc_hours = info["count"], info["pc_hours"]
        percent = round(100 * pc_hours / day_capacity)
        lines.append(
            f"{WEEKDAYS[day.weekday()]} {day.strftime('%d.%m')} "
            f"{load_bar(percent)} {percent}% — "
            f"{count} {plural(count, ('бронь', 'брони', 'броней'))}, {pc_hours} ПК·ч"
        )
    lines.append(f"\n100 % = {CLUB_PCS_TOTAL} ПК × {_HOURS_FULL_DAY} часов")
    await message.answer("\n".join(lines))


# ════════════════════════════════════════
# /stats — статистика за месяц
# ════════════════════════════════════════

@router.message(Command("stats"), IsAdmin())
async def cmd_stats(message: Message) -> None:
    current = now()
    stats = await db.get_month_stats(current.year, current.month)
    reviews = await db.get_reviews_summary()

    if reviews["avg"]:
        rating_line = f"{reviews['avg']} ⭐ (по {reviews['count']} оценкам)"
    else:
        rating_line = "пока нет отзывов"

    text = (
        f"📊 <b>Статистика: {MONTHS[current.month - 1]} {current.year}</b>\n\n"
        f"📨 Заявок: {stats['total']}\n"
        f"✅ Подтверждено: {stats['confirmed']}\n"
        f"❌ Отклонено: {stats['rejected']}\n"
        f"🚫 Отменено клиентами: {stats['cancelled']}\n"
        f"👻 Неявок: {stats['noshow']}\n\n"
        f"👥 Клиентов в базе: {stats['clients_total']}\n"
        f"⭐ Средняя оценка: {rating_line}"
    )
    await message.answer(text)


# ════════════════════════════════════════
# /broadcast — рассылка по базе клиентов
# ════════════════════════════════════════

@router.message(Command("broadcast"), IsAdmin())
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BroadcastForm.text)
    await message.answer(texts.BC_PROMPT)


@router.message(BroadcastForm.text, IsAdmin())
async def broadcast_catch(message: Message, state: FSMContext) -> None:
    """Ловим сообщение для рассылки (текст или фото с подписью) и показываем превью."""
    photo = message.photo[-1].file_id if message.photo else None
    text = (message.text or message.caption or "").strip()
    if not photo and not text:
        await message.answer(texts.BC_EMPTY)
        return

    await state.update_data(bc_text=text, bc_photo=photo)
    await state.set_state(BroadcastForm.confirm)

    # Показываем админу, что именно получат клиенты
    if photo:
        await message.answer_photo(photo, caption=texts.T(text) if text else None)
    else:
        await message.answer(texts.T(text))
    await message.answer(texts.BC_CONFIRM, reply_markup=broadcast_confirm_kb())


@router.callback_query(F.data == "bc:y", IsAdmin())
async def broadcast_send(cb: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() != BroadcastForm.confirm.state:
        await cb.answer("Уже обработано", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()

    ok = fail = 0
    for user_id in await db.get_client_ids():
        try:
            if data.get("bc_photo"):
                caption = texts.T(data["bc_text"]) if data.get("bc_text") else None
                await cb.bot.send_photo(user_id, data["bc_photo"], caption=caption)
            else:
                await cb.bot.send_message(user_id, texts.T(data["bc_text"]))
            ok += 1
        except TelegramAPIError:
            # Клиент не запускал бота / заблокировал / демо-ID — считаем и идём дальше
            fail += 1

    await cb.message.edit_text(
        f"{texts.BC_CONFIRM}\n\n{texts.BC_DONE.format(ok=ok, fail=fail)}",
        reply_markup=None,
    )
    logger.info("Рассылка от %s завершена: доставлено %s, не доставлено %s",
                cb.from_user.id, ok, fail)
    await cb.answer("Готово")


@router.callback_query(F.data == "bc:n", IsAdmin())
async def broadcast_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text(texts.BC_CANCELLED, reply_markup=None)
    await cb.answer("Отменено")
