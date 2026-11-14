#!/usr/bin/env python3
"""End-to-end verification of a RUNNING INFINITY AI stack. Standard library only.

    python scripts/verify_platform.py                 # reads URL/credentials from .env
    python scripts/verify_platform.py --llm           # also tests a real LLM answer (uses your API quota)

Every check performs real HTTP calls against the live system; nothing is mocked. Exit code 1 if any check fails.
Checks that depend on something not configured (e.g. no LLM key) are reported as SKIPPED, never as passed.
"""
import argparse, http.cookiejar, json, mimetypes, os, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = []


def load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Client:
    def __init__(self, base):
        self.base, self.jar = base.rstrip("/"), http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, headers=None, raw=None, csrf=True):
        h = {"X-Requested-With": "infinity"} if csrf else {}
        data = raw
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + "/api" + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=120) as r:
                txt = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(txt) if txt[:1] in "[{" else txt)
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(txt)
            except ValueError:
                return e.code, txt

    def upload(self, path, filename, content: bytes, fields=None):
        b = uuid.uuid4().hex
        parts = []
        for k, v in (fields or {}).items():
            parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\nContent-Type: text/plain\r\n\r\n'.encode() + content + b"\r\n")
        parts.append(f"--{b}--\r\n".encode())
        return self.call("POST", path, raw=b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={b}"})


def check(name, ok, detail="", skipped=False):
    status = "SKIP" if skipped else ("PASS" if ok else "FAIL")
    RESULTS.append({"check": name, "status": status, "detail": detail})
    print(f"[{status}] {name}" + (f"  - {detail}" if detail else ""))
    return ok


def wait(fn, timeout, every=3):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return None


def main():
    env = load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=f"http://localhost:{env.get('WEB_PORT', '8088')}")
    ap.add_argument("--email", default=env.get("BOOTSTRAP_ADMIN_EMAIL", "admin@infinity.local"))
    ap.add_argument("--password", default=env.get("BOOTSTRAP_ADMIN_PASSWORD", ""))
    ap.add_argument("--llm", action="store_true", help="also test a real LLM-generated, citation-validated answer")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for model load / indexing")
    a = ap.parse_args()
    if not a.password:
        sys.exit("No admin password: pass --password or keep BOOTSTRAP_ADMIN_PASSWORD in .env")

    c = Client(a.url)
    s, h = c.call("GET", "/health", csrf=False)
    check("API reachable through the web port", s == 200 and h.get("status") == "ok", a.url)
    if s != 200:
        return finish()

    s, _ = c.call("GET", "/documents")
    check("Unauthenticated requests are rejected (401)", s == 401, f"got {s}")
    s, _ = Client(a.url).call("POST", "/auth/login", {"email": a.email, "password": "definitely-wrong"})
    check("Wrong password is rejected", s == 401, f"got {s}")
    s, _ = c.call("POST", "/auth/login", {"email": a.email, "password": a.password}, csrf=False)
    check("CSRF guard: state-changing call without header is refused", s == 403, f"got {s}")
    s, me = c.call("POST", "/auth/login", {"email": a.email, "password": a.password})
    if not check("Administrator can sign in", s == 200 and me.get("role") == "admin", f"got {s}"):
        return finish()

    s, hd = c.call("GET", "/admin/health")
    comp = {k: bool(v.get("ok", v.get("configured"))) for k, v in hd.items()} if s == 200 else {}
    for k in ("postgres", "redis", "worker", "qdrant"):
        check(f"Component healthy: {k}", comp.get(k, False), json.dumps(hd.get(k, {}))[:160])
    emb_ok = wait(lambda: (c.call("GET", "/admin/health")[1].get("embedder") or {}).get("ok"), a.timeout)
    check("Embedding model loaded (BGE-M3)", bool(emb_ok), "first start downloads ~2.3 GB; rerun with a larger --timeout if needed")
    check("LLM provider configured", comp.get("llm", False), json.dumps(hd.get("llm", {})), skipped=not comp.get("llm", False))

    # --- existing Qdrant data preserved and mirrored
    def parity():
        _, d = c.call("GET", "/admin/health")
        q = d.get("qdrant", {})
        return q if q.get("ok") and q.get("points", 0) > 0 and q["mirrored_chunks"] >= q["points"] else None
    q = wait(parity, 180)
    _, d = c.call("GET", "/admin/health")
    q = q or d.get("qdrant", {})
    check("Existing Qdrant collection reachable and non-empty", q.get("ok") and q.get("points", 0) > 0, f"points={q.get('points')}")
    check("All existing points mirrored for keyword search/citations (no re-index)", bool(q.get("points")) and q.get("mirrored_chunks", 0) >= q.get("points", 1), f"points={q.get('points')} mirrored={q.get('mirrored_chunks')}")

    # --- legacy retrieval works on pre-existing content
    _, docs = c.call("GET", "/documents?limit_n=500")
    legacy = [x for x in docs if x.get("legacy")] if isinstance(docs, list) else []
    check("Pre-existing documents visible in Library", len(legacy) > 0, f"{len(legacy)} document(s)")
    if legacy:
        words = " ".join(legacy[0]["title"].replace("_", " ").replace("-", " ").split()[:3])
        s, res = c.call("POST", "/search", {"query": words, "limit": 5})
        hits = res.get("results", []) if s == 200 else []
        check("Search finds pre-existing content", len(hits) > 0, f"query={words!r} results={len(hits)}")
        if hits:
            s, ch = c.call("GET", "/chunks/" + hits[0]["chunk_id"])
            check("Citation opens with cited passage and context", s == 200 and any(x["is_cited"] for x in ch.get("context", [])), hits[0]["location"])

    # --- upload -> index -> search -> cite -> delete
    token = "ZXVERIFY" + uuid.uuid4().hex[:10].upper()
    body = (f"INFINITY verification note.\n\nThe reference meter model {token} has accuracy class 0.05 and an optical port.\n"
            "This file was created by scripts/verify_platform.py and is deleted at the end of the run.\n").encode()
    s, up = c.upload("/documents/upload", f"verify-{token}.txt", body, {"visibility": "internal"})
    created = up.get("created", []) if s == 201 else []
    check("Upload accepted", len(created) == 1, f"status={s}")
    doc_id = created[0]["id"] if created else None
    if doc_id:
        ready = wait(lambda: (lambda d: d if d.get("status") in ("ready", "failed") else None)(c.call("GET", f"/documents/{doc_id}")[1]), a.timeout)
        check("Document indexed by the background worker", bool(ready) and ready["status"] == "ready", (ready or {}).get("error") or (ready or {}).get("status", "timed out"))
        if ready and ready["status"] == "ready":
            s, res = c.call("POST", "/search", {"query": token, "limit": 5})
            hits = [r for r in res.get("results", []) if r["document_id"] == doc_id] if s == 200 else []
            check("Hybrid search retrieves the new document by exact token", bool(hits), f"channels={hits[0]['channels'] if hits else None}")
            s, res = c.call("POST", "/search", {"query": "reference meter optical port accuracy", "limit": 8})
            sem = [r for r in res.get("results", []) if r["document_id"] == doc_id and "semantic" in r["channels"]] if s == 200 else []
            check("Semantic (vector) channel retrieves the new document", bool(sem), "requires embedder + Qdrant")
            if a.llm:
                s, ans = c.call("POST", "/chat", {"question": f"What accuracy class does the reference meter model {token} have?"})
                m = ans.get("message", {}) if s == 200 else {}
                ids = [x["id"] for x in m.get("citations", [])]
                check("LLM answer is grounded with validated citations", s == 200 and bool(ids) and "0.05" in m.get("content", ""), f"status={s} citations={ids} {ans.get('detail', '') if s != 200 else ''}")
            else:
                check("LLM answer with validated citations", False, "run with --llm to test", skipped=True)

    # --- role isolation
    vemail, vpass = f"verify-viewer-{uuid.uuid4().hex[:6]}@infinity.local", "Viewer-Pass-" + uuid.uuid4().hex[:6]
    s, u = c.call("POST", "/users", {"email": vemail, "name": "Verify Viewer", "password": vpass, "role": "viewer"})
    check("Administrator can create a user", s == 201, f"status={s}")
    if s == 201:
        v = Client(a.url)
        s, _ = v.call("POST", "/auth/login", {"email": vemail, "password": vpass})
        check("Viewer can sign in", s == 200)
        s, _ = v.call("GET", "/users")
        check("Viewer cannot access user administration (403)", s == 403, f"got {s}")
        s, vd = v.call("GET", "/documents?limit_n=500")
        leaked = [x for x in vd if x["visibility"] != "public"] if s == 200 else ["?"]
        check("Viewer sees no internal/confidential documents", s == 200 and not leaked, f"{len(leaked)} leaked")
        if doc_id:
            s, res = v.call("POST", "/search", {"query": token, "limit": 5})
            check("Viewer cannot retrieve internal content via search", s == 200 and not any(r["document_id"] == doc_id for r in res.get("results", [])))
            s, _ = v.call("GET", f"/documents/{doc_id}")
            check("Viewer cannot open an internal document directly", s == 404, f"got {s}")
        c.call("DELETE", "/users/" + u["id"])

    # --- cleanup
    if doc_id:
        s, _ = c.call("DELETE", f"/documents/{doc_id}")
        check("Document deletion removes it", s == 200 and c.call("GET", f"/documents/{doc_id}")[0] == 404)
        _, qh = c.call("GET", "/admin/health")
        check("Deleting the test document left pre-existing points untouched", qh["qdrant"]["points"] >= q.get("points", 0), f"points={qh['qdrant'].get('points')}")
    s, _ = c.call("GET", "/admin/audit?limit_n=5")
    check("Audit log is recording", s == 200)
    finish()


def finish():
    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    out = ROOT / "docs" / "verification-results.json"
    out.write_text(json.dumps({"ran_at": time.strftime("%Y-%m-%d %H:%M:%S"), "results": RESULTS}, indent=2), encoding="utf-8")
    print(f"\n{len(RESULTS) - len(fails)} passed/skipped, {len(fails)} failed. Details: {out}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
