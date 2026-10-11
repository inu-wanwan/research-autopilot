"""Acquisition of originals: Zotero reuse -> Zotero Connector -> open-access download -> human request.

Implementations are captured as a commit-pinned snapshot of selected repository files.
Subscription content may be saved through the Zotero Connector (the institution's normal browser access) as long
as no bot check / CAPTCHA appears (user decision 2026-10-11).  A bot check is recorded, never bypassed, and stops
further Connector saves for the rest of the run.  Every automatic download attempt counts against a per-run
limit (at most ``MAX_RETRIEVALS``) so a run cannot fetch without bound.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from research_autopilot.literature.models import Candidate, Failure, Retrieval, Source
from research_autopilot.literature.net import Net
from research_autopilot.models import utcnow

CACHE = Path(os.environ.get("RA_LITERATURE_CACHE_DIR") or Path.home() / ".research-autopilot" / "literature")
CODE_EXT = {".py", ".m", ".jl", ".r", ".cpp", ".cc", ".c", ".h", ".hpp", ".ipynb", ".md", ".rst", ".txt", ".toml",
            ".cfg", ".yaml", ".yml", ".sh", ".f90", ".rs", ".go", ".java", ".scala"}
MAX_FILE_BYTES = 300_000
MAX_SNAPSHOT_BYTES = 3_000_000
MAX_FILES = 80
MAX_RETRIEVALS = 10  # hard upper bound of automatic download attempts per research run (user decision)


@dataclass
class RetrievalLimit:
    """Per-run budget of automatic downloads (Connector saves, OA downloads, repository snapshots)."""

    remaining: int = MAX_RETRIEVALS
    bot_check: bool = False  # a CAPTCHA / bot check was seen: no more Connector saves in this run

    def __post_init__(self) -> None:
        self.remaining = max(0, min(self.remaining, MAX_RETRIEVALS))

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _limit_reached(src: Source, failures: list[Failure]) -> list[Failure]:
    src.retrieval = Retrieval(method="none", status="requires_human", retrieved_at=utcnow(),
                              detail=f"automatic retrieval limit ({MAX_RETRIEVALS} per run) reached - "
                                     "please save it to Zotero manually or rerun")
    failures.append(Failure(stage="acquisition", target=src.source_id, detail=src.retrieval.detail, needs_human=True))
    return failures


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def acquire_paper(net: Net, src: Source, cand: Candidate, zotero=None,
                  limit: RetrievalLimit | None = None) -> list[Failure]:
    """Fill ``src.retrieval``; returns failures (each with a cause) without raising."""
    failures: list[Failure] = []
    limit = limit if limit is not None else RetrievalLimit()
    access = "subscription" if cand.is_oa is False else "open_access" if cand.is_oa else "unknown"
    dois = [d for d in (cand.doi, f"10.48550/arxiv.{cand.arxiv_id}" if cand.arxiv_id else None) if d]
    # 1. Reuse a PDF already in Zotero / Drive.
    if zotero is not None:
        try:
            if zotero.available():
                for doi in dois:
                    hit = zotero.existing_pdf(doi)
                    if hit:
                        _done(src, "zotero_existing", hit["path"], hit.get("source_url"),
                              f"existing Zotero item {hit['item_key']}", item_key=hit["item_key"])
                        return failures
        except Exception as exc:
            failures.append(Failure(stage="acquisition", target=src.source_id, detail=f"zotero lookup: {exc}"[:300]))
    # 2. Save through the Zotero Connector (publisher translator + PDF), then read the attachment.  Subscription
    #    papers are allowed here; a bot check stops the Connector for the rest of the run.
    if zotero is not None and not limit.bot_check:
        target = cand.doi if cand.doi and not cand.doi.startswith("10.48550") else (cand.url or cand.doi)
        if target:
            if not limit.take():
                return _limit_reached(src, failures)
            try:
                res = zotero.save(target)
                path = _saved_pdf(res)
                if res.get("status") == "ok" and path:
                    _done(src, "zotero_connector", path, _source_url(res), f"Zotero Connector: {res['status']}",
                          item_key=(res.get("items") or [{}])[0].get("key"), access=access)
                    return failures
                blocked = res.get("status") == "blocked_bot_check"
                limit.bot_check = limit.bot_check or blocked
                failures.append(Failure(stage="acquisition", target=src.source_id,
                                        detail=f"zotero connector status {res.get('status')}: {res.get('error') or ''}"[:300],
                                        needs_human=blocked))
            except Exception as exc:
                failures.append(Failure(stage="acquisition", target=src.source_id,
                                        detail=f"zotero connector unavailable: {type(exc).__name__}: {exc}"[:300]))
    # 3. Open-access PDF straight from arXiv / the OA location (private cache, never shared).
    if cand.oa_pdf_url and (cand.is_oa or cand.arxiv_id):
        if not limit.take():
            return _limit_reached(src, failures)
        try:
            r = net.get(cand.oa_pdf_url, accept="application/pdf")
            if r.status_code == 200 and r.content[:5] == b"%PDF-":
                digest = sha256_bytes(r.content)
                path = CACHE / "pdf" / f"{digest}.pdf"
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_bytes(r.content)
                _done(src, "open_access_download", path, cand.oa_pdf_url, "open-access PDF", access="open_access")
                return failures
            failures.append(Failure(stage="acquisition", target=src.source_id,
                                    detail=f"OA download returned HTTP {r.status_code} / not a PDF"))
        except Exception as exc:
            failures.append(Failure(stage="acquisition", target=src.source_id, detail=f"OA download: {exc}"[:300]))
    # 4. Nothing automatic worked (or a bot check appeared): ask a human.
    why = "a bot check / CAPTCHA appeared (not bypassed)" if limit.bot_check else \
        "no open-access copy, no PDF in Zotero and the Zotero Connector could not save it"
    src.retrieval = Retrieval(method="none", status="requires_human", retrieved_at=utcnow(),
                              access="subscription" if cand.is_oa is False else "unknown",
                              detail=f"{why} - please save it to Zotero manually")
    failures.append(Failure(stage="acquisition", target=src.source_id, detail=src.retrieval.detail, needs_human=True))
    return failures


def _done(src: Source, method: str, path: Path, origin: str | None, detail: str, *, item_key: str | None = None,
          access: str = "unknown") -> None:
    src.retrieval = Retrieval(method=method, status="ok", detail=detail, retrieved_at=utcnow(),
                              sha256=sha256_file(path), local_path=str(path), origin_url=origin,
                              zotero_item_key=item_key, access=access)


def _saved_pdf(res: dict) -> Path | None:
    from research_autopilot.literature.zotero.session import attachment_path
    for att in res.get("attachments") or []:
        if att.get("contentType") == "application/pdf":
            p = Path(att["resolvedPath"]) if att.get("resolvedPath") else attachment_path(
                {"key": att.get("key"), "data": {"linkMode": att.get("linkMode"), "path": att.get("path"),
                                                  "filename": att.get("filename")}}, None)
            if p and p.is_file():
                return p
    return None


def _source_url(res: dict) -> str | None:
    return next((a.get("sourceUrl") for a in res.get("attachments") or [] if a.get("contentType") == "application/pdf"),
                None)


def snapshot_repository(net: Net, src: Source, full_name: str, limit: RetrievalLimit | None = None) -> list[Failure]:
    """Commit-pinned snapshot of README / license / source files (bounded)."""
    if limit is not None and not limit.take():
        return _limit_reached(src, [])
    try:
        repo = net.json(f"https://api.github.com/repos/{full_name}")
        branch = repo.get("default_branch") or "main"
        commit = net.json(f"https://api.github.com/repos/{full_name}/commits/{branch}")["sha"]
        tree = net.json(f"https://api.github.com/repos/{full_name}/git/trees/{commit}", params={"recursive": "1"})
        files = [t for t in tree.get("tree", []) if t.get("type") == "blob" and t.get("size", 0) <= MAX_FILE_BYTES
                 and (Path(t["path"]).suffix.lower() in CODE_EXT or Path(t["path"]).name.lower().startswith(("readme", "license", "copying")))]
        files.sort(key=lambda t: (not Path(t["path"]).name.lower().startswith("readme"), t["path"].count("/"), t["path"]))
        dest = CACHE / "repos" / f"{full_name.replace('/', '__')}@{commit[:12]}"
        manifest, total = [], 0
        for t in files[:MAX_FILES]:
            if total + t["size"] > MAX_SNAPSHOT_BYTES:
                break
            r = net.get(f"https://raw.githubusercontent.com/{full_name}/{commit}/{t['path']}")
            if r.status_code != 200:
                continue
            out = dest / t["path"]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(r.content)
            manifest.append({"path": t["path"], "sha256": sha256_bytes(r.content), "bytes": len(r.content)})
            total += len(r.content)
        index = "\n".join(f"{m['sha256']}  {m['path']}" for m in manifest)
        (dest / ".ra_manifest.txt").write_text(index)
        src.version = commit
        src.url = repo.get("html_url")
        src.retrieval = Retrieval(method="repository_snapshot", status="ok", retrieved_at=utcnow(),
                                  sha256=sha256_bytes(index.encode()), local_path=str(dest),
                                  origin_url=f"https://github.com/{full_name}/tree/{commit}", access="open_access",
                                  detail=f"{len(manifest)} file(s), license={((repo.get('license') or {}).get('spdx_id'))}, "
                                         f"{len(files) - len(manifest)} skipped by limits")
        return []
    except Exception as exc:
        src.retrieval = Retrieval(method="repository_snapshot", status="failed", retrieved_at=utcnow(),
                                  detail=f"{type(exc).__name__}: {exc}"[:300])
        return [Failure(stage="acquisition", target=src.source_id, detail=src.retrieval.detail)]
