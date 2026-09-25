"""
Проверка разделения ролей на уровне middleware:
админ не попадает в клиентское меню, клиент — в панель.

Запуск:  python -m tests.test_access
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass, field

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "access.db")
os.environ["ADMIN_ID"] = "1001"
os.environ["OWNER_ID"] = "1001"
os.environ["SEED_DEMO_DATA"] = "0"

import app.database as db          # noqa: E402
import app.roles as roles          # noqa: E402
from app.middlewares import AdminAreaMiddleware, ClientAreaMiddleware  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(("✅" if condition else "❌") + " " + name)
    if not condition:
        failures.append(name)


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeMessage:
    from_user: FakeUser
    replies: list = field(default_factory=list)

    async def answer(self, text, **kw):
        self.replies.append(text)


@dataclass
class FakeCallback:
    from_user: FakeUser
    data: str = "m:book"
    alerts: list = field(default_factory=list)

    async def answer(self, text="", show_alert=False, **kw):
        self.alerts.append(text)


async def run(middleware, event):
    """Вернёт 'passed', если middleware пропустил апдейт к хендлеру."""
    marker = []

    async def handler(e, d):
        marker.append("passed")
        return "passed"

    await middleware(handler, event, {"event_from_user": event.from_user})
    return bool(marker)


async def main() -> None:
    await db.init_db()
    await roles.init()
    await roles.add_admin(2002, roles.ROLE_ADMIN, added_by=1001)
    await db.upsert_client(3003, "client", "Клиент")
    await db.upsert_client(4004, "banned", "Забаненный")
    import app.admin_data as ad
    await ad.set_blocked(4004, True)

    client_mw, admin_mw = ClientAreaMiddleware(), AdminAreaMiddleware()

    # Клиентская зона
    cb_admin = FakeCallback(FakeUser(2002))
    check("админ НЕ попадает в клиентское меню", not await run(client_mw, cb_admin))
    check("админу объясняют, почему", "сотрудник" in (cb_admin.alerts[0] if cb_admin.alerts else ""))
    check("владелец НЕ попадает в клиентское меню",
          not await run(client_mw, FakeCallback(FakeUser(1001))))
    check("клиент попадает в клиентское меню",
          await run(client_mw, FakeCallback(FakeUser(3003))))
    blocked = FakeMessage(FakeUser(4004))
    check("заблокированный клиент не проходит", not await run(client_mw, blocked))
    check("заблокированному отвечают текстом", bool(blocked.replies))

    # Админская зона
    check("админ проходит в панель", await run(admin_mw, FakeCallback(FakeUser(2002), "admin:home")))
    cb_client = FakeCallback(FakeUser(3003), "admin:data")
    check("клиент НЕ проходит в панель", not await run(admin_mw, cb_client))
    check("клиенту показывают отказ", "только администраторам" in (cb_client.alerts[0] if cb_client.alerts else ""))
    check("клиентский апдейт проходит сквозь админский роутер",
          await run(admin_mw, FakeCallback(FakeUser(3003), "m:book")))

    print("\n" + ("ВСЁ ПРОШЛО ✅" if not failures else f"ПРОВАЛЕНО: {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
