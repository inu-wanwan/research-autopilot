"""OS-specific locations: Zotero profile prefs.js, and the PDF sync folder it names."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def zotero_profiles_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Zotero" / "Zotero" / "Profiles"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Zotero" / "Profiles"
    return Path.home() / ".zotero" / "zotero"


def read_zotero_prefs(prefs_js: Path) -> dict[str, object]:
    """Parse `user_pref("name", value);` lines (values are JS literals ~ JSON)."""
    prefs: dict[str, object] = {}
    pat = re.compile(r'^user_pref\("([^"]+)",\s*(.*)\);\s*$')
    for line in prefs_js.read_text(encoding="utf-8").splitlines():
        m = pat.match(line)
        if not m:
            continue
        try:
            prefs[m.group(1)] = json.loads(m.group(2))
        except json.JSONDecodeError:
            prefs[m.group(1)] = m.group(2)
    return prefs


def detect_sync_dir(profiles_dir: Path | None = None) -> dict:
    """Find where linked PDFs land: ZotMoov dst_dir, else Zotero baseAttachmentPath."""
    profiles_dir = profiles_dir or zotero_profiles_dir()
    for prefs_js in sorted(profiles_dir.glob("*/prefs.js")):
        prefs = read_zotero_prefs(prefs_js)
        for key in ("extensions.zotmoov.dst_dir", "extensions.zotero.baseAttachmentPath"):
            if prefs.get(key):
                return {
                    "sync_dir": str(prefs[key]),
                    "source": key,
                    "base_attachment_path": prefs.get("extensions.zotero.baseAttachmentPath"),
                    "local_api_enabled": prefs.get("extensions.zotero.httpServer.localAPI.enabled"),
                }
    return {"sync_dir": None, "source": None}
