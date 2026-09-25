"""
Смоук-тесты без Telegram: проверяем то, что чинили —
роли, удаление данных, защиту от овербукинга, живой контент.

Запуск:  python -m tests.test_smoke
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ADMIN_ID"] = "1001"
os.environ["OWNER_ID"] = "1001"
os.environ["SEED_DEMO_DATA"] = "1"

import app.admin_data as ad      # noqa: E402
import app.database as db        # noqa: E402
import app.roles as roles        # noqa: E402
from app.config import CLUB_PCS_TOTAL  # noqa: E402
from app.utils import now        # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(("✅" if condition else "❌") + " " + name)
    if not condition:
        failures.append(name)


async def main() -> None:
    await db.init_db()
    await roles.init()

    # ── роли ───────────────────────────────────────────────
    check("корневой владелец из конфига — владелец", roles.is_owner(1001))
    check("посторонний — не админ", not roles.is_admin(2002))

    await roles.add_admin(2002, roles.ROLE_ADMIN, added_by=1001)
    check("админ назначен из панели, без перезапуска", roles.is_admin(2002))
    check("назначенный админ не владелец", not roles.is_owner(2002))

    await roles.set_role(2002, roles.ROLE_OWNER)
    check("роль повышается до владельца", roles.is_owner(2002))

    await roles.remove_admin(2002)
    check("права снимаются из панели", not roles.is_admin(2002))

    try:
        await roles.remove_admin(1001)
        root_protected = False
    except PermissionError:
        root_protected = True
    check("корневого владельца удалить нельзя", root_protected)

    try:
        await roles.set_role(1001, roles.ROLE_ADMIN)
        root_fixed = False
    except PermissionError:
        root_fixed = True
    check("роль корневого сотрудника фиксирована конфигом", root_fixed)

    # ── овербукинг ─────────────────────────────────────────
    # Отдельный день, чтобы демо-брони не влияли на расчёт
    from datetime import timedelta
    today = (now().date() + timedelta(days=5)).isoformat()
    await db.create_booking(5001, today, "19:00", 2, CLUB_PCS_TOTAL)
    free = await db.pcs_free(today, "20:00", 1, CLUB_PCS_TOTAL)
    check("пересекающиеся брони занимают ПК", free == 0)
    free_later = await db.pcs_free(today, "23:00", 1, CLUB_PCS_TOTAL)
    check("непересекающееся время свободно", free_later == CLUB_PCS_TOTAL)

    # ── удаление данных ────────────────────────────────────
    bid = await db.create_booking(5002, today, "10:00", 1, 1)
    await db.delete_booking(bid)
    check("бронь удаляется", await db.get_booking(bid) is None)

    await db.add_review(5002, 5, "супер")
    reviews = await db.list_reviews()
    await db.delete_review(reviews[0]["id"])
    check("отзыв удаляется", await db.get_review(reviews[0]["id"]) is None)

    await db.upsert_client(5003, "user", "Тест")
    await db.create_booking(5003, today, "11:00", 1, 1)
    await db.add_review(5003, 4)
    stats = await db.delete_client(5003)
    check("клиент удаляется вместе с бронями и отзывами",
          await db.get_client(5003) is None and stats["bookings"] == 1 and stats["reviews"] == 1)

    tariffs = await ad.tariffs()
    await db.delete_tariff(tariffs[0]["id"])
    check("тариф удаляется", len(await ad.tariffs()) == len(tariffs) - 1)

    faqs = await ad.faqs()
    await db.delete_faq(faqs[0]["id"])
    check("вопрос FAQ удаляется", len(await ad.faqs()) == len(faqs) - 1)

    await db.wipe("demo")
    check("демо-данные вычищаются одной кнопкой", await db.get_client(900000001) is None)

    # ── живой контент и настройки ──────────────────────────
    await ad.add_tariff("🔥 Турнир", "1500 ₽")
    check("новый тариф виден клиенту в «Ценах»", "Турнир" in await ad.tariffs_text())

    await ad.set_setting("club_closed", "1")
    check("пауза клуба включается", await ad.club_closed())
    await ad.set_setting("club_closed", "0")

    await ad.set_setting("club_hours", "10:00–23:00")
    check("режим работы редактируется из панели", await ad.club_hours() == "10:00–23:00")

    await ad.set_setting("ai_enabled", "0")
    check("AI выключается тумблером", not await ad.ai_enabled())

    await db.log_audit(1001, "тест")
    check("журнал действий пишется", (await db.last_audit(1))[0]["action"] == "тест")

    await db.wipe("all")
    check("полная очистка базы работает", not await db.list_reviews() and not await db.get_client(5001))

    print("\n" + ("ВСЁ ПРОШЛО ✅" if not failures else f"ПРОВАЛЕНО: {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
