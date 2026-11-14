"""HTTP fetching with SSRF validation on every redirect hop and a response size cap."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from .netguard import assert_safe_url

USER_AGENT = "InfinityAI-KnowledgeBot/1.0 (+internal engineering knowledge platform)"


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict = field(default_factory=dict)
    content: bytes = b""

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


class FetchError(RuntimeError):
    pass


class SafeFetcher:
    def __init__(self, *, allow_private: bool = False, timeout: float = 30.0, max_bytes: int = 60 * 1024 * 1024, user_agent: str = USER_AGENT) -> None:
        self.allow_private, self.timeout, self.max_bytes, self.user_agent = allow_private, timeout, max_bytes, user_agent

    def get(self, url: str, headers: dict | None = None, params: dict | None = None) -> FetchResult:
        request_headers = {"User-Agent": self.user_agent, "Accept": "*/*", **(headers or {})}
        current = url
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            for _hop in range(6):
                assert_safe_url(current, allow_private=self.allow_private)
                with client.stream("GET", current, headers=request_headers, params=params if current == url else None) as response:
                    if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                        current = urljoin(current, response.headers["location"])
                        continue
                    body = bytearray()
                    for block in response.iter_bytes():
                        body.extend(block)
                        if len(body) > self.max_bytes:
                            raise FetchError(f"response exceeds {self.max_bytes // (1024 * 1024)} MB limit")
                    return FetchResult(url=current, status=response.status_code, headers={k.lower(): v for k, v in response.headers.items()}, content=bytes(body))
        raise FetchError("too many redirects")
