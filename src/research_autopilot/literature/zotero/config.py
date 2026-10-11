"""Zotero automation settings from RA environment variables (replaces the PoC's ZCPOC_* / .env loader)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE = Path.home() / ".research-autopilot" / "zotero"


@dataclass(frozen=True)
class Config:
    profile_dir: Path
    extension_dir: Path
    zotero_url: str
    sync_dir: Path | None       # where ZotMoov puts linked PDFs (Drive sync folder); auto-detected if unset
    collection: str | None      # collection that must be selected in Zotero (Connector saves there)
    headless: bool
    save_timeout_s: float
    page_timeout_s: float

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> "Config":
        from .platform_paths import detect_sync_dir
        e = (os.environ if env is None else env).get
        sync = e("RA_ZOTERO_SYNC_DIR") or detect_sync_dir().get("sync_dir")
        return cls(
            profile_dir=Path(e("RA_ZOTERO_PROFILE_DIR") or BASE / "profile").expanduser(),
            extension_dir=Path(e("RA_ZOTERO_CONNECTOR_DIR") or BASE / "connector").expanduser(),
            zotero_url=e("RA_ZOTERO_URL") or "http://127.0.0.1:23119",
            sync_dir=Path(sync).expanduser() if sync else None,
            collection=(e("RA_ZOTERO_COLLECTION") or "ResearchAutopilot") or None,
            headless=(e("RA_ZOTERO_HEADLESS") or "1") == "1",
            save_timeout_s=float(e("RA_ZOTERO_SAVE_TIMEOUT_S") or 90),
            page_timeout_s=float(e("RA_ZOTERO_PAGE_TIMEOUT_S") or 45),
        )
