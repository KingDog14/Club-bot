"""
AI-модуль бота — ТОЛЬКО GigaChat (Сбер). Других провайдеров нет и не будет.

Одна дешёвая модель из config.AI_MODEL (по умолчанию GigaChat-2 / Lite)
для обеих задач:
  parse_booking(text)  — разбор заявки свободным текстом
  answer_faq(question) — ответ на вопрос строго по базе знаний

Авторизация: в AI_API_KEY просто вставляется «Ключ авторизации»
(Authorization key) из Сбер Studio — ничего кодировать в Base64 вручную
не нужно, Studio формирует ключ сама.

Обе функции БЕЗОПАСНЫ: при любой ошибке (нет ключа, нет сети, истёк/невалидный
токен, превышен лимит) пишут причину в лог и возвращают None / заглушку —
бот продолжает работать на кнопках, пользователь технических ошибок не видит.

Токен доступа GigaChat живёт 30 минут — SDK обновляет его автоматически.
Freemium (GIGACHAT_API_PERS): один поток запросов — вызовы сериализуются
threading.Lock'ом, сверху хендлеры запускают их через asyncio.to_thread.

SDK: pip install gigachat · https://developers.sber.ru/docs/ru/gigachat
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import timedelta

from config import (
    AI_API_KEY,
    AI_MODEL,
    AI_SCOPE,
    AI_TIMEOUT,
    AI_VERIFY_SSL,
    CLUB_ADDRESS,
    CLUB_HOURS,
    CLUB_NAME,
    CLUB_PHONE,
    FAQ_BASE,
    TARIFFS,
)
from utils import now

logger = logging.getLogger(__name__)

# Импорт защищён: если пакет gigachat не установлен, модуль всё равно
# загрузится, а бот продолжит работать на кнопках.
try:
    from gigachat import GigaChat
    from gigachat.exceptions import AuthenticationError, ResponseError
    from gigachat.models import Chat, Messages, MessagesRole

    _SDK_OK = True
except ImportError:  # pragma: no cover - зависит от окружения
    GigaChat = Chat = Messages = MessagesRole = None

    class AuthenticationError(Exception):  # заглушки, чтобы except не упал
        pass

    class ResponseError(Exception):
        pass

    _SDK_OK = False
    logger.warning("Пакет gigachat не установлен — AI отключён. Установите: pip install gigachat")

# Что отвечать пользователю, если AI недоступен (без технических деталей!)
AI_UNAVAILABLE = (
    "🤖 AI-ассистент временно недоступен, используйте кнопки меню. "
    f"Или позвоните нам: {CLUB_PHONE}"
)

_client: "GigaChat | None" = None  # один клиент на всё приложение
_lock = threading.Lock()           # freemium = один поток запросов (последовательно)


def _get_client() -> "GigaChat":
    """Ленивое создание клиента GigaChat — один на одну модель для всех задач."""
    global _client
    if not _SDK_OK:
        raise RuntimeError("Пакет gigachat не установлен")
    if _client is None:
        _client = GigaChat(
            credentials=AI_API_KEY,          # ключ авторизации из Сбер Studio — как он есть
            scope=AI_SCOPE,                  # GIGACHAT_API_PERS — физлицам, freemium
            model=AI_MODEL,                  # одна дешёвая модель (GigaChat-2 / Lite)
            verify_ssl_certs=AI_VERIFY_SSL,  # False — SDK сам подставит сертификаты Минцифры
            timeout=AI_TIMEOUT,
        )
    return _client


def _ask(system: str, user: str) -> str | None:
    """
    Единый безопасный вызов GigaChat (строго по образцу из документации SDK —
    через Chat/Messages/MessagesRole).
    Возвращает текст ответа или None при любой ошибке (подробности — только в лог).
    """
    payload = Chat(
        messages=[
            Messages(role=MessagesRole.SYSTEM, content=system),
            Messages(role=MessagesRole.USER, content=user),
        ],
        temperature=0.1,  # минимум творчества: парсинг и ответы должны быть точными
    )
    try:
        with _lock:
            response = _get_client().chat(payload)
        return response.choices[0].message.content
    except AuthenticationError as exc:
        # Чаще всего: ключ скопирован не полностью или взят не из того проекта
        logger.error(
            "GigaChat: ошибка авторизации (проверьте AI_API_KEY — это «Ключ авторизации» "
            "из Сбер Studio, копируется целиком — и AI_SCOPE): %s", exc,
        )
    except ResponseError as exc:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status == 429:
            # Лимит freemium исчерпан или слишком частые запросы — работаем на кнопках
            logger.warning("GigaChat: превышен лимит токенов/запросов — переключаемся на кнопки")
        else:
            logger.error("GigaChat вернул ошибку API (%s): %s", status, exc)
    except Exception:
        # Сеть, SSL, таймаут и т.п. — истёкший токен SDK обновляет сам
        logger.exception("GigaChat: непредвиденная ошибка")
    return None


# ──────────────────────────────
# 1) Парсинг заявки свободным текстом
# ──────────────────────────────

_PARSE_SYSTEM = """Ты — парсер заявок на бронирование компьютерного клуба.
Из сообщения клиента извлеки поля и верни СТРОГО один JSON-объект без пояснений и без markdown:
{{"date": ..., "time": ..., "duration": ..., "pcs": ...}}

Правила:
- date — "сегодня", "завтра", "послезавтра" или дата "ГГГГ-ММ-ДД"
  (относительные даты и дни недели пересчитай от сегодняшней даты: {today}).
- time — точное время "ЧЧ:ММ" (24-часовой формат) ИЛИ период одним словом:
  "утро", "день", "вечер", "ночь". «Вечером» → "вечер", «утром» → "утро" и т.д.
- duration — длительность в часах, целое число.
- pcs — количество ПК/игроков, целое число («нас двое» → 2).
- Значения не найденных полей — null.
- Если сообщение не про бронирование (приветствие, вопрос о ценах и т.п.) — верни слово null.
"""


def parse_booking(text: str) -> dict | None:
    """
    Разобрать заявку клиента в структуру {date, time, duration, pcs}.

    «хочу завтра вечером часа на 3, нас двое» →
    {"date": "завтра", "time": "вечер", "duration": 3, "pcs": 2}

    Возвращает None, если разобрать не удалось или AI недоступен —
    тогда хендлер предлагает оформить заявку кнопками.
    """
    today = now().date()
    today_str = f"{today.isoformat()} ({today + timedelta(days=1)} — завтра)"
    raw = _ask(_PARSE_SYSTEM.format(today=today_str), text)
    if not raw:
        return None

    raw = raw.strip()
    # На всякий случай срезаем обёртки ```json ... ```
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    if raw.lower() == "null":
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Пробуем вытащить JSON-объект из произвольного текста ответа
        match = re.search(r"\{[^{}]*\}", raw, re.S)
        if not match:
            logger.warning("GigaChat parse_booking: в ответе нет JSON: %r", raw)
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("GigaChat parse_booking: битый JSON: %r", raw)
            return None

    if not isinstance(data, dict):
        return None

    # Нормализация и лёгкая валидация полей
    result: dict = {}

    date_val = str(data.get("date") or "").strip().lower()
    if date_val:
        result["date"] = date_val

    time_val = str(data.get("time") or "").strip().lower()
    if time_val:
        result["time"] = time_val

    for key, limit in (("duration", 12), ("pcs", 10)):
        value = data.get(key)
        try:
            ivalue = int(value) if value is not None else None
        except (TypeError, ValueError):
            ivalue = None
        if ivalue and 1 <= ivalue <= limit:
            result[key] = ivalue

    if not result:
        return None
    logger.info("GigaChat распознал заявку: %s (из %r)", result, text)
    return result


# ──────────────────────────────
# 2) Ответы на вопросы строго по базе знаний
# ──────────────────────────────

_FAQ_SYSTEM = """Ты — ассистент компьютерного клуба {club}.
Отвечай только по базе знаний. Не выдумывай цены, наличие и правила.
Если ответа нет в базе знаний — ответь ровно одной фразой:
«Уточните у администратора: {phone}»
Стиль: коротко, дружелюбно, по-русски, можно 1–2 эмодзи. Без markdown-таблиц.

База знаний:
{knowledge}
"""


def _knowledge_base(faq_base: dict) -> str:
    """
    База знаний для GigaChat: факты о клубе из config.py + FAQ.
    Всё это — данные владельца, а не фантазии модели.
    """
    lines = [
        f"Клуб: {CLUB_NAME}",
        f"Адрес: {CLUB_ADDRESS}",
        f"Режим работы: {CLUB_HOURS}",
        f"Телефон: {CLUB_PHONE}",
        "Тарифы: " + "; ".join(f"{name} — {price}" for name, price in TARIFFS),
        "Частые вопросы и ответы:",
    ]
    lines += [f"- {item['question']} {item['answer']}" for item in faq_base.values()]
    return "\n".join(lines)


def answer_faq(question: str, faq_base: dict | None = None) -> str:
    """
    Ответить на вопрос клиента строго по faq_base (по умолчанию — FAQ_BASE
    из config.py).

    Вопрос не по теме → модель отвечает «Уточните у администратора: {телефон}».
    AI недоступен → дружелюбная заглушка AI_UNAVAILABLE.
    """
    if faq_base is None:
        faq_base = FAQ_BASE
    system = _FAQ_SYSTEM.format(
        club=CLUB_NAME,
        phone=CLUB_PHONE,
        knowledge=_knowledge_base(faq_base),
    )
    return _ask(system, question) or AI_UNAVAILABLE
