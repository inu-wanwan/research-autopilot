"""Text extraction with stable locators (PDF page numbers, repository path:line), cached by content hash."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from pypdf import PdfReader

from research_autopilot.literature.acquire import CACHE

LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "−": "-", "–": "-", "—": "-", "’": "'",
             "‘": "'", "“": '"', "”": '"'}


def pdf_pages(path: Path, sha256: str) -> list[str]:
    cache = CACHE / "text" / f"{sha256}.json"
    if cache.is_file():
        return json.loads(cache.read_text())
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # one unreadable page must not lose the document
            pages.append("")
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(pages))
    return pages


def repo_files(root: Path) -> dict[str, str]:
    out = {}
    for line in (root / ".ra_manifest.txt").read_text().splitlines():
        _, rel = line.split("  ", 1)
        try:
            out[rel] = (root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


def normalize(text: str) -> str:
    """Matching form: NFKC, ligatures/dashes/quotes unified, de-hyphenated line breaks, collapsed space."""
    t = unicodedata.normalize("NFKC", text)
    for a, b in LIGATURES.items():
        t = t.replace(a, b)
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def find_quote(quote: str, pages: list[str]) -> int | None:
    """1-based page where the (normalized) quote occurs, else None."""
    q = normalize(quote)
    if len(q) < 12:
        return None
    for i, page in enumerate(pages, 1):
        if q in normalize(page):
            return i
    return None
