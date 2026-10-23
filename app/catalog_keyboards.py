"""Клавиатуры каталога зон и компьютеров."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def zones_kb(zones: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for zone in zones:
        builder.button(text=f"{zone['color']} {zone['name']} — {zone['price_per_hour']} ₽/час",
                       callback_data=f"b:zone:{zone['id']}")
    builder.button(text="⬅️ В меню", callback_data="m:home")
    builder.adjust(1)
    return builder.as_markup()


def pcs_catalog_kb(pcs: list[dict], zone_id: int, offset: int = 0,
                   page_size: int = 8, conflict_ids: set[int] | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    conflict_ids = conflict_ids or set()
    for pc in pcs[offset:offset + page_size]:
        mark = " ⚠️" if pc["id"] in conflict_ids else ""
        builder.button(text=f"ПК {pc['number']}{mark}", callback_data=f"b:pc:{pc['id']}")
    nav = []
    if offset:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"b:pcpage:{zone_id}:{max(0, offset-page_size)}"))
    if offset + page_size < len(pcs):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"b:pcpage:{zone_id}:{offset+page_size}"))
    if nav: builder.row(*nav)
    builder.button(text="🎲 Любой свободный", callback_data=f"b:pcany:{zone_id}")
    builder.button(text="⬅️ Назад", callback_data="b:back:zone")
    builder.adjust(2)
    return builder.as_markup()


def group_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выбрать конкретные", callback_data="b:group:specific")],
        [InlineKeyboardButton(text="🎲 Любые свободные", callback_data="b:group:any")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="b:back:zone")],
    ])


def group_pc_kb(pcs: list[dict], selected: set[int], available_ids: set[int],
                zone_id: int, offset: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for pc in pcs[offset:offset + page_size]:
        if pc["id"] in available_ids:
            mark = "☑" if pc["id"] in selected else "☐"
            builder.button(text=f"{mark} ПК {pc['number']}", callback_data=f"b:multi:toggle:{pc['id']}")
        else:
            builder.button(text=f"⚠️ ПК {pc['number']} занято", callback_data=f"b:multibusy:{pc['id']}")
    nav = []
    if offset:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"b:multi:page:{zone_id}:{max(0, offset-page_size)}"))
    if offset + page_size < len(pcs):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"b:multi:page:{zone_id}:{offset+page_size}"))
    if nav: builder.row(*nav)
    builder.button(text="✅ Готово", callback_data="b:multi:done")
    builder.button(text="⬅️ Назад", callback_data="b:back:zone")
    builder.adjust(2)
    return builder.as_markup()


def pc_conflict_kb(pc_id: int, zone_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔀 Выбрать другой ПК", callback_data=f"b:pcpage:{zone_id}:0")
    builder.button(text="📤 Всё равно отправить заявку", callback_data=f"b:pcanyway:{pc_id}")
    builder.button(text="⬅️ Назад", callback_data="b:back:zone")
    builder.adjust(1)
    return builder.as_markup()
