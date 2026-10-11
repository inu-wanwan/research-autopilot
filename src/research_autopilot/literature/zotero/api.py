"""Read-only access to the Zotero desktop Local API (http://127.0.0.1:23119/api/).

Requires Zotero 7+ with "Allow other applications on this computer to
communicate with Zotero" enabled. This module never writes to Zotero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx


class ZoteroUnavailable(RuntimeError):
    pass


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
    return doi.lower() or None


@dataclass
class ZoteroLocal:
    base_url: str = "http://127.0.0.1:23119"
    timeout_s: float = 10.0

    def _get(self, path: str, **params) -> httpx.Response:
        try:
            r = httpx.get(f"{self.base_url}{path}", params=params, timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise ZoteroUnavailable(f"Zotero not reachable: {e}") from e
        if r.status_code == 403 and path.startswith("/api/"):
            raise ZoteroUnavailable("Zotero Local API disabled (Settings > Advanced)")
        r.raise_for_status()
        return r

    def ping(self) -> bool:
        try:
            return "Zotero is running" in self._get("/connector/ping").text
        except (ZoteroUnavailable, httpx.HTTPStatusError):
            return False

    def selected_collection(self) -> dict:
        """The collection currently selected in Zotero (where the Connector saves)."""
        try:
            r = httpx.post(f"{self.base_url}/connector/getSelectedCollection", json={},
                           timeout=self.timeout_s)
        except httpx.HTTPError as e:
            raise ZoteroUnavailable(f"Zotero not reachable: {e}") from e
        r.raise_for_status()
        return r.json()

    def collections(self) -> list[dict]:
        return self._get("/api/users/0/collections", limit=100).json()

    def collection_key(self, name: str) -> str | None:
        for c in self.collections():
            if c["data"]["name"] == name:
                return c["key"]
        return None

    def recent_top_items(self, collection_key: str | None = None, limit: int = 25) -> list[dict]:
        path = (f"/api/users/0/collections/{collection_key}/items/top" if collection_key
                else "/api/users/0/items/top")
        return self._get(path, limit=limit, sort="dateAdded", direction="desc").json()

    def item_keys(self, collection_key: str | None = None) -> set[str]:
        return {it["key"] for it in self.recent_top_items(collection_key, limit=100)}

    def children(self, item_key: str) -> list[dict]:
        return self._get(f"/api/users/0/items/{item_key}/children").json()

    def find_by_doi(self, doi: str) -> list[dict]:
        target = normalize_doi(doi)
        hits = self._get("/api/users/0/items/top", q=doi, qmode="everything", limit=50).json()
        return [it for it in hits if _item_doi(it["data"]) == target]


def _item_doi(data: dict) -> str | None:
    if data.get("DOI"):
        return normalize_doi(data["DOI"])
    # Item types without a DOI field (e.g. preprint before Zotero 7) stash it in Extra.
    m = re.search(r"^DOI:\s*(\S+)", data.get("extra") or "", flags=re.M | re.I)
    return normalize_doi(m.group(1)) if m else None


def summarize_item(item: dict) -> dict:
    d = item["data"]
    creators = [
        c.get("name") or f"{c.get('lastName', '')}, {c.get('firstName', '')}".strip(", ")
        for c in d.get("creators", [])
    ]
    return {
        "key": item["key"],
        "itemType": d.get("itemType"),
        "title": d.get("title"),
        "doi": _item_doi(d),
        "creators": creators,
        "date": d.get("date"),
        "publication": d.get("publicationTitle") or d.get("repository") or d.get("publisher"),
        "url": d.get("url"),
        "dateAdded": d.get("dateAdded"),
        "collections": d.get("collections", []),
    }


def summarize_attachment(att: dict, sync_dir: Path | None) -> dict:
    d = att["data"]
    path = d.get("path")
    resolved: Path | None = None
    if path and path.startswith("attachments:") and sync_dir:
        resolved = sync_dir / path.removeprefix("attachments:")
    elif path and d.get("linkMode") == "linked_file":
        resolved = Path(path)
    return {
        "key": att["key"],
        "linkMode": d.get("linkMode"),
        "contentType": d.get("contentType"),
        "filename": d.get("filename"),
        "path": path,
        "sourceUrl": d.get("url"),
        "resolvedPath": str(resolved) if resolved else None,
        "fileExists": resolved.is_file() if resolved else None,
        "fileSize": resolved.stat().st_size if resolved and resolved.is_file() else None,
    }
