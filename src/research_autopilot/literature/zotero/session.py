"""One Zotero automation session: preflight, existing-item reuse, Connector saves (all read-only on Zotero's API)."""

from __future__ import annotations

import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from . import browser, connector
from .api import ZoteroLocal, ZoteroUnavailable, normalize_doi
from .config import Config
from .platform_paths import read_zotero_prefs, zotero_profiles_dir
from .save import save_one


class ZoteroNotReady(Exception):
    pass


def data_dir() -> Path:
    """Zotero data directory (storage/ for imported attachments)."""
    for prefs_js in sorted(zotero_profiles_dir().glob("*/prefs.js")):
        d = read_zotero_prefs(prefs_js).get("extensions.zotero.dataDir")
        if d:
            return Path(str(d))
    return Path.home() / "Zotero"


def attachment_path(att: dict, sync_dir: Path | None) -> Path | None:
    """Local file of a Zotero PDF attachment (linked via ZotMoov or imported into storage/)."""
    d = att.get("data", att)
    path = d.get("path") or ""
    if path.startswith("attachments:") and sync_dir:
        return sync_dir / path.removeprefix("attachments:")
    if d.get("linkMode") == "linked_file" and path:
        return Path(path)
    if d.get("linkMode") in ("imported_url", "imported_file") and d.get("filename"):
        return data_dir() / "storage" / (att.get("key") or d.get("key")) / d["filename"]
    return None


class ZoteroSession:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config.load()
        self.api = ZoteroLocal(self.cfg.zotero_url)
        self._stack: ExitStack | None = None
        self._ctx = None
        self._worker = None
        self.collection_key: str | None = None

    # ---- read-only reuse -------------------------------------------------
    def available(self) -> bool:
        return self.api.ping()

    def existing_pdf(self, doi: str) -> dict | None:
        """An existing library item with this DOI whose PDF file exists locally."""
        for item in self.api.find_by_doi(doi):
            for child in self.api.children(item["key"]):
                d = child["data"]
                if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
                    p = attachment_path(child, self.cfg.sync_dir)
                    if p and p.is_file():
                        return {"item_key": item["key"], "path": p, "source_url": d.get("url"),
                                "title": item["data"].get("title")}
        return None

    # ---- Connector saves -------------------------------------------------
    def preflight(self) -> dict:
        """Refuse to save unless Zotero is up, the Local API works and the expected collection is selected."""
        if not self.api.ping():
            raise ZoteroNotReady("Zotero desktop is not running or not reachable")
        try:
            self.api.collections()
        except ZoteroUnavailable as exc:
            raise ZoteroNotReady(str(exc)) from exc
        selected = self.api.selected_collection()
        name = selected.get("name")
        if self.cfg.collection and name != self.cfg.collection:
            raise ZoteroNotReady(f"Zotero has collection {name!r} selected, expected {self.cfg.collection!r}; "
                                 "the Connector saves into the selected collection")
        if not (self.cfg.extension_dir / "manifest.json").exists():
            raise ZoteroNotReady(f"Zotero Connector not unpacked at {self.cfg.extension_dir}")
        self.collection_key = self.api.collection_key(name) if name else None
        return {"collection": name, "sync_dir": bool(self.cfg.sync_dir)}

    def __enter__(self) -> "ZoteroSession":
        return self

    def __exit__(self, *exc) -> None:
        if self._stack:
            self._stack.close()

    def save(self, target: str) -> dict:
        if self._ctx is None:
            self.preflight()
            self._stack = ExitStack()
            self._ctx = self._stack.enter_context(browser.launch(self.cfg))
            self._worker = browser.connector_worker(self._ctx)
            info = connector.init(self._worker)
            if not info.get("online"):
                raise ZoteroNotReady("Zotero Connector cannot reach Zotero desktop")
        screens = Path(tempfile.mkdtemp(prefix="ra-zotero-screens-"))
        doi = normalize_doi(target) if target.lower().startswith(("10.", "doi:", "https://doi.org")) else None
        res = save_one(self._ctx, self._worker, self.cfg, self.api, self.collection_key, target, screens,
                       skip_existing=bool(doi))  # never create duplicates of a DOI already in the library
        res["platform"] = sys.platform
        return res
