"""Общие помощники для обработчиков (редактирование сообщений и пр.)."""
import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)


async def edit_or_send(cb: CallbackQuery, text: str, **kwargs) -> None:
    """
    Обновить сообщение по нажатию inline-кнопки, а если редактировать
    нельзя (старое сообщение, тот же текст) — отправить новое.
    """
    try:
        await cb.message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        try:
            await cb.message.answer(text, **kwargs)
        except TelegramBadRequest:
            logger.debug("Не удалось ни отредактировать, ни отправить сообщение: %s", exc)


async def edit_card_verdict(cb: CallbackQuery, verdict: str) -> None:
    """
    Дописать в карточку заявки вердикт («✅ Подтверждена» / «❌ Отклонена»)
    и убрать кнопки — по уже обработанной заявке нажимать больше нечего.
    """
    try:
        await cb.message.edit_text(f"{cb.message.html_text}\n\n{verdict}", reply_markup=None)
    except TelegramBadRequest as exc:
        logger.warning("Не удалось обновить карточку заявки: %s", exc)


def plural(n: int, forms: tuple[str, str, str]) -> str:
    """Русское склонение: plural(3, ('бронь', 'брони', 'броней')) → 'брони'."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]
