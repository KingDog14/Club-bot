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
    """Пошаговая рассылка админом."""

    content = State()    # ожидание текста или фото с подписью
    confirm = State()    # подтверждение отправки


class AdminEditForm(StatesGroup):
    """Ввод нового значения при редактировании админом."""

    booking_value = State()
    client_search = State()
    client_message = State()
    booking_message = State()
    tariff_value = State()
    tariff_add = State()
    faq_answer = State()
    faq_add_question = State()
    faq_add_answer = State()
    month = State()
    hours = State()           # режим работы клуба
    closed_reason = State()   # причина паузы (видят клиенты)
    admin_add = State()       # добавление администратора (ID или @username)
