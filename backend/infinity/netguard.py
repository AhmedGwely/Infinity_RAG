"""SSRF protection for admin-configured website and API sources."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

BLOCKED_HOSTNAMES = {"localhost", "host.docker.internal", "gateway.docker.internal", "postgres", "redis", "qdrant", "embedder", "api", "worker", "web"}


class UnsafeURL(ValueError):
    """Raised when a URL targets a disallowed scheme or network."""


def _is_forbidden(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )


def assert_safe_url(url: str, *, allow_private: bool = False) -> str:
    """Validate scheme and resolved addresses; return the URL unchanged if acceptable."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURL("only http and https URLs are allowed")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeURL("URL has no host")
    if allow_private:
        return url
    if host in BLOCKED_HOSTNAMES or host.endswith(".internal") or host.endswith(".local"):
        raise UnsafeURL(f"host {host!r} is not allowed (private network sources are disabled)")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise UnsafeURL(f"cannot resolve host {host!r}") from error
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses or any(_is_forbidden(a) for a in addresses):
        raise UnsafeURL(f"host {host!r} resolves to a private or reserved address")
    return url
