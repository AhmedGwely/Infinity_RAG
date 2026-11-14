"""REST connector for JSON / XML endpoints (read-only GET)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .chunking import ParsedChunk, ParsedDocument
from .models import Document, Source

try:  # defusedxml protects against entity-expansion attacks in untrusted XML
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET  # type: ignore

MAX_RECORDS_CAP = 20000
RECORDS_PER_CHUNK = 15


class ApiConfigError(ValueError):
    pass


def validate_config(raw: dict) -> dict:
    url = (raw.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ApiConfigError("a valid http(s) endpoint URL is required")
    auth = raw.get("auth", "none")
    if auth not in {"none", "bearer", "header"}:
        raise ApiConfigError("auth must be none, bearer or header")
    return {
        "url": url, "auth": auth, "header_name": raw.get("header_name") or "X-API-Key",
        "format": raw.get("format", "auto") if raw.get("format", "auto") in {"auto", "json", "xml"} else "auto",
        "records_path": (raw.get("records_path") or "").strip(), "id_field": (raw.get("id_field") or "").strip(),
        "title_field": (raw.get("title_field") or "").strip(), "params": raw.get("params") or {},
        "max_records": max(1, min(int(raw.get("max_records", 2000)), MAX_RECORDS_CAP)),
    }


def xml_to_obj(element) -> object:
    children = list(element)
    node: dict = {f"@{k}": v for k, v in element.attrib.items()}
    if not children:
        text = (element.text or "").strip()
        return text if not node else {**node, "#text": text} if text else node
    for child in children:
        tag = child.tag.rsplit("}", 1)[-1]
        value = xml_to_obj(child)
        if tag in node:
            if not isinstance(node[tag], list):
                node[tag] = [node[tag]]
            node[tag].append(value)
        else:
            node[tag] = value
    return node


def parse_payload(content: bytes, content_type: str, fmt: str):
    if fmt == "xml" or (fmt == "auto" and ("xml" in content_type or content.lstrip()[:1] == b"<")):
        root = ET.fromstring(content)
        return {root.tag.rsplit("}", 1)[-1]: xml_to_obj(root)}
    return json.loads(content.decode("utf-8-sig"))


def extract_records(payload, path: str) -> list:
    node = payload
    for part in [p for p in path.split(".") if p]:
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            raise ApiConfigError(f"records_path segment {part!r} not found in response")
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        lists = [v for v in node.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        return lists[0] if len(lists) == 1 and not path else [node]
    return [{"value": node}]


def flatten(value, prefix: str = "") -> list[str]:
    lines: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            lines += flatten(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            lines += flatten(v, f"{prefix}[{i}]")
    elif value not in (None, ""):
        lines.append(f"{prefix}: {value}")
    return lines


def records_to_chunks(records: list, cfg: dict, name: str) -> list[ParsedChunk]:
    chunks: list[ParsedChunk] = []
    for start in range(0, len(records), RECORDS_PER_CHUNK):
        block = records[start:start + RECORDS_PER_CHUNK]
        parts = []
        for offset, record in enumerate(block, start=start + 1):
            label = ""
            if isinstance(record, dict):
                rid = record.get(cfg["id_field"]) if cfg["id_field"] else None
                title = record.get(cfg["title_field"]) if cfg["title_field"] else None
                label = " ".join(str(x) for x in (rid, title) if x)
            parts.append(f"Record {offset}" + (f" ({label})" if label else "") + ":\n" + "\n".join(flatten(record)))
        chunks.append(ParsedChunk(text=f"API source: {name}\n\n" + "\n\n".join(parts), locator={"type": "api", "endpoint": cfg["url"], "record_start": start + 1, "record_end": start + len(block)}))
    return chunks


def fetch_records(cfg: dict, secret: str | None, fetcher) -> tuple[list, bytes]:
    headers = {"Accept": "application/json, application/xml;q=0.9"}
    if cfg["auth"] == "bearer" and secret:
        headers["Authorization"] = f"Bearer {secret}"
    elif cfg["auth"] == "header" and secret:
        headers[cfg["header_name"]] = secret
    result = fetcher.get(cfg["url"], headers=headers, params=cfg["params"] or None)
    if result.status >= 400:
        raise RuntimeError(f"API returned HTTP {result.status}")
    payload = parse_payload(result.content, result.content_type, cfg["format"])
    return extract_records(payload, cfg["records_path"])[: cfg["max_records"]], result.content


def sync_api(session: Session, source: Source, secret: str | None, *, fetcher, indexer: Callable[[Session, Document, ParsedDocument], int],
             lookup: Callable[[], Document | None] | None = None) -> dict:
    cfg = validate_config(source.config)
    records, raw = fetch_records(cfg, secret, fetcher)
    content_hash = hashlib.sha256(raw).hexdigest()
    now = datetime.now(timezone.utc)
    doc = lookup() if lookup else session.scalar(select(Document).where(Document.source_id == source.id))
    if doc and doc.content_hash == content_hash and doc.status == "ready":
        doc.last_seen_at = now
        session.commit()
        return {"records": len(records), "changed": False}
    chunks = records_to_chunks(records, cfg, source.name)
    parsed = ParsedDocument(title=f"API: {source.name}", doc_type="api", chunks=chunks)
    if not doc:
        doc = Document(source_id=source.id, title=parsed.title, doc_type="api", uri=cfg["url"], visibility=source.visibility, status="processing", meta={})
        session.add(doc)
        session.flush()
    doc.content_hash, doc.last_seen_at = content_hash, now
    indexer(session, doc, parsed)
    return {"records": len(records), "changed": True, "chunks": len(chunks)}
