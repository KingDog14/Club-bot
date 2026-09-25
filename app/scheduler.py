"""
Планировщик (APScheduler 3.x):
  1) напоминание клиенту за REMIND_BEFORE_HOURS часов до начала брони
  2) запрос отзыва через REVIEW_AFTER_HOURS часов после окончания брони

Задачи создаются при подтверждении заявки админом и восстанавливаются
из базы при перезапуске бота (restore_jobs).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import app.database as db
import app.texts as texts
from app.config import ADMIN_ID, REMIND_BEFORE_HOURS, REVIEW_AFTER_HOURS, TIMEZONE
from app.keyboards import rating_kb, reminder_kb
from app.utils import booking_start_end, day_word, now

logger = logging.getLogger(__name__)

# Единственный экземпляр планировщика на всё приложение
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# Визит считается «давно закончившимся» через столько часов после конца —
# задачи по таким броням при перезапуске не восстанавливаем
REVISIT_LATE_HOURS = 1


# ──────────────────────────────
# Управление задачами брони
# ──────────────────────────────

def schedule_booking_jobs(bot: Bot, booking: dict) -> None:
    """Поставить напоминание и запрос отзыва для подтверждённой брони."""
    current = now()
    start, end = booking_start_end(booking["date"], booking["time"], booking["duration"])

    remind_at = start - timedelta(hours=REMIND_BEFORE_HOURS)
    review_at = end + timedelta(hours=REVIEW_AFTER_HOURS)

    # Если до начала меньше REMIND_BEFORE_HOURS — напоминаем почти сразу
    if remind_at <= current < start:
        remind_at = current + timedelta(seconds=15)

    if remind_at > current:
        scheduler.add_job(
            send_reminder,
            DateTrigger(run_date=remind_at),
            args=[bot, booking["id"]],
            id=f"remind:{booking['id']}",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info("Запланировано напоминание по брони #%s на %s", booking["id"], remind_at)

    if review_at > current:
        scheduler.add_job(
            send_review_request,
            DateTrigger(run_date=review_at),
            args=[bot, booking["id"]],
            id=f"review:{booking['id']}",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info("Запланирован запрос отзыва по брони #%s на %s", booking["id"], review_at)


def cancel_booking_jobs(booking_id: int) -> None:
    """Снять задачи брони (отмена/отклонение — напоминать не нужно)."""
    for job_id in (f"remind:{booking_id}", f"review:{booking_id}"):
        try:
            scheduler.remove_job(job_id)
            logger.info("Задача %s снята", job_id)
        except JobLookupError:
            pass  # задачи нет — это нормально


async def restore_jobs(bot: Bot) -> None:
    """
    После перезапуска: заново запланировать задачи для будущих
    подтверждённых броней (APScheduler 3.x хранит задачи в памяти).
    """
    restored = 0
    for booking in await db.get_future_confirmed(now().date().isoformat()):
        _, end = booking_start_end(booking["date"], booking["time"], booking["duration"])
        # Визит давно закончился — и напоминать, и просить отзыв поздно
        if end + timedelta(hours=REVISIT_LATE_HOURS) <= now():
            continue
        schedule_booking_jobs(bot, booking)
        restored += 1
    if restored:
        logger.info("Планировщик восстановил задачи для %s броней", restored)


# ──────────────────────────────
# Задачи (вызываются планировщиком в своё время)
# ──────────────────────────────

async def send_reminder(bot: Bot, booking_id: int) -> None:
    """Напоминание клиенту: «Ждём вас сегодня в 18:00, 2 ПК. Подтвердите…»"""
    booking = await db.get_booking(booking_id)
    if not booking or booking["status"] != db.STATUS_CONFIRMED:
        return  # бронь уже отменена/отклонена — не напоминаем

    text = texts.REMINDER.format(
        day_word=day_word(booking["date"]),
        time=booking["time"],
        pcs=booking["pcs"],
    )
    try:
        await bot.send_message(
            booking["user_id"],
            texts.T(text),
            reply_markup=reminder_kb(booking_id),
        )
        logger.info("Напоминание по брони #%s отправлено клиенту %s",
                    booking_id, booking["user_id"])
    except TelegramAPIError as exc:
        # Клиент заблокировал бота либо user_id демо-фейковый — просто логируем
        logger.warning("Напоминание по брони #%s не доставлено: %s", booking_id, exc)


async def send_review_request(bot: Bot, booking_id: int) -> None:
    """Запрос отзыва через час после окончания сеанса: «Оцените клуб от 1 до 5»."""
    booking = await db.get_booking(booking_id)
    if not booking or booking["status"] != db.STATUS_CONFIRMED:
        return

    try:
        await bot.send_message(
            booking["user_id"],
            texts.T(texts.REVIEW_REQUEST),
            reply_markup=rating_kb(booking_id),
        )
        logger.info("Запрос отзыва по брони #%s отправлен клиенту %s",
                    booking_id, booking["user_id"])
    except TelegramAPIError as exc:
        logger.warning("Запрос отзыва по брони #%s не доставлен: %s", booking_id, exc)


async def notify_admin(bot: Bot, text: str) -> None:
    """Служебное уведомление админу (подтверждения/отмены по напоминаниям)."""
    try:
        await bot.send_message(ADMIN_ID, text)
    except TelegramAPIError as exc:  # админ не запускал бота — только лог
        logger.warning("Не смог отправить уведомление админу: %s", exc)
