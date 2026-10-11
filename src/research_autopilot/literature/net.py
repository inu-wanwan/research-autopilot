"""Allowlisted, polite HTTP access for literature search and retrieval.

* Only hosts on the allowlist (search APIs, DOI resolver, arXiv, major
  publishers, GitHub) are contacted; redirects are re-checked against it.
* No cookies or credentials are sent; a mailto contact is attached as the
  scholarly APIs request.  GitHub uses a token only if one is configured.
* Requests are spaced per host and responses are size-limited.
Response bodies are data: nothing fetched here is ever executed or obeyed.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import httpx

DEFAULT_ALLOWED = (
    "export.arxiv.org", "arxiv.org", "api.crossref.org", "api.openalex.org", "api.unpaywall.org", "doi.org",
    "dx.doi.org", "api.github.com", "github.com", "raw.githubusercontent.com", "codeload.github.com",
    "proceedings.mlr.press", "openreview.net", "papers.nips.cc", "proceedings.neurips.cc", "aclanthology.org",
    "www.nature.com", "nature.com", "journals.plos.org", "pubs.acs.org", "pubs.aip.org", "link.springer.com",
    "www.sciencedirect.com", "onlinelibrary.wiley.com", "academic.oup.com", "iopscience.iop.org",
    "www.pnas.org", "www.science.org", "ieeexplore.ieee.org", "dl.acm.org", "europepmc.org",
    "www.ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "zenodo.org", "pyscf.org", "docs.python.org",
    "readthedocs.io", "jmlr.org", "www.jmlr.org",
)
MAX_BYTES = 60 * 1024 * 1024


class BlockedHost(Exception):
    pass


class Net:
    def __init__(self, contact_email: str, *, allowed: tuple[str, ...] = DEFAULT_ALLOWED, min_interval_s: float = 1.0,
                 github_token: str | None = None, timeout_s: float = 30.0, transport=None) -> None:
        self.contact_email = contact_email
        self.allowed = tuple(h.lower() for h in allowed)
        self.min_interval_s = min_interval_s
        self.github_token = github_token
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.client = httpx.Client(timeout=timeout_s, follow_redirects=False, transport=transport,
                                   headers={"User-Agent": f"ResearchAutopilot/0.1 (mailto:{contact_email})"})
        self.log: list[dict] = []  # host / path / status only (no query secrets, no bodies)

    def allowed_host(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == a or host.endswith("." + a) for a in self.allowed) and urlparse(url).scheme == "https"

    def get(self, url: str, *, params: dict | None = None, accept: str | None = None, max_bytes: int = MAX_BYTES,
            redirects: int = 6) -> httpx.Response:
        for _ in range(redirects + 1):
            if not self.allowed_host(url):
                raise BlockedHost(f"host not on the literature allowlist: {urlparse(url).hostname}")
            self._pace(url)
            headers = {"Accept": accept} if accept else {}
            if self.github_token and urlparse(url).hostname == "api.github.com":
                headers["Authorization"] = f"Bearer {self.github_token}"
            with self.client.stream("GET", url, params=params, headers=headers) as r:
                self.log.append({"host": urlparse(url).hostname, "path": urlparse(url).path[:120],
                                 "status": r.status_code})
                if r.is_redirect and "location" in r.headers:
                    url = str(r.url.join(r.headers["location"]))
                    params = None
                    continue
                body = bytearray()
                for chunk in r.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError(f"response larger than {max_bytes} bytes")
                # iter_bytes() already decoded any content-encoding; do not let httpx decode it twice.
                headers = {k: v for k, v in r.headers.items() if k.lower() not in ("content-encoding", "content-length")}
                return httpx.Response(r.status_code, headers=headers, content=bytes(body), request=r.request)
        raise ValueError("too many redirects")

    def json(self, url: str, **kw) -> dict:
        r = self.get(url, accept="application/json", **kw)
        r.raise_for_status()
        return r.json()

    def _pace(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()


def from_env() -> Net:
    email = os.environ.get("RA_LITERATURE_CONTACT_EMAIL", "").strip()
    if not email:
        raise RuntimeError("RA_LITERATURE_CONTACT_EMAIL is required by the scholarly APIs (polite pool / Unpaywall)")
    extra = tuple(h.strip() for h in os.environ.get("RA_LITERATURE_EXTRA_HOSTS", "").split(",") if h.strip())
    return Net(email, allowed=DEFAULT_ALLOWED + extra, github_token=os.environ.get("RA_GITHUB_TOKEN") or None)
