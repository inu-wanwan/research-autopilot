"""Discovery across arXiv, Crossref, OpenAlex, Unpaywall (OA lookup) and GitHub, with de-duplication.

Every service is optional at run time: a failing service is recorded and the
others continue (spec 2.1: keep going with what is reachable).
"""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable

from research_autopilot.literature.models import Candidate, Failure, SourceType
from research_autopilot.literature.net import Net

ARXIV_ID = re.compile(r"(?:arxiv[:/ ]|abs/|pdf/)?(\d{4}\.\d{4,5})(v\d+)?", re.I)
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi.strip(), flags=re.I)
    return doi.lower() or None


def norm_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def arxiv_from_doi(doi: str | None) -> str | None:
    m = re.match(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", doi or "", re.I)
    return m.group(1) if m else None


# ---------------------------------------------------------------- services


def _arxiv_query(query: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 3][:8]
    return " AND ".join(f"all:{w}" for w in words) or f"all:{query}"


def search_arxiv(net: Net, query: str, limit: int = 8) -> list[Candidate]:
    r = net.get("https://export.arxiv.org/api/query",
                params={"search_query": _arxiv_query(query), "start": 0, "max_results": limit,
                        "sortBy": "relevance"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for e in root.findall("a:entry", ATOM):
        raw_id = (e.findtext("a:id", "", ATOM) or "").rsplit("/", 1)[-1]
        m = ARXIV_ID.search(raw_id)
        if not m:
            continue
        doi = e.findtext("arxiv:doi", None, ATOM)
        out.append(Candidate(
            candidate_id=f"arxiv:{m.group(1)}", title=" ".join((e.findtext("a:title", "", ATOM) or "").split()),
            authors=[a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
            year=_year(e.findtext("a:published", "", ATOM)), arxiv_id=m.group(1), arxiv_version=m.group(2),
            doi=normalize_doi(doi), url=f"https://arxiv.org/abs/{m.group(1)}",
            abstract=" ".join((e.findtext("a:summary", "", ATOM) or "").split())[:1500], found_by=["arxiv"],
            oa_pdf_url=f"https://arxiv.org/pdf/{m.group(1)}{m.group(2) or ''}", is_oa=True,
            venue=e.findtext("arxiv:journal_ref", None, ATOM)))
    return out


def search_crossref(net: Net, query: str, limit: int = 8) -> list[Candidate]:
    data = net.json("https://api.crossref.org/works",
                    params={"query.bibliographic": query, "rows": limit, "mailto": net.contact_email,
                            "select": "DOI,title,author,issued,container-title,type,URL,relation"})
    out = []
    for it in data.get("message", {}).get("items", []):
        doi = normalize_doi(it.get("DOI"))
        out.append(Candidate(
            candidate_id=f"doi:{doi}", title=" ".join((it.get("title") or [""])[0].split()),
            authors=[f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])],
            year=_first_year(it.get("issued")), doi=doi, url=it.get("URL"),
            venue=(it.get("container-title") or [None])[0], found_by=["crossref"],
            kind_hint=SourceType.REVIEW if it.get("type") == "review-article" else None,
            related_ids=[f"doi:{normalize_doi(r.get('id'))}" for rel in (it.get("relation") or {}).values()
                         for r in rel if r.get("id-type") == "doi"]))
    return out


def search_openalex(net: Net, query: str, limit: int = 8) -> list[Candidate]:
    data = net.json("https://api.openalex.org/works",
                    params={"search": query, "per-page": limit, "mailto": net.contact_email})
    out = []
    for w in data.get("results", []):
        doi = normalize_doi(w.get("doi"))
        ids = w.get("ids") or {}
        arx = None
        for loc in w.get("locations") or []:
            m = ARXIV_ID.search((loc.get("landing_page_url") or "")) if "arxiv" in (loc.get("landing_page_url") or "") else None
            if m:
                arx = m.group(1)
        oa = w.get("best_oa_location") or {}
        out.append(Candidate(
            candidate_id=f"openalex:{(ids.get('openalex') or '').rsplit('/', 1)[-1]}",
            title=" ".join((w.get("title") or "").split()),
            authors=[a.get("author", {}).get("display_name", "") for a in w.get("authorships", [])],
            year=w.get("publication_year"), doi=doi, arxiv_id=arx or arxiv_from_doi(doi),
            url=(w.get("primary_location") or {}).get("landing_page_url") or w.get("doi"),
            venue=((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
            found_by=["openalex"], is_oa=(w.get("open_access") or {}).get("is_oa"),
            oa_pdf_url=oa.get("pdf_url"),
            kind_hint=SourceType.REVIEW if w.get("type") == "review" else None))
    return out


def unpaywall(net: Net, doi: str) -> tuple[bool | None, str | None]:
    data = net.json(f"https://api.unpaywall.org/v2/{doi}", params={"email": net.contact_email})
    best = data.get("best_oa_location") or {}
    return data.get("is_oa"), best.get("url_for_pdf")


def search_github(net: Net, query: str, limit: int = 5) -> list[Candidate]:
    data = net.json("https://api.github.com/search/repositories",
                    params={"q": query, "sort": "stars", "per_page": limit})
    return [Candidate(candidate_id=f"github:{r['full_name']}", title=r["full_name"],
                      authors=[(r.get("owner") or {}).get("login", "")], year=_year(r.get("created_at") or ""),
                      url=r.get("html_url"), abstract=(r.get("description") or "")[:500], found_by=["github"],
                      repo_full_name=r["full_name"], stars=r.get("stargazers_count"),
                      kind_hint=SourceType.REFERENCE_IMPLEMENTATION)
            for r in data.get("items", [])]


# ---------------------------------------------------------------- orchestration


def discover(net: Net, query: str, *, seeds: list[str] = (), code_query: str | None = None,
             per_service: int = 8) -> tuple[list[Candidate], list[Failure]]:
    """Search every service; failures are recorded per service and never abort the search."""
    found: list[Candidate] = []
    failures: list[Failure] = []
    calls: list[tuple[str, Callable[[], list[Candidate]]]] = [
        ("arxiv", lambda: search_arxiv(net, query, per_service)),
        ("crossref", lambda: search_crossref(net, query, per_service)),
        ("openalex", lambda: search_openalex(net, query, per_service)),
        ("github", lambda: search_github(net, code_query or _keywords(query), 5)),
    ]
    for seed in seeds:  # explicit DOIs / arXiv ids / repositories from the task
        calls.append((f"seed {seed}", lambda s=seed: _seed(net, s)))
    for name, fn in calls:
        try:
            found.extend(fn())
        except Exception as exc:  # network, quota, parse: keep going with the other services
            failures.append(Failure(stage="search", target=name, detail=f"{type(exc).__name__}: {exc}"[:300]))
    merged = merge(found)
    for c in merged:  # OA lookup for DOIs that have no PDF location yet
        if c.doi and not c.oa_pdf_url and not c.repo_full_name and not arxiv_from_doi(c.doi):
            try:
                c.is_oa, c.oa_pdf_url = unpaywall(net, c.doi)
                c.found_by.append("unpaywall")
            except Exception as exc:
                failures.append(Failure(stage="search", target=f"unpaywall {c.doi}", detail=str(exc)[:200]))
    return merged, failures


def _seed(net: Net, seed: str) -> list[Candidate]:
    seed = seed.strip()
    if m := re.match(r"(?:https?://github\.com/)?([\w.-]+/[\w.-]+)$", seed):
        if "/" in seed and not seed.startswith("10."):
            r = net.json(f"https://api.github.com/repos/{m.group(1)}")
            return [Candidate(candidate_id=f"github:{r['full_name']}", title=r["full_name"], url=r.get("html_url"),
                              repo_full_name=r["full_name"], stars=r.get("stargazers_count"),
                              abstract=(r.get("description") or "")[:500], found_by=["seed"],
                              kind_hint=SourceType.REFERENCE_IMPLEMENTATION)]
    if m := ARXIV_ID.fullmatch(seed.replace("arXiv:", "").replace("arxiv:", "")):
        return [c for c in search_arxiv(net, f'id:{m.group(1)}', 1) or []] or \
            _arxiv_by_id(net, m.group(1))
    doi = normalize_doi(seed)
    if doi and doi.startswith("10."):
        data = net.json(f"https://api.crossref.org/works/{doi}", params={"mailto": net.contact_email})
        it = data.get("message", {})
        return [Candidate(candidate_id=f"doi:{doi}", title=" ".join((it.get("title") or [""])[0].split()),
                          authors=[f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])],
                          year=_first_year(it.get("issued")), doi=doi, url=it.get("URL"),
                          venue=(it.get("container-title") or [None])[0], found_by=["seed"],
                          arxiv_id=arxiv_from_doi(doi))]
    raise ValueError(f"unrecognized seed {seed!r} (DOI, arXiv id or GitHub owner/name)")


def _arxiv_by_id(net: Net, arxiv_id: str) -> list[Candidate]:
    r = net.get("https://export.arxiv.org/api/query", params={"id_list": arxiv_id})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for e in root.findall("a:entry", ATOM):
        raw = (e.findtext("a:id", "", ATOM) or "").rsplit("/", 1)[-1]
        m = ARXIV_ID.search(raw)
        if m:
            out.append(Candidate(candidate_id=f"arxiv:{m.group(1)}", title=" ".join((e.findtext("a:title", "", ATOM) or "").split()),
                                 authors=[a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
                                 year=_year(e.findtext("a:published", "", ATOM)), arxiv_id=m.group(1),
                                 arxiv_version=m.group(2), url=f"https://arxiv.org/abs/{m.group(1)}",
                                 oa_pdf_url=f"https://arxiv.org/pdf/{m.group(1)}{m.group(2) or ''}", is_oa=True,
                                 abstract=" ".join((e.findtext("a:summary", "", ATOM) or "").split())[:1500],
                                 found_by=["seed"], doi=normalize_doi(e.findtext("arxiv:doi", None, ATOM))))
    return out


def merge(cands: list[Candidate]) -> list[Candidate]:
    """One candidate per work: same DOI, same arXiv id or same normalized title (+year +-1) merge;
    preprint and published records of one work are linked through ``related_ids``."""
    groups: list[Candidate] = []
    for c in cands:
        hit = next((g for g in groups if _same_work(g, c)), None)
        if hit is None:
            groups.append(c.model_copy(deep=True))
            continue
        if c.doi and hit.doi and c.doi != hit.doi:
            hit.related_ids = sorted(set(hit.related_ids) | {f"doi:{c.doi}"})
        for field in ("doi", "arxiv_id", "arxiv_version", "url", "venue", "abstract", "oa_pdf_url", "year",
                      "repo_full_name", "stars", "kind_hint"):
            if getattr(hit, field) in (None, "") and getattr(c, field) not in (None, ""):
                setattr(hit, field, getattr(c, field))
        if c.is_oa:
            hit.is_oa = True
        if len(c.authors) > len(hit.authors):
            hit.authors = c.authors
        hit.found_by = sorted(set(hit.found_by) | set(c.found_by))
        hit.related_ids = sorted(set(hit.related_ids) | set(c.related_ids))
    for g in groups:
        g.related_ids = [r for r in g.related_ids if r != f"doi:{g.doi}"]
    return groups


def _same_work(a: Candidate, b: Candidate) -> bool:
    if a.repo_full_name or b.repo_full_name:
        return bool(a.repo_full_name) and a.repo_full_name == b.repo_full_name
    if a.doi and b.doi and a.doi == b.doi:
        return True
    aa = a.arxiv_id or arxiv_from_doi(a.doi)
    bb = b.arxiv_id or arxiv_from_doi(b.doi)
    if aa and bb:
        return aa == bb
    if norm_title(a.title) and norm_title(a.title) == norm_title(b.title):
        return a.year is None or b.year is None or abs(a.year - b.year) <= 1
    return False


def _keywords(query: str, n: int = 6) -> str:
    """GitHub search rejects long queries: keep the first distinctive words (acronyms first)."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9-]+", query)[:40]
    stop = {"the", "and", "how", "what", "with", "from", "for", "into", "their", "them", "realizes", "reference",
            "implementation", "setup", "model", "models", "experimental"}
    acr = [w for w in words if w.isupper() and len(w) >= 3]
    rest = [w for w in words if w.lower() not in stop and w not in acr and len(w) > 3]
    picked = list(dict.fromkeys(acr + rest))[:n]
    return " ".join(picked) if picked else query[:120]


def _year(s: str) -> int | None:
    m = re.match(r"(\d{4})", s or "")
    return int(m.group(1)) if m else None


def _first_year(issued: dict | None) -> int | None:
    try:
        return int((issued or {}).get("date-parts", [[None]])[0][0])
    except (TypeError, ValueError, IndexError):
        return None
