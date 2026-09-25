#!/usr/bin/env python3
"""
Точка входа. Запуск:  python bot.py

Бот компьютерного клуба (ДЕМО-ШАБЛОН).
Настройки — в config.py, тексты — в texts.py, AI (GigaChat) — в ai_helper.py.
"""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database as db
from handlers import admin, client
from scheduler import restore_jobs, scheduler

log = logging.getLogger("bot")


def setup_logging() -> None:
    """Логирование в файл bot.log и в консоль."""
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    # aiogram/apscheduler шумят на DEBUG — оставляем INFO
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def on_startup(bot: Bot) -> None:
    """Инициализация: база, демо-данные, планировщик, восстановление задач."""
    await db.init_db()
    scheduler.start()
    await restore_jobs(bot)

    log.info("════════════════════════════════════════")
    log.info("Клуб: %s", config.CLUB_NAME)
    log.info("Демо-режим (плашка 🧪): %s", "вкл" if config.DEMO_MODE else "выкл")
    if config.USE_AI:
        log.info("AI: GigaChat, модель %s", config.AI_MODEL)
    else:
        log.info("AI: выключен (бот работает на кнопках)")
    if config.ADMIN_ID == 123456789:
        log.warning("ADMIN_ID не изменён — заявки уходят на вымышленный id. "
                    "Укажите свой Telegram ID в config.py")
    log.info("Бот запущен. Никогда не останавливайтесь, мечтайте о большем 🚀")
    log.info("════════════════════════════════════════")


async def main() -> None:
    setup_logging()

    if not config.BOT_TOKEN or config.BOT_TOKEN == "СЮДА_ТОКЕН":
        log.error(
            "Не задан BOT_TOKEN!\n"
            "1) Откройте @BotFather в Telegram → /newbot → получите токен\n"
            "2) Вставьте его в config.py:  BOT_TOKEN = \"123456:ABC...\"\n"
            "3) Запустите снова: python bot.py"
        )
        sys.exit(1)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # admin — первым: его команды важнее клиентского фолбэка на любой текст
    dp.include_router(admin.router)
    dp.include_router(client.router)

    await on_startup(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Корректное завершение по Ctrl+C; ошибки (SystemExit и т.п.)
        # не глушим — супервизору важен ненулевой код выхода
        pass
