"""Вспомогательные функции: даты, время, форматирование."""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from config import TIMEZONE

TZ = ZoneInfo(TIMEZONE)

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def now() -> datetime:
    """Текущее время в часовом поясе клуба."""
    return datetime.now(TZ)


def today() -> date:
    return now().date()


def iso_days_ahead(days: int) -> str:
    """Дата через N дней от сегодня в формате ГГГГ-ММ-ДД."""
    return (today() + timedelta(days=days)).isoformat()


def booking_start_end(date_str: str, time_str: str, duration: int) -> tuple[datetime, datetime]:
    """Начало и конец брони как aware-datetime в поясе клуба."""
    start = datetime.combine(
        date.fromisoformat(date_str),
        dtime.fromisoformat(time_str),
        tzinfo=TZ,
    )
    return start, start + timedelta(hours=duration)


def human_date(date_str: str) -> str:
    """'2026-09-25' → 'Пт 25.09 · завтра'"""
    d = date.fromisoformat(date_str)
    result = f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"
    if d == today():
        result += " · сегодня"
    elif d == today() + timedelta(days=1):
        result += " · завтра"
    return result


def day_word(date_str: str) -> str:
    """'сегодня' / 'завтра' / '26.09' — для текста напоминания."""
    d = date.fromisoformat(date_str)
    if d == today():
        return "сегодня"
    if d == today() + timedelta(days=1):
        return "завтра"
    return d.strftime("%d.%m")


def end_time_str(time_str: str, duration: int) -> str:
    """'22:00', 4 → '02:00' (время окончания сеанса)."""
    _, end = booking_start_end(today().isoformat(), time_str, duration)
    return end.strftime("%H:%M")


def load_bar(percent: int, width: int = 10) -> str:
    """Текстовый индикатор загрузки: [████░░░░░░]."""
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"
