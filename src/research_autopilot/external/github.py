"""Identification of an external repository and its isolated, commit-pinned fetch.

Everything fetched here is untrusted data: README text and source are never interpreted as instructions,
nothing is executed on the host, no Git credentials are used (anonymous HTTPS through the literature
allowlist), and the archive is unpacked with tarfile's ``data`` filter (no absolute paths, no links out of
the tree, no device files).  Integrity is checked file by file against the commit's Git tree (blob SHA-1).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

from research_autopilot.external.models import Identification, Integrity, PaperRef, Provider
from research_autopilot.literature.net import Net

MAX_ARCHIVE_BYTES = 150 * 1024 * 1024
FULL_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def ext_root() -> Path:
    return Path(os.environ.get("RA_EXTERNAL_DIR") or Path.home() / ".research-autopilot" / "external")


def normalize_name(name: str) -> str:
    name = name.strip().removeprefix("https://").removeprefix("http://").removeprefix("github.com/")
    name = name.removesuffix(".git").strip("/")
    if not FULL_NAME.match(name):
        raise ValueError(f"not a GitHub owner/name: {name!r}")
    return name


# ---------------------------------------------------------------- identification


def identify(net: Net, name: str, *, commit: str | None = None, paper: PaperRef | None = None,
             paper_text: str | None = None) -> Identification:
    name = normalize_name(name)
    repo = net.json(f"https://api.github.com/repos/{name}")
    full = repo["full_name"]
    if commit:
        info = net.json(f"https://api.github.com/repos/{full}/commits/{commit}")
    else:
        info = net.json(f"https://api.github.com/repos/{full}/commits/{repo.get('default_branch') or 'main'}")
    owner = repo.get("owner") or {}
    try:
        user = net.json(f"https://api.github.com/users/{owner.get('login')}")
    except Exception:
        user = {}
    ident = Identification(
        full_name=full, url=repo.get("html_url") or f"https://github.com/{full}", owner=owner.get("login", ""),
        owner_type=owner.get("type"), owner_name=user.get("name"), description=(repo.get("description") or "")[:500],
        default_branch=repo.get("default_branch"), commit=info["sha"],
        commit_date=((info.get("commit") or {}).get("committer") or {}).get("date"),
        fork_of=(repo.get("parent") or {}).get("full_name") if repo.get("fork") else None,
        archived=bool(repo.get("archived")), github_license=(repo.get("license") or {}).get("spdx_id"), paper=paper)
    try:
        readme = net.get(f"https://raw.githubusercontent.com/{full}/{ident.commit}/README.md").text
    except Exception:
        readme = ""
    classify_provider(ident, readme, paper_text)
    return ident


def _surnames(authors: list[str]) -> list[str]:
    out = []
    for a in authors:
        parts = re.sub(r"[^A-Za-z\s-]", " ", a).split()
        if parts:
            out.append(parts[-1].lower())
    return [s for s in out if len(s) >= 3]


def classify_provider(ident: Identification, readme: str, paper_text: str | None) -> None:
    """official > author > third_party > unknown, each with the evidence that supports it."""
    paper = ident.paper
    ev, unc = ident.provider_evidence, ident.uncertainties
    lower_readme = (readme or "").lower()
    if ident.fork_of:
        unc.append(f"repository is a fork of {ident.fork_of}")
    if paper is None or not (paper.title or paper.doi or paper.arxiv_id):
        ident.provider = Provider.UNKNOWN
        unc.append("no corresponding paper was given; the paper relation is unknown")
        return
    refs = [x for x in (paper.arxiv_id, (paper.doi or "").lower(), (paper.title or "").lower()[:60]) if x]
    if any(r.lower() in lower_readme for r in refs):
        ev.append("README references the paper (" + ", ".join(r for r in refs if r.lower() in lower_readme) + ")")
    else:
        unc.append("README does not reference the paper by arXiv id / DOI / title")
    if paper_text and ident.full_name.lower() in re.sub(r"\s+", "", paper_text.lower()):
        ev.append(f"paper text links github.com/{ident.full_name}")
        ident.provider = Provider.OFFICIAL
        return
    surnames = _surnames(paper.authors)
    owner_login = ident.owner.lower()
    owner_name = (ident.owner_name or "").lower()
    hit = [s for s in surnames if s in owner_login or (owner_name and s in owner_name.split())]
    if hit:
        ev.append(f"owner {ident.owner!r} ({ident.owner_name or 'no public name'}) matches paper author(s) {hit}")
        if not owner_name:
            unc.append("author match is based on the login name only")
        ident.provider = Provider.AUTHOR
        if paper_text is None:
            unc.append("paper text not available: whether the paper itself links this repository is unknown")
        return
    readme_authors = [s for s in surnames if s in lower_readme]
    if readme_authors:
        ev.append(f"README names paper author(s) {readme_authors}, but the owner is not one of them")
        unc.append("README credits the authors; ownership by the authors is not established")
    ident.provider = Provider.THIRD_PARTY


# ---------------------------------------------------------------- fetch + integrity


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def source_dir(full_name: str, commit: str) -> Path:
    return ext_root() / "src" / f"{full_name.replace('/', '__')}@{commit[:12]}"


def fetch(net: Net, ident: Identification) -> tuple[Path, Integrity, str]:
    """Download the commit archive into a private directory (read-only afterwards) and verify it."""
    dest = source_dir(ident.full_name, ident.commit)
    origin = f"https://codeload.github.com/{ident.full_name}/tar.gz/{ident.commit}"
    if not dest.exists():
        r = net.get(origin, max_bytes=MAX_ARCHIVE_BYTES)
        if r.status_code != 200:
            raise RuntimeError(f"archive download returned HTTP {r.status_code}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
                tf.extractall(tmp, filter="data")
            roots = [p for p in Path(tmp).iterdir()]
            if len(roots) != 1 or not roots[0].is_dir():
                raise RuntimeError("unexpected archive layout")
            shutil.move(str(roots[0]), dest)
        _make_read_only(dest)
    tree = net.json(f"https://api.github.com/repos/{ident.full_name}/git/trees/{ident.commit}",
                    params={"recursive": "1"})
    return dest, verify_tree(dest, tree), origin


def verify_tree(root: Path, tree: dict) -> Integrity:
    blobs = {t["path"]: t for t in tree.get("tree", []) if t.get("type") == "blob"}
    submodules = [t["path"] for t in tree.get("tree", []) if t.get("type") == "commit"]
    files = sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink())
    integ = Integrity(files=len(files))
    seen = set()
    for p in files:
        rel = p.relative_to(root).as_posix()
        seen.add(rel)
        t = blobs.get(rel)
        if t is None:
            integ.mismatches.append(f"{rel}: not in the commit tree")
            continue
        data = os.readlink(p).encode() if p.is_symlink() else p.read_bytes()
        if git_blob_sha1(data) == t["sha"]:
            integ.verified_blobs += 1
        else:
            integ.mismatches.append(f"{rel}: blob hash differs from the commit")
    integ.missing = sorted(set(blobs) - seen) + [f"{s} (submodule, not fetched)" for s in submodules]
    if tree.get("truncated"):
        integ.missing.append("(GitHub tree listing truncated; not every file could be checked)")
    integ.tree_sha256 = tree_sha256(root)
    integ.ok = not integ.mismatches and integ.verified_blobs == integ.files and integ.files > 0
    return integ


def tree_sha256(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.rglob("*") if x.is_file() and not x.is_symlink()):
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode() + b"\0" + hashlib.sha256(p.read_bytes()).hexdigest().encode() + b"\n")
    return h.hexdigest()


def _make_read_only(root: Path) -> None:
    for p in [root, *root.rglob("*")]:
        if p.is_symlink():
            continue
        mode = p.stat().st_mode
        p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
