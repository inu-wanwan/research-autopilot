"""License and terms-of-use assessment (deterministic; no LLM).

Policy (user decision 2026-10-11):

* permissive / public domain                    -> automatic for every intended use (attribution recorded);
* weak / strong / network copyleft (LGPL, MPL, GPL, AGPL)
                                                -> automatic for internal research use without distribution;
                                                   copying into the research repository or redistribution
                                                   needs a human;
* no license, unrecognised, non-commercial / no-derivatives terms, or contradicting sources
                                                -> a human decides.

The assessment only reads the commit-pinned files and the GitHub API metadata; it never treats their text as
instructions.
"""

from __future__ import annotations

import re
from pathlib import Path

from research_autopilot.external.models import LicenseAssessment

FAMILY = {  # spdx prefix -> category
    "MIT": "permissive", "BSD": "permissive", "Apache": "permissive", "ISC": "permissive", "Zlib": "permissive",
    "PSF": "permissive", "BSL-1.0": "permissive", "0BSD": "permissive", "Python": "permissive",
    "Unlicense": "public_domain", "CC0": "public_domain",
    "LGPL": "weak_copyleft", "MPL": "weak_copyleft", "EPL": "weak_copyleft",
    "GPL": "strong_copyleft", "AGPL": "network_copyleft",
}
CONDITIONS = {
    "permissive": {"use": "allowed", "modify": "allowed", "redistribute": "allowed with notice",
                   "attribution": "keep copyright and license notice", "share_alike": "no"},
    "public_domain": {"use": "allowed", "modify": "allowed", "redistribute": "allowed", "attribution": "not required",
                      "share_alike": "no"},
    "weak_copyleft": {"use": "allowed", "modify": "allowed",
                      "redistribute": "modified files of the library must stay under the same license",
                      "attribution": "keep notices", "share_alike": "file/library level"},
    "strong_copyleft": {"use": "allowed (including private modification)", "modify": "allowed",
                        "redistribute": "derived works must be GPL and ship source",
                        "attribution": "keep notices", "share_alike": "whole derived work"},
    "network_copyleft": {"use": "allowed", "modify": "allowed",
                         "redistribute": "derived works must be AGPL; offering it over a network counts as distribution",
                         "attribution": "keep notices", "share_alike": "whole derived work incl. network use"},
}
RESTRICTIVE = re.compile(r"non-?commercial|no ?derivatives|all rights reserved|may not be (used|copied|modified)|"
                         r"proprietary|academic use only|research use only|without (the )?(prior )?written permission",
                         re.I)
LICENSE_FILE = re.compile(r"^(licen[sc]e|copying|unlicense)(\.(md|txt|rst))?$", re.I)


def text_spdx(text: str) -> str | None:
    """Identify a license from its text (only well-known texts; anything else is None).

    The title at the top decides first: the GPL-3.0 text itself mentions the Affero GPL in section 13."""
    t = " ".join(text.split()).lower()
    return _spdx_in(t[:400]) or _spdx_in(t)


def _spdx_in(t: str) -> str | None:
    if m := re.search(r"spdx-license-identifier:\s*([a-z0-9.+-]+)", t):
        return m.group(1).upper() if m.group(1).upper() in ("MIT", "ISC", "0BSD") else _canon(m.group(1))
    if "gnu affero general public license" in t:
        return "AGPL-3.0"
    if "gnu lesser general public license" in t or "gnu library general public license" in t:
        return "LGPL-3.0" if "version 3" in t else "LGPL-2.1"
    if "gnu general public license" in t:
        if "version 3" in t:
            return "GPL-3.0"
        if "version 2" in t:
            return "GPL-2.0"
        return "GPL"
    if "apache license" in t and "version 2.0" in t:
        return "Apache-2.0"
    if "mozilla public license" in t:
        return "MPL-2.0" if "2.0" in t else "MPL"
    if "permission is hereby granted, free of charge" in t:
        return "MIT"
    if "redistribution and use in source and binary forms" in t:
        return "BSD-3-Clause" if "neither the name" in t or "endorse or promote" in t else "BSD-2-Clause"
    if "permission to use, copy, modify, and/or distribute this software for any purpose" in t:
        return "ISC"
    if "free and unencumbered software released into the public domain" in t:
        return "Unlicense"
    if "creative commons" in t:
        return "CC-unknown"
    return None


def _canon(spdx: str) -> str:
    s = spdx.strip()
    for k in ("AGPL", "LGPL", "GPL", "MPL", "Apache", "BSD", "MIT", "ISC"):
        if s.upper().startswith(k.upper()):
            return k + s[len(k):]
    return s


def category_of(spdx: str | None) -> str:
    if not spdx:
        return "unknown"
    base = spdx.upper()
    for prefix in ("AGPL", "LGPL", "GPL", "MPL", "EPL"):
        if base.startswith(prefix):
            return FAMILY[prefix]
    for prefix, cat in FAMILY.items():
        if base.startswith(prefix.upper()):
            return cat
    return "unknown"


def _family(spdx: str) -> str:
    """Comparable family ('GPL-3.0-or-later' ~ 'GPL-3.0' ~ 'GPL'; versions only conflict when both are given)."""
    s = spdx.upper().replace("-OR-LATER", "").replace("-ONLY", "").replace("+", "")
    return s


def assess(root: Path, github_spdx: str | None, intended_use: str = "internal_research") -> LicenseAssessment:
    a = LicenseAssessment(intended_use=intended_use)
    if github_spdx and github_spdx not in ("NOASSERTION", "OTHER"):
        a.detected["github_api"] = _canon(github_spdx)
    elif github_spdx:
        a.detected["github_api"] = github_spdx
    restrictive_hits = []
    for p in sorted(root.iterdir()):
        if p.is_file() and LICENSE_FILE.match(p.name):
            text = p.read_text(errors="replace")[:200_000]
            a.detected[p.name] = text_spdx(text) or "unrecognized"
            if RESTRICTIVE.search(text) and a.detected[p.name] in ("unrecognized", "CC-unknown"):
                restrictive_hits.append(p.name)
    headers = _header_licenses(root)
    for spdx, files in headers.items():
        a.detected[f"source headers ({len(files)} file(s), e.g. {files[0]})"] = spdx
    known = {k: v for k, v in a.detected.items() if v not in ("unrecognized", "NOASSERTION", "OTHER", "CC-unknown")}
    families = {_family(v) for v in known.values()}
    # Same family with and without a version (e.g. "GPL" header + "GPL-3.0" file) is not a conflict.
    versioned = {f for f in families if re.search(r"\d", f)}
    unversioned = {f for f in families - versioned if any(v.startswith(f) for v in versioned)}
    families -= unversioned
    if not a.detected:
        a.category, a.decision = "none", "needs_human"
        a.reasons.append("no license file, no license metadata and no license headers: terms are unknown")
    elif restrictive_hits:
        a.category, a.decision = "unknown", "needs_human"
        a.reasons.append(f"license text with restrictive or custom terms ({', '.join(restrictive_hits)})")
    elif not known:
        a.category, a.decision = "unknown", "needs_human"
        a.reasons.append("license could not be identified: " + ", ".join(f"{k}={v}" for k, v in a.detected.items()))
    elif len(families) > 1:
        a.category, a.decision = "conflicting", "needs_human"
        a.reasons.append("sources disagree: " + ", ".join(f"{k}={v}" for k, v in known.items()))
    else:
        a.spdx = max(known.values(), key=len)
        a.category = category_of(a.spdx)
        a.conditions = dict(CONDITIONS.get(a.category, {}))
        if a.category == "unknown":
            a.decision = "needs_human"
            a.reasons.append(f"{a.spdx} is not in the automatic policy")
        elif a.category in ("permissive", "public_domain"):
            a.decision = "auto_ok"
            a.reasons.append(f"{a.spdx} permits {intended_use.replace('_', ' ')}")
        elif intended_use == "internal_research":
            a.decision = "auto_ok"
            a.reasons.append(f"{a.spdx} ({a.category.replace('_', ' ')}) permits private use and modification; "
                             "internal research use involves no distribution")
        else:
            a.decision = "needs_human"
            a.reasons.append(f"{a.spdx} share-alike terms apply to {intended_use.replace('_', ' ')}")
    if "unrecognized" in a.detected.values() and a.decision == "auto_ok":
        a.reasons.append("note: an additional license file was not recognized; the recognised terms were used")
    return a


def _header_licenses(root: Path, limit: int = 400) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    n = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".py", ".m", ".c", ".h", ".cpp", ".jl", ".r", ".sh"):
            continue
        n += 1
        if n > limit:
            break
        head = "\n".join(p.read_text(errors="replace").splitlines()[:40])
        spdx = text_spdx(head)
        if spdx:
            out.setdefault(spdx, []).append(p.relative_to(root).as_posix())
    return out
