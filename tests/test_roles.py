"""
Проверка ролей из конфига: ADMIN_ID — админ, OWNER_ID — владелец.

Раньше обе переменные получали роль владельца, и сотрудник из ADMIN_ID
отображался в панели как «👑 Владелец». Проверяем новые правила,
миграцию старых баз и защиту корневых сотрудников.

Запуск:  python -m tests.test_roles
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "roles.db")
os.environ["ADMIN_ID"] = "2002"          # корневой АДМИНИСТРАТОР
os.environ["OWNER_ID"] = "1001"          # корневой ВЛАДЕЛЕЦ
os.environ["SEED_DEMO_DATA"] = "0"

import aiosqlite                      # noqa: E402

import app.database as db             # noqa: E402
import app.roles as roles             # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(("✅" if condition else "❌") + " " + name)
    if not condition:
        failures.append(name)


async def scenario_main() -> None:
    await db.init_db()

    # База «со старой версии»: ADMIN_ID ошибочно записан владельцем
    async with aiosqlite.connect(os.environ["DB_PATH"]) as conn:
        await conn.execute(
            "INSERT INTO admins (user_id, role, name, added_by, created_at)"
            " VALUES (2002, 'owner', 'из конфига', 0, '2024-01-01T00:00:00')")
        await conn.commit()

    await roles.init()
    check("OWNER_ID из конфига — владелец", roles.is_owner(1001))
    check("ADMIN_ID из конфига — АДМИНИСТРАТОР, не владелец",
          roles.is_admin(2002) and not roles.is_owner(2002))
    check("старая запись «ADMIN_ID = владелец» исправилась при старте",
          roles.role_of(2002) == roles.ROLE_ADMIN)
    check("в приветствии панели — «👮 Администратор»",
          roles.ROLE_LABELS[roles.role_of(2002)] == "👮 Администратор")

    # Корневых нельзя ни удалить, ни перевести в другую роль из панели
    for uid, new_role in ((1001, roles.ROLE_ADMIN), (2002, roles.ROLE_OWNER)):
        try:
            await roles.remove_admin(uid); removed = True
        except PermissionError:
            removed = False
        check(f"корневого {uid} удалить нельзя", not removed)
        try:
            await roles.set_role(uid, new_role); switched = True
        except PermissionError:
            switched = False
        check(f"роль корневого {uid} фиксирована конфигом", not switched)

    # Обычных сотрудников владелец управляет из панели свободно
    await roles.add_admin(3003, roles.ROLE_ADMIN, added_by=1001)
    check("назначенный из панели — админ, не владелец",
          roles.is_admin(3003) and not roles.is_owner(3003))
    await roles.set_role(3003, roles.ROLE_OWNER)
    check("назначенного можно повысить до владельца", roles.is_owner(3003))
    await roles.remove_admin(3003)
    check("назначенного можно снять", not roles.is_admin(3003))


def scenario_subprocess(admin_id: str, owner_id: str, code: str) -> bool:
    """Мини-сценарий в отдельном процессе со своим набором переменных."""
    env = {**os.environ,
           "DB_PATH": os.path.join(tempfile.mkdtemp(), "roles.db"),
           "ADMIN_ID": admin_id, "OWNER_ID": owner_id, "SEED_DEMO_DATA": "0"}
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       cwd=str(Path(__file__).resolve().parent.parent),
                       capture_output=True, text=True)
    if r.returncode != 0:
        print((r.stderr or "ошибка без вывода").strip().splitlines()[-1])
    return r.returncode == 0


def main() -> None:
    asyncio.run(scenario_main())

    check("ADMIN_ID и OWNER_ID совпадают — роль владельца",
          scenario_subprocess("1001", "1001", """
import asyncio
import app.database as db, app.roles as roles
async def m():
    await db.init_db(); await roles.init()
    assert roles.is_owner(1001), "один и тот же ID должен быть владельцем"
asyncio.run(m())
"""))

    check("OWNER_ID не заполнен — ADMIN_ID подстраховывает владельцем",
          scenario_subprocess("1001", "123456789", """
import asyncio
import aiosqlite
import app.database as db, app.roles as roles
from app.config import DB_PATH
async def m():
    await db.init_db()
    async with aiosqlite.connect(DB_PATH) as conn:   # фантом со старых версий
        await conn.execute("INSERT INTO admins (user_id, role, name, added_by,"
                           " created_at) VALUES (123456789, 'owner', 'из конфига',"
                           " 0, '2024-01-01T00:00:00')")
        await conn.commit()
    await roles.init()
    assert roles.is_owner(1001), "без OWNER_ID владельцем становится ADMIN_ID"
    assert not roles.is_admin(123456789), "фантомная запись о заглушке убрана"
asyncio.run(m())
"""))

    print("\n" + ("ВСЁ ПРОШЛО ✅" if not failures else f"ПРОВАЛЕНО: {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
