"""HTML to readable text and link extraction (used by the website connector)."""

from __future__ import annotations

from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

_DROP = ["script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe", "aside"]


def html_to_text(html: str, base_url: str | None = None) -> tuple[str | None, str, list[str]]:
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute = urldefrag(urljoin(base_url, href))[0] if base_url else href
        links.append(absolute)
    title = soup.title.get_text(strip=True) if soup.title else None
    for tag in soup(_DROP):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    lines: list[str] = []
    for element in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "tr", "pre", "td", "th", "dt", "dd"]):
        if element.name in {"td", "th"} and element.find_parent("tr"):
            continue
        if element.name == "tr":
            cells = [c.get_text(" ", strip=True) for c in element.find_all(["td", "th"])]
            text = " | ".join(c for c in cells if c)
        else:
            text = element.get_text(" ", strip=True)
        if not text:
            continue
        if element.name in {"h1", "h2", "h3", "h4"}:
            lines.append("\n" + text)
        elif element.name == "li":
            lines.append("- " + text)
        else:
            lines.append(text)
    deduped: list[str] = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    text = "\n\n".join(deduped).strip()
    return title, text, list(dict.fromkeys(links))
