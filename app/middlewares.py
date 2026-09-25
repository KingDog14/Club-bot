"""
Middleware-слой: разделение ролей и защита клиентской части.

Главное правило продакшена: **администратор — это администратор,
а клиент — это клиент**. Сотрудник клуба не попадает в клиентское меню,
клиент не попадает в панель. Раньше оба меню были доступны всем подряд.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

import app.database as db
import app.roles as roles
import app.texts as texts
from app.config import CLUB_PHONE

logger = logging.getLogger(__name__)


def _is_callback(event: TelegramObject) -> bool:
    """Callback-кнопка (проверяем по утиной типизации — удобно для тестов)."""
    return isinstance(event, CallbackQuery) or hasattr(event, "data")


async def _reply(event: TelegramObject, text: str, alert: bool = True) -> None:
    if _is_callback(event):
        await event.answer(text, show_alert=alert)
    elif hasattr(event, "answer"):
        await event.answer(text)


class ClientAreaMiddleware(BaseMiddleware):
    """
    Пропускает в клиентские хендлеры только клиентов:

    • сотрудник клуба (owner/admin) получает подсказку открыть панель;
    • заблокированный клиент получает вежливый отказ.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if roles.is_admin(user.id):
            await _reply(event, texts.CLIENT_ONLY)
            return None

        if await db.is_blocked(user.id):
            await _reply(event, texts.BLOCKED.format(phone=CLUB_PHONE))
            return None

        return await handler(event, data)


class AdminAreaMiddleware(BaseMiddleware):
    """
    Пропускает в панель только сотрудников клуба.

    Клиент, которому досталось старое сообщение с админскими кнопками,
    получает «🔒 Раздел доступен только администраторам», а не молчание.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if roles.is_admin(user.id):
            return await handler(event, data)

        # Клиентские апдейты идут дальше — их подхватит клиентский роутер.
        if _is_callback(event) and (getattr(event, "data", "") or "").startswith(("admin:", "adm:")):
            logger.warning("Клиент %s попытался открыть админский раздел: %s", user.id, event.data)
            await event.answer(texts.ADMIN_ONLY, show_alert=True)
            return None
        return await handler(event, data)
