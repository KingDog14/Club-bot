"""
Все клавиатуры бота.

ПРАВИЛО ШАБЛОНА: если в config.py ссылка пустая ("") — кнопка для неё
не создаётся. Например, CLUB_VK_URL = "" → кнопки «Мы ВКонтакте» нет.
"""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import app.texts as texts
from app.config import (
    ADMIN_ID,
    BOOKING_DAYS_AHEAD,
    CLUB_MAPS_URL,
    CLUB_SITE_URL,
    CLUB_VK_URL,
    DURATIONS,
    FAQ_BASE,
    PCS_VARIANTS,
    TIME_PERIODS,
    TIME_SLOTS,
)
from app.utils import WEEKDAYS, today as club_today
from datetime import timedelta


# ──────────────────────────────
# Главное меню
# ──────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.MENU_BOOK, callback_data="m:book")
    kb.button(text=texts.MENU_MY, callback_data="m:my")
    kb.button(text=texts.MENU_PRICES, callback_data="m:prices")
    kb.button(text=texts.MENU_ADDRESS, callback_data="m:addr")
    kb.button(text=texts.MENU_FAQ, callback_data="m:faq")
    kb.button(text=texts.MENU_CONTACT, callback_data="m:contact")
    kb.button(text=texts.MENU_REVIEW, callback_data="m:review")
    kb.adjust(1, 1, 2, 2, 1)
    return kb.as_markup()


def my_bookings_kb(bookings: list[dict]) -> InlineKeyboardMarkup:
    """Список активных броней клиента с возможностью отменить свою бронь."""
    kb = InlineKeyboardBuilder()
    for b in bookings:
        kb.button(text=f"❌ Отменить #{b['id']} · {b['date'][8:10]}.{b['date'][5:7]} {b['time']}",
                  callback_data=f"my:cancel:{b['id']}")
    kb.button(text=texts.MENU_BOOK, callback_data="m:book")
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


def back_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    return kb.as_markup()


# ──────────────────────────────
# Бронирование (шаги)
# ──────────────────────────────

def book_mode_kb() -> InlineKeyboardMarkup:
    """Выбор способа брони — показывается только при USE_AI = True."""
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BOOK_MODE_BTN, callback_data="b:mode:btn")
    kb.button(text=texts.BOOK_MODE_AI, callback_data="b:mode:ai")
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


def dates_kb() -> InlineKeyboardMarkup:
    """Ближайшие BOOKING_DAYS_AHEAD дней: «Пт 26.09», «Сб 27.09»…"""
    kb = InlineKeyboardBuilder()
    d0 = club_today()  # «сегодня» — по часовому поясу клуба, а не сервера
    for i in range(BOOKING_DAYS_AHEAD):
        d = d0 + timedelta(days=i)
        label = f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"
        if i == 0:
            label = "Сегодня, " + label
        elif i == 1:
            label = "Завтра, " + label
        kb.button(text=label, callback_data=f"b:date:{d.isoformat()}")
    kb.button(text="❌ Отмена", callback_data="b:cancel")
    kb.adjust(2)
    return kb.as_markup()


def times_kb(period: str | None = None) -> InlineKeyboardMarkup:
    """
    Слоты времени. Если задан period («вечер» и т.п.) —
    показываются только слоты этого периода (уточнение после AI-парсинга).
    """
    slots = TIME_PERIODS.get(period, TIME_SLOTS) if period else TIME_SLOTS
    kb = InlineKeyboardBuilder()
    for slot in slots:
        kb.button(text=slot, callback_data=f"b:time:{slot}")
    kb.button(text="❌ Отмена", callback_data="b:cancel")
    kb.adjust(4)
    return kb.as_markup()


def durations_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for h in DURATIONS:
        kb.button(text=f"{h} ч", callback_data=f"b:dur:{h}")
    kb.button(text="❌ Отмена", callback_data="b:cancel")
    kb.adjust(5, 1)
    return kb.as_markup()


def pcs_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in PCS_VARIANTS:
        kb.button(text=f"{n} 🖥", callback_data=f"b:pcs:{n}")
    kb.button(text="❌ Отмена", callback_data="b:cancel")
    kb.adjust(5, 1)
    return kb.as_markup()


def phone_kb() -> ReplyKeyboardMarkup:
    """Reply-клавиатура на шаге телефона: поделиться номером или пропустить."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BOOK_PHONE_SHARE, request_contact=True)],
            [KeyboardButton(text=texts.BOOK_PHONE_SKIP)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BOOK_CONFIRM_OK, callback_data="b:ok")
    kb.button(text=texts.BOOK_CONFIRM_CANCEL, callback_data="b:cancel")
    kb.adjust(1)
    return kb.as_markup()


def ai_fail_kb() -> InlineKeyboardMarkup:
    """AI не разобрал текст — предложить перейти на кнопки."""
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BOOK_MODE_BTN, callback_data="b:mode:btn")
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


# ──────────────────────────────
# Карточка заявки для админа
# ──────────────────────────────

def admin_booking_kb(booking_id: int, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"adm:ok:{booking_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm:no:{booking_id}")
    # Открывает личный чат с клиентом прямо из карточки
    kb.button(text="💬 Написать", url=f"tg://user?id={user_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


# ──────────────────────────────
# Напоминание и отзыв
# ──────────────────────────────

def reminder_kb(booking_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.REMINDER_YES_BTN, callback_data=f"rem:y:{booking_id}")
    kb.button(text=texts.REMINDER_NO_BTN, callback_data=f"rem:n:{booking_id}")
    kb.adjust(2)
    return kb.as_markup()


def rating_kb(booking_id: int = 0) -> InlineKeyboardMarkup:
    """Оценка 1–5. booking_id=0 — отзыв из главного меню (без привязки к брони)."""
    kb = InlineKeyboardBuilder()
    for r in (1, 2, 3, 4, 5):
        kb.button(text=f"{r} ⭐", callback_data=f"rev:{r}:{booking_id}")
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(5, 1)
    return kb.as_markup()


def review_skip_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.REVIEW_BAD_SKIP, callback_data="rev:skip")
    return kb.as_markup()


def review_maps_kb() -> InlineKeyboardMarkup | None:
    """Кнопка «Оставить отзыв на 2ГИС» — только если ссылка задана."""
    if not CLUB_MAPS_URL:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.REVIEW_MAPS_BTN, url=CLUB_MAPS_URL)
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


# ──────────────────────────────
# FAQ
# ──────────────────────────────

def faq_kb(items: list[dict] | None = None) -> InlineKeyboardMarkup:
    """
    Кнопки FAQ. Вопросы берутся из базы (их правит администратор в панели),
    а FAQ_BASE из config.py остаётся только начальным наполнением.
    """
    kb = InlineKeyboardBuilder()
    if items is None:
        items = [{"id": key, "question": value["question"]}
                 for key, value in FAQ_BASE.items()]
    for item in items:
        kb.button(text=item["question"][:60], callback_data=f"f:{item['id']}")
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


def faq_back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.FAQ_BACK, callback_data="m:faq")
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


# ──────────────────────────────
# Адрес и контакты (кнопки-ссылки появляются, только если URL задан)
# ──────────────────────────────

def _social_buttons(kb: InlineKeyboardBuilder) -> None:
    if CLUB_MAPS_URL:
        kb.button(text=texts.BTN_MAPS, url=CLUB_MAPS_URL)
    if CLUB_VK_URL:
        kb.button(text=texts.BTN_VK, url=CLUB_VK_URL)
    if CLUB_SITE_URL:
        kb.button(text=texts.BTN_SITE, url=CLUB_SITE_URL)


def address_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    _social_buttons(kb)
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


def contact_kb(admin_id: int = ADMIN_ID) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BTN_ADMIN_CHAT, url=f"tg://user?id={admin_id}")
    _social_buttons(kb)
    kb.button(text=texts.MENU_BACK, callback_data="m:home")
    kb.adjust(1)
    return kb.as_markup()


# ──────────────────────────────
# Рассылка (админ)
# ──────────────────────────────

def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=texts.BC_CONFIRM_OK, callback_data="bc:y")
    kb.button(text=texts.BC_CONFIRM_CANCEL, callback_data="bc:n")
    kb.adjust(2)
    return kb.as_markup()
