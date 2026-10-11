"""Read-only, bounded repository survey that an adapter generator works from (Phase 7B).

The survey summarizes *structure*, not data: generalized path shapes with
counts, JSON key trees with types (long strings truncated), and short excerpts
of README / config files.  Hidden directories, virtual environments and files
whose names look like secrets are never opened.  The result is what an LLM
sees, so it is deterministic and size-limited.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
             ".ipynb_checkpoints", ".tox", "site-packages"}
SECRET = re.compile(r"(^\.env|secret|credential|token|passw|private|id_rsa|id_ed25519|\.pem$|\.key$|\.p12$)", re.I)
TEXT_HINTS = ("readme.md", "readme.txt", "readme")
CONFIG_EXT = (".yaml", ".yml", ".toml")
MAX_ENTRIES = 400_000
MAX_SHAPES = 120
MAX_SAMPLED = 15
MAX_SAMPLE_CHARS = 3500
MAX_SURVEY_CHARS = 90_000  # keeps one generator call well inside a model context window
MAX_JSON_SAMPLE_BYTES = 8 * 1024 * 1024
MAX_STRING = 80


def _generalize(segment: str) -> str:
    seg = re.sub(r"[0-9a-f]{7,64}", "<hex>", segment)
    return re.sub(r"\d+(?:\.\d+)?", "<n>", seg)


def _shape(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return "…"
    if isinstance(value, dict):
        items = list(value.items())
        out = {k: _shape(v, depth + 1) for k, v in items[:25]}
        if len(items) > 25:
            out["…"] = f"{len(items) - 25} more keys"
        return out
    if isinstance(value, list):
        return {"list_len": len(value), "item": _shape(value[0], depth + 1)} if value else {"list_len": 0}
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"
    return value


def _terms(focus: str | None) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (focus or "").lower()) if len(t) >= 3]


def _score(path: str, terms: list[str]) -> int:
    low = path.lower()
    return sum(1 for t in terms if t in low)


def survey(root: Path, focus: str | None = None) -> dict[str, Any]:
    """``focus`` (e.g. what a task needs) ranks matching paths/configs first; it never hides other data."""
    root = root.expanduser().resolve()
    terms = _terms(focus)
    shapes: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "examples": [], "bytes": 0})
    ext_counts: dict[str, int] = defaultdict(int)
    texts: dict[str, str] = {}
    configs: dict[str, str] = {}
    seen = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                             and not os.path.islink(os.path.join(dirpath, d)))
        rel_dir = os.path.relpath(dirpath, root)
        for name in sorted(filenames):
            seen += 1
            if seen > MAX_ENTRIES:
                truncated = True
                break
            if name.startswith(".") or SECRET.search(name):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            ext = os.path.splitext(name)[1].lower() or "<none>"
            ext_counts[ext] += 1
            key = "/".join(_generalize(s) for s in rel.split("/"))
            sh = shapes[key]
            sh["count"] += 1
            try:
                sh["bytes"] += os.path.getsize(full)
            except OSError:
                pass
            if len(sh["examples"]) < 2:
                sh["examples"].append(rel)
            low = name.lower()
            if rel_dir.count(os.sep) <= 2 and low in TEXT_HINTS and len(texts) < 4:
                texts[rel] = _read_text(full, 3000)
            elif ext in CONFIG_EXT and "config" in rel.lower():
                configs[rel] = full  # read after ranking
        if truncated:
            break
    # Rank: focus matches first, then larger families; keep small families too (results are often few files).
    ranked = sorted(shapes.items(), key=lambda kv: (-_score(kv[0], terms), -kv[1]["count"], kv[0]))
    top = ranked[:MAX_SHAPES]
    # Sample JSON structure for the best-ranked families AND the largest ones (e.g. caches a result refers to).
    json_keys = [k for k, _ in top if k.endswith(".json")]
    by_size = sorted(json_keys, key=lambda k: -shapes[k]["count"])
    to_sample = set(json_keys[:MAX_SAMPLED // 2 + 1]) | set(by_size[:MAX_SAMPLED // 2])
    out_shapes = []
    for key, sh in top:
        entry = {"shape": key, "count": sh["count"], "total_bytes": sh["bytes"], "examples": sh["examples"]}
        if key in to_sample:
            entry["json_structure"] = _json_sample(root / sh["examples"][0])
        out_shapes.append(entry)
    chosen = sorted(configs, key=lambda r: (-_score(r, terms), r.count("/"), r))[:6]
    configs = {r: _read_text(configs[r], 2500) for r in chosen}
    out_shapes = _fit(out_shapes, budget=MAX_SURVEY_CHARS - len(json.dumps([texts, configs], ensure_ascii=False)))
    return {"root_name": root.name, "focus": focus, "files_seen": min(seen, MAX_ENTRIES), "truncated": truncated,
            "shapes_total": len(shapes), "shapes_shown": len(out_shapes), "omitted_shapes": len(shapes) - len(out_shapes),
            "extensions": dict(sorted(ext_counts.items(), key=lambda kv: -kv[1])[:30]),
            "path_shapes": out_shapes, "readme_excerpts": texts, "config_excerpts": configs,
            "notes": "Path segments are generalized: <n> = number, <hex> = hex id. Values are truncated samples."}


def _fit(shapes: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Drop JSON samples from the end, then the lowest-ranked shapes, until the survey fits."""
    def size(xs):
        return len(json.dumps(xs, ensure_ascii=False, default=str))
    out = [dict(x) for x in shapes]
    for entry in reversed(out):
        if size(out) <= budget:
            return out
        entry.pop("json_structure", None)
    while out and size(out) > budget:
        out.pop()
    return out


def _read_text(path: str, limit: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _json_sample(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_JSON_SAMPLE_BYTES:
            return f"<json larger than {MAX_JSON_SAMPLE_BYTES} bytes; not sampled>"
        doc = json.loads(path.read_bytes())
        for depth in (0, 1, 2):  # shallower until it fits the per-sample budget
            shaped = _shape(doc, depth)
            if len(json.dumps(shaped, ensure_ascii=False, default=str)) <= MAX_SAMPLE_CHARS:
                return shaped
        return _shape(doc, 3)
    except (OSError, ValueError) as exc:
        return f"<unreadable: {type(exc).__name__}>"


def survey_size(s: dict[str, Any]) -> int:
    return len(json.dumps(s, ensure_ascii=False, default=str))
