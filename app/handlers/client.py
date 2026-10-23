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
from aiogram.types import (CallbackQuery, Message, ReplyKeyboardRemove, User,
                           InlineKeyboardButton, InlineKeyboardMarkup)

import app.admin_data as ad
import app.ai_helper as ai_helper
import app.database as db
import app.booking_units as booking_units
import app.followups as followups
import app.intent_parser as intent_parser
import app.config_loader as catalog
import app.roles as roles
import app.texts as texts
from app.config import (
    ADMIN_ID,
    CLUB_MAPS_URL,
    CLUB_PCS_TOTAL,
    CLUB_PHONE,
    CONSENT_VERSION,
    FAQ_BASE,
    MAX_ACTIVE_BOOKINGS,
    PRIVACY_POLICY_URL,
    DEMO_MODE,
    TIME_PERIODS,
    USE_AI,
)
from app.catalog_keyboards import (zones_kb, pcs_catalog_kb, pc_conflict_kb,
                                   group_choice_kb, group_pc_kb)
from app.keyboards import (
    address_kb,
    admin_booking_kb,
    ai_fail_kb,
    back_menu_kb,
    book_mode_kb,
    confirm_kb,
    consent_kb,
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
from app.states import BookingForm, FaqForm, FollowupForm, ReviewForm
from app.utils import booking_start_end, format_duration, human_date, iso_days_ahead, now

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
    # 152-ФЗ: /start — ещё НЕ согласие. Пока кнопка «Я согласен» не нажата,
    # данные клиента не сохраняются — показываем только экран согласия.
    if not await db.has_consent(message.from_user.id):
        await message.answer(texts.T(texts.consent_text()), reply_markup=consent_kb())
        return
    await db.upsert_client(message.from_user.id, message.from_user.username,
                           message.from_user.full_name)
    await message.answer(texts.T(texts.WELCOME), reply_markup=main_menu_kb())


@router.callback_query(F.data == "c:agree:v2")
async def consent_agree(cb: CallbackQuery, state: FSMContext) -> None:
    """
    Кнопка «✅ Я согласен» на экране согласия — это согласие на обработку
    персональных данных (152-ФЗ). Фиксируем его в таблице consents
    (доказательная база оператора) и только после этого сохраняем
    имя/username — обработка данных начинается с этого момента.
    """
    await state.clear()
    if not PRIVACY_POLICY_URL and not DEMO_MODE:
        await cb.answer(texts.CONSENT_UNAVAILABLE, show_alert=True)
        return
    await db.grant_consent(cb.from_user.id, CONSENT_VERSION, PRIVACY_POLICY_URL)
    await db.upsert_client(cb.from_user.id, cb.from_user.username, cb.from_user.full_name)
    logger.info("Согласие на обработку ПДн получено: %s (v%s)",
                cb.from_user.id, CONSENT_VERSION)
    await edit_or_send(cb, texts.T(texts.WELCOME), reply_markup=main_menu_kb())
    await cb.answer(texts.CONSENT_ACCEPTED_TOAST)


@router.callback_query(F.data == "c:revoke")
async def consent_revoke(cb: CallbackQuery, state: FSMContext) -> None:
    await db.revoke_consent(cb.from_user.id)
    await state.clear()
    await edit_or_send(cb, texts.T(texts.CONSENT_REVOKED))
    await cb.message.answer(texts.T(texts.consent_text()), reply_markup=consent_kb())
    await cb.answer("Согласие отозвано")


@router.callback_query(F.data == "c:decline")
async def consent_decline(cb: CallbackQuery, state: FSMContext) -> None:
    """Отказ: данные не собираются, брони недоступны — до нажатия «Я согласен»."""
    await state.clear()
    await edit_or_send(cb, texts.T(texts.CONSENT_DECLINED), reply_markup=consent_kb())
    await cb.answer()


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
    await edit_or_send(cb, texts.T(texts.CONTACT), reply_markup=contact_kb())
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
    pc_number = filled.pop("pc_number", None)
    if pc_number:
        matches = [pc for pc in (await catalog.load_config())["pcs"]
                   if pc["is_active"] and pc["status"] == "active" and pc["number"] == pc_number]
        if len(matches) == 1:
            chosen = matches[0]
            filled.update(zone_id=chosen["zone_id"], pc_id=chosen["id"],
                          pc_preference="specific", pcs=1, catalog_done=True)
            if filled.get("date") and filled.get("time"):
                available = await catalog.get_available_pcs_for_time(
                    filled["date"], filled["time"], filled.get("duration", 1), chosen["zone_id"])
                filled["pc_conflict"] = chosen["id"] not in {item["id"] for item in available}
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

    pc_number = str(parsed.get("pc_number") or "").strip()
    if pc_number and len(pc_number) <= 12 and re.fullmatch(r"[\w-]+", pc_number):
        out["pc_number"] = pc_number

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
    elif not data.get("catalog_done"):
        zones = await catalog.get_zones()
        zones = [z for z in zones if await catalog.get_pcs_by_zone(z["id"])]
        if not zones:
            await state.update_data(catalog_done=True)
            await _booking_next_step(event, state)
        elif data.get("zone_id") is None:
            if len(zones) == 1:
                await state.update_data(zone_id=zones[0]["id"])
                await _booking_next_step(event, state)
            else:
                await state.set_state(BookingForm.zone)
                await _send_step(event, "🎮 Выберите зону:", zones_kb(zones))
        elif "pcs" not in data:
            zone = next((z for z in zones if z["id"] == data["zone_id"]), None)
            if zone and not zone.get("requires_quantity", 1):
                await state.update_data(pcs=1)
                await _booking_next_step(event, state)
            else:
                await _ask_pcs(event, state)
        elif data["pcs"] > 1 and not data.get("group_mode"):
            await state.set_state(BookingForm.pc)
            await _send_step(event, "🖥 Хотите конкретные места или подберём свободные вместе?", group_choice_kb())
        elif data["pcs"] > 1 and data.get("group_mode") == "specific" and not data.get("catalog_done"):
            await _ask_group_selection(event, state, data["zone_id"])
        elif data["pcs"] == 1 and data.get("pc_preference") is None:
            await _ask_catalog_pc(event, state, data["zone_id"])
        else:
            await state.update_data(catalog_done=True)
            await _booking_next_step(event, state)
    elif data.get("pc_preference") == "any" and "pcs" not in data:
        await _ask_pcs(event, state)
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


async def _ask_catalog_pc(event, state: FSMContext, zone_id: int, offset: int = 0) -> None:
    pcs = await catalog.get_pcs_by_zone(zone_id)
    data = await state.get_data()
    free = await catalog.get_available_pcs_for_time(
        data.get("date", ""), data.get("time", "00:00"), data.get("duration", 1), zone_id
    ) if data.get("date") else pcs
    free_ids = {pc["id"] for pc in free}
    markup = pcs_catalog_kb(pcs, zone_id, offset, conflict_ids={pc["id"] for pc in pcs} - free_ids)
    await state.set_state(BookingForm.pc)
    await _send_step(event, "🖥 Выберите ПК в этой зоне. ⚠️ означает, что по данным бота на него есть пересечение с подтверждённой бронью.", markup)


async def _ask_group_selection(event, state: FSMContext, zone_id: int, offset: int = 0) -> None:
    data = await state.get_data()
    pcs = [pc for pc in (await catalog.load_config())["pcs"]
           if pc["zone_id"] == zone_id and pc["is_active"] and pc["status"] == "active"]
    free = await catalog.get_available_pcs_for_time(data["date"], data["time"], data["duration"], zone_id)
    selected = set(data.get("requested_pc_ids", []))
    markup = group_pc_kb(pcs, selected, {pc["id"] for pc in free}, zone_id, offset)
    await state.set_state(BookingForm.pc)
    await _send_step(event, f"🖥 Выберите {data['pcs']} места (можно несколько). Выбрано: {len(selected)} из {data['pcs']}.", markup)


async def _ask_name(event, state: FSMContext) -> None:
    await state.set_state(BookingForm.name)
    await _send_step(event, texts.BOOK_NAME, back_menu_kb())


# ── групповое бронирование: мультивыбор мест ──

@router.callback_query(F.data == "b:group:specific")
async def book_group_specific(cb: CallbackQuery, state: FSMContext):
    data=await state.get_data(); await state.update_data(group_mode="specific",requested_pc_ids=[],catalog_done=False)
    await _ask_group_selection(cb,state,data["zone_id"]); await cb.answer()

@router.callback_query(F.data == "b:group:any")
async def book_group_any(cb: CallbackQuery, state: FSMContext):
    await state.update_data(group_mode="any",pc_preference="any",requested_pc_ids=[],catalog_done=True)
    await _booking_next_step(cb,state); await cb.answer()

@router.callback_query(F.data.startswith("b:multi:toggle:"))
async def book_group_toggle(cb: CallbackQuery,state:FSMContext):
    pc_id=int(cb.data.rsplit(":",1)[1]); data=await state.get_data()
    selected=list(data.get("requested_pc_ids",[])); target=await catalog.get_pc_by_id(pc_id)
    if not target or target["status"]!="active" or target["zone_id"]!=data.get("zone_id"):
        await cb.answer("Это место недоступно",show_alert=True); return
    free=await catalog.get_available_pcs_for_time(data["date"],data["time"],data["duration"],data["zone_id"])
    if pc_id not in {item["id"] for item in free}:
        await cb.answer("Этот ПК уже занят по подтверждённой брони бота",show_alert=True); return
    if pc_id in selected: selected.remove(pc_id)
    elif len(selected)<int(data.get("pcs",1)): selected.append(pc_id)
    else:
        await cb.answer(f"Выбрано {len(selected)} из {data['pcs']}. Уберите одно место.",show_alert=True); return
    await state.update_data(requested_pc_ids=selected)
    await _ask_group_selection(cb,state,data["zone_id"]); await cb.answer()

@router.callback_query(F.data.startswith("b:multi:page:"))
async def book_group_page(cb: CallbackQuery,state:FSMContext):
    _,_,_,zone_id,offset=cb.data.split(":")
    await _ask_group_selection(cb,state,int(zone_id),int(offset)); await cb.answer()

@router.callback_query(F.data == "b:multi:done")
async def book_group_done(cb: CallbackQuery,state:FSMContext):
    data=await state.get_data(); selected=list(data.get("requested_pc_ids",[])); count=int(data.get("pcs",1))
    if len(selected)!=count:
        await cb.answer(f"Выбрано {len(selected)} из {count}. Выберите ещё {count-len(selected)} мест.",show_alert=True); return
    await state.update_data(pc_preference="specific",pc_id=selected[0] if selected else None,
                            requested_pc_ids=selected,catalog_done=True)
    await _booking_next_step(cb,state); await cb.answer()

@router.callback_query(F.data.startswith("b:multibusy:"))
async def book_group_busy(cb: CallbackQuery):
    await cb.answer("Это место уже занято по подтверждённой брони бота.",show_alert=True)

# ── выбор зоны и компьютера из каталога ──

@router.callback_query(F.data.startswith("b:zone:"))
async def book_choose_zone(cb: CallbackQuery, state: FSMContext) -> None:
    zone_id = int(cb.data.rsplit(":", 1)[1])
    zones = await catalog.get_zones()
    zone = next((item for item in zones if item["id"] == zone_id), None)
    if not zone or not await catalog.get_pcs_by_zone(zone_id):
        await cb.answer("Эта зона сейчас недоступна", show_alert=True)
        return
    await state.update_data(zone_id=zone_id)
    await _booking_next_step(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("b:pcpage:"))
async def book_pc_page(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, zone_id, offset = cb.data.split(":")
    await _ask_catalog_pc(cb, state, int(zone_id), int(offset))
    await cb.answer()


@router.callback_query(F.data.startswith("b:pc:"))
async def book_choose_catalog_pc(cb: CallbackQuery, state: FSMContext) -> None:
    pc_id = int(cb.data.rsplit(":", 1)[1])
    pc = await catalog.get_pc_by_id(pc_id)
    data = await state.get_data()
    if not pc or pc["status"] != "active" or pc["zone_id"] != data.get("zone_id"):
        await cb.answer("Этот ПК сейчас недоступен", show_alert=True)
        return
    available = await catalog.get_available_pcs_for_time(
        data["date"], data["time"], data["duration"], data["zone_id"]
    )
    if pc_id not in {item["id"] for item in available}:
        await state.update_data(candidate_pc_id=pc_id)
        await edit_or_send(cb, "⚠️ Этот ПК пересекается с подтверждённой бронью через бота. Можно выбрать другой или всё равно отправить пожелание админу.",
                           reply_markup=pc_conflict_kb(pc_id, data["zone_id"]))
        await cb.answer()
        return
    await state.update_data(pc_id=pc_id, pc_preference="specific", pcs=1, catalog_done=True)
    await _booking_next_step(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("b:pcanyway:"))
async def book_pc_anyway(cb: CallbackQuery, state: FSMContext) -> None:
    pc_id = int(cb.data.rsplit(":", 1)[1])
    pc = await catalog.get_pc_by_id(pc_id)
    if not pc or pc["status"] != "active":
        await cb.answer("ПК больше недоступен", show_alert=True)
        return
    await state.update_data(pc_id=pc_id, pc_preference="specific", pc_conflict=True,
                            pcs=1, catalog_done=True)
    await _booking_next_step(cb, state)
    await cb.answer()


@router.callback_query(F.data.startswith("b:pcany:"))
async def book_choose_any_pc(cb: CallbackQuery, state: FSMContext) -> None:
    zone_id = int(cb.data.rsplit(":", 1)[1])
    await state.update_data(zone_id=zone_id, pc_id=None, pc_preference="any", catalog_done=True)
    await _booking_next_step(cb, state)
    await cb.answer()


@router.callback_query(F.data == "b:back:zone")
async def book_back_to_zone(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    zones = await catalog.get_zones()
    if len(zones) > 1:
        await state.update_data(zone_id=None, pc_id=None, pc_preference=None,
                                catalog_done=False, pcs=None)
        await state.set_state(BookingForm.zone)
        await edit_or_send(cb, "🎮 Выберите зону:", reply_markup=zones_kb(zones))
    elif data.get("zone_id"):
        await _ask_catalog_pc(cb, state, data["zone_id"])
    await cb.answer()


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


async def _capacity_warning(day: str, time_str: str, duration: int, want: int) -> str:
    """Предупреждает о нагрузке, но оставляет решение о заявке администратору."""
    free = await db.pcs_free(day, time_str, duration, CLUB_PCS_TOTAL)
    if want > free:
        return "\n\n⚠️ На это время уже есть подтверждённая бронь. Заявка будет отправлена админу; он подтвердит её, если свободных ПК хватит."
    return ""


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
    zone = next((z for z in await catalog.get_zones() if z["id"] == data.get("zone_id")), None)
    requested_ids = data.get("requested_pc_ids") or ([data["pc_id"]] if data.get("pc_id") else [])
    pcs_requested = [await catalog.get_pc_by_id(pc_id) for pc_id in requested_ids]
    pc_labels = ", ".join(f"ПК {item['number']}" for item in pcs_requested if item)
    catalog_line = (f"🎯 {zone['name']} · " + (pc_labels or "любой свободный") + "\n") if zone else ""
    text = (
        f"{texts.BOOK_CONFIRM_TITLE}\n\n"
        f"📅 {human_date(data['date'])} в {data['time']}\n"
        f"⏱ {data['duration']} ч · 🖥 {data['pcs']} ПК\n"
        f"{catalog_line}"
        f"👤 {data['name']}\n"
        f"📞 {data.get('phone') or 'не указан — связь в Telegram'}"
    )
    if data.get("pc_conflict"):
        text += "\n\n⚠️ ПК уже занят по данным бота. Заявка уйдёт админу как пожелание."
    warning = await _capacity_warning(data['date'], data['time'], data['duration'], data['pcs'])
    await message.answer(texts.T(text + warning), reply_markup=confirm_kb())


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
    warning = await _capacity_warning(data["date"], data["time"], data["duration"], data["pcs"])
    booking_id = await db.create_booking(
        cb.from_user.id, data["date"], data["time"], data["duration"], data["pcs"],
        zone_id=data.get("zone_id"), pc_id=data.get("pc_id"),
        pc_preference=data.get("pc_preference"), pc_conflict=bool(data.get("pc_conflict")),
        requested_pc_ids=data.get("requested_pc_ids"),
    )
    if data.get("phone"):
        await db.set_client_phone(cb.from_user.id, data["phone"])
    await db.set_client_name(cb.from_user.id, data["name"])
    await state.clear()

    await edit_or_send(cb, texts.T(texts.BOOK_SENT + warning), reply_markup=back_menu_kb())
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

    zone = next((z for z in await catalog.get_zones() if z["id"] == booking.get("zone_id")), None)
    unit_rows = await booking_units.units_for_booking(booking["id"])
    requested_ids = [row["requested_unit_id"] for row in unit_rows if row.get("requested_unit_id")]
    preferred_pcs = [await catalog.get_pc_by_id(pc_id) for pc_id in requested_ids]
    preferred_pcs = [pc for pc in preferred_pcs if pc]
    requested_text = ", ".join(f"ПК {pc['number']}" for pc in preferred_pcs) or "любой"
    requested_line = (f"🎯 Зона: {zone['name']}\n🖥 Мест: {booking['pcs']}\n👥 Гостей: {booking['pcs']}\nПожелания: {requested_text}\n") if zone else ""
    conflict_line = ""
    if preferred_pcs:
        available = await catalog.get_available_pcs_for_time(
            booking["date"], booking["time"], booking["duration"], booking.get("zone_id"))
        free_numbers = [pc["number"] for pc in preferred_pcs if pc["id"] in {item["id"] for item in available}]
        conflict_line = ("По боту: ПК " + ", ".join(free_numbers) + " свободны на это время\n"
                         if len(free_numbers)==len(preferred_pcs) else "⚠️ Есть пожелание по ПК с конфликтом/занятостью\n")
    group_mark=" (групповая)" if booking["pcs"]>1 else ""
    text = (
        f"🎮 <b>Новая заявка #{booking['id']}{group_mark}</b>\n"
        f"📅 {human_date(booking['date'])} в {booking['time']}\n"
        f"⏱ {booking['duration']} ч · 🖥 {booking['pcs']} ПК\n"
        f"{requested_line}{conflict_line}"
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
    for booking in items:
        if booking["status"] == db.STATUS_CONFIRMED:
            _, end = booking_start_end(booking["date"], booking["time"], booking["duration"], booking.get("duration_minutes", 0))
            if end <= now(): booking["status"] = "completed"
    line_items=[]
    for b in items:
        units=await booking_units.units_for_booking(b["id"])
        assigned=sum(1 for unit in units if unit.get("status")=="assigned" and unit.get("unit_id"))
        places=(f"мест назначено {assigned}/{b['pcs']}" if b.get("zone_id") and assigned<b["pcs"]
                else f"{b['pcs']} ПК")
        line_items.append(f"#{b['id']} · 📅 {human_date(b['date'])} в {b['time']} · "
                          f"⏱ {format_duration(b)} · 🖥 {places} · {db.STATUS_LABELS.get(b['status'], b['status'])}")
    lines="\n".join(line_items)
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

def _followup_add_markup(booking_id:int, pcs:list[dict], selected:set[int]) -> InlineKeyboardMarkup:
    rows=[]
    for pc in pcs:
        mark="☑" if pc["id"] in selected else "☐"
        rows.append([InlineKeyboardButton(text=f"{mark} ПК {pc['number']}",callback_data=f"fu:add_toggle:{booking_id}:{pc['id']}")])
    rows.append([InlineKeyboardButton(text=f"📨 Отправить запрос ({len(selected)})",callback_data=f"fu:add_send:{booking_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад",callback_data=f"fu:manage:{booking_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def _followup_admin_notice(bot:Bot,booking:dict,request_id:int,text:str):
    markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить",callback_data=f"fuadm:yes:{request_id}"),
        InlineKeyboardButton(text="❌ Отклонить",callback_data=f"fuadm:no:{request_id}")]])
    for admin_id in roles.admin_ids() or [ADMIN_ID]:
        try: await bot.send_message(admin_id,text,reply_markup=markup)
        except TelegramAPIError: logger.exception("Не удалось отправить follow-up админу %s",admin_id)

async def _request_late_or_extend(bot:Bot,booking:dict,kind:str,value:int,user:User):
    import json
    request_id=await followups.create_request(booking["id"],user.id,kind,value)
    labels=[]
    for unit in await booking_units.units_for_booking(booking["id"]):
        if unit.get("unit_id"):
            pc=await catalog.get_pc_by_id(unit["unit_id"])
            if pc: labels.append(f"ПК {pc['number']}")
    username=f"@{user.username}" if user.username else user.full_name
    if kind=="extend":
        conflicts=await followups.extension_conflicts(booking["id"],value)
        conflict_text=(f"⚠️ Конфликт с бронью #{conflicts[0]['id']}" if conflicts else "По боту: конфликтов нет")
        text=(f"⏱ <b>Запрос на продление #{booking['id']}</b>\n👤 {username}\n🖥 {', '.join(labels) or 'места не назначены'}\n"
              f"📅 {booking['time']} · +{value} мин\n{conflict_text}")
        client_text=("⏱ Запрос на продление отправлен админу. Ожидайте подтверждения." if not conflicts else
                     "⚠️ По данным бота после вашей брони есть конфликт. Запрос передан админу; он уточнит возможность продления.")
    elif kind=="late":
        text=(f"⏰ <b>Клиент опаздывает #{booking['id']}</b>\n👤 {username}\n🖥 {', '.join(labels) or 'места не назначены'}\n"
              f"📅 Бронь: {booking['time']} · опоздание ~{value} мин")
        client_text="Админ уведомлён. Если опоздаете больше чем на 30 минут — бронь может быть отменена."
    else:
        text=(f"⏩ <b>Клиент приедет раньше #{booking['id']}</b>\n👤 {username}\n🖥 {', '.join(labels) or 'места не назначены'}\n"
              f"📅 Бронь: {booking['time']} · раньше на ~{value} мин")
        client_text="Админ уведомлён о вашем раннем приезде. Время брони не изменяется."
    await _followup_admin_notice(bot,booking,request_id,text)
    return client_text

async def _submit_add_units_request(bot:Bot,booking:dict,user:User,unit_ids:list[int]) -> bool:
    import json
    if not unit_ids: return False
    request_id=await followups.create_request(booking["id"],user.id,"add_unit",len(unit_ids),json.dumps(unit_ids))
    await booking_units.add_pending_units(booking["id"],unit_ids,request_id)
    username=f"@{user.username}" if user.username else user.full_name
    labels=[]
    for pcid in unit_ids:
        pc=await catalog.get_pc_by_id(pcid)
        if pc: labels.append(f"ПК {pc['number']}")
    await _followup_admin_notice(bot,booking,request_id,
        f"➕ <b>Дополнение к брони #{booking['id']}</b>\n👤 {username}\n🖥 Запрошены: {', '.join(labels)}")
    return True

@router.callback_query(F.data.startswith("fu:manage:"))
async def followup_manage(cb:CallbackQuery,state:FSMContext):
    booking_id=int(cb.data.rsplit(":",1)[1]); booking=await followups.active_booking_for_user(booking_id,cb.from_user.id)
    if not booking: await cb.answer("Эта бронь уже завершена или недоступна.",show_alert=True); return
    await state.clear()
    await edit_or_send(cb,f"🛠 Управление бронью #{booking_id}\n📅 {human_date(booking['date'])} · {booking['time']}",reply_markup=followups.manage_keyboard(booking_id))
    await cb.answer()

@router.callback_query(F.data.startswith("fu:add:"))
async def followup_add_start(cb:CallbackQuery,state:FSMContext):
    bid=int(cb.data.rsplit(":",1)[1]); booking=await followups.active_booking_for_user(bid,cb.from_user.id)
    if not booking or not booking["zone_id"]: await cb.answer("Бронь завершена или не поддерживает выбор мест.",show_alert=True); return
    pcs=await booking_units.available_units(booking["date"],booking["time"],booking["duration"],booking["zone_id"],bid)
    if not pcs:
        await cb.answer(f"На это время мест в зоне не осталось.",show_alert=True); return
    await state.set_state(FollowupForm.add_units); await state.update_data(followup_booking_id=bid,followup_units=[])
    await edit_or_send(cb,"➕ Выберите одно или несколько свободных мест для дополнительного запроса:",reply_markup=_followup_add_markup(bid,pcs,set()))
    await cb.answer()

@router.callback_query(F.data.startswith("fu:add_toggle:"),FollowupForm.add_units)
async def followup_add_toggle(cb:CallbackQuery,state:FSMContext):
    _,_,_,bid,pcid=cb.data.split(":"); bid=int(bid);pcid=int(pcid)
    booking=await followups.active_booking_for_user(bid,cb.from_user.id)
    if not booking: await cb.answer("Бронь завершена.",show_alert=True); return
    pcs=await booking_units.available_units(booking["date"],booking["time"],booking["duration"],booking["zone_id"],bid)
    if pcid not in {pc["id"] for pc in pcs}: await cb.answer("Это место уже недоступно.",show_alert=True); return
    selected=list((await state.get_data()).get("followup_units",[]))
    if pcid in selected:selected.remove(pcid)
    else:selected.append(pcid)
    await state.update_data(followup_units=selected)
    await edit_or_send(cb,"➕ Выберите свободные места:",reply_markup=_followup_add_markup(bid,pcs,set(selected)))
    await cb.answer()

@router.callback_query(F.data.startswith("fu:add_send:"),FollowupForm.add_units)
async def followup_add_send(cb:CallbackQuery,state:FSMContext):
    bid=int(cb.data.rsplit(":",1)[1]); booking=await followups.active_booking_for_user(bid,cb.from_user.id)
    selected=list((await state.get_data()).get("followup_units",[]))
    if not booking or not selected: await cb.answer("Выберите хотя бы одно место.",show_alert=True); return
    await _submit_add_units_request(cb.bot,booking,cb.from_user,selected)
    await state.clear(); await edit_or_send(cb,"✅ Запрос на добавление мест отправлен администратору.",reply_markup=followups.manage_keyboard(bid)); await cb.answer()

@router.callback_query(F.data.startswith("fu:extend:"))
async def followup_extend_menu(cb:CallbackQuery):
    bid=int(cb.data.rsplit(":",1)[1])
    if not await followups.active_booking_for_user(bid,cb.from_user.id): await cb.answer("Бронь завершена.",show_alert=True); return
    await edit_or_send(cb,"⏱ На сколько продлить? Продление вступит в силу только после подтверждения администратора.",reply_markup=followups.extend_keyboard(bid)); await cb.answer()

@router.callback_query(F.data.startswith("fu:extend_request:"))
async def followup_extend_request(cb:CallbackQuery):
    _,_,bid,minutes=cb.data.split(":");booking=await followups.active_booking_for_user(int(bid),cb.from_user.id)
    if not booking: await cb.answer("Бронь завершена.",show_alert=True); return
    reply=await _request_late_or_extend(cb.bot,booking,"extend",int(minutes),cb.from_user)
    await edit_or_send(cb,reply,reply_markup=followups.manage_keyboard(int(bid))); await cb.answer()

@router.callback_query(F.data.startswith("fu:late:"))
async def followup_late_menu(cb:CallbackQuery):
    bid=int(cb.data.rsplit(":",1)[1])
    if not await followups.active_booking_for_user(bid,cb.from_user.id): await cb.answer("Бронь завершена.",show_alert=True); return
    await edit_or_send(cb,"⏰ На сколько минут опоздаете? Время брони не сдвигается.",reply_markup=followups.late_keyboard(bid)); await cb.answer()

@router.callback_query(F.data.startswith("fu:late_request:"))
async def followup_late_request(cb:CallbackQuery):
    _,_,bid,minutes=cb.data.split(":");booking=await followups.active_booking_for_user(int(bid),cb.from_user.id)
    if not booking: await cb.answer("Бронь завершена.",show_alert=True); return
    reply=await _request_late_or_extend(cb.bot,booking,"late",int(minutes),cb.from_user)
    await edit_or_send(cb,reply,reply_markup=followups.manage_keyboard(int(bid))); await cb.answer()

@router.callback_query(F.data.startswith("fu:early:"))
async def followup_early_menu(cb:CallbackQuery):
    bid=int(cb.data.rsplit(":",1)[1])
    if not await followups.active_booking_for_user(bid,cb.from_user.id): await cb.answer("Бронь завершена.",show_alert=True); return
    await edit_or_send(cb,"⏩ На сколько минут планируете приехать раньше? Время брони не изменяется.",reply_markup=followups.early_keyboard(bid)); await cb.answer()

@router.callback_query(F.data.startswith("fu:early_request:"))
async def followup_early_request(cb:CallbackQuery):
    _,_,bid,minutes=cb.data.split(":");booking=await followups.active_booking_for_user(int(bid),cb.from_user.id)
    if not booking: await cb.answer("Бронь завершена.",show_alert=True); return
    reply=await _request_late_or_extend(cb.bot,booking,"early",int(minutes),cb.from_user)
    await edit_or_send(cb,reply,reply_markup=followups.manage_keyboard(int(bid))); await cb.answer()

@router.callback_query(F.data.startswith("fu:cancel:"))
async def followup_cancel_ask(cb:CallbackQuery):
    bid=int(cb.data.rsplit(":",1)[1]);booking=await followups.active_booking_for_user(bid,cb.from_user.id)
    if not booking: await cb.answer("Бронь завершена.",show_alert=True); return
    await edit_or_send(cb,f"Точно отменить бронь #{bid} на {booking['time']}?",reply_markup=followups.cancel_confirm_keyboard(bid)); await cb.answer()

@router.callback_query(F.data.startswith("fu:cancel_yes:"))
async def followup_cancel_yes(cb:CallbackQuery):
    bid=int(cb.data.rsplit(":",1)[1]);booking=await followups.active_booking_for_user(bid,cb.from_user.id)
    if not booking or not await followups.cancel_booking(bid,cb.from_user.id): await cb.answer("Не удалось отменить: бронь уже завершена.",show_alert=True); return
    cancel_booking_jobs(bid); await db.log_audit(cb.from_user.id,f"client cancelled booking #{bid}")
    await notify_admin(cb.bot,f"❌ Клиент отменил бронь #{bid}. Места освобождены.")
    await edit_or_send(cb,"✅ Бронь отменена. Администратор уведомлён.",reply_markup=main_menu_kb()); await cb.answer()

@router.message()
async def fallback_text(message: Message, state: FSMContext) -> None:
    await db.upsert_client(message.from_user.id, message.from_user.username,
                           message.from_user.full_name)
    if await ad.ai_enabled() and await state.get_state() is None:
        parsed=intent_parser.parse_followup(message.text or "")
        if parsed["intent"]!="UNKNOWN":
            bookings=await db.get_client_bookings(message.from_user.id,only_active=True)
            active=[]
            for item in bookings:
                full=await followups.active_booking_for_user(item["id"],message.from_user.id)
                if full: active.append(full)
            if active:
                booking=active[0]; intent=parsed["intent"]; value=parsed["params"].get("minutes")
                if intent=="CANCEL":
                    await message.answer(f"Отменить бронь #{booking['id']}?",reply_markup=followups.cancel_confirm_keyboard(booking['id'])); return
                if intent in {"LATE","EARLY"} and value:
                    kind="late" if intent=="LATE" else "early"
                    reply=await _request_late_or_extend(message.bot,booking,kind,int(value),message.from_user)
                    await message.answer(reply); return
                if intent=="EXTEND" and value and 1<=int(value)<=240:
                    reply=await _request_late_or_extend(message.bot,booking,"extend",int(value),message.from_user)
                    await message.answer(reply); return
                if intent=="ADD_UNIT":
                    number=parsed["params"].get("unit_number")
                    pcs=[pc for pc in (await catalog.load_config())["pcs"] if pc["is_active"] and pc["status"]=="active"
                         and pc["zone_id"]==booking.get("zone_id") and pc["number"]==str(number)]
                    if pcs:
                        available=await booking_units.available_units(booking["date"],booking["time"],booking["duration"],booking["zone_id"],booking["id"])
                        if pcs[0]["id"] in {pc["id"] for pc in available}:
                            await _submit_add_units_request(message.bot,booking,message.from_user,[pcs[0]["id"]])
                            await message.answer("✅ Запрос на добавление места отправлен администратору."); return
                        await message.answer("Этот ПК занят по данным бота. Откройте управление бронью для выбора другого места.",reply_markup=followups.manage_keyboard(booking['id'])); return
                await message.answer("Вот что можно сделать с активной бронью:",reply_markup=followups.manage_keyboard(booking['id'])); return
    await message.answer(texts.T(texts.UNKNOWN), reply_markup=main_menu_kb())
