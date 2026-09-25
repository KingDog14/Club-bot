"""FSM-состояния (машина состояний aiogram)."""
from aiogram.fsm.state import State, StatesGroup


class BookingForm(StatesGroup):
    """Пошаговое бронирование."""

    mode = State()       # выбор способа: кнопки или AI-текст
    ai_text = State()    # ожидание свободного текста заявки (путь Б)
    date = State()       # дата
    time = State()       # время
    duration = State()   # длительность
    pcs = State()        # количество ПК
    name = State()       # имя клиента
    phone = State()      # телефон (можно пропустить)
    confirm = State()    # финальная проверка


class FaqForm(StatesGroup):
    """Свободный вопрос к AI в разделе FAQ."""

    question = State()


class ReviewForm(StatesGroup):
    """Комментарий после низкой оценки (1–3)."""

    comment = State()


class BroadcastForm(StatesGroup):
    """Рассылка админом."""

    text = State()       # ожидание сообщения для рассылки
    confirm = State()    # подтверждение отправки
