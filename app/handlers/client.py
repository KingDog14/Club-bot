"""
Клиентские обработчики: главное меню, бронирование (кнопки и AI-текст),
FAQ, отзывы, реакции на напоминания.

Бронирование — два пути:
  Путь А (кнопки): дата → время → длительность → ПК → имя → телефон → проверка.
  Путь Б (AI, если USE_AI=True): свободный текст разбирает GigaChat,
  неясные поля бот уточняет теми же кнопками, дальше — общий сценарий.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove, User

import app.admin_data as ad
import app.ai_helper as ai_helper
import app.database as db
import app.roles as roles
import app.texts as texts
from app.config import (
    ADMIN_ID,
    CLUB_MAPS_URL,
    CLUB_PCS_TOTAL,
    CLUB_PHONE,
    FAQ_BASE,
    MAX_ACTIVE_BOOKINGS,
    TIME_PERIODS,
    USE_AI,
)
from app.keyboards import (
    address_kb,
    admin_booking_kb,
    ai_fail_kb,
    back_menu_kb,
    book_mode_kb,
    confirm_kb,
    contact_kb,
    dates_kb,
    durations_kb,
    faq_back_kb,
    faq_kb,
    main_menu_kb,
    my_bookings_kb,
    pcs_kb,
    phone_kb,
    rating_kb,
    review_maps_kb,
    review_skip_kb,
    times_kb,
)
from app.middlewares import ClientAreaMiddleware
from app.scheduler import cancel_booking_jobs, notify_admin, notify_owners
from app.states import BookingForm, FaqForm, ReviewForm
from app.utils import booking_start_end, human_date, iso_days_ahead, now

from .common import edit_or_send

logger = logging.getLogger(__name__)
router = Router()

# Клиентский роутер обслуживает ТОЛЬКО клиентов: сотрудники клуба и
# заблокированные пользователи сюда не попадают (см. app/middlewares.py).
router.message.outer_middleware(ClientAreaMiddleware())
router.callback_query.outer_middleware(ClientAreaMiddleware())


# ════════════════════════════════════════
# /start, /cancel и главное меню
# ════════════════════════════════════════

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.upsert_client(message.from_user.id, message.from_user.username,
                           message.from_user.full_name)
    await message.answer(texts.T(texts.WELCOME), reply_markup=main_menu_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Сброс любого текущего сценария (бронирование, рассылка и т.д.)."""
    await state.clear()
    await message.answer(texts.T(texts.CANCELLED), reply_markup=ReplyKeyboardRemove())
    await message.answer(texts.T(texts.WELCOME), reply_markup=main_menu_kb())


@router.callback_query(F.data == "m:home")
async def menu_home(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await db.upsert_client(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
    await edit_or_send(cb, texts.T(texts.WELCOME), reply_markup=main_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "m:prices")
async def menu_prices(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    # Тарифы берём из базы — их правит администратор в панели
    await edit_or_send(cb, texts.T(await ad.tariffs_text()), reply_markup=back_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "m:addr")
async def menu_address(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text = texts.ADDRESS.format(hours=await ad.club_hours())
    await edit_or_send(cb, texts.T(text), reply_markup=address_kb())
    await cb.answer()


@router.callback_query(F.data == "m:contact")
async def menu_contact(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    admin_id = (roles.admin_ids() or [ADMIN_ID])[0]
    await edit_or_send(cb, texts.T(texts.CONTACT), reply_markup=contact_kb(admin_id))
    await cb.answer()


# ════════════════════════════════════════
# FAQ (кнопки + свободный вопрос AI)
# ════════════════════════════════════════

@router.callback_query(F.data == "m:faq")
async def menu_faq(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    ai_on = await ad.ai_enabled()
    text = texts.FAQ_MENU + (texts.FAQ_MENU_AI_SUFFIX if ai_on else " 👇")
    if ai_on:
        # Всё текстовое, что клиент напишет в этом разделе, уйдёт GigaChat
        await state.set_state(FaqForm.question)
    await edit_or_send(cb, texts.T(text), reply_markup=faq_kb(await ad.faqs()))
    await cb.answer()


@router.callback_query(F.data.startswith("f:"))
async def faq_button(cb: CallbackQuery) -> None:
    """Ответ на частый вопрос — мгновенно, без AI. Вопросы живут в базе."""
    key = cb.data.split(":", 1)[1]
    item = await ad.faq_by_id(int(key)) if key.isdigit() else FAQ_BASE.get(key)
    if not item:
        await cb.answer("Этот вопрос удалён администратором", show_alert=True)
        return
    text = f"<b>{item['question']}</b>\n\n{item['answer']}"
    await edit_or_send(cb, texts.T(text), reply_markup=faq_back_kb())
    await cb.answer()


@router.message(FaqForm.question, F.text)
async def faq_ai_question(message: Message) -> None:
    """Свободный вопрос в разделе FAQ — отвечает GigaChat строго по базе знаний."""
    if not await ad.ai_enabled():
        # AI выключили тумблером в панели, пока клиент был в разделе
        await message.answer(texts.T(texts.UNKNOWN), reply_markup=faq_back_kb())
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    faq_base = {str(x["id"]): {"question": x["question"], "answer": x["answer"]}
                for x in await ad.faqs()}
    tariffs = [(t["name"], t["price"]) for t in await ad.tariffs()]
    # SDK GigaChat синхронный — выносим запрос в поток, чтобы не блокировать бота
    answer = await asyncio.to_thread(ai_helper.answer_faq, message.text, faq_base, tariffs)
    await message.answer(texts.T(answer), reply_markup=faq_back_kb())
    # Остаёмся в состоянии — клиент может задать следующий вопрос


# ════════════════════════════════════════
# БРОНИРОВАНИЕ — вход
# ════════════════════════════════════════

async def _booking_blocked(user_id: int) -> str | None:
    """Причина, по которой бронировать сейчас нельзя (или None)."""
    if await ad.club_closed():
        reason = await ad.closed_reason()
        return texts.CLUB_CLOSED.format(
            reason=f"{reason}\n\n" if reason else "", phone=CLUB_PHONE)
    active = await db.get_client_bookings(user_id, only_active=True)
    if len(active) >= MAX_ACTIVE_BOOKINGS:
        return texts.BOOK_LIMIT.format(count=len(active))
    return None


@router.callback_query(F.data == "m:book")
async def menu_book(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    blocked = await _booking_blocked(cb.from_user.id)
    if blocked:
        await edit_or_send(cb, texts.T(blocked), reply_markup=back_menu_kb())
        await cb.answer()
        return
    if await ad.ai_enabled():
        # Есть GigaChat — даём выбор: кнопки или свободный текст
        await state.set_state(BookingForm.mode)
        await edit_or_send(cb, texts.T(texts.BOOK_CHOOSE_MODE), reply_markup=book_mode_kb())
    else:
        await _ask_date(cb, state)
    await cb.answer()


@router.callback_query(F.data == "b:mode:btn")
async def book_mode_buttons(cb: CallbackQuery, state: FSMContext) -> None:
    await _ask_date(cb, state)
    await cb.answer()


@router.callback_query(F.data == "b:mode:ai")
async def book_mode_ai(cb: CallbackQuery, state: FSMContext) -> None:
    if not await ad.ai_enabled():  # AI выключили — уводим на кнопки
        await _ask_date(cb, state)
        await cb.answer()
        return
    await state.set_state(BookingForm.ai_text)
    await edit_or_send(cb, texts.T(texts.BOOK_AI_PROMPT), reply_markup=back_menu_kb())
    await cb.answer()


# ════════════════════════════════════════
# БРОНИРОВАНИЕ — путь Б (AI-текст)
# ════════════════════════════════════════

@router.message(BookingForm.ai_text, F.text)
async def book_ai_text(message: Message, state: FSMContext) -> None:
    if not await ad.ai_enabled():
        await _ask_date(message, state)
        return
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    parsed = await asyncio.to_thread(ai_helper.parse_booking, message.text)

    if not parsed:
        # Не разобрал / AI недоступен — предлагаем кнопки (НИКАКИХ падений)
        await message.answer(texts.T(texts.BOOK_AI_FAIL), reply_markup=ai_fail_kb())
        return

    filled = _normalize_parsed(parsed)
    await state.update_data(**filled)
    summary = _parsed_summary(filled)
    if summary:
        await message.answer(texts.T(texts.BOOK_AI_PARSED.format(summary=summary)))
    # Общий сценарий: бот сам спросит недостающие поля кнопками
    await _booking_next_step(message, state)


def _normalize_parsed(parsed: dict) -> dict:
    """
    Привести ответ GigaChat к формату FSM-данных:
    date → «ГГГГ-ММ-ДД» (только не в прошлом), time → «ЧЧ:ММ»,
    «вечер/утро/…» → time_period (бот уточнит слот кнопками).
    """
    out: dict = {}

    iso = _resolve_date(parsed.get("date", ""))
    if iso:
        out["date"] = iso

    time_raw = str(parsed.get("time", "")).strip().lower().replace(".", ":").replace("-", ":")
    match = re.fullmatch(r"(\d{1,2})\s*:\s*(\d{2})", time_raw)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
        if 0 <= hours <= 23 and 0 <= minutes <= 59:
            out["time"] = f"{hours:02d}:{minutes:02d}"
    elif time_raw in TIME_PERIODS:
        out["time_period"] = time_raw

    # Повторная проверка границ на случай, кто бы ни прислал данные
    for key, limit in (("duration", 12), ("pcs", 10)):
        value = parsed.get(key)
        if isinstance(value, int) and 1 <= value <= limit:
            out[key] = value
    return out


def _resolve_date(raw: object) -> str | None:
    """'сегодня'/'завтра'/'послезавтра'/'ГГГГ-ММ-ДД' → ISO-дата или None."""
    value = str(raw or "").strip().lower()
    words = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    if value in words:
        return iso_days_ahead(words[value])
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    # Даты в прошлом не предлагаем — пусть клиент выберет кнопкой
    return parsed.isoformat() if parsed >= now().date() else None


def _parsed_summary(filled: dict) -> str:
    """'📅 Сб 26.09 · завтра · 🕒 вечер · ⏱ 3 ч · 🖥 2 ПК' — что понял AI."""
    parts = []
    if filled.get("date"):
        parts.append("📅 " + human_date(filled["date"]))
    if filled.get("time"):
        parts.append("🕒 " + filled["time"])
    elif filled.get("time_period"):
        parts.append("🕒 " + filled["time_period"])
    if filled.get("duration"):
        parts.append(f"⏱ {filled['duration']} ч")
    if filled.get("pcs"):
        parts.append(f"🖥 {filled['pcs']} ПК")
    return " · ".join(parts)


# ════════════════════════════════════════
# БРОНИРОВАНИЕ — общие шаги (пути А и Б)
# ════════════════════════════════════════

async def _send_step(event: Message | CallbackQuery, text: str, reply_markup) -> None:
    """Отправить вопрос шага: по кнопке редактируем, по сообщению отвечаем."""
    if isinstance(event, CallbackQuery):
        await edit_or_send(event, texts.T(text), reply_markup=reply_markup)
    else:
        await event.answer(texts.T(text), reply_markup=reply_markup)


async def _booking_next_step(event: Message | CallbackQuery, state: FSMContext) -> None:
    """Найти первое незаполненное поле и задать по нему вопрос."""
    data = await state.get_data()
    if "date" not in data:
        await _ask_date(event, state)
    elif "time" not in data:
        await _ask_time(event, state, data.get("time_period"))
    elif "duration" not in data:
        await _ask_duration(event, state)
    elif "pcs" not in data:
        await _ask_pcs(event, state)
    else:
        await _ask_name(event, state)


async def _ask_date(event, state: FSMContext) -> None:
    await state.set_state(BookingForm.date)
    await _send_step(event, texts.BOOK_DATE, dates_kb())


async def _ask_time(event, state: FSMContext, period: str | None = None) -> None:
    await state.set_state(BookingForm.time)
    if period:
        text = texts.BOOK_TIME_PERIOD.format(period=period)
    else:
        text = texts.BOOK_TIME
    await _send_step(event, text, times_kb(period))


async def _ask_duration(event, state: FSMContext) -> None:
    await state.set_state(BookingForm.duration)
    await _send_step(event, texts.BOOK_DURATION, durations_kb())


async def _ask_pcs(event, state: FSMContext) -> None:
    await state.set_state(BookingForm.pcs)
    await _send_step(event, texts.BOOK_PCS, pcs_kb())


async def _ask_name(event, state: FSMContext) -> None:
    await state.set_state(BookingForm.name)
    await _send_step(event, texts.BOOK_NAME, back_menu_kb())


# ── выбор даты/времени/длительности/ПК ──

@router.callback_query(F.data.startswith("b:date:"))
async def book_choose_date(cb: CallbackQuery, state: FSMContext) -> None:
    chosen = cb.data.split(":", 2)[2]
    if date.fromisoformat(chosen) < now().date():
        await cb.answer(texts.BOOK_PAST, show_alert=True)
        return
    await state.update_data(date=chosen)
    await _booking_next_step(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("b:time:"))
async def book_choose_time(cb: CallbackQuery, state: FSMContext) -> None:
    chosen = cb.data.split(":", 2)[2]
    data = await state.get_data()
    day = data.get("date")
    if day:
        start, _ = booking_start_end(day, chosen, 1)
        if start < now():
            await cb.answer(texts.BOOK_PAST, show_alert=True)
            return
    await state.update_data(time=chosen)
    await _booking_next_step(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("b:dur:"))
async def book_choose_duration(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(duration=int(cb.data.rsplit(":", 1)[1]))
    await _booking_next_step(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("b:pcs:"))
async def book_choose_pcs(cb: CallbackQuery, state: FSMContext) -> None:
    want = int(cb.data.rsplit(":", 1)[1])
    data = await state.get_data()
    error = await _capacity_error(data.get("date"), data.get("time"),
                                  data.get("duration", 1), want)
    if error:
        await cb.answer(re.sub(r"<[^>]+>", "", error), show_alert=True)
        return
    await state.update_data(pcs=want)
    await _booking_next_step(cb, state)
    await cb.answer()


async def _capacity_error(day: str | None, time_str: str | None,
                          duration: int, want: int) -> str | None:
    """Проверка свободных ПК — защита от овербукинга (раньше её не было)."""
    if not day or not time_str:
        return None
    free = await db.pcs_free(day, time_str, duration, CLUB_PCS_TOTAL)
    if want > free:
        return (texts.NO_FREE_PCS_NONE if free == 0
                else texts.NO_FREE_PCS.format(free=free, want=want))
    return None


# ── имя ──

@router.message(BookingForm.name, F.text)
async def book_enter_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not 2 <= len(name) <= 30:
        await message.answer(texts.T(texts.BOOK_NAME_BAD))
        return
    await state.update_data(name=name)
    await _ask_phone(message, state)


# ── телефон (опционально) ──

async def _ask_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(BookingForm.phone)
    await message.answer(texts.T(texts.BOOK_PHONE), reply_markup=phone_kb())


@router.message(BookingForm.phone, F.contact)
async def book_phone_contact(message: Message, state: FSMContext) -> None:
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    await state.update_data(phone=phone)
    await message.answer("👌", reply_markup=ReplyKeyboardRemove())
    await _show_confirmation(message, state)


@router.message(BookingForm.phone, F.text == texts.BOOK_PHONE_SKIP)
async def book_phone_skip(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=None)
    await message.answer("👌", reply_markup=ReplyKeyboardRemove())
    await _show_confirmation(message, state)


@router.message(BookingForm.phone, F.text)
async def book_phone_text(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    digits = re.sub(r"\D", "", phone)
    if not (7 <= len(digits) <= 15) or len(phone) > 25:
        await message.answer(texts.T(texts.BOOK_PHONE_BAD), reply_markup=phone_kb())
        return
    await state.update_data(phone=phone)
    await message.answer("👌", reply_markup=ReplyKeyboardRemove())
    await _show_confirmation(message, state)


# ── проверка и отправка ──

async def _show_confirmation(message: Message, state: FSMContext) -> None:
    await state.set_state(BookingForm.confirm)
    data = await state.get_data()
    text = (
        f"{texts.BOOK_CONFIRM_TITLE}\n\n"
        f"📅 {human_date(data['date'])} в {data['time']}\n"
        f"⏱ {data['duration']} ч · 🖥 {data['pcs']} ПК\n"
        f"👤 {data['name']}\n"
        f"📞 {data.get('phone') or 'не указан — связь в Telegram'}"
    )
    await message.answer(texts.T(text), reply_markup=confirm_kb())


@router.callback_query(F.data == "b:ok")
async def book_send(cb: CallbackQuery, state: FSMContext) -> None:
    # Защита от двойного нажатия: после отправки состояние очищается
    if await state.get_state() != BookingForm.confirm.state:
        await cb.answer("Заявка уже обработана", show_alert=True)
        return

    data = await state.get_data()

    # Повторная проверка перед записью: пока клиент думал, всё могло измениться
    blocked = await _booking_blocked(cb.from_user.id)
    if blocked:
        await state.clear()
        await edit_or_send(cb, texts.T(blocked), reply_markup=back_menu_kb())
        await cb.answer()
        return
    error = await _capacity_error(data["date"], data["time"], data["duration"], data["pcs"])
    if error:
        await state.clear()
        await edit_or_send(cb, texts.T(error), reply_markup=back_menu_kb())
        await cb.answer()
        return

    booking_id = await db.create_booking(
        cb.from_user.id, data["date"], data["time"], data["duration"], data["pcs"]
    )
    if data.get("phone"):
        await db.set_client_phone(cb.from_user.id, data["phone"])
    await db.set_client_name(cb.from_user.id, data["name"])
    await state.clear()

    await edit_or_send(cb, texts.T(texts.BOOK_SENT), reply_markup=back_menu_kb())
    await cb.answer("Заявка отправлена ✅")

    booking = await db.get_booking(booking_id)
    await _notify_admin_new_booking(
        cb.bot, booking, name=data["name"], phone=data.get("phone"), user=cb.from_user
    )
    logger.info("Новая заявка #%s от %s", booking_id, cb.from_user.id)


@router.callback_query(F.data == "b:cancel")
async def book_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_send(cb, texts.T(texts.BOOK_CANCELLED), reply_markup=back_menu_kb())
    await cb.answer("Отменено")


async def _notify_admin_new_booking(
    bot: Bot, booking: dict, *, name: str, phone: str | None, user: User
) -> None:
    """Карточка заявки админу с кнопками «✅ / ❌ / 💬»."""
    username = f"@{user.username}" if user.username else f"id {user.id}"
    if phone:
        phone_line = phone
    elif user.username:
        phone_line = f"Не указан, связь @{user.username}"
    else:
        phone_line = f"Не указан, связь в Telegram ({username})"

    text = (
        f"🎮 <b>Новая заявка #{booking['id']}</b>\n"
        f"📅 {human_date(booking['date'])} в {booking['time']}\n"
        f"⏱ {booking['duration']} ч · 🖥 {booking['pcs']} ПК\n"
        f"👤 {name} ({username})\n"
        f"📞 {phone_line}"
    )
    markup = admin_booking_kb(booking["id"], user.id)
    delivered = 0
    for admin_id in roles.admin_ids() or [ADMIN_ID]:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
            delivered += 1
        except TelegramAPIError as exc:
            # Чаще всего: админ ещё не запускал бота (/start)
            logger.error(
                "Не удалось отправить карточку заявки #%s админу %s: %s. "
                "Админ должен хотя бы раз нажать /start в боте!",
                booking["id"], admin_id, exc,
            )
    if not delivered:
        logger.error("Заявка #%s не доставлена НИ ОДНОМУ администратору!", booking["id"])


# ════════════════════════════════════════
# «📋 Мои брони» — клиент видит и отменяет свои брони сам
# ════════════════════════════════════════

@router.callback_query(F.data == "m:my")
async def menu_my_bookings(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    items = await db.get_client_bookings(cb.from_user.id, only_active=True)
    if not items:
        await edit_or_send(cb, texts.T(texts.MY_BOOKINGS_EMPTY), reply_markup=back_menu_kb())
        await cb.answer()
        return
    lines = "\n".join(
        f"#{b['id']} · 📅 {human_date(b['date'])} в {b['time']} · "
        f"⏱ {b['duration']} ч · 🖥 {b['pcs']} ПК · {db.STATUS_LABELS.get(b['status'], b['status'])}"
        for b in items
    )
    await edit_or_send(cb, texts.T(texts.MY_BOOKINGS_TITLE.format(lines=lines)),
                       reply_markup=my_bookings_kb(items))
    await cb.answer()


@router.callback_query(F.data.startswith("my:cancel:"))
async def my_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    booking_id = int(cb.data.rsplit(":", 1)[1])
    booking = await db.get_booking(booking_id)
    # Отменить можно только СВОЮ активную бронь
    if (not booking or booking["user_id"] != cb.from_user.id
            or booking["status"] not in db.STATUSES_ACTIVE):
        await cb.answer(texts.MY_CANCEL_FAIL, show_alert=True)
        return
    await db.set_booking_status(booking_id, db.STATUS_CANCELLED)
    cancel_booking_jobs(booking_id)
    client = await db.get_client(cb.from_user.id) or {}
    await notify_admin(
        cb.bot,
        f"❌ Клиент {client.get('name') or cb.from_user.full_name} отменил бронь "
        f"#{booking_id}: {human_date(booking['date'])} в {booking['time']} · "
        f"{booking['pcs']} ПК. Место освобождено.",
    )
    await cb.answer(texts.MY_CANCELLED.format(id=booking_id), show_alert=True)
    await menu_my_bookings(cb, state)


# ════════════════════════════════════════
# Напоминание за 2 часа: «Приду» / «Не смогу»
# ════════════════════════════════════════

@router.callback_query(F.data.startswith("rem:y:"))
async def reminder_yes(cb: CallbackQuery) -> None:
    booking = await db.get_booking(int(cb.data.rsplit(":", 1)[1]))
    if not booking or booking["status"] != db.STATUS_CONFIRMED:
        await cb.answer(texts.REMINDER_INACTIVE, show_alert=True)
        return
    await edit_or_send(cb, texts.T(texts.REMINDER_YES_ANSWER), reply_markup=None)
    client = await db.get_client(booking["user_id"])
    name = (client or {}).get("name") or cb.from_user.full_name
    await notify_admin(
        cb.bot,
        f"✅ {name} подтвердил визит: {human_date(booking['date'])} "
        f"в {booking['time']} · {booking['pcs']} ПК (заявка #{booking['id']})",
    )
    await cb.answer()


@router.callback_query(F.data.startswith("rem:n:"))
async def reminder_no(cb: CallbackQuery) -> None:
    booking = await db.get_booking(int(cb.data.rsplit(":", 1)[1]))
    if not booking or booking["status"] != db.STATUS_CONFIRMED:
        await cb.answer(texts.REMINDER_INACTIVE, show_alert=True)
        return
    # Место освобождается; задачи (напоминание/отзыв) больше не нужны
    await db.set_booking_status(booking["id"], db.STATUS_CANCELLED)
    cancel_booking_jobs(booking["id"])
    await edit_or_send(cb, texts.T(texts.REMINDER_NO_ANSWER), reply_markup=None)
    client = await db.get_client(booking["user_id"])
    name = (client or {}).get("name") or cb.from_user.full_name
    await notify_admin(
        cb.bot,
        f"❌ {name} не сможет прийти: {human_date(booking['date'])} "
        f"в {booking['time']} · {booking['pcs']} ПК. Место освобождено "
        f"(заявка #{booking['id']})",
    )
    await cb.answer()


# ════════════════════════════════════════
# Отзывы: из меню и по запросу после сеанса
# ════════════════════════════════════════

@router.callback_query(F.data == "m:review")
async def menu_review(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_send(cb, texts.T(texts.REVIEW_MENU_PROMPT), reply_markup=rating_kb(0))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^rev:[1-5]:\d+$"))
async def review_rate(cb: CallbackQuery, state: FSMContext) -> None:
    """Оценка 1–5. callback: rev:<оценка>:<бронь id или 0>."""
    _, rating_str, booking_str = cb.data.split(":")
    rating = int(rating_str)
    await state.update_data(review_rating=rating, review_booking_id=int(booking_str))

    booking_id = int(booking_str) or None
    if rating >= 4:
        # 4–5: благодарим и просим отзыв на 2ГИС (кнопка — только если ссылка задана)
        await db.add_review(cb.from_user.id, rating, booking_id=booking_id)
        text = texts.REVIEW_THANKS
        if CLUB_MAPS_URL:
            text += "\n\n" + texts.REVIEW_MAPS_ASK
        await edit_or_send(cb, texts.T(text),
                           reply_markup=review_maps_kb() or back_menu_kb())
        await state.clear()
    else:
        # 1–3: просим комментарий — уйдёт владельцу
        await state.set_state(ReviewForm.comment)
        await edit_or_send(cb, texts.T(texts.REVIEW_BAD_ASK), reply_markup=review_skip_kb())
    await cb.answer()


@router.callback_query(F.data == "rev:skip")
async def review_skip_comment(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    rating = data.get("review_rating", 0)
    await state.clear()
    await db.add_review(cb.from_user.id, rating,
                        booking_id=data.get("review_booking_id") or None)
    await _send_complaint_to_owner(cb.bot, cb.from_user, rating, text=None)
    await edit_or_send(cb, texts.T(texts.REVIEW_BAD_SENT), reply_markup=back_menu_kb())
    await cb.answer()


@router.message(ReviewForm.comment, F.text)
async def review_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rating = data.get("review_rating", 0)
    await state.clear()
    await db.add_review(message.from_user.id, rating, message.text.strip(),
                        booking_id=data.get("review_booking_id") or None)
    await _send_complaint_to_owner(message.bot, message.from_user, rating,
                                   text=message.text.strip())
    await message.answer(texts.T(texts.REVIEW_BAD_SENT), reply_markup=back_menu_kb())


async def _send_complaint_to_owner(
    bot: Bot, user: User, rating: int, text: str | None
) -> None:
    """Жалоба (оценка 1–3) уходит владельцу."""
    username = f"@{user.username}" if user.username else f"id {user.id}"
    message_text = (
        f"⚠️ <b>Жалоба — оценка {rating} ⭐</b>\n"
        f"От: {user.full_name} ({username})"
    )
    if text:
        message_text += f"\n\n«{text}»"
    await notify_owners(bot, message_text)


# ════════════════════════════════════════
# Фолбэк на любой прочий текст (регистрировать ПОСЛЕДНИМ в роутере!)
# ════════════════════════════════════════

@router.message()
async def fallback_text(message: Message, state: FSMContext) -> None:
    await db.upsert_client(message.from_user.id, message.from_user.username,
                           message.from_user.full_name)
    await message.answer(texts.T(texts.UNKNOWN), reply_markup=main_menu_kb())
