import json, tempfile, unittest
from tests import stubs  # noqa: F401  (installs stand-ins when SQLAlchemy etc. are absent)

from infinity import crawler
from infinity.apiconnector import (ApiConfigError, extract_records, parse_payload, records_to_chunks, sync_api, validate_config as api_validate, fetch_records)
from infinity.crawler import ConfigError, in_scope, normalize_url, sync_website, validate_config
from infinity.fetcher import FetchResult
from infinity.models import Document, Source


class FakeFetcher:
    user_agent = "TestBot"

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, dict(headers or {})))
        if url in self.pages:
            status, ctype, body, extra = (self.pages[url] + ({},))[:4]
            return FetchResult(url=url, status=status, headers={"content-type": ctype, **extra}, content=body if isinstance(body, bytes) else body.encode())
        return FetchResult(url=url, status=404, headers={"content-type": "text/html"}, content=b"nf")


def page(title, body, links=()):
    anchors = "".join(f'<a href="{l}">x</a>' for l in links)
    return (200, "text/html", f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1><p>{body}</p>{anchors}</main></body></html>")


class CrawlerTests(unittest.TestCase):
    def run_sync(self, pages, config, existing=None, fetcher=None):
        fetcher = fetcher or FakeFetcher(pages)
        source = Source(id="s1", name="Test", kind="website", config=config, visibility="public")
        docs = existing if existing is not None else {}
        indexed = []

        class Session:
            def add(self, d): docs[d.uri] = d
            def flush(self):
                for n, x in enumerate(docs.values()):
                    if not isinstance(getattr(x, "id", None), str): x.id = f"doc{n}"  # SQLAlchemy assigns defaults at flush
            def commit(self): pass
            def rollback(self): pass

        def indexer(session, doc, parsed):
            doc.status = "ready"; doc.chunk_count = len(parsed.chunks); indexed.append((doc.uri, parsed)); return len(parsed.chunks)

        with tempfile.TemporaryDirectory() as tmp:
            stats = sync_website(Session(), source, fetcher=fetcher, indexer=indexer, upload_dir=tmp, sleep=lambda s: None, lookup=docs.get)
        return stats, docs, indexed, fetcher

    def test_config_validation(self):
        with self.assertRaises(ConfigError): validate_config({})
        with self.assertRaises(ConfigError): validate_config({"start_urls": ["ftp://x"]})
        with self.assertRaises(ConfigError): validate_config({"url": "https://a.com", "include_patterns": ["("]})
        cfg = validate_config({"url": "https://a.com/x", "max_pages": 9999, "delay_seconds": 0})
        self.assertEqual(cfg["max_pages"], 500); self.assertEqual(cfg["delay_seconds"], 1.0)
        self.assertEqual(cfg["allowed_domains"], ["a.com"])

    def test_scope_and_normalize(self):
        cfg = validate_config({"url": "https://a.com", "exclude_patterns": [r"/news/"]})
        self.assertTrue(in_scope("https://a.com/p", cfg)); self.assertTrue(in_scope("https://sub.a.com/p", cfg))
        self.assertFalse(in_scope("https://evil.com/p", cfg)); self.assertFalse(in_scope("https://a.com/news/1", cfg))
        self.assertEqual(normalize_url("HTTPS://A.com#frag"), "https://a.com/")

    def test_crawl_follows_links_respects_robots_and_scope(self):
        pages = {
            "https://a.com/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /private/"),
            "https://a.com/": page("Home", "Welcome", ["/products/", "/private/secret", "https://other.com/x", "/spec.pdf"]),
            "https://a.com/products/": page("Products", "Mk6N class 0.2S meter"),
            "https://a.com/private/secret": page("Secret", "nope"),
        }
        stats, docs, indexed, fetcher = self.run_sync(pages, {"url": "https://a.com/", "download_documents": False})
        self.assertEqual(sorted(docs), ["https://a.com/", "https://a.com/products/"])
        self.assertEqual(stats["skipped_robots"], 1); self.assertEqual(stats["skipped_scope"], 1)
        self.assertEqual(stats["new"], 2)
        self.assertNotIn("https://a.com/private/secret", [c[0] for c in fetcher.calls])
        text = " ".join(c.text for _, p in indexed for c in p.chunks)
        self.assertIn("class 0.2S", text)

    def test_unreachable_robots_means_disallow_all(self):
        class Boom(FakeFetcher):
            def get(self, url, headers=None, params=None):
                if url.endswith("robots.txt"): raise OSError("down")
                return super().get(url, headers, params)
        stats, docs, *_ = self.run_sync({"https://a.com/": page("H", "x")}, {"url": "https://a.com/"}, fetcher=Boom({}))
        self.assertEqual(docs, {}); self.assertEqual(stats["skipped_robots"], 1)

    def test_change_detection_and_conditional_headers(self):
        pages = {"https://a.com/": page("Home", "version one")}
        _, docs, _, _ = self.run_sync(pages, {"url": "https://a.com/", "max_depth": 0})
        doc = docs["https://a.com/"]; doc.meta = {"etag": '"abc"'}
        stats, _, indexed, fetcher = self.run_sync(pages, {"url": "https://a.com/", "max_depth": 0}, existing=docs)
        self.assertEqual((stats["unchanged"], stats["updated"], stats["new"]), (1, 0, 0)); self.assertEqual(indexed, [])
        self.assertEqual(fetcher.calls[-1][1].get("If-None-Match"), '"abc"')
        pages["https://a.com/"] = page("Home", "version two")
        stats, _, indexed, _ = self.run_sync(pages, {"url": "https://a.com/", "max_depth": 0}, existing=docs)
        self.assertEqual(stats["updated"], 1); self.assertEqual(len(indexed), 1)

    def test_304_counts_unchanged(self):
        pages = {"https://a.com/": page("Home", "x")}
        _, docs, _, _ = self.run_sync(pages, {"url": "https://a.com/", "max_depth": 0})
        docs["https://a.com/"].meta = {"etag": "e1"}
        pages["https://a.com/"] = (304, "text/html", "")
        stats, *_ = self.run_sync(pages, {"url": "https://a.com/", "max_depth": 0}, existing=docs)
        self.assertEqual(stats["unchanged"], 1)

    def test_discovers_and_downloads_documents(self):
        from docx import Document as W
        import io
        buf = io.BytesIO(); d = W(); d.add_heading("Datasheet", 1); d.add_paragraph("Accuracy class 0.2S"); d.save(buf)
        pages = {
            "https://a.com/": page("Home", "docs", ["/files/ds.docx"]),
            "https://a.com/files/ds.docx": (200, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buf.getvalue()),
        }
        stats, docs, indexed, _ = self.run_sync(pages, {"url": "https://a.com/", "max_depth": 1})
        self.assertEqual(stats["documents_downloaded"], 1)
        ds = docs["https://a.com/files/ds.docx"]
        self.assertEqual(ds.doc_type, "docx"); self.assertTrue(ds.file_path.endswith("ds.docx"))

    def test_http_errors_are_recorded_not_fatal(self):
        stats, *_ = self.run_sync({"https://a.com/": page("H", "ok", ["/missing"])}, {"url": "https://a.com/"})
        self.assertEqual(stats["new"], 1); self.assertTrue(any("HTTP 404" in e for e in stats["errors"]))


class ApiConnectorTests(unittest.TestCase):
    def test_validate(self):
        with self.assertRaises(ApiConfigError): api_validate({"url": "ftp://x"})
        with self.assertRaises(ApiConfigError): api_validate({"url": "https://x", "auth": "magic"})

    def test_json_records_path_and_chunks(self):
        payload = parse_payload(json.dumps({"data": {"items": [{"sku": "MK6N", "name": "Mk6N", "spec": {"class": "0.2S"}}, {"sku": "MK10M", "name": "Mk10M", "spec": {"class": "0.5S"}}]}}).encode(), "application/json", "auto")
        records = extract_records(payload, "data.items")
        self.assertEqual(len(records), 2)
        cfg = api_validate({"url": "https://x/api", "id_field": "sku", "title_field": "name"})
        chunks = records_to_chunks(records, cfg, "Catalog")
        self.assertIn("spec.class: 0.2S", chunks[0].text); self.assertIn("(MK10M Mk10M)", chunks[0].text)
        self.assertEqual(chunks[0].locator["record_end"], 2)
        with self.assertRaises(ApiConfigError): extract_records(payload, "data.nope")

    def test_xml(self):
        xml = b"<catalog><item id='1'><name>Mk6N</name></item><item id='2'><name>Mk10M</name></item></catalog>"
        records = extract_records(parse_payload(xml, "application/xml", "auto"), "catalog.item")
        self.assertEqual([r["name"] for r in records], ["Mk6N", "Mk10M"]); self.assertEqual(records[0]["@id"], "1")

    def test_auth_header_and_sync_change_detection(self):
        body = json.dumps([{"a": 1}]).encode()
        fetcher = FakeFetcher({"https://x/api": (200, "application/json", body)})
        source = Source(id="s", name="Cat", kind="api", config={"url": "https://x/api", "auth": "bearer"}, visibility="internal")
        doc_holder = {}
        class S:
            def add(self, d): doc_holder["d"] = d
            def flush(self): doc_holder["d"].id = "d1"
            def commit(self): pass
        def indexer(s, d, p): d.status = "ready"; return len(p.chunks)
        r1 = sync_api(S(), source, "tok123", fetcher=fetcher, indexer=indexer, lookup=lambda: doc_holder.get("d"))
        self.assertTrue(r1["changed"]); self.assertEqual(fetcher.calls[0][1]["Authorization"], "Bearer tok123")
        r2 = sync_api(S(), source, "tok123", fetcher=fetcher, indexer=indexer, lookup=lambda: doc_holder.get("d"))
        self.assertFalse(r2["changed"])


if __name__ == "__main__":
    unittest.main()
