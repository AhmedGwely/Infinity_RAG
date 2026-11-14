"""Regression: credentials encrypted with a DIFFERENT APP_SECRET_KEY must never cause an HTTP 500.

Before the fix, providers.effective()/save() raised ValueError on a wrong key, so chat, the Providers page, the System page
and even saving a replacement key all failed with 500. Now: chat -> 409 with an actionable message, settings stay readable,
the old record is preserved until the admin saves a new key, and saving a new key replaces it.
DB-backed tests need a migrated Postgres (DATABASE_URL) and are skipped otherwise."""
import os
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from infinity import keytool
from infinity.crypto import SecretBox, SecretUnreadableError, key_fingerprint

KEY_OLD = "old-key-" + "A" * 40
KEY_NEW = "new-key-" + "B" * 40

try:
    from sqlalchemy import create_engine, text
    _url = os.environ.get("DATABASE_URL", "")
    if not _url:
        raise RuntimeError
    with create_engine(_url).connect() as _c:
        _c.execute(text("select 1 from app_settings limit 1"))
    DB_OK = True
except Exception:
    DB_OK = False


class CryptoTest(unittest.TestCase):
    def test_wrong_key_raises_specific_actionable_error(self):
        token = SecretBox(KEY_OLD).encrypt_json({"gemini": "k"})
        with self.assertRaises(SecretUnreadableError) as ctx:
            SecretBox(KEY_NEW).decrypt_json(token)
        self.assertIsInstance(ctx.exception, ValueError)  # backwards compatible
        self.assertIn("Re-enter", str(ctx.exception))
        self.assertFalse(SecretBox(KEY_NEW).can_decrypt(token))
        self.assertTrue(SecretBox(KEY_OLD).can_decrypt(token))

    def test_fingerprint_is_stable_and_short(self):
        self.assertEqual(key_fingerprint(KEY_OLD), key_fingerprint(KEY_OLD))
        self.assertEqual(len(key_fingerprint(KEY_OLD)), 12)
        self.assertNotIn(KEY_OLD, key_fingerprint(KEY_OLD))

    def test_keytool_finds_key_despite_whitespace_quotes_and_cr(self):
        records = [("llm (AI provider keys)", SecretBox(KEY_OLD).encrypt_json({"gemini": "k"}))]
        candidates = [("garbage", "x" * 30), ("quoted+crlf", f'"{KEY_OLD}"\r'), ("padded", f"  {KEY_OLD}  "), ("current", KEY_NEW)]
        res = keytool.test_candidates(candidates, records)
        matched = {r["label"] for r in res if all(r["ok"].values())}
        self.assertEqual(matched, {"quoted+crlf", "padded"})


@unittest.skipUnless(DB_OK, "migrated Postgres (DATABASE_URL) not available")
class UnreadableCredentialsFlowTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from infinity import main, providers
        import infinity.routers.chat as chat
        from infinity.config import get_config
        from infinity.crypto import hash_password, token_hash
        from infinity.db import session_scope
        from infinity.models import AppSetting, Chunk, Document, User, UserSession

        class FakeQ:
            chunk = str(uuid.uuid4())
            def search(self, v, n): return [(self.chunk, 0.9)]
        class FakeE:
            def embed_query(self, t): return [0.0] * 1024
        self.q = FakeQ()
        for name, value in (("qdrant", lambda: self.q), ("embedder", lambda: FakeE())):
            patcher = mock.patch.object(chat, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # the app runs with KEY_NEW, no .env fallback keys; the stored record was encrypted with KEY_OLD
        cfg = replace(get_config(), secret_key=KEY_NEW, gemini_api_key=None, openai_api_key=None, llm_provider="gemini")
        self.patch = mock.patch.object(providers, "get_config", return_value=cfg)
        self.patch.start()
        self.addCleanup(self.patch.stop)

        self.old_token = SecretBox(KEY_OLD).encrypt_json({"gemini": "old-gemini-key"})
        token = "t" + uuid.uuid4().hex
        with session_scope() as db:
            db.merge(AppSetting(key="llm", value={"provider": "gemini", "models": {"gemini": "some-model"}}, secret_enc=self.old_token))
            user = User(email=f"{uuid.uuid4().hex[:8]}@test.io", name="T", password_hash=hash_password("Passw0rd!long"), role="admin")
            db.add(user); db.flush()
            db.add(UserSession(id=token_hash(token), user_id=user.id, expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
            doc = Document(title="Manual", doc_type="pdf", visibility="internal", status="ready"); db.add(doc); db.flush()
            db.add(Chunk(id=self.q.chunk, document_id=doc.id, ord=0, text="Accuracy Class 0.2S.", locator={"type": "pdf", "pdf_pages": [3]}))
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.client.cookies.set("infinity_session", token)
        self.hdr = {"X-Requested-With": "infinity"}
        self.providers = providers

    def _stored(self):
        from infinity.db import session_scope
        from infinity.models import AppSetting
        with session_scope() as db:
            return db.get(AppSetting, "llm").secret_enc

    def test_chat_returns_clear_409_not_500(self):
        r = self.client.post("/api/chat", json={"question": "what is the accuracy class?"}, headers=self.hdr)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("APP_SECRET_KEY", r.json()["detail"])
        self.assertNotIn("old-gemini-key", r.text)

    def test_settings_page_stays_readable_and_flags_problem(self):
        r = self.client.get("/api/admin/settings/llm")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["credentials_unreadable"])
        self.assertFalse(body["keys"]["gemini"]["configured"])  # never pretend the old key exists
        self.assertIn("re-enter", body["message"].lower())

    def test_connection_test_reports_instead_of_crashing(self):
        r = self.client.post("/api/admin/settings/llm/test", json={"provider": "gemini"}, headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])
        self.assertEqual(r.json()["code"], "credentials")

    def test_non_secret_change_preserves_the_old_record(self):
        r = self.client.put("/api/admin/settings/llm", json={"models": {"gemini": "another-model"}}, headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored(), self.old_token)  # byte-identical
        self.assertTrue(r.json()["credentials_unreadable"])

    def test_saving_a_new_key_replaces_the_unreadable_value(self):
        r = self.client.put("/api/admin/settings/llm", json={"keys": {"gemini": "brand-new-key-1234"}}, headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["credentials_unreadable"])
        self.assertTrue(body["keys"]["gemini"]["configured"])
        self.assertNotIn("brand-new-key-1234", r.text)  # preview is masked
        self.assertEqual(SecretBox(KEY_NEW).decrypt_json(self._stored())["gemini"], "brand-new-key-1234")
        # and chat no longer reports a credentials problem (it may fail later at the real provider, but not with 409/500 here)
        with mock.patch.object(self.providers, "create_llm_provider", return_value=type("L", (), {"complete_with_instructions": lambda s, p, system_instruction: "## Verified from sources\nClass 0.2S [S1]."})()):
            r2 = self.client.post("/api/chat", json={"question": "what is the accuracy class?"}, headers=self.hdr)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["message"]["citations"][0]["id"], "S1")


if __name__ == "__main__":
    unittest.main()
