"""
Пакет приложения — бот компьютерного клуба (ДЕМО-ШАБЛОН).

Структура:
    runner.py    — сборка и запуск бота (вызывается из ../main.py)
    config.py    — все настройки (переопределяются через .env / окружение)
    texts.py     — все тексты бота
    states.py    — FSM-состояния диалогов
    utils.py     — даты/время/форматирование
    database.py  — SQLite (aiosqlite): таблицы + демо-данные
    keyboards.py — все клавиатуры
    ai_helper.py — AI-модуль: ТОЛЬКО GigaChat
    scheduler.py — напоминания и запросы отзывов (APScheduler)
    handlers/    — обработчики: client.py, admin.py, common.py
"""

__version__ = "1.0.0"
