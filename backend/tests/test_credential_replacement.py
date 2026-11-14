"""Explicit provider-key replacement when the stored credential can no longer be decrypted (APP_SECRET_KEY changed).

The new key is validated with the provider (stubbed here: no network, no real key), encrypted with the CURRENT APP_SECRET_KEY,
and stored in one transaction without ever decrypting the old value. DB-backed tests are skipped without a migrated Postgres."""
import logging
import os
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx

from infinity import providers
from infinity.crypto import SecretBox

KEY_OLD = "old-key-" + "A" * 40      # the APP_SECRET_KEY that is lost
KEY_NEW = "new-key-" + "B" * 40      # the APP_SECRET_KEY the app runs with now
NEW_API_KEY = "AIzaSy-brand-new-gemini-key-9876"
OLD_API_KEY = "AIzaSy-old-unrecoverable-key-1234"

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


class ValidateKeyTest(unittest.TestCase):
    """validate_key maps the provider's answer without needing (or leaking) any other key."""

    def _call(self, provider, status=None, body=None, exc=None, model="gemini-x"):
        side = {"side_effect": exc} if exc else {"return_value": (status, body or {})}
        with mock.patch.object(providers, "_http_get", **side) as m:
            try:
                return providers.validate_key(provider, NEW_API_KEY, model), m
            except Exception as error:  # noqa: BLE001
                return error, m

    def test_gemini_valid_sends_key_in_header_not_url(self):
        res, m = self._call("gemini", 200)
        self.assertEqual(res, {"status": "valid", "model_warning": None})
        url, headers = m.call_args.args[0], m.call_args.args[1]
        self.assertNotIn(NEW_API_KEY, url)
        self.assertEqual(headers["x-goog-api-key"], NEW_API_KEY)
        self.assertIn("/models/gemini-x", url)

    def test_gemini_unknown_model_is_a_warning_not_a_rejection(self):
        res, _ = self._call("gemini", 404)
        self.assertEqual(res["status"], "valid")
        self.assertIn("gemini-x", res["model_warning"])

    def test_gemini_invalid_key_is_rejected_without_echoing_it(self):
        res, _ = self._call("gemini", 400, {"error": {"message": "API key not valid. Please pass a valid API key."}})
        self.assertIsInstance(res, providers.CredentialRejected)
        self.assertNotIn(NEW_API_KEY, str(res))

    def test_server_error_and_network_failure_mean_unavailable(self):
        self.assertIsInstance(self._call("gemini", 503)[0], providers.ValidationUnavailable)
        self.assertIsInstance(self._call("gemini", exc=httpx.ConnectError("boom"))[0], providers.ValidationUnavailable)

    def test_openai(self):
        self.assertEqual(self._call("openai", 200)[0]["status"], "valid")
        self.assertIsInstance(self._call("openai", 401)[0], providers.CredentialRejected)


@unittest.skipUnless(DB_OK, "migrated Postgres (DATABASE_URL) not available")
class ReplaceCredentialFlowTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from infinity import main
        import infinity.routers.chat as chat
        from infinity.config import get_config
        from infinity.crypto import hash_password, token_hash
        from infinity.db import session_scope
        from infinity.models import AppSetting, AuditLog, Chunk, Document, Source, User, UserSession

        class FakeQ:
            chunk = str(uuid.uuid4())
            def search(self, v, n): return [(self.chunk, 0.9)]
        class FakeE:
            def embed_query(self, t): return [0.0] * 1024
        self.q = FakeQ()
        for name, value in (("qdrant", lambda: self.q), ("embedder", lambda: FakeE())):
            patcher = mock.patch.object(chat, name, value)
            patcher.start(); self.addCleanup(patcher.stop)

        cfg = replace(get_config(), secret_key=KEY_NEW, gemini_api_key=None, openai_api_key=None, llm_provider="gemini")
        patcher = mock.patch.object(providers, "get_config", return_value=cfg)
        patcher.start(); self.addCleanup(patcher.stop)

        self.old_blob = SecretBox(KEY_OLD).encrypt_json({"gemini": OLD_API_KEY, "openai": "sk-old-openai"})
        self.source_blob = SecretBox(KEY_OLD).encrypt_json({"token": "source-secret"})
        self.src_name = "src-" + uuid.uuid4().hex[:8]
        token = "t" + uuid.uuid4().hex
        with session_scope() as db:
            db.merge(AppSetting(key="llm", secret_enc=self.old_blob,
                                value={"provider": "gemini", "models": {"gemini": "gemini-test-model"}, "ollama_base_url": "http://ollama.test:11434"}))
            db.add(Source(name=self.src_name, kind="api", config={"url": "https://x.test"}, secret_enc=self.source_blob))
            user = User(email=f"{uuid.uuid4().hex[:8]}@test.io", name="T", password_hash=hash_password("Passw0rd!long"), role="admin")
            db.add(user); db.flush()
            db.add(UserSession(id=token_hash(token), user_id=user.id, expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
            doc = Document(title="Manual", doc_type="pdf", visibility="internal", status="ready"); db.add(doc); db.flush()
            db.add(Chunk(id=self.q.chunk, document_id=doc.id, ord=0, text="Accuracy Class 0.2S.", locator={"type": "pdf", "pdf_pages": [3]}))
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.client.cookies.set("infinity_session", token)
        self.hdr = {"X-Requested-With": "infinity"}
        self.AppSetting, self.AuditLog, self.Source, self.session_scope = AppSetting, AuditLog, Source, session_scope
        self.audit_baseline = 0; self.audit_baseline = self._audit_count()  # audit_log persists across tests; compare against this, not zero

        # capture every log record so we can prove no secret is ever logged
        self.records = []
        handler = logging.Handler(); handler.emit = lambda r: self.records.append(r.getMessage())
        logging.getLogger().addHandler(handler); self.addCleanup(logging.getLogger().removeHandler, handler)
        self.old_level = logging.getLogger().level; logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel, self.old_level)

    # -- helpers
    def _row(self):
        with self.session_scope() as db:
            r = db.get(self.AppSetting, "llm")
            return r.secret_enc, dict(r.value or {})

    def _source_blob(self):
        with self.session_scope() as db:
            return db.query(self.Source).filter_by(name=self.src_name).one().secret_enc

    def _audit_count(self):
        with self.session_scope() as db:
            return db.query(self.AuditLog).filter_by(action="llm_credential_replaced").count()

    def _new_audit_rows(self):
        return self._audit_count() - self.audit_baseline

    def _replace(self, key=NEW_API_KEY, **extra):
        return self.client.post("/api/admin/settings/llm/credentials", json={"provider": "gemini", "api_key": key, **extra}, headers=self.hdr)

    # -- tests
    def test_replacement_works_when_old_credential_cannot_be_decrypted(self):
        with self.assertRaises(Exception):  # sanity: the old blob really is unreadable with the key the app has
            SecretBox(KEY_NEW).decrypt_json(self.old_blob)
        with mock.patch.object(providers, "_http_get", return_value=(200, {})) as http:
            r = self._replace()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["result"]["ok"])
        self.assertTrue(body["result"]["archived_previous"])
        self.assertFalse(body["credentials_unreadable"])
        self.assertTrue(body["keys"]["gemini"]["configured"])
        # encrypted with the CURRENT key
        blob, value = self._row()
        self.assertEqual(SecretBox(KEY_NEW).decrypt_json(blob), {"gemini": NEW_API_KEY})
        # the new key (not the old one) was what got validated, via header
        self.assertEqual(http.call_args.args[1]["x-goog-api-key"], NEW_API_KEY)
        # old blob preserved byte-for-byte, still encrypted, still readable with the lost key if it is ever found
        self.assertEqual(self._new_audit_rows(), 1)   # audit entry committed together with the key
        archived = value["archived_secrets"][0]["token"]
        self.assertEqual(archived, self.old_blob)
        self.assertEqual(SecretBox(KEY_OLD).decrypt_json(archived)["gemini"], OLD_API_KEY)

    def test_unrelated_settings_and_source_credentials_are_preserved(self):
        with mock.patch.object(providers, "_http_get", return_value=(200, {})):
            self.assertEqual(self._replace().status_code, 200)
        _, value = self._row()
        self.assertEqual(value["provider"], "gemini")
        self.assertEqual(value["models"], {"gemini": "gemini-test-model"})
        self.assertEqual(value["ollama_base_url"], "http://ollama.test:11434")
        self.assertEqual(self._source_blob(), self.source_blob)  # untouched, still under the old key

    def test_invalid_key_is_rejected_and_nothing_changes(self):
        with mock.patch.object(providers, "_http_get", return_value=(400, {"error": {"message": "API key not valid."}})):
            r = self._replace()
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(r.json()["code"], "key_rejected")
        self.assertIn("Nothing was saved", r.json()["detail"])
        self.assertEqual(self._row()[0], self.old_blob)
        self.assertNotIn("archived_secrets", self._row()[1])
        self.assertEqual(self._new_audit_rows(), 0)

    def test_unreachable_provider_blocks_save_unless_admin_skips_validation(self):
        with mock.patch.object(providers, "_http_get", side_effect=httpx.ConnectError("down")):
            r = self._replace()
            self.assertEqual(r.status_code, 502, r.text)
            self.assertEqual(r.json()["code"], "validation_unavailable")
            self.assertEqual(self._row()[0], self.old_blob)
            r2 = self._replace(verify=False)  # explicit admin choice: no outbound call at all
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertFalse(r2.json()["result"]["validated"])
        self.assertEqual(SecretBox(KEY_NEW).decrypt_json(self._row()[0])["gemini"], NEW_API_KEY)

    def test_failure_before_commit_rolls_everything_back(self):
        import infinity.routers.admin as admin
        with mock.patch.object(providers, "_http_get", return_value=(200, {})), \
             mock.patch.object(admin.audit, "record", side_effect=RuntimeError("audit write failed")):
            r = self._replace()
        self.assertEqual(r.status_code, 500)
        blob, value = self._row()
        self.assertEqual(blob, self.old_blob)               # the key write was rolled back with the failed audit write
        self.assertNotIn("archived_secrets", value)
        self.assertEqual(self._new_audit_rows(), 0)

    def test_readable_record_keeps_the_other_providers_key(self):
        both = SecretBox(KEY_NEW).encrypt_json({"gemini": "g-existing-key-1", "openai": "sk-existing-openai-1"})
        with self.session_scope() as db:
            db.get(self.AppSetting, "llm").secret_enc = both
        with mock.patch.object(providers, "_http_get", return_value=(200, {})):
            r = self.client.post("/api/admin/settings/llm/credentials", json={"provider": "openai", "api_key": "sk-new-openai-key-22"}, headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["result"]["archived_previous"])
        stored = SecretBox(KEY_NEW).decrypt_json(self._row()[0])
        self.assertEqual(stored, {"gemini": "g-existing-key-1", "openai": "sk-new-openai-key-22"})

    def test_unknown_model_saves_the_key_with_a_warning(self):
        with mock.patch.object(providers, "_http_get", return_value=(404, {})):
            r = self._replace()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("gemini-test-model", r.json()["result"]["warning"])

    def test_chat_uses_the_newly_saved_key(self):
        with mock.patch.object(providers, "_http_get", return_value=(200, {})):
            self.assertEqual(self._replace().status_code, 200)
        seen = {}
        class LLM:
            def complete_with_instructions(self, prompt, *, system_instruction):
                return "## Verified from sources\nClass 0.2S [S1]."
        def fake_create(settings):
            seen["key"] = settings.gemini_api_key
            seen["provider"] = settings.llm_provider
            return LLM()
        with mock.patch.object(providers, "create_llm_provider", side_effect=fake_create):
            r = self.client.post("/api/chat", json={"question": "what is the accuracy class?"}, headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, {"key": NEW_API_KEY, "provider": "gemini"})   # decrypted with the current key, handed to the provider
        self.assertEqual(r.json()["message"]["citations"][0]["id"], "S1")
        self.assertNotIn(NEW_API_KEY, r.text)

    def test_secrets_never_appear_in_responses_or_logs(self):
        with mock.patch.object(providers, "_http_get", return_value=(200, {})):
            r = self._replace()
        views = [r.text, self.client.get("/api/admin/settings/llm").text]
        for blob in views:
            for secret in (NEW_API_KEY, OLD_API_KEY, "sk-old-openai"):
                self.assertNotIn(secret, blob)
        self.assertTrue(self.records, "expected some log output to inspect")
        for message in self.records:
            for secret in (NEW_API_KEY, OLD_API_KEY, KEY_NEW, KEY_OLD):
                self.assertNotIn(secret, message)
        with self.session_scope() as db:   # nor in the audit trail
            detail = str(db.query(self.AuditLog).filter_by(action="llm_credential_replaced").all()[-1].detail)
        self.assertNotIn(NEW_API_KEY, detail)

    def test_legacy_put_route_also_archives_instead_of_discarding(self):
        r = self.client.put("/api/admin/settings/llm", json={"keys": {"gemini": "legacy-route-key-123"}}, headers=self.hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._row()[1]["archived_secrets"][0]["token"], self.old_blob)

    def test_short_or_unknown_provider_input_is_rejected_cleanly(self):
        self.assertEqual(self._replace(key="short", verify=False).status_code, 422)
        r = self.client.post("/api/admin/settings/llm/credentials", json={"provider": "ollama", "api_key": "x" * 20}, headers=self.hdr)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self._row()[0], self.old_blob)


if __name__ == "__main__":
    unittest.main()
