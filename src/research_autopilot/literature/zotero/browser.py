"""Launch Playwright's bundled Chromium with a dedicated profile + Zotero Connector."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, Worker, sync_playwright

from .config import Config


@contextmanager
def launch(cfg: Config) -> Iterator[BrowserContext]:
    if not (cfg.extension_dir / "manifest.json").exists():
        raise FileNotFoundError(
            f"Zotero Connector not found at {cfg.extension_dir}; run `research-agent literature zotero fetch-connector`"
        )
    cfg.profile_dir.mkdir(parents=True, exist_ok=True)
    ext = str(cfg.extension_dir.resolve())
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(cfg.profile_dir),
            channel="chromium",  # bundled Chromium; branded Chrome ignores --load-extension
            headless=cfg.headless,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"],
            accept_downloads=True,
        )
        try:
            yield ctx
        finally:
            ctx.close()


def connector_worker(ctx: BrowserContext, timeout_s: float = 20) -> Worker:
    """Return the Zotero Connector MV3 service worker.

    An unpacked load gets a path-derived extension ID (the Web Store package has
    no manifest "key"), so match on the worker script name instead of the ID.
    """

    def is_connector(w: Worker) -> bool:
        return w.url.startswith("chrome-extension://") and w.url.endswith("/background-worker.js")

    for w in ctx.service_workers:
        if is_connector(w):
            return w
    return ctx.wait_for_event("serviceworker", predicate=is_connector, timeout=timeout_s * 1000)
