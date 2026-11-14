"""Regression test: POST /api/chat must succeed end to end (it used to return 500 because
conversation.updated_at was set from reply.created_at before the row was flushed, i.e. NULL).

Needs a migrated Postgres (DATABASE_URL); skipped automatically when none is reachable.
LLM, embedder and Qdrant are replaced with in-process fakes."""
import os
import sys
import unittest
import uuid
from unittest import mock
from datetime import datetime, timedelta, timezone

try:
    from sqlalchemy import create_engine, text
    _url = os.environ.get("DATABASE_URL", "")
    if not _url:
        raise RuntimeError("DATABASE_URL not set")
    with create_engine(_url).connect() as _c:
        _c.execute(text("select 1 from messages limit 1"))
    DB_OK = True
except Exception:  # no database / not migrated
    DB_OK = False


@unittest.skipUnless(DB_OK, "migrated Postgres (DATABASE_URL) not available")
class ChatFlowTest(unittest.TestCase):
    def test_chat_returns_cited_answer_and_followup(self):
        from fastapi.testclient import TestClient
        from infinity import main, providers
        import infinity.routers.chat as chat
        from infinity.crypto import hash_password, token_hash
        from infinity.db import session_scope
        from infinity.models import Chunk, Document, User, UserSession

        chunk_id = str(uuid.uuid4())

        class FakeQ:
            def search(self, vector, n): return [(chunk_id, 0.9)]

        class FakeE:
            def embed_query(self, t): return [0.0] * 1024

        class FakeLLM:
            def complete_with_instructions(self, prompt, *, system_instruction):
                return "## Verified from sources\nClass 0.2S [S1]."

        fake_provider = lambda db, o=None: (FakeLLM(), "fake", "fake-model")  # noqa: E731
        for target, name, value in ((chat, "qdrant", lambda: FakeQ()), (chat, "embedder", lambda: FakeE()),
                                    (providers, "build_provider", fake_provider)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)  # never leak fakes into other tests

        token = "t" + uuid.uuid4().hex
        with session_scope() as db:
            user = User(email=f"{uuid.uuid4().hex[:8]}@test.io", name="T", password_hash=hash_password("Passw0rd!long"), role="admin")
            db.add(user); db.flush()
            db.add(UserSession(id=token_hash(token), user_id=user.id, expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
            doc = Document(title="Manual", doc_type="pdf", visibility="internal", status="ready"); db.add(doc); db.flush()
            db.add(Chunk(id=chunk_id, document_id=doc.id, ord=0, text="Accuracy Class 0.2S.", locator={"type": "pdf", "pdf_pages": [3]}))

        client = TestClient(main.app, raise_server_exceptions=False)
        client.cookies.set("infinity_session", token)
        hdr = {"X-Requested-With": "infinity"}
        r = client.post("/api/chat", json={"question": "what is the accuracy class?"}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["message"]["citations"][0]["id"], "S1")
        r2 = client.post("/api/chat", json={"question": "and its standard?", "conversation_id": r.json()["conversation_id"]}, headers=hdr)
        self.assertEqual(r2.status_code, 200, r2.text)


if __name__ == "__main__":
    unittest.main()
