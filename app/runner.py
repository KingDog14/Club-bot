#!/usr/bin/env python3
"""
Сборка и запуск бота. Точка входа — ../main.py (python main.py).

Бот компьютерного клуба (ДЕМО-ШАБЛОН).
Настройки — в app/config.py, тексты — в app/texts.py, AI (GigaChat) — в app/ai_helper.py.
"""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

import app.config as config
import app.database as db
import app.roles as roles
from app.bot_commands import apply_all
from app.handlers import admin, client
from app.scheduler import restore_jobs, scheduler


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
    """Инициализация: база, роли, демо-данные, планировщик, восстановление задач."""
    await db.init_db()
    await roles.init()
    scheduler.start()
    await restore_jobs(bot)
    await apply_all(bot, roles.admin_ids())

    log.info("════════════════════════════════════════")
    log.info("Клуб: %s", config.CLUB_NAME)
    log.info("Демо-режим (плашка 🧪): %s", "вкл" if config.DEMO_MODE else "выкл")
    if config.USE_AI:
        log.info("AI: GigaChat, модель %s", config.AI_MODEL)
    else:
        log.info("AI: выключен (бот работает на кнопках)")
    log.info("Сотрудников с доступом к панели: %s", len(roles.admin_ids()))
    if config.ADMIN_ID == 123456789:
        log.warning("ADMIN_ID не изменён — заявки уходят на вымышленный id. "
                    "Укажите свой Telegram ID в .env или app/config.py")
    log.info("Бот запущен. Никогда не останавливайтесь, мечтайте о большем 🚀")
    log.info("════════════════════════════════════════")


async def main() -> None:
    setup_logging()

    if not config.BOT_TOKEN or config.BOT_TOKEN == "СЮДА_ТОКЕН":
        log.error(
            "Не задан BOT_TOKEN!\n"
            "1) Откройте @BotFather в Telegram → /newbot → получите токен\n"
            "2) Вставьте его в .env (BOT_TOKEN=...) или в app/config.py\n"
            "3) Запустите снова: python main.py"
        )
        sys.exit(1)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # admin — первым: его команды важнее клиентского фолбэка на любой текст
    dp.include_router(admin.router)
    dp.include_router(client.router)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        """
        Бот не должен падать из-за одного плохого апдейта:
        ошибку пишем в лог, пользователю показываем вежливое сообщение.
        """
        log.exception("Ошибка при обработке апдейта: %s", event.exception)
        update = event.update
        try:
            if update.callback_query:
                await update.callback_query.answer(
                    "Что-то пошло не так. Попробуйте ещё раз 🙏", show_alert=True)
            elif update.message:
                await update.message.answer("Что-то пошло не так. Попробуйте ещё раз 🙏")
        except TelegramAPIError:
            pass
        return True

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
