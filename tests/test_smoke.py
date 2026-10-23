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
    await ad.tariffs()  # инициализировать стандартные демо-тарифы до добавления зоны
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

    # ── овербукинг ─────────────────────────────────────────
    # Отдельный день, чтобы демо-брони не влияли на расчёт
    from datetime import timedelta
    today = (now().date() + timedelta(days=5)).isoformat()
    pending_id = await db.create_booking(5001, today, "19:00", 2, CLUB_PCS_TOTAL)
    check("заявка не резервирует ПК до подтверждения", await db.pcs_free(today, "20:00", 1, CLUB_PCS_TOTAL) == CLUB_PCS_TOTAL)
    check("админ может атомарно подтвердить доступную заявку",
          await db.confirm_booking(pending_id, CLUB_PCS_TOTAL))
    free = await db.pcs_free(today, "20:00", 1, CLUB_PCS_TOTAL)
    check("пересекающиеся брони занимают ПК", free == 0)
    free_later = await db.pcs_free(today, "23:00", 1, CLUB_PCS_TOTAL)
    check("непересекающееся время свободно", free_later == CLUB_PCS_TOTAL)

    # ── модульный каталог ПК/зон ────────────────────────────
    from app.zones_module import add_zone
    from app.pcs_module import add_pc, parse_pc_import
    from app.config_loader import reload_config, get_available_pcs_for_time
    catalog_day = (now().date() + timedelta(days=6)).isoformat()
    zone_id = await add_zone("test-vip", "Тест VIP", 250, color="🔵")
    pc34 = await add_pc("34", zone_id, "RTX 4090")
    pc35 = await add_pc("35", zone_id, "RTX 4090")
    pc36 = await add_pc("36", zone_id, "RTX 4090")
    await reload_config()
    available = await get_available_pcs_for_time(catalog_day, "18:00", 2, zone_id)
    check("каталог отдаёт активные ПК зоны", {pc["id"] for pc in available} == {pc34, pc35, pc36})
    catalog_booking = await db.create_booking(5010, catalog_day, "18:00", 2, 1,
                                              zone_id=zone_id, pc_id=pc34, pc_preference="specific")
    check("админ может назначить ПК при подтверждении",
          await db.confirm_booking(catalog_booking, CLUB_PCS_TOTAL, pc34))
    conflict_booking = await db.create_booking(5011, catalog_day, "19:00", 1, 1,
                                               zone_id=zone_id, pc_id=pc34, pc_preference="specific")
    check("тот же ПК нельзя подтвердить на пересекающееся время",
          not await db.confirm_booking(conflict_booking, CLUB_PCS_TOTAL, pc34))
    import aiosqlite
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("DELETE FROM booking_units WHERE booking_id=?", (catalog_booking,))
        await conn.commit()
    await db.init_db()
    from app.booking_units import units_for_booking
    migrated_units = await units_for_booking(catalog_booking)
    check("legacy assigned_pc_id переносится в booking_units без потери",
          migrated_units[0]["unit_id"] == pc34 and migrated_units[0]["status"] == "assigned")
    check("импорт распознаёт строки CSV", len(parse_pc_import("36,test-vip,RTX 4090\n37,test-vip,RTX 4090")) == 2)

    from app.booking_units import confirm_all, units_for_booking
    group_booking = await db.create_booking(5012, (now().date() + timedelta(days=8)).isoformat(),
                                            "18:00", 2, 3, zone_id=zone_id, pc_preference="any")
    group_rows = await units_for_booking(group_booking)
    check("групповая бронь создаёт одну заявку и три места", len(group_rows) == 3)
    ok, assigned, total = await confirm_all(group_booking)
    group_rows = await units_for_booking(group_booking)
    check("автоподтверждение назначает все три ПК", ok and assigned == total == 3
          and all(row["status"] == "assigned" for row in group_rows))

    pc37 = await add_pc("37", zone_id, "RTX 4090")
    import json
    from app.booking_units import add_pending_units
    from app.followups import (create_request, approve_add_unit_request,
                               approve_extension, set_request_status, cancel_booking)
    addon = await create_request(group_booking, 5012, "add_unit", 1, json.dumps([pc37]))
    await add_pending_units(group_booking, [pc37], addon)
    check("follow-up добавляет место к существующей группе после одобрения",
          await approve_add_unit_request(addon))
    extension = await create_request(group_booking, 5012, "extend", 30)
    extended, updated = await approve_extension(extension)
    check("продление на 30 минут точно сохраняет дробную длительность",
          extended and updated["duration_minutes"] == 150)
    late = await create_request(group_booking, 5012, "late", 20)
    check("уведомление об опоздании закрывается без изменения брони",
          await set_request_status(late, "approved") and (await db.get_booking(group_booking))["duration_minutes"] == 150)
    check("клиент может отменить активную бронь вместе со всеми местами",
          await cancel_booking(group_booking, 5012) and (await db.get_booking(group_booking))["status"] == db.STATUS_CANCELLED)
    from app.intent_parser import parse_followup
    check("follow-up парсер понимает опоздание, продление и добавление ПК",
          parse_followup("Опоздаю на 20 минут")["params"]["minutes"] == 20
          and parse_followup("Продли на 1 час")["params"]["minutes"] == 60
          and parse_followup("добавь ещё ПК 35")["params"]["unit_number"] == "35")

    # ── персональные данные: согласие (152-ФЗ) ─────────────
    check("демо-клиенты помечены как давшие согласие", await db.has_consent(900000001))
    check("новый пользователь — без согласия", not await db.has_consent(6001))
    await db.grant_consent(6001, "1.0", "https://example.com/policy")
    check("нажатие «Я согласен» фиксируется", await db.has_consent(6001))
    info = await db.consent_info(6001)
    check("версия и ссылка на политику сохраняются",
          bool(info) and info["version"] == "1.0"
          and info["policy_url"] == "https://example.com/policy")
    await db.grant_consent(6001, "1.1")
    check("повторное согласие пишется в историю",
          (await db.consent_info(6001))["version"] == "1.1")

    # ── удаление данных ────────────────────────────────────
    bid = await db.create_booking(5002, today, "10:00", 1, 1)
    await db.delete_booking(bid)
    check("бронь удаляется", await db.get_booking(bid) is None)

    await db.add_review(5002, 5, "супер")
    reviews = await db.list_reviews()
    await db.delete_review(reviews[0]["id"])
    check("отзыв удаляется", await db.get_review(reviews[0]["id"]) is None)

    await db.upsert_client(5003, "user", "Тест")
    await db.grant_consent(5003, "1.0")
    await db.create_booking(5003, today, "11:00", 1, 1)
    await db.add_review(5003, 4)
    stats = await db.delete_client(5003)
    check("клиент удаляется вместе с бронями, отзывами и согласием",
          await db.get_client(5003) is None and stats["bookings"] == 1
          and stats["reviews"] == 1 and stats["consents"] == 1
          and not await db.has_consent(5003))

    tariffs = await ad.tariffs()
    await db.delete_tariff(tariffs[0]["id"])
    check("тариф удаляется", len(await ad.tariffs()) == len(tariffs) - 1)

    faqs = await ad.faqs()
    await db.delete_faq(faqs[0]["id"])
    check("вопрос FAQ удаляется", len(await ad.faqs()) == len(faqs) - 1)

    await db.wipe("demo")
    check("демо-данные вычищаются одной кнопкой",
          await db.get_client(900000001) is None and not await db.has_consent(900000001))

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
    check("полная очистка базы работает",
          not await db.list_reviews() and not await db.get_client(5001)
          and not await db.has_consent(6001))

    print("\n" + ("ВСЁ ПРОШЛО ✅" if not failures else f"ПРОВАЛЕНО: {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
