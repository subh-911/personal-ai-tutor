from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from app.config import settings


SUPPORTED_TEXT_MIMES = {"text/plain", "text/markdown", "text/x-markdown"}
PDF_MIME = "application/pdf"


@dataclass
class ParsedDocument:
    text: str
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class UnsupportedMediaError(ValueError):
    pass


def parse_pdf(data: bytes) -> ParsedDocument:
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    info = reader.metadata or {}
    title = (info.title or None) if hasattr(info, "title") else None
    return ParsedDocument(
        text=text,
        title=title,
        metadata={"page_count": len(reader.pages), "format": "pdf"},
    )


def parse_text(data: bytes, mime: str) -> ParsedDocument:
    text = data.decode("utf-8", errors="replace")
    fmt = "markdown" if "markdown" in mime else "text"
    return ParsedDocument(text=text, metadata={"format": fmt})


def parse_html(html: str, base_url: str) -> ParsedDocument:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    root = soup.find("main") or soup.body or soup
    text = "\n".join(line.strip() for line in root.get_text("\n").splitlines() if line.strip())
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    return ParsedDocument(text=text, title=title, metadata={"format": "html", "source_url": base_url})


async def fetch_url(url: str) -> tuple[str, str]:
    headers = {"User-Agent": "personal-ai-tutor/0.1 (+https://example.invalid)"}
    timeout = httpx.Timeout(settings.scrape_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text, str(response.url)


def parse_upload(*, filename: str, content_type: str | None, data: bytes) -> ParsedDocument:
    mime = (content_type or "").lower()
    lower_name = filename.lower()

    if mime == PDF_MIME or lower_name.endswith(".pdf"):
        return parse_pdf(data)
    if mime in SUPPORTED_TEXT_MIMES or lower_name.endswith((".txt", ".md", ".markdown")):
        effective_mime = mime if mime in SUPPORTED_TEXT_MIMES else (
            "text/markdown" if lower_name.endswith((".md", ".markdown")) else "text/plain"
        )
        return parse_text(data, effective_mime)

    raise UnsupportedMediaError(f"unsupported upload type: filename={filename!r} content_type={mime!r}")
