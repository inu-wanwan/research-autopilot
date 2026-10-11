"""Fetch the Zotero Connector from the Chrome Web Store and unpack it.

Branded Google Chrome (>=137) ignores --load-extension, so we load the
unpacked extension into Playwright's bundled Chromium instead.
"""

from __future__ import annotations

import io
import json
import shutil
import struct
import zipfile
from pathlib import Path

import httpx

ZOTERO_CONNECTOR_ID = "ekhagklcjbdpajgpjgmbionohlpdbjgc"
CRX_URL = (
    "https://clients2.google.com/service/update2/crx?response=redirect"
    "&prodversion=154.0&acceptformat=crx2,crx3"
    f"&x=id%3D{ZOTERO_CONNECTOR_ID}%26uc"
)


def crx_to_zip(data: bytes) -> bytes:
    """Strip the CRX2/CRX3 header and return the embedded zip archive."""
    if data[:4] != b"Cr24":
        raise ValueError("not a CRX file")
    version = struct.unpack("<I", data[4:8])[0]
    if version == 3:
        header_len = struct.unpack("<I", data[8:12])[0]
        return data[12 + header_len :]
    if version == 2:
        pub_len, sig_len = struct.unpack("<II", data[8:16])
        return data[16 + pub_len + sig_len :]
    raise ValueError(f"unsupported CRX version {version}")


def fetch_connector(dest: Path, timeout: float = 60.0) -> dict:
    """Download and unpack the connector into `dest`. Returns its manifest."""
    resp = httpx.get(CRX_URL, follow_redirects=True, timeout=timeout)
    resp.raise_for_status()
    zdata = crx_to_zip(resp.content)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(zdata)) as zf:
        zf.extractall(dest)
    # Web Store packages carry a _metadata dir Chromium refuses for unpacked loads.
    shutil.rmtree(dest / "_metadata", ignore_errors=True)
    return read_manifest(dest)


def read_manifest(ext_dir: Path) -> dict:
    return json.loads((ext_dir / "manifest.json").read_text(encoding="utf-8"))
