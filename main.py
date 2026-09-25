#!/usr/bin/env python3
"""
Точка входа бота компьютерного клуба (ДЕМО-ШАБЛОН).

Запуск:  python main.py

Весь код — в пакете app/: настройки в app/config.py (переопределяются
через .env / переменные окружения), тексты — в app/texts.py.
"""
import asyncio

from app.runner import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Корректное завершение по Ctrl+C; ошибки (SystemExit и т.п.)
        # не глушим — супервизору важен ненулевой код выхода
        pass
