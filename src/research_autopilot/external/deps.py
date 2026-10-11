"""Static dependency / runtime survey of a fetched repository.

Nothing in the repository is executed or imported here: ``setup.py`` is parsed with ``ast`` (literal values
only), imports are collected from the syntax tree, and README text is only data.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections import Counter
from pathlib import Path

from research_autopilot.external.models import DependencySurvey

IMPORT_TO_PYPI = {
    "sklearn": "scikit-learn", "cv2": "opencv-python-headless", "PIL": "Pillow", "yaml": "PyYAML",
    "skimage": "scikit-image", "Bio": "biopython", "bs4": "beautifulsoup4", "dateutil": "python-dateutil",
    "attr": "attrs", "google.protobuf": "protobuf", "jwt": "PyJWT", "serial": "pyserial", "Crypto": "pycryptodome",
    "OpenSSL": "pyOpenSSL", "dotenv": "python-dotenv", "magic": "python-magic", "zmq": "pyzmq", "git": "GitPython",
    "pyscf": "pyscf", "rdkit": "rdkit", "torch": "torch", "tensorflow": "tensorflow", "jax": "jax",
}
NOT_INSTALLABLE = {"matlab": "MATLAB engine", "pysmac": "pySMAC (needs the SMAC Java distribution)"}
UNSUPPORTED_EXT = {".m": "MATLAB", ".cu": "CUDA", ".R": "R", ".jl": "Julia", ".f90": "Fortran (needs a compiler)",
                   ".f": "Fortran (needs a compiler)"}
DEP_FILES = ("requirements.txt", "requirements-dev.txt", "requirements_dev.txt", "setup.py", "setup.cfg",
             "pyproject.toml", "environment.yml", "environment.yaml", "Pipfile", "conda.yaml")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".tox"}


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS)]


def survey(root: Path) -> DependencySurvey:
    s = DependencySurvey()
    files = _files(root)
    s.languages = dict(Counter(p.suffix or p.name for p in files).most_common(15))
    for ext, lang in UNSUPPORTED_EXT.items():
        n = sum(1 for p in files if p.suffix == ext)
        if n:
            s.unsupported.append(f"{lang} ({n} {ext} file(s)) cannot be run in the RA sandbox")
    for p in files:
        if p.name in DEP_FILES:
            rel = p.relative_to(root).as_posix()
            s.dependency_files.append(rel)
            s.declared += _declared(p)
            if p.name == "setup.py":
                s.install_steps.append(f"{rel}: setup script found; RA does not execute it (code is used from PYTHONPATH)")
    py = [p for p in files if p.suffix == ".py"]
    local = {p.stem for p in py} | {p.parent.name for p in py if (p.parent / "__init__.py").exists()}
    s.local_modules = sorted(local)
    stdlib = set(sys.stdlib_module_names)
    imports: Counter = Counter()
    importers: dict[str, set[Path]] = {}
    py2 = []
    for p in py:
        try:
            tree = ast.parse(p.read_text(errors="replace"))
        except SyntaxError:
            py2.append(p.relative_to(root).as_posix())
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for n in names:
                top = n.split(".")[0]
                if top in local:
                    importers.setdefault(top, set()).add(p.parent)
                elif top not in stdlib and top != "__future__":
                    imports[top] += 1
        if "__main__" in p.read_text(errors="replace") or re.match(r"(example|demo|main|run)", p.stem, re.I):
            s.entrypoints.append(p.relative_to(root).as_posix())
        if re.match(r"(test_.*|.*_test)$", p.stem) or "tests" in p.relative_to(root).parts:
            s.tests.append(p.relative_to(root).as_posix())
    if py2:
        s.notes.append(f"{len(py2)} Python file(s) do not parse as Python 3 (e.g. {py2[0]}); they cannot run")
    s.imports = sorted(imports)
    # Directories whose modules are imported by plain name must be on PYTHONPATH.
    dirs = {"."}
    for mod in importers:
        for p in py:
            if p.stem == mod or (p.parent.name == mod and p.name == "__init__.py"):
                base = p.parent if p.stem == mod else p.parent.parent
                dirs.add(base.relative_to(root).as_posix() or ".")
    s.python_dirs = sorted(dirs)
    reqs: list[str] = []
    if s.declared:
        reqs = [r for r in s.declared if not r.startswith(("-", "git+", "http"))]
    for imp in s.imports:
        if imp in NOT_INSTALLABLE:
            s.unsupported.append(f"import {imp}: {NOT_INSTALLABLE[imp]} is not installable from PyPI")
            continue
        name = IMPORT_TO_PYPI.get(imp, imp)
        if not any(_req_name(r) == _req_name(name) for r in reqs):
            reqs.append(name)
    s.requirements = reqs
    if s.tests and not any(_req_name(r) == "pytest" for r in reqs):
        s.requirements.append("pytest")
    return s


def _req_name(r: str) -> str:
    return re.split(r"[<>=!~\[; ]", r.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _declared(p: Path) -> list[str]:
    text = p.read_text(errors="replace")
    if p.name.startswith("requirements"):
        return [ln.split("#")[0].strip() for ln in text.splitlines() if ln.split("#")[0].strip()]
    if p.name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return []
        return list((data.get("project") or {}).get("dependencies") or [])
    if p.name == "setup.py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "install_requires":
                try:
                    return [str(x) for x in ast.literal_eval(node.value)]
                except ValueError:
                    return []
        return []
    if p.name in ("environment.yml", "environment.yaml", "conda.yaml"):
        out = []
        for ln in text.splitlines():
            m = re.match(r"\s+-\s+([A-Za-z0-9_.-]+(?:[=<>!~]=?[\w.*]+)?)\s*$", ln)
            if m and m.group(1) not in ("pip", "python") and not m.group(1).startswith("python="):
                out.append(m.group(1).replace("=", "==", 1) if re.search(r"[^=<>!~]=[^=]", m.group(1)) else m.group(1))
        return out
    return []
