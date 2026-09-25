"""
Меню команд Telegram — РАЗНОЕ для клиентов и сотрудников клуба.

Клиент никогда не видит админских команд, сотрудник — клиентских.
Списки применяются при старте и сразу же при назначении/снятии админа.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

logger = logging.getLogger(__name__)

CLIENT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]

ADMIN_COMMANDS = [
    BotCommand(command="start", description="Панель управления"),
    BotCommand(command="today", description="Брони на сегодня"),
    BotCommand(command="week", description="Загрузка на неделю"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="find", description="Найти клиента"),
    BotCommand(command="broadcast", description="Рассылка"),
    BotCommand(command="cancel", description="Вернуться в панель"),
    BotCommand(command="help", description="Помощь"),
]


async def apply_for_user(bot: Bot, user_id: int, is_admin: bool) -> None:
    """Выдать пользователю набор команд по его роли."""
    try:
        await bot.set_my_commands(
            ADMIN_COMMANDS if is_admin else CLIENT_COMMANDS,
            scope=BotCommandScopeChat(chat_id=user_id),
        )
    except TelegramAPIError:
        logger.debug("Не удалось задать команды для %s (не запускал бота)", user_id)


async def apply_all(bot: Bot, admin_ids: list[int]) -> None:
    await bot.set_my_commands(CLIENT_COMMANDS, scope=BotCommandScopeDefault())
    for uid in admin_ids:
        await apply_for_user(bot, uid, True)
