"""Inline-панель администратора. Клиентские сценарии в этом модуле не затрагиваются."""
from __future__ import annotations

import asyncio, csv, io, logging
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as K, Message

import app.admin_data as ad
import app.database as db
import app.roles as roles
import app.texts as texts
from app.bot_commands import apply_for_user
from app.config import ADMIN_ID, AI_MODEL, CLUB_HOURS, CLUB_PCS_TOTAL, OWNER_ID, USE_AI
from app.middlewares import AdminAreaMiddleware
from app.scheduler import cancel_booking_jobs, schedule_booking_jobs
from app.states import AdminEditForm, BroadcastForm
from app.utils import MONTHS, WEEKDAYS, end_time_str, human_date, load_bar, now

logger = logging.getLogger(__name__)
router = Router()
PAGE = 10
_broadcast_cancelled: set[int] = set()

# Панель открыта только сотрудникам клуба; клиент получает понятный отказ,
# а не «зависшую» кнопку.
router.message.outer_middleware(AdminAreaMiddleware())
router.callback_query.outer_middleware(AdminAreaMiddleware())


class IsAdmin(BaseFilter):
    """Сотрудник клуба: владелец или администратор (список — в базе)."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user and roles.is_admin(event.from_user.id))


class IsOwner(BaseFilter):
    """Только владелец: управление админами и удаление данных."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user and roles.is_owner(event.from_user.id))


def kb(rows): return K(inline_keyboard=rows)
def btn(text, data): return B(text=text, callback_data=data)
def back(data="admin:home"): return [btn("⬅️ Назад", data)]
async def show(cb: CallbackQuery, text: str, markup: K):
    try: await cb.message.edit_text(text, reply_markup=markup)
    except TelegramAPIError: await cb.message.answer(text, reply_markup=markup)
    await cb.answer()
async def log_action(user: int, action: str):
    """Действие админа: в лог-файл и в журнал базы (виден в панели)."""
    logger.info("ADMIN %s | %s", user, action)
    try: await db.log_audit(user, action)
    except Exception: logger.exception("Не удалось записать журнал действий")


def home_kb():
    return kb([[btn("📅 Сегодня","admin:today:0"),btn("📊 Статистика","admin:stats")],
               [btn("👥 Клиенты","admin:clients"),btn("⭐ Отзывы","admin:reviews:0")],
               [btn("📢 Рассылка","admin:broadcast"),btn("⚙️ Настройки","admin:settings")],
               [btn("❓ Помощь","admin:help")]])

async def screen_home(target, state: FSMContext):
    await state.clear(); name = target.from_user.first_name or target.from_user.full_name
    role = roles.ROLE_LABELS.get(roles.role_of(target.from_user.id), "")
    closed = await ad.club_closed()
    text=(f"👋 Привет, {name}.\n{role} · панель управления клубом"
          + ("\n\n🔒 <b>Клуб на паузе</b> — новые брони не принимаются." if closed else "")
          + "\n\nЧто делаем?")
    if isinstance(target, CallbackQuery): await show(target,text,home_kb())
    else: await target.answer(text,reply_markup=home_kb())

@router.message(CommandStart(), IsAdmin())
async def start(m: Message,state:FSMContext): await screen_home(m,state)
@router.callback_query(F.data=="admin:home",IsAdmin())
async def home(c:CallbackQuery,state:FSMContext): await screen_home(c,state)
@router.message(Command("cancel"),IsAdmin())
async def cancel(m:Message,state:FSMContext): await screen_home(m,state)

# Сегодня и карточка
async def screen_today(c:CallbackQuery,page=0):
    items=await db.get_bookings_for_date(now().date().isoformat()); page=max(0,min(page,max(0,(len(items)-1)//PAGE)))
    rows=[]
    for x in items[page*PAGE:(page+1)*PAGE]:
        cl=await db.get_client(x['user_id']) or {}; icon="🟢" if x['status']==db.STATUS_CONFIRMED else "🟡" if x['status']==db.STATUS_NEW else "🔴"
        rows.append([btn(f"{icon} {x['time']}–{end_time_str(x['time'],x['duration'])} · {x['pcs']} ПК · {cl.get('name') or x['user_id']}",f"admin:booking:{x['id']}")])
    nav=[]
    if page: nav.append(btn("◀️",f"admin:today:{page-1}"))
    if (page+1)*PAGE<len(items): nav.append(btn("▶️",f"admin:today:{page+1}"))
    if nav: rows.append(nav)
    rows += [[btn("⬅️ Назад","admin:home"),btn("🔄 Обновить",f"admin:today:{page}")]]
    await show(c,f"📅 <b>Брони на сегодня</b> · {human_date(now().date().isoformat())}\n\n"+("Выберите бронь:" if items else "Броней пока нет."),kb(rows))
@router.callback_query(F.data.startswith("admin:today:"),IsAdmin())
async def today_cb(c): await screen_today(c,int(c.data.rsplit(':',1)[1]))
@router.message(Command("today"),IsAdmin())
async def today_cmd(m):
    await m.answer("📅 Брони на сегодня",reply_markup=kb([[btn("Открыть список","admin:today:0")],back()]))

async def screen_booking(c,bid):
    x=await db.get_booking(bid)
    if not x: await c.answer("Бронь не найдена",show_alert=True); return
    cl=await db.get_client(x['user_id']) or {}; username=f"@{cl['username']}" if cl.get('username') else "без username"
    text=(f"🎮 <b>Бронь #{bid}</b>\n📅 {human_date(x['date'])}, {x['time']}–{end_time_str(x['time'],x['duration'])}\n"
          f"🖥 {x['pcs']} ПК\n👤 {cl.get('name') or 'Без имени'} ({username})\n📞 {cl.get('phone') or 'не указан'}\n📊 Статус: {db.STATUS_LABELS.get(x['status'],x['status'])}")
    rows=[[btn("✅ Подтвердить",f"admin:booking_status:{bid}:confirmed"),btn("✏️ Изменить",f"admin:booking_edit:{bid}")],
          [btn("🚫 Неявка",f"admin:booking_status:{bid}:noshow"),btn("❌ Отменить",f"admin:booking_status:{bid}:cancelled")],
          [btn("💬 Написать клиенту",f"admin:booking_message:{bid}")],
          [btn("🗑 Удалить бронь",f"admin:ask:booking_del:{bid}")],back("admin:today:0")]
    await show(c,text,kb(rows))
@router.callback_query(F.data.regexp(r"^admin:booking:\d+$"),IsAdmin())
async def booking(c): await screen_booking(c,int(c.data.rsplit(':',1)[1]))
@router.callback_query(F.data.startswith("admin:booking_status:"),IsAdmin())
async def booking_status(c):
    _,_,bid,status=c.data.split(':'); x=await db.get_booking(int(bid))
    if not x: await c.answer("Не найдено",show_alert=True); return
    await db.set_booking_status(int(bid),status)
    if status==db.STATUS_CONFIRMED:
        if x['status']!=db.STATUS_CONFIRMED: await db.add_visit(x['user_id'])
        schedule_booking_jobs(c.bot,{**x,'status':status})
    else: cancel_booking_jobs(int(bid))
    try: await c.bot.send_message(x['user_id'],f"Статус брони #{bid} изменён: {db.STATUS_LABELS[status]}")
    except TelegramAPIError: pass
    await log_action(c.from_user.id,f"booking #{bid} -> {status}"); await screen_booking(c,int(bid))

# Совместимость с кнопками в карточках новых заявок, которые отправляет
# неизменённая клиентская часть.
@router.callback_query(F.data.startswith("adm:ok:"), IsAdmin())
async def legacy_confirm(c: CallbackQuery):
    bid = int(c.data.rsplit(":", 1)[1]); x = await db.get_booking(bid)
    if not x or x["status"] != db.STATUS_NEW:
        await c.answer("Заявка уже обработана", show_alert=True); return
    await db.set_booking_status(bid, db.STATUS_CONFIRMED); await db.add_visit(x["user_id"])
    schedule_booking_jobs(c.bot, {**x, "status": db.STATUS_CONFIRMED})
    try: await c.bot.send_message(x["user_id"], f"✅ Бронь #{bid} подтверждена")
    except TelegramAPIError: pass
    await log_action(c.from_user.id, f"booking #{bid} -> confirmed"); await screen_booking(c, bid)

@router.callback_query(F.data.startswith("adm:no:"), IsAdmin())
async def legacy_reject(c: CallbackQuery):
    bid = int(c.data.rsplit(":", 1)[1]); x = await db.get_booking(bid)
    if not x or x["status"] != db.STATUS_NEW:
        await c.answer("Заявка уже обработана", show_alert=True); return
    await db.set_booking_status(bid, db.STATUS_REJECTED); cancel_booking_jobs(bid)
    try: await c.bot.send_message(x["user_id"], f"❌ Бронь #{bid} отклонена")
    except TelegramAPIError: pass
    await log_action(c.from_user.id, f"booking #{bid} -> rejected"); await screen_booking(c, bid)

@router.callback_query(F.data.startswith("admin:booking_edit:"),IsAdmin())
async def booking_edit(c):
    bid=c.data.rsplit(':',1)[1]; await show(c,"Что меняем?",kb([[btn("📅 Дату",f"admin:edit:{bid}:date"),btn("🕐 Время",f"admin:edit:{bid}:time")],[btn("⏱ Длительность",f"admin:edit:{bid}:duration"),btn("🖥 ПК",f"admin:edit:{bid}:pcs")],back(f"admin:booking:{bid}")]))
@router.callback_query(F.data.startswith("admin:edit:"),IsAdmin())
async def edit_prompt(c,state):
    _,_,bid,field=c.data.split(':'); await state.set_state(AdminEditForm.booking_value); await state.update_data(bid=int(bid),field=field)
    hints={'date':'ГГГГ-ММ-ДД','time':'ЧЧ:ММ','duration':'число часов','pcs':'число ПК'}
    await show(c,f"Введите новое значение ({hints[field]}):",kb([back(f"admin:booking:{bid}")]))
@router.message(AdminEditForm.booking_value,IsAdmin())
async def edit_value(m,state):
    d=await state.get_data(); value=m.text.strip()
    try:
        if d['field']=='date': date.fromisoformat(value)
        elif d['field']=='time': datetime.strptime(value,'%H:%M')
        else:
            value=int(value)
            if value<1 or (d['field']=='pcs' and value>CLUB_PCS_TOTAL): raise ValueError
        # Не даём админу случайно устроить овербукинг
        cur=await db.get_booking(d['bid'])
        probe={**cur,d['field']:value}
        free=await db.pcs_free(probe['date'],probe['time'],int(probe['duration']),CLUB_PCS_TOTAL,exclude_booking_id=d['bid'])
        if int(probe['pcs'])>free:
            await m.answer(f"⚠️ На это время свободно только {free} ПК. Введите другое значение или нажмите «Назад»."); return
        await ad.update_booking(d['bid'],d['field'],value); x=await db.get_booking(d['bid']); await state.clear()
        try: await m.bot.send_message(x['user_id'],f"✏️ Бронь #{x['id']} изменена: {human_date(x['date'])}, {x['time']}, {x['duration']} ч., {x['pcs']} ПК")
        except TelegramAPIError: pass
        await log_action(m.from_user.id,f"edited booking #{x['id']} {d['field']}"); await m.answer("✅ Изменено",reply_markup=kb([[btn("К карточке",f"admin:booking:{x['id']}")]]))
    except (ValueError,TypeError): await m.answer("Некорректное значение. Попробуйте ещё раз или нажмите «Назад».")

async def message_prompt(c,state,kind,id):
    await state.set_state(AdminEditForm.booking_message if kind=='booking' else AdminEditForm.client_message); await state.update_data(target=id,kind=kind)
    await show(c,"Введите сообщение клиенту:",kb([back(f"admin:{kind}:{id}")]))
@router.callback_query(F.data.startswith("admin:booking_message:"),IsAdmin())
async def bm(c,state):
    bid=int(c.data.rsplit(':',1)[1]); x=await db.get_booking(bid); await message_prompt(c,state,'booking',x['user_id'])
@router.message(AdminEditForm.booking_message,IsAdmin())
@router.message(AdminEditForm.client_message,IsAdmin())
async def send_personal(m,state):
    d=await state.get_data()
    try: await m.bot.send_message(d['target'],m.text); result="✅ Отправлено"
    except TelegramAPIError: result="❌ Не удалось доставить"
    await state.clear(); await log_action(m.from_user.id,f"message client {d['target']}"); await m.answer(result,reply_markup=home_kb())

# Клиенты
async def screen_clients(c):
    total=len(await ad.list_clients()); await show(c,f"👥 <b>Клиенты ({total})</b>",kb([[btn("🔍 Поиск","admin:client_search"),btn("🌟 Постоянные","admin:client_list:regular:0")],[btn("👤 Все","admin:client_list:all:0"),btn("💤 Неактивные","admin:client_list:inactive:0")],[btn("🆕 Новые","admin:client_list:new:0")],back()]))
@router.callback_query(F.data=="admin:clients",IsAdmin())
async def clients(c): await screen_clients(c)
async def client_list(c,kind,page,query=""):
    items=await ad.list_clients(kind,query); rows=[]
    for x in items[page*PAGE:(page+1)*PAGE]: rows.append([btn(f"👤 {x.get('name') or x['user_id']} · {x.get('phone') or 'без телефона'}",f"admin:client:{x['user_id']}")])
    nav=[]
    if page: nav.append(btn("◀️",f"admin:client_list:{kind}:{page-1}"))
    if (page+1)*PAGE<len(items): nav.append(btn("▶️",f"admin:client_list:{kind}:{page+1}"))
    if nav: rows.append(nav)
    rows.append(back("admin:clients")); await show(c,f"👥 Найдено: {len(items)}",kb(rows))
@router.callback_query(F.data.startswith("admin:client_list:"),IsAdmin())
async def clist(c):
    _,_,kind,page=c.data.split(':'); await client_list(c,kind,int(page))
@router.callback_query(F.data=="admin:client_search",IsAdmin())
async def csearch(c,state):
    await state.set_state(AdminEditForm.client_search); await show(c,"Введите имя, телефон или @username:",kb([back("admin:clients")]))
@router.message(AdminEditForm.client_search,IsAdmin())
async def csearch_result(m,state):
    items=await ad.list_clients(query=m.text.strip()); await state.clear(); rows=[[btn(f"👤 {x.get('name') or x['user_id']}",f"admin:client:{x['user_id']}")] for x in items[:PAGE]]; rows.append(back("admin:clients")); await m.answer(f"🔍 Найдено: {len(items)}",reply_markup=kb(rows))
@router.message(Command("find"),IsAdmin())
async def find_cmd(m,state): await state.set_state(AdminEditForm.client_search); await m.answer("Введите имя, телефон или @username:",reply_markup=kb([back()]))
@router.callback_query(F.data.regexp(r"^admin:client:\d+$"),IsAdmin())
async def client_card(c):
    uid=int(c.data.rsplit(':',1)[1]); x=await db.get_client(uid)
    if not x: await c.answer("Клиент не найден",show_alert=True); return
    rating=await ad.client_rating(uid); username=f"@{x['username']}" if x.get('username') else "без username"
    text=f"👤 <b>{x.get('name') or 'Без имени'}</b> ({username})\n📞 {x.get('phone') or 'не указан'}\n🎮 {x['visits']} визитов\n📅 Последний: {x.get('last_visit') or 'нет'}\n⭐ Средняя оценка: {rating or 'нет'}"
    block="✅ Разблокировать" if x.get('blocked') else "🚫 Заблокировать"
    await show(c,text,kb([[btn("📩 Написать",f"admin:client_message:{uid}"),btn("📜 История броней",f"admin:client_history:{uid}:0")],[btn(block,f"admin:client_block:{uid}"),btn("🗑 Удалить клиента",f"admin:ask:client_del:{uid}")],back("admin:clients")]))
@router.callback_query(F.data.startswith("admin:client_message:"),IsAdmin())
async def cm(c,state): await message_prompt(c,state,'client',int(c.data.rsplit(':',1)[1]))
@router.callback_query(F.data.startswith("admin:client_block:"),IsAdmin())
async def block(c):
    uid=int(c.data.rsplit(':',1)[1]); x=await db.get_client(uid); await ad.set_blocked(uid,not bool(x.get('blocked'))); await log_action(c.from_user.id,f"client {uid} blocked={not bool(x.get('blocked'))}"); await client_card(c)
@router.callback_query(F.data.startswith("admin:client_history:"),IsAdmin())
async def history(c):
    _,_,_,uid,page=c.data.split(':'); items=await ad.client_bookings(int(uid)); page=int(page); lines=[f"#{x['id']} · {human_date(x['date'])} {x['time']} · {db.STATUS_LABELS.get(x['status'],x['status'])}" for x in items[page*PAGE:(page+1)*PAGE]]
    rows=[]
    if page: rows.append([btn("◀️",f"admin:client_history:{uid}:{page-1}")])
    if (page+1)*PAGE<len(items): rows.append([btn("▶️",f"admin:client_history:{uid}:{page+1}")])
    rows.append(back(f"admin:client:{uid}")); await show(c,"📜 <b>История броней</b>\n\n"+("\n".join(lines) or "Пусто"),kb(rows))

# Рассылка
@router.callback_query(F.data=="admin:broadcast",IsAdmin())
async def broadcast(c,state):
    await state.clear(); await show(c,"📢 <b>Рассылка</b>\n\nШаг 1 — кому:",kb([[btn("👥 Все","admin:bc_aud:all"),btn("🌟 Постоянные","admin:bc_aud:regular")],[btn("💤 Неактивные","admin:bc_aud:inactive"),btn("🆕 Новые","admin:bc_aud:new")],back()]))
@router.message(Command("broadcast"),IsAdmin())
async def bc_cmd(m,state): await state.clear(); await m.answer("📢 Выберите получателей",reply_markup=kb([[btn("Открыть рассылку","admin:broadcast")],back()]))
@router.callback_query(F.data.startswith("admin:bc_aud:"),IsAdmin())
async def bc_aud(c,state):
    audience=c.data.rsplit(':',1)[1]; await state.update_data(audience=audience); await show(c,"Шаг 2 — что отправляем?",kb([[btn("✏️ Текст",f"admin:bc_type:{audience}:text"),btn("🖼 Фото + текст",f"admin:bc_type:{audience}:photo")],back("admin:broadcast")]))
@router.callback_query(F.data.startswith("admin:bc_type:"),IsAdmin())
async def bc_type(c,state):
    _,_,audience,kind=c.data.split(':'); await state.set_state(BroadcastForm.content); await state.update_data(audience=audience,kind=kind); await show(c,"Отправьте текст:" if kind=='text' else "Отправьте фото с подписью:",kb([back(f"admin:bc_aud:{audience}")]))
@router.message(BroadcastForm.content,IsAdmin())
async def bc_content(m,state):
    d=await state.get_data(); photo=m.photo[-1].file_id if m.photo else None; text=(m.text or m.caption or '').strip()
    if (d['kind']=='photo' and not photo) or (not text and not photo): await m.answer("Нужен подходящий контент."); return
    ids=await ad.audience(d['audience']); await state.update_data(text=text,photo=photo); await state.set_state(BroadcastForm.confirm)
    preview = ("🖼 Фото\n" if photo else "") + text
    await m.answer(f"Вот что уйдёт ({len(ids)} получателей):\n\n{preview}",reply_markup=kb([[btn("▶️ Отправить","admin:bc_send"),btn("✏️ Изменить",f"admin:bc_type:{d['audience']}:{d['kind']}")],back(f"admin:bc_aud:{d['audience']}")]))
@router.callback_query(F.data=="admin:bc_send",IsAdmin())
async def bc_send(c,state):
    if await state.get_state()!=BroadcastForm.confirm.state: await c.answer("Сценарий устарел",show_alert=True); return
    d=await state.get_data(); ids=await ad.audience(d['audience']); _broadcast_cancelled.discard(c.from_user.id); await c.message.edit_text(f"Отправлено 0/{len(ids)}...",reply_markup=kb([[btn("⏹ Отменить","admin:bc_stop")]])); await c.answer()
    ok=fail=0
    for i,uid in enumerate(ids,1):
        if c.from_user.id in _broadcast_cancelled: break
        try:
            if d.get('photo'): await c.bot.send_photo(uid,d['photo'],caption=d.get('text') or None)
            else: await c.bot.send_message(uid,d['text'])
            ok+=1
        except TelegramAPIError: fail+=1
        if i%10==0 or i==len(ids):
            try: await c.message.edit_text(f"Отправлено {i}/{len(ids)}...",reply_markup=kb([[btn("⏹ Отменить","admin:bc_stop")]]))
            except TelegramAPIError: pass
        await asyncio.sleep(.04)
    stopped=c.from_user.id in _broadcast_cancelled; _broadcast_cancelled.discard(c.from_user.id); await state.clear(); await log_action(c.from_user.id,f"broadcast ok={ok} fail={fail} stopped={stopped}"); await c.message.edit_text(f"{'⏹ Остановлено' if stopped else '✅ Готово'}\nДоставлено: {ok}\nОшибок: {fail}",reply_markup=home_kb())
@router.callback_query(F.data=="admin:bc_stop",IsAdmin())
async def bc_stop(c): _broadcast_cancelled.add(c.from_user.id); await c.answer("Останавливаю…")

# Статистика
async def stats_screen(c,year,month):
    s=await db.get_month_stats(year,month); r=await db.get_reviews_summary(); new=await ad.new_clients_count(year,month)
    first=date(year,month,1); last=(date(year+1,1,1) if month==12 else date(year,month+1,1))-timedelta(days=1); loads=await db.get_week_load(first.isoformat(),last.day); cap=max(1,CLUB_PCS_TOTAL*12)
    daily=[0]*7; counts=[0]*7
    for i in range(last.day):
        d=first+timedelta(days=i); daily[d.weekday()]+=loads.get(d.isoformat(),{}).get('pc_hours',0); counts[d.weekday()]+=1
    bars='\n'.join(f"{WEEKDAYS[i]} {load_bar(round(100*daily[i]/max(1,cap*counts[i])))} {round(100*daily[i]/max(1,cap*counts[i]))}%" for i in range(7))
    text=f"📊 <b>Статистика за {MONTHS[month-1]} {year}</b>\n\n📥 Заявок: {s['total']}\n✅ Подтверждено: {s['confirmed']}\n❌ Отклонено: {s['rejected']}\n🚫 Неявок: {s['noshow']}\n👥 Новых клиентов: {new}\n⭐ Средняя оценка: {r['avg'] or 'нет'}\n\nЗагрузка по дням:\n{bars}"
    await show(c,text,kb([[btn("📅 Другой месяц","admin:stats_month"),btn("📤 Экспорт","admin:export")],back()]))
@router.callback_query(F.data=="admin:stats",IsAdmin())
async def stats(c): n=now(); await stats_screen(c,n.year,n.month)
@router.message(Command("stats"),IsAdmin())
async def stats_cmd(m): await m.answer("📊 Статистика",reply_markup=kb([[btn("Открыть","admin:stats")],back()]))
@router.callback_query(F.data=="admin:stats_month",IsAdmin())
async def month_pick(c):
    n=now(); rows=[]
    for k in range(0,12,3):
        row=[]
        for j in range(3):
            d=(n.replace(day=1)-timedelta(days=30*(k+j))).replace(day=1); row.append(btn(f"{MONTHS[d.month-1][:3]} {d.year}",f"admin:stats_at:{d.year}:{d.month}"))
        rows.append(row)
    rows.append(back("admin:stats")); await show(c,"Выберите месяц:",kb(rows))
@router.callback_query(F.data.startswith("admin:stats_at:"),IsAdmin())
async def stats_at(c): _,_,y,m=c.data.split(':'); await stats_screen(c,int(y),int(m))
@router.message(Command("week"),IsAdmin())
async def week(m):
    base=now().date(); data=await db.get_week_load(base.isoformat(),7); lines=["📈 <b>Загрузка на неделю</b>"]
    for i in range(7):
        d=base+timedelta(days=i); h=data.get(d.isoformat(),{}).get('pc_hours',0); p=round(100*h/max(1,CLUB_PCS_TOTAL*12)); lines.append(f"{WEEKDAYS[d.weekday()]} {d:%d.%m} {load_bar(p)} {p}%")
    await m.answer('\n'.join(lines),reply_markup=home_kb())

async def export_db(target):
    clients=await ad.list_clients(); out=io.StringIO(); w=csv.writer(out); w.writerow(['user_id','username','name','phone','visits','last_visit','blocked'])
    for x in clients: w.writerow([x.get(k,'') for k in ['user_id','username','name','phone','visits','last_visit','blocked']])
    await target.bot.send_document(target.from_user.id,BufferedInputFile(out.getvalue().encode('utf-8-sig'),filename='clients.csv'))
@router.callback_query(F.data=="admin:export",IsAdmin())
async def export(c): await export_db(c); await log_action(c.from_user.id,"database export"); await c.answer("Экспорт готов")

# Настройки
@router.callback_query(F.data=="admin:settings",IsAdmin())
async def settings(c):
    owner = roles.is_owner(c.from_user.id)
    rows=[[btn("💰 Тарифы","admin:tariffs"),btn("❓ FAQ","admin:faqs")],
          [btn("🕐 Режим работы","admin:hours"),btn("🤖 AI","admin:ai")],
          [btn("👮 Админы","admin:admins"),btn("📤 Экспорт базы","admin:export")],
          [btn("🔒 Пауза","admin:pause"),btn("📜 Журнал","admin:audit")]]
    if owner: rows.append([btn("🗄 Управление данными","admin:data")])
    rows.append(back())
    await show(c,"⚙️ <b>Настройки</b>",kb(rows))
@router.callback_query(F.data=="admin:tariffs",IsAdmin())
async def tariffs(c):
    ts=await ad.tariffs()
    rows=[[btn(f"{x['name']}: {x['price']}",f"admin:tariff_edit:{x['id']}"),btn("🗑",f"admin:ask:tariff_del:{x['id']}")] for x in ts]
    rows += [[btn("➕ Добавить тариф","admin:tariff_add")],back("admin:settings")]
    await show(c,"💰 <b>Тарифы</b>\n\nНажмите на тариф, чтобы изменить цену, или 🗑 — чтобы удалить.",kb(rows))
@router.callback_query(F.data.startswith("admin:tariff_edit:"),IsAdmin())
async def tariff_edit(c,state): await state.set_state(AdminEditForm.tariff_value); await state.update_data(tid=int(c.data.rsplit(':',1)[1])); await show(c,"Введите новую цену:",kb([back("admin:tariffs")]))
@router.message(AdminEditForm.tariff_value,IsAdmin())
async def tariff_value(m,state): d=await state.get_data(); await ad.update_tariff(d['tid'],m.text.strip()); await state.clear(); await log_action(m.from_user.id,"tariff edited"); await m.answer("✅ Сохранено",reply_markup=kb([[btn("К тарифам","admin:tariffs")]]))
@router.callback_query(F.data=="admin:tariff_add",IsAdmin())
async def tariff_add(c,state): await state.set_state(AdminEditForm.tariff_add); await show(c,"Введите «Название | Цена»:",kb([back("admin:tariffs")]))
@router.message(AdminEditForm.tariff_add,IsAdmin())
async def tariff_added(m,state):
    try: name,price=map(str.strip,m.text.split('|',1)); assert name and price
    except (ValueError,AssertionError): await m.answer("Формат: Название | Цена"); return
    await ad.add_tariff(name,price); await state.clear(); await m.answer("✅ Добавлено",reply_markup=kb([[btn("К тарифам","admin:tariffs")]]))
async def screen_faqs(c, page=0):
    fs=await ad.faqs(); page=max(0,min(page,max(0,(len(fs)-1)//PAGE)))
    rows=[[btn(x['question'][:44],f"admin:faq:{x['id']}"),btn("🗑",f"admin:ask:faq_del:{x['id']}")] for x in fs[page*PAGE:(page+1)*PAGE]]
    nav=[]
    if page: nav.append(btn("◀️",f"admin:faqs_page:{page-1}"))
    if (page+1)*PAGE<len(fs): nav.append(btn("▶️",f"admin:faqs_page:{page+1}"))
    if nav: rows.append(nav)
    rows += [[btn("➕ Добавить вопрос","admin:faq_add")],back("admin:settings")]
    await show(c,"❓ <b>FAQ</b>\n\nНажмите на вопрос, чтобы изменить ответ, или 🗑 — чтобы удалить вопрос.\nЭти же вопросы клиенты видят в разделе «❓ FAQ».",kb(rows))
@router.callback_query(F.data=="admin:faqs",IsAdmin())
async def faqs(c): await screen_faqs(c)
@router.callback_query(F.data.startswith("admin:faqs_page:"),IsAdmin())
async def faqs_page(c): await screen_faqs(c,int(c.data.rsplit(':',1)[1]))
@router.callback_query(F.data.startswith("admin:faq:"),IsAdmin())
async def faq_edit(c,state): await state.set_state(AdminEditForm.faq_answer); await state.update_data(fid=int(c.data.rsplit(':',1)[1])); await show(c,"Введите новый ответ:",kb([back("admin:faqs")]))
@router.message(AdminEditForm.faq_answer,IsAdmin())
async def faq_answer(m,state): d=await state.get_data(); await ad.update_faq(d['fid'],m.text.strip()); await state.clear(); await m.answer("✅ Ответ сохранён",reply_markup=kb([[btn("К FAQ","admin:faqs")]]))
@router.callback_query(F.data=="admin:faq_add",IsAdmin())
async def faq_add(c,state): await state.set_state(AdminEditForm.faq_add_question); await show(c,"Введите новый вопрос:",kb([back("admin:faqs")]))
@router.message(AdminEditForm.faq_add_question,IsAdmin())
async def faq_q(m,state): await state.update_data(question=m.text.strip()); await state.set_state(AdminEditForm.faq_add_answer); await m.answer("Теперь введите ответ:")
@router.message(AdminEditForm.faq_add_answer,IsAdmin())
async def faq_a(m,state): d=await state.get_data(); await ad.add_faq(d['question'],m.text.strip()); await state.clear(); await m.answer("✅ Добавлено",reply_markup=kb([[btn("К FAQ","admin:faqs")]]))
@router.callback_query(F.data=="admin:ai",IsAdmin())
async def ai(c):
    enabled=(await ad.setting('ai_enabled','1' if USE_AI else '0'))=='1'; await show(c,f"🤖 <b>AI</b>\n\nСтатус: {'🟢 Включён' if enabled else '🔴 Выключен'}\nМодель: {AI_MODEL}",kb([[btn("🔴 Выключить" if enabled else "🟢 Включить","admin:ai_toggle"),btn("⚙️ Модель","admin:ai_model")],back("admin:settings")]))
@router.callback_query(F.data=="admin:ai_toggle",IsAdmin())
async def ai_toggle(c): enabled=(await ad.setting('ai_enabled','1' if USE_AI else '0'))=='1'; await ad.set_setting('ai_enabled','0' if enabled else '1'); await log_action(c.from_user.id,"AI toggle"); await ai(c)
@router.callback_query(F.data=="admin:ai_model",IsAdmin())
async def ai_model(c): await c.answer(f"Модель задаётся конфигурацией: {AI_MODEL}",show_alert=True)
@router.callback_query(F.data=="admin:pause",IsAdmin())
async def pause(c):
    closed=await ad.club_closed(); reason=await ad.closed_reason()
    await show(c,f"🔒 <b>Пауза</b>\n\nКлуб: {'🔴 Закрыт' if closed else '🟢 Работает'}\n"
                 f"Причина для клиентов: {reason or 'не указана'}\n\n"
                 "На паузе бот не принимает новые брони и вежливо объясняет это клиентам.",
               kb([[btn("▶️ Открыть приём" if closed else "🔒 Закрыть клуб","admin:pause_toggle")],
                   [btn("✏️ Причина паузы","admin:pause_reason")],back("admin:settings")]))
@router.callback_query(F.data=="admin:pause_toggle",IsAdmin())
async def pause_toggle(c):
    closed=await ad.club_closed(); await ad.set_setting('club_closed','0' if closed else '1')
    await log_action(c.from_user.id,f"club closed={not closed}"); await pause(c)
@router.callback_query(F.data=="admin:pause_reason",IsAdmin())
async def pause_reason(c,state):
    await state.set_state(AdminEditForm.closed_reason)
    await show(c,"Введите причину паузы (её увидят клиенты), например «Технические работы до 18:00»:",kb([back("admin:pause")]))
@router.message(AdminEditForm.closed_reason,IsAdmin())
async def pause_reason_save(m,state):
    await ad.set_setting('closed_reason',(m.text or '').strip()[:200]); await state.clear()
    await log_action(m.from_user.id,"closed reason updated")
    await m.answer("✅ Сохранено",reply_markup=kb([[btn("К паузе","admin:pause")]]))
@router.callback_query(F.data=="admin:hours",IsAdmin())
async def hours(c):
    await show(c,f"🕐 <b>Режим работы</b>\n\nСейчас: {await ad.club_hours()}\n\nЭтот текст видят клиенты в разделе «📍 Адрес».",
               kb([[btn("✏️ Изменить","admin:hours_edit")],back("admin:settings")]))
@router.callback_query(F.data=="admin:hours_edit",IsAdmin())
async def hours_edit(c,state):
    await state.set_state(AdminEditForm.hours); await show(c,"Введите режим работы (например «круглосуточно» или «10:00–23:00»):",kb([back("admin:hours")]))
@router.message(AdminEditForm.hours,IsAdmin())
async def hours_save(m,state):
    value=(m.text or "").strip()
    if not 2<=len(value)<=100: await m.answer("Введите текст от 2 до 100 символов."); return
    await ad.set_setting("club_hours",value); await state.clear(); await log_action(m.from_user.id,f"hours -> {value}")
    await m.answer("✅ Сохранено",reply_markup=kb([[btn("К настройкам","admin:settings")]]))

HELP = (
    "❓ <b>Помощь</b>\n\n"
    "<b>Брони</b>\n"
    "• «📅 Сегодня» — список на день, карточка брони\n"
    "• Подтвердить, изменить, отметить неявку, отменить и <b>удалить</b> бронь\n\n"
    "<b>Клиенты</b>\n"
    "• Поиск, история, сообщение, блокировка и <b>удаление</b> клиента\n\n"
    "<b>Отзывы</b>\n"
    "• Просмотр, ответ клиенту и <b>удаление</b> отзыва\n\n"
    "<b>Настройки</b>\n"
    "• Тарифы и FAQ — добавить, изменить, <b>удалить</b>; клиенты видят их сразу\n"
    "• Режим работы, пауза клуба с причиной, тумблер AI\n"
    "• 👮 Админы — назначить и снять сотрудника прямо из панели\n"
    "• 📜 Журнал — кто и что делал\n"
    "• 🗄 Управление данными (владелец) — чистка демо-данных и базы\n\n"
    "Клиенты вас не видят: у сотрудников своя панель, у клиентов — своё меню."
)
@router.callback_query(F.data=="admin:help",IsAdmin())
async def help_cb(c): await show(c,HELP,kb([back()]))
@router.message(Command("help"),IsAdmin())
async def help_cmd(m): await m.answer(HELP,reply_markup=kb([back()]))


# ════════════════════════════════════════
# УДАЛЕНИЕ ДАННЫХ
#
# Раньше из панели нельзя было удалить ни бронь, ни клиента,
# ни «неудачный» вопрос FAQ. Теперь удаляется всё — но только
# через подтверждение, чтобы ничего не снести случайно.
# ════════════════════════════════════════

# action → (заголовок подтверждения, нужен ли статус владельца)
DELETE_ACTIONS = {
    "booking_del": ("🗑 Удалить бронь #{arg}?\n\nБронь исчезнет из статистики. Клиент получит уведомление.", False),
    "client_del":  ("🗑 Удалить клиента {arg}?\n\nВместе с ним удалятся ВСЕ его брони и отзывы.", False),
    "tariff_del":  ("🗑 Удалить тариф?\n\nОн пропадёт из раздела «💰 Цены» у клиентов.", False),
    "faq_del":     ("🗑 Удалить вопрос из FAQ?\n\nКлиенты больше не увидят его в меню.", False),
    "review_del":  ("🗑 Удалить отзыв #{arg}?\n\nОн перестанет учитываться в средней оценке.", False),
    "admin_del":   ("🗑 Снять права у сотрудника {arg}?\n\nОн потеряет доступ к панели.", True),
    "wipe_demo":   ("🧹 Удалить демо-данные?\n\nУдалятся вымышленные клиенты, их брони и отзывы.", True),
    "wipe_bookings": ("🧹 Удалить ВСЕ брони?\n\nДействие необратимо.", True),
    "wipe_reviews":  ("🧹 Удалить ВСЕ отзывы?\n\nДействие необратимо.", True),
    "wipe_clients":  ("🧹 Удалить ВСЕХ клиентов?\n\nВместе с бронями и отзывами. Необратимо.", True),
    "wipe_all":      ("💣 Очистить базу ПОЛНОСТЬЮ?\n\nКлиенты, брони и отзывы будут удалены. Необратимо.", True),
}


@router.callback_query(F.data.startswith("admin:ask:"), IsAdmin())
async def ask_confirm(c: CallbackQuery):
    """Экран подтверждения перед любым удалением."""
    parts = c.data.split(":")
    action, arg = parts[2], (parts[3] if len(parts) > 3 else "")
    meta = DELETE_ACTIONS.get(action)
    if not meta:
        await c.answer("Неизвестное действие", show_alert=True); return
    title, owner_only = meta
    if owner_only and not roles.is_owner(c.from_user.id):
        await c.answer("🔒 Действие доступно только владельцу клуба", show_alert=True); return
    await show(c, f"<b>Подтвердите действие</b>\n\n{title.format(arg=arg)}",
               kb([[btn("✅ Да, удалить", f"admin:do:{action}:{arg}"),
                    btn("↩️ Отмена", "admin:cancel_action")]]))


@router.callback_query(F.data == "admin:cancel_action", IsAdmin())
async def cancel_action(c: CallbackQuery, state: FSMContext):
    await c.answer("Отменено"); await screen_home(c, state)


@router.callback_query(F.data.startswith("admin:do:"), IsAdmin())
async def do_confirm(c: CallbackQuery, state: FSMContext):
    parts = c.data.split(":")
    action, arg = parts[2], (parts[3] if len(parts) > 3 else "")
    meta = DELETE_ACTIONS.get(action)
    if not meta:
        await c.answer("Неизвестное действие", show_alert=True); return
    if meta[1] and not roles.is_owner(c.from_user.id):
        await c.answer("🔒 Действие доступно только владельцу клуба", show_alert=True); return

    if action == "booking_del":
        bid = int(arg); booking = await db.get_booking(bid)
        if not booking:
            await c.answer("Бронь уже удалена", show_alert=True); await screen_today(c); return
        cancel_booking_jobs(bid)
        await db.delete_booking(bid)
        try: await c.bot.send_message(booking["user_id"], texts.BOOKING_DELETED)
        except TelegramAPIError: pass
        await log_action(c.from_user.id, f"deleted booking #{bid}")
        await show(c, f"✅ Бронь #{bid} удалена.", kb([[btn("📅 К броням", "admin:today:0")], back()]))

    elif action == "client_del":
        uid = int(arg); stats = await db.delete_client(uid)
        await log_action(c.from_user.id, f"deleted client {uid}")
        await show(c, f"✅ Клиент {uid} удалён.\nБроней: {stats['bookings']}, отзывов: {stats['reviews']}.",
                   kb([[btn("👥 К клиентам", "admin:clients")], back()]))

    elif action == "tariff_del":
        await db.delete_tariff(int(arg)); await log_action(c.from_user.id, f"deleted tariff {arg}")
        await c.answer("Тариф удалён"); await tariffs(c)

    elif action == "faq_del":
        await db.delete_faq(int(arg)); await log_action(c.from_user.id, f"deleted faq {arg}")
        await c.answer("Вопрос удалён"); await screen_faqs(c)

    elif action == "review_del":
        await db.delete_review(int(arg)); await log_action(c.from_user.id, f"deleted review {arg}")
        await c.answer("Отзыв удалён"); await screen_reviews(c)

    elif action == "admin_del":
        uid = int(arg)
        try: await roles.remove_admin(uid)
        except PermissionError as exc:
            await c.answer(f"🔒 {exc}", show_alert=True); return
        await apply_for_user(c.bot, uid, False)   # вернуть клиентское меню команд
        try: await c.bot.send_message(uid, "ℹ️ Права администратора клуба сняты. "
                                           "Отправьте /start, чтобы открыть клиентское меню.")
        except TelegramAPIError: pass
        await log_action(c.from_user.id, f"removed admin {uid}")
        await c.answer("Права сняты"); await screen_admins(c)

    else:  # массовые очистки
        what = action.removeprefix("wipe_")
        removed = await db.wipe(what)
        await log_action(c.from_user.id, f"wipe {what} ({removed} строк)")
        await show(c, f"🧹 Готово. Удалено записей: <b>{removed}</b>.",
                   kb([[btn("🗄 К данным", "admin:data")], back()]))


@router.callback_query(F.data == "admin:data", IsOwner())
async def data_screen(c: CallbackQuery):
    await show(c,
               "🗄 <b>Управление данными</b>\n\n"
               "Здесь можно почистить базу перед запуском клуба «вживую».\n"
               "Каждое действие требует подтверждения.",
               kb([[btn("🧹 Удалить демо-данные", "admin:ask:wipe_demo")],
                   [btn("📅 Удалить все брони", "admin:ask:wipe_bookings"),
                    btn("⭐ Удалить все отзывы", "admin:ask:wipe_reviews")],
                   [btn("👥 Удалить всех клиентов", "admin:ask:wipe_clients")],
                   [btn("💣 Очистить базу полностью", "admin:ask:wipe_all")],
                   [btn("📤 Сначала выгрузить CSV", "admin:export")],
                   back("admin:settings")]))


@router.callback_query(F.data == "admin:data", IsAdmin())
async def data_denied(c: CallbackQuery):
    await c.answer("🔒 Управление данными доступно только владельцу", show_alert=True)


# ════════════════════════════════════════
# ОТЗЫВЫ: просмотр и удаление
# ════════════════════════════════════════

async def screen_reviews(c: CallbackQuery, page: int = 0):
    items = await db.list_reviews()
    page = max(0, min(page, max(0, (len(items) - 1) // PAGE)))
    summary = await db.get_reviews_summary()
    rows = []
    for r in items[page * PAGE:(page + 1) * PAGE]:
        who = r.get("client_name") or r["user_id"]
        preview = (r.get("text") or "без комментария")[:28]
        rows.append([btn(f"{'⭐' * r['rating']} · {who}: {preview}", f"admin:review:{r['id']}"),
                     btn("🗑", f"admin:ask:review_del:{r['id']}")])
    nav = []
    if page: nav.append(btn("◀️", f"admin:reviews:{page - 1}"))
    if (page + 1) * PAGE < len(items): nav.append(btn("▶️", f"admin:reviews:{page + 1}"))
    if nav: rows.append(nav)
    rows.append(back())
    await show(c, f"⭐ <b>Отзывы</b>\n\nВсего: {summary['count']} · Средняя оценка: "
                  f"{summary['avg'] or 'нет'}\n\n" + ("Нажмите 🗑, чтобы удалить отзыв."
                                                      if items else "Отзывов пока нет."), kb(rows))


@router.callback_query(F.data.startswith("admin:reviews:"), IsAdmin())
async def reviews_cb(c: CallbackQuery):
    await screen_reviews(c, int(c.data.rsplit(":", 1)[1]))


@router.callback_query(F.data.startswith("admin:review:"), IsAdmin())
async def review_card(c: CallbackQuery):
    r = await db.get_review(int(c.data.rsplit(":", 1)[1]))
    if not r:
        await c.answer("Отзыв удалён", show_alert=True); await screen_reviews(c); return
    client = await db.get_client(r["user_id"]) or {}
    await show(c, f"⭐ <b>Отзыв #{r['id']}</b>\n\nОценка: {'⭐' * r['rating']} ({r['rating']}/5)\n"
                  f"Клиент: {client.get('name') or r['user_id']}\n"
                  f"Дата: {r['created_at'][:16].replace('T', ' ')}\n\n"
                  f"{r.get('text') or 'Без комментария'}",
               kb([[btn("📩 Ответить клиенту", f"admin:client_message:{r['user_id']}"),
                    btn("🗑 Удалить", f"admin:ask:review_del:{r['id']}")],
                   back("admin:reviews:0")]))


# ════════════════════════════════════════
# АДМИНИСТРАТОРЫ: назначение прямо из панели
#
# Раньше админа можно было задать только переменными ADMIN_ID/OWNER_ID
# и перезапуском бота. Теперь — кнопками, без перезапуска.
# ════════════════════════════════════════

async def screen_admins(c: CallbackQuery):
    items = await roles.list_admins()
    owner = roles.is_owner(c.from_user.id)
    lines, rows = [], []
    for a in items:
        uid = a["user_id"]
        title = a.get("name") or (f"@{a['username']}" if a.get("username") else str(uid))
        root = " · из конфига" if roles.is_root(uid) else ""
        lines.append(f"{roles.ROLE_LABELS.get(a['role'], a['role'])} — <code>{uid}</code> {title}{root}")
        if owner and not roles.is_root(uid):
            rows.append([btn(f"⚙️ {title}", f"admin:admin_card:{uid}")])
    if owner:
        rows.append([btn("➕ Добавить администратора", "admin:admin_add")])
    rows.append(back("admin:settings"))
    hint = ("\n\nВладелец назначает и снимает сотрудников кнопками ниже."
            if owner else "\n\nИзменять список может только владелец.")
    await show(c, "👮 <b>Сотрудники клуба</b>\n\n" + "\n".join(lines) + hint, kb(rows))


@router.callback_query(F.data == "admin:admins", IsAdmin())
async def admins_screen(c: CallbackQuery):
    await screen_admins(c)


@router.callback_query(F.data.startswith("admin:admin_card:"), IsOwner())
async def admin_card(c: CallbackQuery):
    uid = int(c.data.rsplit(":", 1)[1])
    role = roles.role_of(uid)
    if not role:
        await c.answer("Сотрудник уже удалён", show_alert=True); await screen_admins(c); return
    other = roles.ROLE_ADMIN if role == roles.ROLE_OWNER else roles.ROLE_OWNER
    await show(c, f"👮 <b>Сотрудник {uid}</b>\n\nРоль: {roles.ROLE_LABELS[role]}",
               kb([[btn(f"🔁 Сделать: {roles.ROLE_LABELS[other]}", f"admin:admin_role:{uid}:{other}")],
                   [btn("🗑 Снять права", f"admin:ask:admin_del:{uid}")],
                   back("admin:admins")]))


@router.callback_query(F.data.startswith("admin:admin_role:"), IsOwner())
async def admin_role(c: CallbackQuery):
    _, _, uid, role = c.data.split(":")
    try: await roles.set_role(int(uid), role)
    except (ValueError, PermissionError) as exc:
        await c.answer(f"🔒 {exc}", show_alert=True); return
    await log_action(c.from_user.id, f"admin {uid} role -> {role}")
    await c.answer("Роль обновлена"); await screen_admins(c)


@router.callback_query(F.data == "admin:admin_add", IsOwner())
async def admin_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminEditForm.admin_add)
    await show(c, "➕ <b>Новый сотрудник</b>\n\nПришлите его Telegram ID (число), "
                  "или @username клиента, который уже писал боту, "
                  "или просто перешлите сюда любое его сообщение.\n\n"
                  "Свой ID можно узнать у бота @userinfobot.",
               kb([back("admin:admins")]))


@router.message(AdminEditForm.admin_add, IsAdmin())
async def admin_add_value(m: Message, state: FSMContext):
    if not roles.is_owner(m.from_user.id):
        await state.clear(); await m.answer("🔒 Только владелец может назначать сотрудников."); return

    uid = username = name = None
    if m.forward_from:                      # переслали сообщение человека
        uid, username, name = m.forward_from.id, m.forward_from.username, m.forward_from.full_name
    elif m.forward_date:                    # пересылка скрыта настройками приватности
        await m.answer("У этого пользователя скрыт профиль при пересылке. "
                       "Попросите его ID (@userinfobot) и пришлите числом."); return
    else:
        raw = (m.text or "").strip()
        if raw.isdigit():
            uid = int(raw)
        elif raw.startswith("@"):
            found = await ad.list_clients(query=raw)
            match = [x for x in found if (x.get("username") or "").lower() == raw[1:].lower()]
            if not match:
                await m.answer("Не нашёл такого клиента в базе. Он должен хотя бы раз написать боту — "
                               "или пришлите числовой ID."); return
            uid, username, name = match[0]["user_id"], match[0].get("username"), match[0].get("name")
        else:
            await m.answer("Нужен числовой ID, @username или пересланное сообщение."); return

    if uid == m.from_user.id:
        await m.answer("Вы уже владелец 🙂"); return
    if roles.is_admin(uid):
        await m.answer("Этот пользователь уже сотрудник клуба."); return

    client = await db.get_client(uid)
    await roles.add_admin(uid, roles.ROLE_ADMIN, added_by=m.from_user.id,
                          username=username or (client or {}).get("username"),
                          name=name or (client or {}).get("name"))
    await state.clear()
    await apply_for_user(m.bot, uid, True)        # сразу выдать админское меню команд
    await log_action(m.from_user.id, f"added admin {uid}")
    try:
        await m.bot.send_message(uid, "👮 Вам выданы права администратора клуба.\n"
                                      "Отправьте /start, чтобы открыть панель.")
    except TelegramAPIError:
        pass
    await m.answer(f"✅ Пользователь <code>{uid}</code> назначен администратором.",
                   reply_markup=kb([[btn("👮 К списку", "admin:admins")]]))


# ════════════════════════════════════════
# ЖУРНАЛ ДЕЙСТВИЙ
# ════════════════════════════════════════

@router.callback_query(F.data == "admin:audit", IsAdmin())
async def audit(c: CallbackQuery):
    rows = await db.last_audit(20)
    lines = [f"{r['created_at'][5:16].replace('T', ' ')} · {r['user_id']} · {r['action']}"
             for r in rows]
    await show(c, "📜 <b>Журнал действий</b>\n\n" + ("\n".join(lines) or "Пока пусто."),
               kb([[btn("🔄 Обновить", "admin:audit")], back("admin:settings")]))


# ════════════════════════════════════════
# Разделение ролей: админ не лезет в клиентское меню
# ════════════════════════════════════════

@router.callback_query(F.data.regexp(r"^(m|b|f|rev|my):"), IsAdmin())
async def client_area_for_admin(c: CallbackQuery, state: FSMContext):
    """Сотрудник нажал кнопку из клиентского меню — возвращаем его в панель."""
    await c.answer("Это клиентское меню. Вы вошли как сотрудник клуба.", show_alert=True)
    await screen_home(c, state)


@router.message(IsAdmin())
async def admin_fallback(m: Message, state: FSMContext):
    """Любой прочий текст от админа: панель, а не клиентский фолбэк."""
    await screen_home(m, state)
