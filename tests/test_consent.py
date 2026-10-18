"""Регрессии согласия: python -m unittest tests.test_consent -v."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from app import database as db, keyboards, texts
from app.handlers import client
from app.middlewares import ClientAreaMiddleware


class ConsentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        with patch.object(db, "SEED_DEMO_DATA", False):
            await db.init_db()
        self.cb = SimpleNamespace(
            from_user=SimpleNamespace(id=701, username="test", full_name="Test"),
            data="c:agree:v2", answer=AsyncMock(),
            message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
        )
        self.state = SimpleNamespace(clear=AsyncMock())

    async def test_policy_is_available_before_agreement(self):
        with patch.object(keyboards, "PRIVACY_POLICY_URL", "https://example.com/policy"):
            buttons = [b for row in keyboards.consent_kb().inline_keyboard for b in row]
        self.assertTrue(any(b.url == "https://example.com/policy" for b in buttons))
        self.assertTrue(any(b.text == "✅ Я согласен" and b.callback_data == "c:agree:v2" for b in buttons))
        self.assertFalse(await db.has_consent(701))
        self.assertIsNone(await db.get_client(701))

    async def test_no_policy_disables_agreement_even_from_old_message(self):
        with patch.object(keyboards, "PRIVACY_POLICY_URL", ""), patch.object(keyboards, "DEMO_MODE", False):
            buttons = [b for row in keyboards.consent_kb().inline_keyboard for b in row]
        self.assertFalse(any(b.callback_data == "c:agree:v2" for b in buttons))
        with patch.object(client, "PRIVACY_POLICY_URL", ""), patch.object(client, "DEMO_MODE", False):
            await client.consent_agree(self.cb, self.state)
        self.assertFalse(await db.has_consent(701))
        self.assertIsNone(await db.get_client(701))
        self.cb.message.edit_text.assert_not_awaited()

    async def test_demo_allows_agreement_without_policy(self):
        with patch.object(keyboards, "PRIVACY_POLICY_URL", ""), patch.object(keyboards, "DEMO_MODE", True):
            buttons = [b for row in keyboards.consent_kb().inline_keyboard for b in row]
        self.assertTrue(any(b.callback_data == "c:agree:v2" for b in buttons))
        self.assertFalse(any(b.url for b in buttons))
        with patch.object(texts, "PRIVACY_POLICY_URL", ""), patch.object(texts, "DEMO_MODE", True):
            self.assertIn("вымышленные данные", texts.consent_text())
        with patch.object(client, "PRIVACY_POLICY_URL", ""), patch.object(client, "DEMO_MODE", True):
            await client.consent_agree(self.cb, self.state)
        self.assertTrue(await db.has_consent(701))
        self.assertIsNone((await db.consent_info(701))["policy_url"])

    async def test_production_without_policy_shows_unavailable(self):
        with patch.object(texts, "PRIVACY_POLICY_URL", ""), patch.object(texts, "DEMO_MODE", False):
            self.assertEqual(texts.consent_text(), texts.CONSENT_UNAVAILABLE)

    async def test_agree_revoke_and_agree_again(self):
        with patch.object(client, "PRIVACY_POLICY_URL", "https://example.com/policy"):
            await client.consent_agree(self.cb, self.state)
        record = await db.consent_info(701)
        self.assertTrue(record["created_at"])
        self.assertEqual(record["policy_url"], "https://example.com/policy")
        self.assertTrue(await db.has_consent(701))
        await db.set_client_phone(701, "+79990000000")
        await client.consent_revoke(self.cb, self.state)
        self.assertIsNone((await db.get_client(701))["phone"])
        self.assertFalse(await db.has_consent(701))
        self.assertTrue((await db.consent_info(701))["revoked_at"])
        self.state.clear.assert_awaited()
        handler = AsyncMock()
        self.cb.data = "m:book"
        with patch("app.middlewares.roles.is_admin", return_value=False):
            await ClientAreaMiddleware()(handler, self.cb, {"event_from_user": self.cb.from_user})
        handler.assert_not_awaited()
        # Повторный отзыв безопасен; новое согласие не восстанавливает телефон.
        await db.revoke_consent(701)
        await db.grant_consent(701, "2.0", "https://example.com/policy")
        self.assertTrue(await db.has_consent(701))
        self.assertIsNone((await db.get_client(701))["phone"])
        async with aiosqlite.connect(db.DB_PATH) as conn:
            rows = await (await conn.execute("SELECT revoked_at FROM consents ORDER BY id")).fetchall()
        self.assertIsNotNone(rows[0][0])
        self.assertIsNone(rows[1][0])

    async def test_blocked_client_can_revoke(self):
        self.cb.data = "c:revoke"
        handler = AsyncMock()
        with patch("app.middlewares.roles.is_admin", return_value=False), patch.object(db, "is_blocked", AsyncMock(return_value=True)):
            await ClientAreaMiddleware()(handler, self.cb, {"event_from_user": self.cb.from_user})
        handler.assert_awaited_once()

    async def test_legacy_start_button_does_not_bypass_gate(self):
        self.cb.data = "c:agree"
        handler = AsyncMock()
        with patch("app.middlewares.roles.is_admin", return_value=False):
            await ClientAreaMiddleware()(handler, self.cb, {"event_from_user": self.cb.from_user})
        handler.assert_not_awaited()

    async def test_legacy_database_migration_keeps_history(self):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("DROP TABLE consents")
            await conn.execute("CREATE TABLE consents (id INTEGER PRIMARY KEY, user_id INTEGER, version TEXT, policy_url TEXT, created_at TEXT)")
            await conn.execute("INSERT INTO consents VALUES (1, 701, '1.0', NULL, '2025-01-01')")
            await conn.commit()
        with patch.object(db, "SEED_DEMO_DATA", False):
            await db.init_db()
            await db.init_db()
        self.assertTrue(await db.has_consent(701))
        await db.revoke_consent(701)
        self.assertEqual((await db.consent_info(701))["created_at"], "2025-01-01")
        self.assertFalse(await db.has_consent(701))


if __name__ == "__main__":
    unittest.main()
