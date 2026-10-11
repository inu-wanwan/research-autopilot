"""Save one DOI/URL via the Zotero Connector and verify the result in Zotero."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Worker

from . import botcheck, connector
from .config import Config
from .api import ZoteroLocal, normalize_doi, summarize_attachment, summarize_item

log = logging.getLogger(__name__)

DOI_RE = re.compile(r"^(doi:\s*|https?://(dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.I)


def resolve_target(target: str) -> tuple[str, str | None]:
    """Return (url_to_open, doi_or_None) for a DOI or URL input."""
    target = target.strip()
    m = DOI_RE.match(target)
    if m:
        doi = m.group(3)
        return f"https://doi.org/{doi}", normalize_doi(doi)
    if target.startswith(("http://", "https://")):
        return target, None
    raise ValueError(f"not a DOI or http(s) URL: {target!r}")


def _pdf_ok(att: dict, need_sync: bool) -> bool:
    if att["contentType"] != "application/pdf":
        return False
    if need_sync:
        return att["linkMode"] == "linked_file" and bool(att["fileExists"])
    return att["linkMode"] in ("imported_url", "imported_file", "linked_file")


def _blocked(page, res: dict, screens_dir: Path) -> bool:
    """Record and stop on a robot/CAPTCHA check. Never solved or bypassed."""
    evidence = botcheck.detect(page)
    if not evidence:
        return False
    shot = screens_dir / f"{int(time.time())}_botcheck.png"
    page.screenshot(path=str(shot))
    res.update(status="blocked_bot_check", bot_check={"evidence": evidence, "url": page.url,
                                                      "title": page.title(), "screenshot": shot.name},
               error=f"robot/CAPTCHA check detected ({evidence}); skipped, not bypassed")
    log.warning("%s blocked by bot check: %s", res["input"], evidence)
    return True


def save_one(ctx: BrowserContext, worker: Worker, cfg: Config, z: ZoteroLocal,
             collection_key: str | None, target: str, screens_dir: Path,
             skip_existing: bool = False) -> dict:
    t0 = time.monotonic()
    res: dict = {"input": target, "status": "error", "stages": {}, "error": None}

    def stage(name: str, **data) -> None:
        res["stages"][name] = {"t": round(time.monotonic() - t0, 2), **data}
        log.info("%s %s %s", target, name, data or "")

    page = None
    try:
        url, doi = resolve_target(target)
        res.update(open_url=url, input_doi=doi)
        existing = [it["key"] for it in z.find_by_doi(doi)] if doi else []
        res["existing_items_with_doi"] = existing
        if existing and skip_existing:
            res["status"] = "skipped_existing"
            return res

        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until="load", timeout=cfg.page_timeout_s * 1000)
        except PlaywrightError as e:
            res.update(status="page_load_failed", error=str(e).splitlines()[0])
            return res
        page.bring_to_front()
        http_status = resp.status if resp else None
        stage("page_loaded", final_url=page.url, title=page.title(), http_status=http_status)
        if _blocked(page, res, screens_dir):
            return res
        if http_status and http_status >= 400:
            # e.g. unknown DOI (404) or publisher access block (403). Never retried/bypassed.
            res.update(status="page_load_failed", error=f"HTTP {http_status} at {page.url}")
            page.screenshot(path=str(screens_dir / f"{int(time.time())}_http{http_status}.png"))
            return res

        info = connector.wait_for_translators(worker, page.url)
        res["tab"] = info
        stage("translators", translators=info["translators"], isPDF=info["isPDF"])
        if not info["translators"] and not info["isPDF"]:
            if _blocked(page, res, screens_dir):
                return res
            # The connector would save a plain webpage snapshot; not useful as a paper record.
            res.update(status="no_translator", error="connector found no translator for page")
            page.screenshot(path=str(screens_dir / f"{int(time.time())}_no_translator.png"))
            return res

        before = z.item_keys(collection_key)
        connector.trigger_save(worker, info["tabId"])
        stage("save_triggered")

        new_keys: set[str] = set()
        deadline = time.monotonic() + cfg.save_timeout_s
        while time.monotonic() < deadline and not new_keys:
            time.sleep(1)
            new_keys = z.item_keys(collection_key) - before
        if not new_keys:
            if _blocked(page, res, screens_dir):
                return res
            res.update(status="no_item_created", error=f"no new item within {cfg.save_timeout_s}s")
            page.screenshot(path=str(screens_dir / f"{int(time.time())}_no_item.png"))
            return res
        items = [it for it in z.recent_top_items(collection_key, limit=50) if it["key"] in new_keys]
        res["items"] = [summarize_item(it) for it in items]
        stage("item_created", keys=sorted(new_keys))
        item_key = items[0]["key"]

        # Wait for the PDF attachment, and (if configured) for ZotMoov to move it
        # into the sync folder as a linked file.
        need_sync = cfg.sync_dir is not None
        atts: list[dict] = []
        while True:
            atts = [summarize_attachment(a, cfg.sync_dir) for a in z.children(item_key)
                    if a["data"]["itemType"] == "attachment"]
            if any(_pdf_ok(a, need_sync) for a in atts) or time.monotonic() > deadline:
                break
            time.sleep(1)
        res["attachments"] = atts
        pdf = next((a for a in atts if a["contentType"] == "application/pdf"), None)
        res["pdf_attached"] = pdf is not None
        res["pdf_in_sync_dir"] = bool(pdf and pdf["fileExists"]) if need_sync else None
        stage("attachments", n=len(atts), pdf=res["pdf_attached"], synced=res["pdf_in_sync_dir"])

        got = res["items"][0]
        res["doi_match"] = (got["doi"] == doi) if doi else None
        # URL inputs have no DOI up front, so also look for twins after saving.
        twins = [it["key"] for it in z.find_by_doi(got["doi"])] if got["doi"] else []
        res["other_items_with_same_doi"] = sorted(set(twins) - new_keys)
        res["duplicate_created"] = bool(res["other_items_with_same_doi"])
        if not res["pdf_attached"]:
            res["status"] = "saved_without_pdf"
        elif need_sync and not res["pdf_in_sync_dir"]:
            res["status"] = "saved_pdf_not_synced"
        elif doi and not res["doi_match"]:
            res["status"] = "saved_doi_mismatch"
        else:
            res["status"] = "ok"
        return res
    except Exception as e:  # record every failure cause instead of aborting the batch
        log.exception("save failed: %s", target)
        res.update(status="error", error=f"{type(e).__name__}: {e}")
        return res
    finally:
        if page is not None:
            page.close()
        res["elapsed_s"] = round(time.monotonic() - t0, 2)
