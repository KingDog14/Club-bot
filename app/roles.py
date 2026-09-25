"""
Роли и права доступа.

Раньше админы задавались ТОЛЬКО переменными ADMIN_ID / OWNER_ID.
Теперь список админов живёт в базе (таблица `admins`) и управляется
прямо из панели: «⚙️ Настройки → 👮 Админы».

Роли:
  owner — владелец: всё, включая управление админами и удаление данных;
  admin — администратор: брони, клиенты, рассылки, контент.

ADMIN_ID и OWNER_ID из конфига остаются «корневыми» владельцами:
они автоматически добавляются в базу при старте и не могут быть удалены —
иначе можно было бы случайно потерять доступ к панели.
"""
from __future__ import annotations

import logging

import aiosqlite

from app.config import ADMIN_ID, DB_PATH, OWNER_ID
from app.utils import now

logger = logging.getLogger(__name__)

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_LABELS = {ROLE_OWNER: "👑 Владелец", ROLE_ADMIN: "👮 Администратор"}

# Корневые владельцы из конфига — удалить их из панели нельзя
ROOT_IDS: set[int] = {i for i in (ADMIN_ID, OWNER_ID) if i and i > 0}

# Кэш «user_id → роль», чтобы не ходить в базу на каждое сообщение
_cache: dict[int, str] = {}


# ──────────────────────────────
# Загрузка и кэш
# ──────────────────────────────

async def refresh() -> dict[int, str]:
    """Перечитать список админов из базы в кэш."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT user_id, role FROM admins")).fetchall()
    _cache.clear()
    _cache.update({int(r["user_id"]): r["role"] for r in rows})
    return dict(_cache)


async def init() -> None:
    """Создать корневых владельцев (из конфига) и прогреть кэш."""
    async with aiosqlite.connect(DB_PATH) as conn:
        for uid in ROOT_IDS:
            await conn.execute(
                """
                INSERT INTO admins (user_id, role, name, added_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET role = 'owner'
                """,
                (uid, ROLE_OWNER, "из конфига", 0, now().isoformat()),
            )
        await conn.commit()
    await refresh()
    logger.info("Админы: %s", ", ".join(f"{k}:{v}" for k, v in sorted(_cache.items())) or "нет")


# ──────────────────────────────
# Проверки (синхронные — работают по кэшу)
# ──────────────────────────────

def role_of(user_id: int | None) -> str | None:
    return _cache.get(int(user_id)) if user_id else None


def is_admin(user_id: int | None) -> bool:
    """Любой сотрудник клуба: администратор или владелец."""
    return role_of(user_id) is not None


def is_owner(user_id: int | None) -> bool:
    return role_of(user_id) == ROLE_OWNER


def is_root(user_id: int | None) -> bool:
    """Владелец из конфига — его нельзя удалить или понизить."""
    return int(user_id) in ROOT_IDS if user_id else False


def admin_ids() -> list[int]:
    return sorted(_cache)


def owner_ids() -> list[int]:
    return sorted(uid for uid, role in _cache.items() if role == ROLE_OWNER)


# ──────────────────────────────
# Управление из панели
# ──────────────────────────────

async def list_admins() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT * FROM admins ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, user_id"
        )).fetchall()
    return [dict(r) for r in rows]


async def add_admin(user_id: int, role: str, *, added_by: int,
                    username: str | None = None, name: str | None = None) -> None:
    if role not in (ROLE_OWNER, ROLE_ADMIN):
        raise ValueError("Неизвестная роль")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO admins (user_id, role, username, name, added_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                role = excluded.role,
                username = COALESCE(excluded.username, admins.username),
                name = COALESCE(excluded.name, admins.name)
            """,
            (user_id, role, username, name, added_by, now().isoformat()),
        )
        await conn.commit()
    await refresh()


async def set_role(user_id: int, role: str) -> None:
    if is_root(user_id) and role != ROLE_OWNER:
        raise PermissionError("Владельца из конфига нельзя понизить")
    await add_admin(user_id, role, added_by=0)


async def remove_admin(user_id: int) -> None:
    if is_root(user_id):
        raise PermissionError("Владельца из конфига нельзя удалить")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await conn.commit()
    await refresh()
