"""Text extraction with stable locators (PDF page numbers, repository path:line), cached by content hash."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from pypdf import PdfReader

from research_autopilot.literature import acquire

LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "−": "-", "–": "-", "—": "-", "’": "'",
             "‘": "'", "“": '"', "”": '"'}


def pdf_pages(path: Path, sha256: str) -> list[str]:
    cache = acquire.CACHE / "text" / f"{sha256}.json"
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


def normalize(text: str, join_hyphens: bool = True) -> str:
    """Matching form: NFKC, ligatures/dashes/quotes unified, collapsed space.

    A hyphen at a line break is either a split word ("effi-\ncient") or a compound ("sparsity-\ninducing");
    ``join_hyphens`` selects which reading is produced, and matching tries both.
    """
    t = unicodedata.normalize("NFKC", text)
    for a, b in LIGATURES.items():
        t = t.replace(a, b)
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2" if join_hyphens else r"\1-\2", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def find_quote(quote: str, pages: list[str]) -> int | None:
    """1-based page where the (normalized) quote occurs, else None."""
    base = normalize(quote, True)
    if len(base) < 12:
        return None
    # A sentence can mix split words and compounds at line ends, so also compare hyphen-free forms.
    qs = {base, normalize(quote, False), base.replace("-", "")}
    for i, page in enumerate(pages, 1):
        joined = normalize(page, True)
        variants = (joined, normalize(page, False), joined.replace("-", ""))
        if any(q in v for q in qs for v in variants):
            return i
    return None
