"""Website connector: polite, robots.txt-respecting crawler with discovery of linked documents and change detection."""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from sqlalchemy import select
from sqlalchemy.orm import Session

from .chunking import ParsedChunk, ParsedDocument, split_text
from .htmltext import html_to_text
from .models import Document, Source
from .parsers import SUPPORTED_SUFFIXES, parse_file

log = logging.getLogger(__name__)
DOC_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", ".csv"}
MAX_PAGES_CAP, MAX_DEPTH_CAP = 500, 6


class ConfigError(ValueError):
    pass


def normalize_url(url: str) -> str:
    p = urlparse(url)
    path = p.path or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def validate_config(raw: dict) -> dict:
    urls = raw.get("start_urls") or ([raw["url"]] if raw.get("url") else [])
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.splitlines() if u.strip()]
    if not urls:
        raise ConfigError("at least one start URL is required")
    for u in urls:
        if urlparse(u).scheme not in {"http", "https"} or not urlparse(u).hostname:
            raise ConfigError(f"invalid start URL: {u}")
    domains = [d.strip().lower() for d in (raw.get("allowed_domains") or []) if d.strip()] or sorted({urlparse(u).hostname.lower() for u in urls})
    patterns = {}
    for key in ("include_patterns", "exclude_patterns"):
        values = [v for v in (raw.get(key) or []) if v.strip()]
        for v in values:
            try:
                re.compile(v)
            except re.error as error:
                raise ConfigError(f"invalid regular expression {v!r}: {error}") from error
        patterns[key] = values
    return {
        "start_urls": [normalize_url(u) for u in urls],
        "allowed_domains": domains,
        "max_pages": max(1, min(int(raw.get("max_pages", 50)), MAX_PAGES_CAP)),
        "max_depth": max(0, min(int(raw.get("max_depth", 2)), MAX_DEPTH_CAP)),
        "delay_seconds": max(1.0, float(raw.get("delay_seconds", 1.0))),  # politeness floor
        "download_documents": bool(raw.get("download_documents", True)),
        **patterns,
    }


def in_scope(url: str, cfg: dict) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not any(host == d or host.endswith("." + d) for d in cfg["allowed_domains"]):
        return False
    if cfg["include_patterns"] and not any(re.search(p, url) for p in cfg["include_patterns"]):
        return False
    return not any(re.search(p, url) for p in cfg["exclude_patterns"])


def is_document_link(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() in DOC_EXTENSIONS


class Robots:
    """Per-origin robots.txt cache. Unreachable (5xx) robots.txt is treated as 'disallow all' for safety."""

    def __init__(self, fetcher, user_agent: str) -> None:
        self._fetcher, self._ua, self._parsers = fetcher, user_agent, {}

    def _parser(self, url: str):
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self._parsers:
            parser = RobotFileParser()
            try:
                result = self._fetcher.get(origin + "/robots.txt")
                if result.status == 200:
                    parser.parse(result.content.decode("utf-8", "replace").splitlines())
                elif 400 <= result.status < 500:
                    parser.parse([])  # no robots.txt: everything allowed
                else:
                    parser.parse(["User-agent: *", "Disallow: /"])
            except Exception:
                parser.parse(["User-agent: *", "Disallow: /"])
            self._parsers[origin] = parser
        return self._parsers[origin]

    def allowed(self, url: str) -> bool:
        return self._parser(url).can_fetch(self._ua, url)

    def delay(self, url: str) -> float:
        return float(self._parser(url).crawl_delay(self._ua) or 0)


@dataclass
class SyncStats:
    pages_fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_robots: int = 0
    skipped_scope: int = 0
    documents_downloaded: int = 0
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["errors"] = self.errors[:25]
        d["error_count"] = len(self.errors)
        return d


def sync_website(
    session: Session, source: Source, *, fetcher, indexer: Callable[[Session, Document, ParsedDocument], int],
    upload_dir: str, progress: Callable[[str], None] | None = None, sleep: Callable[[float], None] = time.sleep,
    lookup: Callable[[str], Document | None] | None = None,
) -> dict:
    cfg = validate_config(source.config)
    find = lookup or (lambda uri: session.scalar(select(Document).where(Document.source_id == source.id, Document.uri == uri)))
    robots = Robots(fetcher, fetcher.user_agent)
    stats = SyncStats()
    queue: list[tuple[str, int]] = [(u, 0) for u in cfg["start_urls"]]
    seen: set[str] = set()
    last_request = 0.0
    while queue and stats.pages_fetched < cfg["max_pages"]:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if not in_scope(url, cfg):
            stats.skipped_scope += 1
            continue
        if not robots.allowed(url):
            stats.skipped_robots += 1
            continue
        wait = max(cfg["delay_seconds"], robots.delay(url)) - (time.monotonic() - last_request)
        if wait > 0 and last_request:
            sleep(wait)
        last_request = time.monotonic()
        existing = find(url)
        headers = {}
        if existing and existing.meta.get("etag"):
            headers["If-None-Match"] = existing.meta["etag"]
        if existing and existing.meta.get("last_modified"):
            headers["If-Modified-Since"] = existing.meta["last_modified"]
        try:
            result = fetcher.get(url, headers=headers)
        except Exception as error:
            stats.errors.append(f"{url}: {type(error).__name__}: {str(error)[:150]}")
            continue
        stats.pages_fetched += 1
        now = datetime.now(timezone.utc)
        if result.status == 304 and existing:
            existing.last_seen_at = now
            stats.unchanged += 1
            session.commit()
            continue
        if result.status >= 400:
            stats.errors.append(f"{url}: HTTP {result.status}")
            continue
        try:
            if result.content_type in {"text/html", "application/xhtml+xml"} or (not result.content_type and b"<html" in result.content[:2000].lower()):
                title, text, links = html_to_text(result.content.decode("utf-8", "replace"), result.url)
                for link in links:
                    link = normalize_url(link)
                    if link in seen:
                        continue
                    if is_document_link(link):
                        if cfg["download_documents"]:
                            queue.insert(0, (link, depth))  # documents don't consume crawl depth
                    elif depth < cfg["max_depth"]:
                        queue.append((link, depth + 1))
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                if existing and existing.content_hash == content_hash and existing.status == "ready":
                    existing.last_seen_at = now
                    stats.unchanged += 1
                    session.commit()
                    continue
                doc = existing or Document(source_id=source.id, uri=url, title=(title or url)[:500], doc_type="web", visibility=source.visibility, status="processing", meta={})
                if not existing:
                    session.add(doc)
                    session.flush()
                parsed = ParsedDocument(title=(title or url)[:500], doc_type="web", chunks=[ParsedChunk(text=t, locator={"type": "web", "url": url}) for t in split_text(text)])
                if not parsed.chunks:
                    parsed.issues.append("page has no extractable text")
                    stats.errors.append(f"{url}: no extractable text")
                    session.rollback()
                    continue
                doc.title, doc.content_hash, doc.last_seen_at = parsed.title, content_hash, now
                doc.meta = {**(doc.meta or {}), "etag": result.headers.get("etag"), "last_modified": result.headers.get("last-modified")}
                indexer(session, doc, parsed)
                stats.updated += 1 if existing else 0
                stats.new += 0 if existing else 1
            elif is_document_link(url) or Path(urlparse(url).path).suffix.lower() in SUPPORTED_SUFFIXES:
                if not cfg["download_documents"]:
                    continue
                content_hash = hashlib.sha256(result.content).hexdigest()
                if existing and existing.content_hash == content_hash and existing.status == "ready":
                    existing.last_seen_at = now
                    stats.unchanged += 1
                    session.commit()
                    continue
                doc = existing or Document(source_id=source.id, uri=url, title=Path(urlparse(url).path).name or url, doc_type="", visibility=source.visibility, status="processing", meta={})
                if not existing:
                    session.add(doc)
                    session.flush()
                folder = Path(upload_dir) / doc.id
                folder.mkdir(parents=True, exist_ok=True)
                filename = re.sub(r"[^\w.\-]+", "_", Path(urlparse(url).path).name or "download")
                path = folder / filename
                path.write_bytes(result.content)
                doc.file_path, doc.size_bytes, doc.content_hash, doc.last_seen_at = str(path), len(result.content), content_hash, now
                doc.meta = {**(doc.meta or {}), "filename": filename, "etag": result.headers.get("etag"), "last_modified": result.headers.get("last-modified")}
                parsed = parse_file(path, filename)
                doc.doc_type = parsed.doc_type
                doc.title = parsed.title[:500]
                indexer(session, doc, parsed)
                stats.documents_downloaded += 1
                stats.updated += 1 if existing else 0
                stats.new += 0 if existing else 1
        except Exception as error:
            session.rollback()
            stats.errors.append(f"{url}: {type(error).__name__}: {str(error)[:150]}")
        if progress:
            progress(f"{stats.pages_fetched} fetched, {stats.new} new, {stats.updated} updated, {stats.unchanged} unchanged")
    return stats.as_dict()
